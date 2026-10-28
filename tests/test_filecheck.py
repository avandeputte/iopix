"""Damaged files (filecheck.py): JPEGs that lack their end marker or stop early, damaged PNGs and undecodable
pictures are found; only a complete JPEG missing its end marker is repaired, backed up first, and Undo puts the
file back."""
import io
import sqlite3

import pytest
from PIL import Image

from app.photos import filecheck


def _jpeg(progressive=False, size=(640, 480)) -> bytes:
    im = Image.effect_noise(size, 60).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, progressive=progressive)
    return buf.getvalue()


@pytest.mark.parametrize("progressive", [False, True])
def test_the_jpeg_marker_walk(progressive):
    good = _jpeg(progressive)
    assert filecheck._jpeg_ends(good) == "ok"
    assert filecheck._jpeg_ends(good + b"TRAILER" * 10) == "ok"  # data after the end marker is a camera's trailer
    assert filecheck._jpeg_ends(good[:-2]) == "no_eoi"
    assert filecheck._jpeg_ends(good[: len(good) // 2]) == "no_eoi"
    assert filecheck._jpeg_ends(b"\x89PNG....") == "bad"
    # a damaged byte inside the picture data that looks like a marker (FF95): the file is whole, the data damaged
    sos = good.index(b"\xff\xda")
    k = sos + (len(good) - sos) // 2
    hurt = good[:k] + b"\xff\x95\x12\x34" + good[k + 4:]
    assert filecheck._jpeg_ends(hurt) == "corrupt"
    # the same damage with a junk length that runs the walk off the end: still not "cut off", the file ends with FFD9
    hurt2 = good[:k] + b"\xff\xc4\xff\xf0" + good[k + 4:]
    assert filecheck._jpeg_ends(hurt2) == "corrupt"


def test_what_the_check_finds(tmp_path):
    if not filecheck.jpegtran():
        pytest.skip("jpegtran not installed here")
    good = _jpeg()
    cases = {"good.jpg": good, "nomarker.jpg": good[:-2], "cut.jpg": good[: len(good) // 2], "empty.jpg": b""}
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, "PNG")
    png = bytearray(buf.getvalue())
    png[40] ^= 0xFF  # a byte inside the image data: its chunk checksum no longer matches
    cases["bad.png"], cases["ok.png"] = bytes(png), buf.getvalue()
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
    sos = good.index(b"\xff\xda")
    k = sos + (len(good) - sos) // 2
    cases["glitch.jpg"] = good[:k] + b"\xff\x95\x12\x34" + good[k + 4:]
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
    got = {n: (filecheck.check_file(str(tmp_path / n)) or {}).get("problem") for n in cases}
    assert got == {"good.jpg": None, "nomarker.jpg": "end_marker", "cut.jpg": "truncated", "empty.jpg": "empty", "bad.png": "png_crc", "ok.png": None, "glitch.jpg": "corrupt_data"}
    assert filecheck.check_file(str(tmp_path / "nomarker.jpg"))["fixable"] and not filecheck.check_file(str(tmp_path / "cut.jpg"))["fixable"]
    assert not filecheck.check_file(str(tmp_path / "glitch.jpg"))["fixable"]  # never "repaired" as if it were cut off


def test_run_repair_and_undo_on_the_scratch_library(client, lib):
    if not filecheck.jpegtran():
        pytest.skip("jpegtran not installed here")
    from app.data import index
    con = index.open_index()
    pid, path = con.execute("SELECT id, path FROM photos WHERE video = 0 AND lower(fmt) IN ('jpg', 'jpeg') ORDER BY id LIMIT 1").fetchone()
    con.close()
    original = open(path, "rb").read()
    db = sqlite3.connect(lib["db"])
    row_before = db.execute("SELECT fileSize, modificationDate FROM Images WHERE id = ?", (pid,)).fetchone()
    db.close()
    status, end = filecheck._walk(original)
    assert status == "ok"
    damaged = original[:end]  # cut just before the main picture's end marker (and any trailer after it), as on the burst photos
    with open(path, "wb") as f:
        f.write(damaged)
    try:
        assert filecheck.check_file(path)["problem"] == "end_marker"
        filecheck._job(filecheck.progress.gen, [p for p in filecheck._photos() if p["id"] == pid])
        found = {x["photo"]: x for x in client.get("/api/filecheck").json()["found"]}
        assert found[pid]["fixable"]
        r = client.post("/api/filecheck/repair", json={"ids": [pid]}).json()
        assert r["repaired"] == [pid], r
        assert open(path, "rb").read() == damaged + b"\xff\xd9" and filecheck.check_file(path) is None
        db = sqlite3.connect(lib["db"])
        assert db.execute("SELECT fileSize, modificationDate FROM Images WHERE id = ?", (pid,)).fetchone() == (len(damaged) + 2, None)
        db.close()
        entry = client.get(f"/api/history?photo={pid}").json()["entries"][0]
        assert entry["action"] == "filerepair" and "undo" in entry["restore"]
        assert client.post(f"/api/history/{entry['id']}/restore", json={"kind": "undo"}).status_code == 200
        assert open(path, "rb").read() == damaged  # back as it was before the repair
        # nothing to repair on a sound file
        with open(path, "wb") as f:
            f.write(original)
        assert client.post("/api/filecheck/repair", json={"ids": [pid]}).json()["failed"]
    finally:
        with open(path, "wb") as f:
            f.write(original)
        db = sqlite3.connect(lib["db"])  # digiKam's row as it was: other tests find this photo by its size
        db.execute("UPDATE Images SET fileSize = ?, modificationDate = ? WHERE id = ?", (*row_before, pid))
        db.commit()
        db.close()
        index.build()


def test_keep_rect_follows_the_orientation():
    assert filecheck.keep_rect(4000, 3000, 2000, 1) == [0, 0, 4000, 2000]
    assert filecheck.keep_rect(4000, 3000, 2000, 3) == [0, 1008, 4000, 1992]   # bottom of the stored picture is at the top
    assert filecheck.keep_rect(4000, 3000, 2000, 6) == [1008, 0, 1992, 4000]   # ... at the left
    assert filecheck.keep_rect(4000, 3000, 2000, 8) == [0, 0, 2000, 4000]      # ... at the right


def _db_row(lib, pid):
    db = sqlite3.connect(lib["db"])
    try:
        return (db.execute("SELECT fileSize, modificationDate FROM Images WHERE id = ?", (pid,)).fetchone(),
                db.execute("SELECT width, height, orientation FROM ImageInformation WHERE imageid = ?", (pid,)).fetchone(),
                db.execute("SELECT tagid, property, value FROM ImageTagProperties WHERE imageid = ? AND value LIKE '%<rect%'", (pid,)).fetchall())
    finally:
        db.close()


def test_keeping_the_intact_part_of_a_cut_off_photo(client, lib):
    """A photo whose data stops two thirds of the way down is cropped to the part above, without re-encoding;
    Undo puts the damaged file back. A damaged file jpegtran refuses outright is decoded and saved again."""
    if not filecheck.jpegtran():
        pytest.skip("jpegtran not installed here")
    from app.data import index
    con = index.open_index()
    pid, path = con.execute("SELECT id, path FROM photos p WHERE video = 0 AND lower(fmt) IN ('jpg', 'jpeg') ORDER BY id LIMIT 1").fetchone()
    con.close()
    original = open(path, "rb").read()
    rows_before = _db_row(lib, pid)
    try:
        for damage in ("cut", "two_sof"):
            status, end = filecheck._walk(original)
            sos = original.index(b"\xff\xda")
            cut = original[: sos + (end - sos) * 2 // 3]  # the bottom third of the picture data is gone
            if damage == "two_sof":  # a second frame header in the data: jpegtran refuses the file altogether
                sof = next(original.index(m) for m in (b"\xff\xc0", b"\xff\xc2", b"\xff\xc1") if m in original)
                seg = original[sof:sof + 2 + ((original[sof + 2] << 8) | original[sof + 3])]
                cut = cut + seg + b"\x00" * 64
            with open(path, "wb") as f:
                f.write(cut)
            index.build()
            found = filecheck.check_file(path)
            assert found and found["problem"] in ("truncated", "corrupt_data") and 0.4 < found.get("keep", 0) < 0.8, found
            r = client.post("/api/filecheck/keep", json={"ids": [pid]}).json()
            assert r["cropped"] == [pid], r
            assert filecheck.check_file(path) is None  # what is left is sound
            entry = client.get(f"/api/history?photo={pid}").json()["entries"][0]
            assert entry["action"] == "lossless" and "Cropped" in entry["summary"]
            assert ("re-encoded" in entry["summary"]) == (damage == "two_sof")
            assert client.post(f"/api/history/{entry['id']}/restore", json={"kind": "undo"}).status_code == 200
            assert open(path, "rb").read() == cut
    finally:
        with open(path, "wb") as f:
            f.write(original)
        db = sqlite3.connect(lib["db"])
        db.execute("UPDATE Images SET fileSize = ?, modificationDate = ? WHERE id = ?", (*rows_before[0], pid))
        db.execute("UPDATE ImageInformation SET width = ?, height = ?, orientation = ? WHERE imageid = ?", (*rows_before[1], pid))
        db.execute("DELETE FROM ImageTagProperties WHERE imageid = ? AND value LIKE '%<rect%'", (pid,))  # the face boxes as they were
        db.executemany("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", [(pid, *r) for r in rows_before[2]])
        db.commit()
        db.close()
        index.build()
