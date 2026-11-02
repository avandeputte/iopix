"""Jersey numbers (app/ai/jerseys.py): reading them on detected people, learning who wears which, and suggesting
the player for people whose face is not named."""
import sqlite3

import pytest

from app.ai import jerseys
from app.data import index, vdb

pytest.importorskip("numpy")


def test_read_numbers_keeps_one_or_two_digit_numbers_per_person():
    from PIL import Image
    im = Image.new("RGB", (1000, 800), (40, 90, 40))
    calls = []

    class Ocr:
        def __call__(self, crop):
            return [(t, c) for t, c, _b in self.boxes(crop)]

        def boxes(self, crop):
            calls.append(crop.size)
            w, h = crop.size
            return [("10", 0.97, (w * 0.3, h * 0.3, w * 0.6, h * 0.5)), ("GOALS", 0.9, (w * 0.3, h * 0.6, w * 0.7, h * 0.7)),
                    ("7", 0.4, (w * 0.4, h * 0.4, w * 0.6, h * 0.5)), ("2024", 0.95, (w * 0.3, h * 0.1, w * 0.6, h * 0.2)),
                    ("3", 0.99, (w * 0.01, h * 0.5, w * 0.08, h * 0.6))]  # a small "3" at the edge: a banner behind, not the shirt
    ocr = Ocr()
    boxes = [(0.1, 0.1, 0.2, 0.6), (0.5, 0.2, 0.01, 0.02)]  # the second is too small to crop
    found = jerseys.read_numbers(im, boxes, ocr)
    assert [f[:3] for f in found] == [((0.1, 0.1, 0.2, 0.6), "10", 0.97)] and len(found[0][3]) == 3
    assert len(calls) == 1 and calls[0][0] >= jerseys.CROP_WIDTH  # the torso crop is enlarged for the recogniser


def _index(tmp_path):
    p = tmp_path / "index.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE photos (id INTEGER PRIMARY KEY, album_id INTEGER, year INTEGER, w INTEGER, h INTEGER);
        CREATE TABLE faces (photo_id INTEGER, tag_id INTEGER, x INTEGER, y INTEGER, w INTEGER, h INTEGER, kind TEXT);
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, person INTEGER);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE photo_tags (tag_id INTEGER, photo_id INTEGER);
        INSERT INTO tags VALUES (10, 'Sam', 1), (11, 'Lee', 1);
        INSERT INTO albums VALUES (5, '20240914 Game'), (6, '20230505 Other season');
        INSERT INTO photos VALUES (9, 5, 2024, 1000, 800);
    """)
    # photos 1-3: Sam's face sits in the person box wearing 10 (album 5, 2024); photo 4: number 10 on an unnamed box
    # photo 5: album 6, 2023: Lee wears 10 there; photo 6: album 6, unnamed 10; photo 7: 2024, another folder, unnamed 10
    con.executemany("INSERT INTO photos VALUES (?,?,?,?,?)", [(1, 5, 2024, 1000, 800), (2, 5, 2024, 1000, 800), (3, 5, 2024, 1000, 800), (4, 5, 2024, 1000, 800),
                                                             (5, 6, 2023, 1000, 800), (6, 6, 2023, 1000, 800), (7, 7, 2024, 1000, 800), (8, 7, 2024, 1000, 800)])
    con.executemany("INSERT INTO faces VALUES (?,?,?,?,?,?,?)", [(1, 10, 150, 90, 60, 60, "confirmed"), (2, 10, 150, 90, 60, 60, "confirmed"), (3, 10, 150, 90, 60, 60, "confirmed"),
                                                                 (5, 11, 150, 90, 60, 60, "confirmed"), (8, 10, 150, 90, 60, 60, "confirmed")])
    con.execute("INSERT INTO photo_tags VALUES (10, 3)")  # Sam already tagged on photo 3 (and on 1 and 2 through the faces: not modelled here)
    con.commit()
    con.close()
    return p


def test_roster_and_suggestions(tmp_path, monkeypatch, lib):
    p = _index(tmp_path)
    monkeypatch.setattr(index, "open_index", lambda path=None: sqlite3.connect(f"file:{p}?mode=ro", uri=True))
    with vdb.writer() as con:
        con.execute("DELETE FROM ai_jerseys")
        con.executemany("INSERT INTO ai_jerseys VALUES (?,?,?,?,?,?,?,?,?,?)", [(pid, 100, 50, 200, 600, "10", 0.95, 200, 180, 150) for pid in (1, 2, 3, 4, 5, 6, 7, 8)])
        con.execute("INSERT INTO ai_jerseys VALUES (9, 100, 50, 200, 600, '10', 0.95, 20, 180, 150)")  # the other team's red 10, in Sam's folder
        con.execute("DELETE FROM suggestion_dismissed")
    try:
        out = jerseys.suggestions()
        items = {i["id"]: i for i in out["items"]}
        assert set(items) == {4, 7}  # 6: Lee's folder has only one named sighting; 8: Sam's own face is in the box; 9: a red shirt, Sam wears blue
        from app.ai import dismissed
        dismissed.set_dismissed("jersey", [(4, 10)])
        assert set(i["id"] for i in jerseys.suggestions()["items"]) == {7}
        dismissed.set_dismissed("jersey", [(4, 10)], on=False)
        assert items[4]["name"] == "Sam" and items[4]["basis"] == "folder" and items[4]["n"] == 3 and "20240914 Game" in items[4]["text"]
        assert items[7]["name"] == "Sam" and items[7]["basis"] == "year" and items[7]["n"] >= 3  # another folder, the same year: Sam wears 10 in 2024
        assert items[4]["box"] == {"x": 0.1, "y": 0.05, "w": 0.2, "h": 0.6}
        assert {(pl["name"], tuple(pl["numbers"])) for pl in out["players"]} == {("Sam", ("10",))}
        assert out["read"] == 9 and out["named"] == 5
        assert [i["id"] for i in jerseys.suggestions([7])["items"]] == [7]
        assert jerseys.for_photo(4)[0]["number"] == "10"
        assert jerseys.results()[4] == "10"
    finally:
        with vdb.writer() as con:
            con.execute("DELETE FROM ai_jerseys")


def test_endpoint(client, lib):
    d = client.get("/api/jerseys").json()
    assert {"total", "items", "players", "ready"} <= set(d)
