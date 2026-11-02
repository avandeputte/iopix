"""Year suggestions from the library's clues (app/ai/dateclues.py): the people's dated years, look-alikes, the camera,
and how the clues combine with the age estimates."""
import sqlite3

import pytest

from app.ai import dateclues
from app.data import index
from app.faces import ages

np = pytest.importorskip("numpy")


def _index(tmp_path):
    p = tmp_path / "index.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE photos (id INTEGER PRIMARY KEY, path TEXT, w INTEGER, h INTEGER, ts INTEGER, year INTEGER, dprec INTEGER, video INTEGER, camera TEXT);
        CREATE TABLE faces (photo_id INTEGER, tag_id INTEGER, x INTEGER, y INTEGER, w INTEGER, h INTEGER, kind TEXT);
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, person INTEGER);
        INSERT INTO tags VALUES (10, 'Ann', 1), (11, 'Bob', 1), (12, 'Cy', 1);
    """)
    rows, faces = [], []
    for k, year in enumerate(range(1990, 2010)):  # Ann and Bob together every year 1990-2009; Cy alone in 2000-2004
        pid = 100 + k
        rows.append((pid, f"/p/{pid}.jpg", 4000, 3000, int((year - 1970) * 31557600), year, None, 0, "Canon EOS 300D" if 2003 <= year <= 2006 else None))
        faces += [(pid, 10, 100, 100, 200, 200, "confirmed"), (pid, 11, 600, 100, 200, 200, "confirmed")]
        if 2000 <= year <= 2004:
            faces.append((pid, 12, 1000, 100, 200, 200, "confirmed"))
    rows += [(1, "/p/1.jpg", 4000, 3000, 0, 1974, 3, 0, None),              # Ann, Bob and Cy together: 2000-2004
             (2, "/p/2.jpg", 4000, 3000, 0, 1974, 3, 0, None),              # Cy alone
             (3, "/p/3.jpg", 4000, 3000, 0, 1974, 3, 0, "Canon EOS 300D"),  # nobody named, the camera of 2003-2006
             (4, "/p/4.jpg", 4000, 3000, 0, 1974, 3, 0, None),              # nothing to go on
             (5, "/p/5.jpg", 4000, 3000, 0, 1974, 3, 1, None)]              # a video: never suggested
    faces += [(1, 10, 100, 100, 200, 200, "confirmed"), (1, 11, 600, 100, 200, 200, "confirmed"), (1, 12, 1000, 100, 200, 200, "confirmed"),
              (2, 12, 100, 100, 200, 200, "confirmed"), (4, 10, 100, 100, 200, 200, "unknown")]
    con.executemany("INSERT INTO photos VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO faces VALUES (?,?,?,?,?,?,?)", faces)
    con.commit()
    con.close()
    return p


def test_combine_weights_clues_by_their_uncertainty():
    tight = {"year": 1995, "sd": 1.0}
    loose = {"year": 2005, "sd": 10.0}
    out = dateclues.combine([tight, loose], this_year=2026)
    assert out["year"] == 1995 and out["lo"] <= 1990 and out["hi"] >= 2000  # the tight clue decides, the disagreement widens the range
    one = dateclues.combine([tight], this_year=2026)
    assert one == {"year": 1995, "lo": 1993, "hi": 1997}
    assert dateclues.combine([{"year": 2040, "sd": 1.0}], this_year=2026)["hi"] == 2026  # never after this year


def test_people_look_and_camera_clues(tmp_path, monkeypatch):
    p = _index(tmp_path)
    monkeypatch.setattr(index, "open_index", lambda path=None: sqlite3.connect(f"file:{p}?mode=ro", uri=True))
    monkeypatch.setattr(ages, "available", lambda: False)
    monkeypatch.setattr(dateclues, "_look_years", lambda con, ids: {2: (1998.0, 3.0, 20, 0.8)} if 2 in ids else {})
    out = dateclues.suggestions()
    items = {i["id"]: i for i in out["items"]}
    assert set(items) == {1, 2, 3}  # photo 4 has no named face, no camera and no look-alike; 5 is a video
    one = items[1]
    assert [c["kind"] for c in one["clues"]] == ["people"]
    assert "together in 2000–2004" in one["clues"][0]["text"] and 2001 <= one["year"] <= 2003 and one["hi"] - one["lo"] <= 6
    two = items[2]
    assert [c["kind"] for c in two["clues"]] == ["people", "look"]
    assert "Cy in 2000–2004" in two["clues"][0]["text"] and two["clues"][1]["text"].startswith("looks like photos from about 1998")
    assert 1999 <= two["year"] <= 2003
    three = items[3]
    assert [c["kind"] for c in three["clues"]] == ["camera"]
    assert three["clues"][0]["text"] == "Canon EOS 300D used in 2003–2006" and 2003 <= three["year"] <= 2006
    assert out["items"][0]["hi"] - out["items"][0]["lo"] <= out["items"][-1]["hi"] - out["items"][-1]["lo"]  # tightest first
    assert [i["id"] for i in dateclues.suggestions([3])["items"]] == [3]
    assert out["people"] == []  # no age model: no calibration list


def test_age_clue_joins_the_others_and_its_birth_floor_holds(tmp_path, monkeypatch):
    p = _index(tmp_path)
    monkeypatch.setattr(index, "open_index", lambda path=None: sqlite3.connect(f"file:{p}?mode=ro", uri=True))
    monkeypatch.setattr(ages, "available", lambda: True)
    monkeypatch.setattr(ages, "coverage", lambda: (1, 1))
    monkeypatch.setattr(dateclues, "_look_years", lambda con, ids: {})
    monkeypatch.setattr(ages, "suggestions", lambda ids=None: {
        "items": [{"id": 2, "year": 1990, "lo": 1988, "hi": 1992, "floor": 1996, "people": [{"tag": 12, "name": "Cy", "age": 3, "year": 1990}]}],
        "people": [{"tag": 12, "name": "Cy", "birth": 1996, "entered": True, "basis": "entered", "n": 0}]})
    out = dateclues.suggestions()
    two = {i["id"]: i for i in out["items"]}[2]
    assert [c["kind"] for c in two["clues"]] == ["ages", "people"]
    assert two["clues"][0]["text"] == "Cy looks 3"
    assert two["lo"] >= 1996 and two["year"] >= 1996  # an entered birth year is a floor for the whole suggestion
    assert two["people"][0]["name"] == "Cy"
    assert out["people"][0]["basis"] == "entered"


def test_suggestions_endpoint_and_mcp_tool(client, lib):
    d = client.get("/api/datesuggest").json()
    assert {"total", "items", "people", "ready", "sources", "faces"} <= set(d)
    assert set(d["sources"]) == {"ages", "look"}
    for it in d["items"]:
        assert it["clues"] and it["lo"] <= it["year"] <= it["hi"]
