"""Place suggestions for photos without a position (app/geo/placeguess.py)."""
import sqlite3

import pytest

from app.data import index
from app.geo import placeguess

T0 = 1_600_000_000  # 2020-09-13 12:26 UTC


def _index(tmp_path, rows):
    p = tmp_path / "index.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE photos (id INTEGER PRIMARY KEY, ts INTEGER, day TEXT, year INTEGER, dprec INTEGER, video INTEGER,
                             lat REAL, lon REAL, place TEXT, region TEXT, country TEXT);
    """)
    con.executemany("INSERT INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return p


def _use(monkeypatch, p):
    monkeypatch.setattr(index, "open_index", lambda path=None: sqlite3.connect(f"file:{p}?mode=ro", uri=True))


def test_between_near_and_day(tmp_path, monkeypatch):
    D = "2020-09-13"
    rows = [
        # a walk through Ghent with a phone (positions) and a camera (none)
        (1, T0, D, 2020, None, 0, 51.0500, 3.7200, "Ghent", "East Flanders", "Belgium"),
        (2, T0 + 600, D, 2020, None, 0, None, None, None, None, None),       # between 1 and 3
        (3, T0 + 1200, D, 2020, None, 0, 51.0520, 3.7240, "Ghent", "East Flanders", "Belgium"),
        (4, T0 + 1200 + 1800, D, 2020, None, 0, None, None, None, None, None),  # 30 min after 3, nothing after: near
        (5, T0 - 5 * 3600, D, 2020, None, 0, None, None, None, None, None),  # five hours earlier: the day's one place
        (6, T0, D, 2020, None, 0, None, None, None, None, None),             # a video: never
        (7, T0, "2020-09-14", 2020, None, 0, None, None, None, None, None),  # another day without any position: nothing
        (8, T0, D, 2020, 3, 0, None, None, None, None, None),                # date unknown: nothing
    ]
    rows[5] = (6, T0, D, 2020, None, 1, None, None, None, None, None)
    _use(monkeypatch, _index(tmp_path, rows))
    monkeypatch.setattr(placeguess, "_look_sims", lambda ids, others: {})
    out = placeguess.suggestions()
    items = {i["id"]: i for i in out["items"]}
    assert set(items) == {2, 4, 5}
    two = items[2]
    assert two["basis"] == "between" and two["from"] == [1, 3] and abs(two["lat"] - 51.051) < 1e-3 and two["km"] < 0.5
    assert two["place"] == "Ghent" and two["country"] == "Belgium"
    assert items[4]["basis"] == "near" and items[4]["from"] == [3] and "30 min earlier" in items[4]["text"]
    assert items[5]["basis"] == "day" and len(items[5]["from"]) == 2
    assert [i["id"] for i in out["items"]] == [2, 4, 5]  # surest first
    assert [i["id"] for i in placeguess.suggestions([4])["items"]] == [4]


def test_several_places_that_day_and_a_stay(tmp_path, monkeypatch):
    D, B, A = "2020-09-13", "2020-09-12", "2020-09-14"
    rows = [
        # the morning at a beach, the evening in town, 30 km apart; the camera photo at noon-ish, far from both in time
        (1, T0 - 4 * 3600, D, 2020, None, 0, 51.33, 3.20, "Knokke", "West Flanders", "Belgium"),
        (2, T0 - 4 * 3600 + 60, D, 2020, None, 0, 51.33, 3.21, "Knokke", "West Flanders", "Belgium"),
        (3, T0 + 5 * 3600, D, 2020, None, 0, 51.05, 3.72, "Ghent", "East Flanders", "Belgium"),
        (4, T0, D, 2020, None, 0, None, None, None, None, None),
        # a stay in Venice: positions the day before and after, none on the day
        (5, T0 - 86400, B, 2020, None, 0, 45.44, 12.33, "Venice", "Veneto", "Italy"),
        (6, T0 + 86400, A, 2020, None, 0, 45.44, 12.32, "Venice", "Veneto", "Italy"),
    ]
    rows2 = [(7, T0, "2020-09-13", 2020, None, 0, None, None, None, None, None)]
    p = _index(tmp_path, rows)
    _use(monkeypatch, p)
    # with nothing to say how it looks: the place nearest in time
    monkeypatch.setattr(placeguess, "_look_sims", lambda ids, others: {})
    four = {i["id"]: i for i in placeguess.suggestions([4])["items"]}[4]
    assert four["basis"] == "day-time" and four["place"] == "Knokke"
    # the embeddings say it looks like town: the town wins
    monkeypatch.setattr(placeguess, "_look_sims", lambda ids, others: {(4, 1): 0.5, (4, 2): 0.5, (4, 3): 0.7})
    four = {i["id"]: i for i in placeguess.suggestions([4])["items"]}[4]
    assert four["basis"] == "day" and four["place"] == "Ghent" and "looks most like" in four["text"]
    # too close to call: back to time
    monkeypatch.setattr(placeguess, "_look_sims", lambda ids, others: {(4, 1): 0.6, (4, 2): 0.6, (4, 3): 0.61})
    assert {i["id"]: i for i in placeguess.suggestions([4])["items"]}[4]["basis"] == "day-time"
    # the Venice stay: a photo on the day between
    p2 = _index(tmp_path / "b", [r for r in rows if r[0] in (5, 6)] + rows2) if (tmp_path / "b").mkdir() is None else None
    _use(monkeypatch, p2)
    monkeypatch.setattr(placeguess, "_look_sims", lambda ids, others: {})
    seven = {i["id"]: i for i in placeguess.suggestions()["items"]}[7]
    assert seven["basis"] == "stay" and seven["place"] == "Venice" and seven["km"] >= 1.0 and sorted(seven["from"]) == [5, 6]


def test_endpoint_and_photo_field(client, lib):
    d = client.get("/api/placesuggest").json()
    assert {"total", "items"} <= set(d)
    for it in d["items"][:5]:
        assert {"id", "lat", "lon", "km", "basis", "text", "from"} <= set(it)
        p = client.get(f"/api/photo/{it['id']}").json()
        assert p.get("placeSuggestion", {}).get("id") == it["id"]
