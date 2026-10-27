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
    got = {n: (filecheck.check_file(str(tmp_path / n)) or {}).get("problem") for n in cases}
    assert got == {"good.jpg": None, "nomarker.jpg": "end_marker", "cut.jpg": "truncated", "empty.jpg": "empty", "bad.png": "png_crc", "ok.png": None}
    assert filecheck.check_file(str(tmp_path / "nomarker.jpg"))["fixable"] and not filecheck.check_file(str(tmp_path / "cut.jpg"))["fixable"]


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
        filecheck._job(filecheck._run["gen"], [p for p in filecheck._photos() if p["id"] == pid])
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
