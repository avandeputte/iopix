"""Year suggestions for undated photos (ages.py): each person's dated faces calibrate the age model for
them, an entered birth year stands in when those are few, and an undated photo's suggestion combines its
named people into a year with a range. Nothing is written."""
import sqlite3

import pytest

from app.data import index
from app.faces import ages

np = pytest.importorskip("numpy")


def test_calibration_per_person():
    born = 2004
    rng = np.random.default_rng(1)
    # a child photographed from 2008 to 2026 whose age the model underestimates by a fifth
    pts = [(y, 0.8 * (y - born) + rng.normal(0, 0.7)) for y in np.linspace(2008, 2026, 30)]
    c = ages.calibrate(pts)
    assert c["basis"] == "fit" and 1.1 < c["c1"] < 1.4
    assert abs(c["c0"] + c["c1"] * 0.8 * (2015 - born) - 2015) < 1.5  # an undated photo at 2015 comes out near 2015
    assert abs(c["birth"] - born) <= 3
    few = ages.calibrate([(2000, 30), (2001, 31.5), (2003, 32)])
    assert few["basis"] == "dated" and few["c1"] == 1.0 and abs(few["c0"] - 1970) <= 1
    assert ages.calibrate([(2000, 30), (2001, 31.5), (2003, 32)], birth=1968)["basis"] == "entered"  # an entered year beats three photos
    assert ages.calibrate([(2000, 30)], birth=1968) == {"c0": 1968.0, "c1": 1.0, "sd": 4.0, "n": 1, "basis": "entered", "birth": 1968}
    assert ages.calibrate([(2000, 30)]) is None
    assert ages.calibrate(pts, birth=2003)["basis"] == "fit"
    # beyond the fitted ages: a year per year from the nearest end, and a wider range
    y_in, sd_in = ages.year_for(c, c["amin"])
    y_out, sd_out = ages.year_for(c, c["amin"] - 3)
    assert abs((y_in - y_out) - 3) < 1e-6 and sd_out > sd_in  # enough photos: they decide, the entered year is shown


def _index(tmp_path):
    p = tmp_path / "index.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE photos (id INTEGER PRIMARY KEY, path TEXT, w INTEGER, h INTEGER, ts INTEGER, year INTEGER, dprec INTEGER, video INTEGER);
        CREATE TABLE faces (photo_id INTEGER, tag_id INTEGER, x INTEGER, y INTEGER, w INTEGER, h INTEGER, kind TEXT);
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, person INTEGER);
        INSERT INTO tags VALUES (10, 'Ann', 1), (11, 'Bob', 1), (12, 'Cy', 1);
    """)
    rows, faces = [], []
    for k, year in enumerate(range(1990, 2010)):  # Ann born 1980, Bob born 1950: dated photos of both
        pid = 100 + k
        rows.append((pid, f"/p/{pid}.jpg", 4000, 3000, int((year - 1970) * 31557600), year, None, 0))
        faces += [(pid, 10, 100, 100, 200, 200, "confirmed"), (pid, 11, 600, 100, 200, 200, "confirmed")]
    rows += [(1, "/p/1.jpg", 4000, 3000, 0, 1974, 3, 0), (2, "/p/2.jpg", 4000, 3000, 0, 1974, 3, 0), (3, "/p/3.jpg", 4000, 3000, 0, 1974, 3, 0)]
    faces += [(1, 10, 100, 100, 200, 200, "confirmed"), (1, 11, 600, 100, 200, 200, "confirmed"),  # Ann and Bob together
              (2, 12, 100, 100, 200, 200, "confirmed"),                                            # Cy: nothing known
              (3, 10, 100, 100, 200, 200, "unknown")]                                              # not a named face
    con.executemany("INSERT INTO photos VALUES (?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO faces VALUES (?,?,?,?,?,?,?)", faces)
    con.commit()
    con.close()
    return p


def test_suggestions_combine_the_people_in_a_photo(tmp_path, monkeypatch):
    p = _index(tmp_path)
    monkeypatch.setattr(index, "open_index", lambda path=None: sqlite3.connect(f"file:{p}?mode=ro", uri=True))
    stored = {}
    for k, year in enumerate(range(1990, 2010)):
        stored[(100 + k, 100, 100, 200, 200)] = year - 1980 + 0.5   # the model sees Ann as born 1979.5
        stored[(100 + k, 600, 100, 200, 200)] = year - 1950 - 0.5  # and Bob as born 1950.5
    stored[(1, 100, 100, 200, 200)] = 15.5   # Ann looks 15 in the undated photo: about 1995
    stored[(1, 600, 100, 200, 200)] = 44.5   # Bob looks 44: about 1995
    stored[(2, 100, 100, 200, 200)] = 30.0
    monkeypatch.setattr(ages, "_ages", lambda: stored)
    monkeypatch.setattr(ages, "births", lambda: {})
    out = ages.suggestions()
    assert [i["id"] for i in out["items"]] == [1]  # Cy has nothing to calibrate with; photo 3 has no named face
    s = out["items"][0]
    assert s["year"] == 1995 and s["lo"] <= 1994 and s["hi"] >= 1996 and s["hi"] - s["lo"] <= 4
    assert {(x["name"], x["age"]) for x in s["people"]} == {("Ann", 16), ("Bob", 44)}
    people = {x["name"]: x for x in out["people"]}
    assert people["Ann"]["basis"] == "fit" and abs(people["Ann"]["birth"] - 1980) <= 1
    assert people["Cy"]["basis"] is None and people["Cy"]["birth"] is None
    # a birth year entered for Cy makes photo 2 suggestible: born 1960, looks 30
    monkeypatch.setattr(ages, "births", lambda: {12: 1960})
    two = {i["id"]: i for i in ages.suggestions()["items"]}[2]
    assert two["year"] == 1990 and two["hi"] - two["lo"] >= 8  # one person, from an entered year only: a wide range
    assert [i["id"] for i in ages.suggestions([2])["items"]] == [2]


def test_birth_years_and_the_suggestions_endpoint(client, lib):
    con = index.open_index()
    tag = con.execute("SELECT id FROM tags WHERE person = 1 LIMIT 1").fetchone()
    con.close()
    if not tag:
        pytest.skip("no person in the scratch library")
    tag = tag[0]
    assert client.post("/api/people/birth", json={"tag": tag, "year": 1700}).status_code == 400
    assert client.post("/api/people/birth", json={"tag": 999999999, "year": 1970}).status_code == 404
    assert client.post("/api/people/birth", json={"tag": tag, "year": 1971}).json()["ok"]
    assert ages.births()[tag] == 1971
    d = client.get("/api/datesuggest").json()
    assert {"total", "items", "people", "ready", "faces"} <= set(d)
    assert client.post("/api/people/birth", json={"tag": tag, "year": None}).json()["ok"]
    assert tag not in ages.births()
