"""The file scanner: digiKam's unique hash reproduced, full rows for uploads, and the collection scan finding new,
changed and vanished files and folders, with the guard against an unmounted disk."""
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.photos import edits, scanner
from app.data import index


def _db(lib):
    return sqlite3.connect(lib["db"])


def _path(lib, rel, name):
    return Path(lib["root"]) / rel.strip("/") / name


def _photo_with(lib, where: str):
    """(id, path, album id) of a JPEG digiKam scanned whose file is unchanged since (same size)."""
    con = _db(lib)
    try:
        for iid, rel, name, size, album in con.execute(
                "SELECT i.id, a.relativePath, i.name, i.fileSize, i.album FROM Images i JOIN Albums a ON a.id = i.album "
                f"WHERE i.status = 1 AND i.category = 1 AND (i.name LIKE '%.jpg' OR i.name LIKE '%.JPG' OR i.name LIKE '%.jpeg') AND {where} ORDER BY i.id"):
            p = _path(lib, rel, name)
            if p.exists() and p.stat().st_size == size:
                return iid, p, album
    finally:
        con.close()
    pytest.skip("no suitable photo in the fixture")


def _forget(lib, ids, files=()):
    from test_import import forget_identity
    con = _db(lib)
    for t in ("ImageInformation", "ImageMetadata", "ImagePositions", "ImageComments", "ImageCopyright", "ImageTags", "ImageTagProperties", "VideoMetadata", "ImageProperties"):
        con.executemany(f"DELETE FROM {t} WHERE imageid = ?", [(i,) for i in ids])
    con.executemany("DELETE FROM Images WHERE id = ?", [(i,) for i in ids])
    con.commit(); con.close()
    forget_identity(ids)
    for f in files:
        Path(f).unlink(missing_ok=True); Path(str(f) + ".xmp").unlink(missing_ok=True)
    index.build()


def test_unique_hash_is_digikams():
    """Against the source library the fixture is cut from (read only): files digiKam hashed and nobody changed since."""
    from conftest import SRC_DB, SRC_ROOT
    con = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    try:
        version = int((con.execute("SELECT value FROM Settings WHERE keyword = 'uniqueHashVersion'").fetchone() or ["1"])[0])
        rows = con.execute("SELECT a.relativePath, i.name, i.uniqueHash, i.fileSize FROM Images i JOIN Albums a ON a.id = i.album "
                           "WHERE i.status = 1 AND i.uniqueHash IS NOT NULL ORDER BY i.id DESC LIMIT 400").fetchall()
    finally:
        con.close()
    if version < 2:
        pytest.skip("the source library uses digiKam's old Exif-based hash")
    checked = 0
    for rel, name, uh, size in rows:
        p = Path(SRC_ROOT) / rel.strip("/") / name
        if p.exists() and p.stat().st_size == size:
            assert scanner.unique_hash(str(p), version) == uh, p
            checked += 1
    assert checked >= 10, checked


def test_upload_gets_the_rows_digikams_scan_writes(client, lib, tmp_path):
    src_id, src, album = _photo_with(lib, "i.id IN (SELECT imageid FROM ImageMetadata WHERE make IS NOT NULL) AND i.id IN (SELECT imageid FROM ImagePositions)")
    con = _db(lib)
    meta = con.execute("SELECT make, model, lens, aperture, focalLength, exposureTime, sensitivity FROM ImageMetadata WHERE imageid = ?", (src_id,)).fetchone()
    pos = con.execute("SELECT latitudeNumber, longitudeNumber FROM ImagePositions WHERE imageid = ?", (src_id,)).fetchone()
    info = con.execute("SELECT width, height, orientation, creationDate FROM ImageInformation WHERE imageid = ?", (src_id,)).fetchone()
    con.close()
    # a different file (its comment changed), so it is scanned rather than copied from its twin
    probe = tmp_path / "zz-scan-probe.jpg"
    shutil.copy(src, probe)
    subprocess.run([*edits.exiftool_cmd(), "-q", "-overwrite_original", "-Comment=scanner probe", str(probe)], check=True)
    folders = client.get("/api/folders").json()["folders"]
    f = next(x for x in folders if x["id"] == album)
    r = client.post(f"/api/folders/{album}/upload", files=[("files", ("zz-scan-probe.jpg", probe.read_bytes(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    nid = r.json()["added"][0]["id"]
    dest = _path(lib, f["rel"], r.json()["added"][0]["name"])
    try:
        con = _db(lib)
        img = con.execute("SELECT uniqueHash, fileSize, modificationDate FROM Images WHERE id = ?", (nid,)).fetchone()
        assert img[0] == scanner.unique_hash(str(dest), scanner.hash_version()) and img[1] == dest.stat().st_size and img[2], "digiKam's hash, size and time"
        m2 = con.execute("SELECT make, model, lens, aperture, focalLength, exposureTime, sensitivity FROM ImageMetadata WHERE imageid = ?", (nid,)).fetchone()
        assert m2[:3] == meta[:3] and abs(m2[3] - meta[3]) < 0.01 and abs(m2[4] - meta[4]) < 0.01 and m2[6] == meta[6], (m2, meta)
        p2 = con.execute("SELECT latitudeNumber, longitudeNumber FROM ImagePositions WHERE imageid = ?", (nid,)).fetchone()
        assert p2 and abs(p2[0] - pos[0]) < 1e-5 and abs(p2[1] - pos[1]) < 1e-5
        i2 = con.execute("SELECT width, height, orientation, creationDate, format, colorModel FROM ImageInformation WHERE imageid = ?", (nid,)).fetchone()
        assert i2[:3] == info[:3] and i2[3][:19] == info[3][:19] and i2[4] == "JPG" and i2[5] == 5
        con.close()
        # the identical file: attributes copied from the photo it duplicates
        r = client.post(f"/api/folders/{album}/upload", files=[("files", ("zz-scan-twin.jpg", src.read_bytes(), "image/jpeg"))])
        tid = r.json()["added"][0]["id"]
        con = _db(lib)
        tags = lambda i: sorted(t for (t,) in con.execute("SELECT tagid FROM ImageTags WHERE imageid = ?", (i,)))
        assert tags(tid) == tags(src_id), "a copy of a known file keeps its tags, faces and labels"
        con.close()
        _forget(lib, [tid], [_path(lib, f["rel"], "zz-scan-twin.jpg")])
    finally:
        _forget(lib, [nid], [dest])


def test_scan_finds_new_changed_and_vanished_files(client, lib, tmp_path):
    src_id, src, album = _photo_with(lib, "1")
    root = Path(lib["root"])
    newdir = root / "zz-scan-new" / "sub"
    newdir.mkdir(parents=True)
    added = newdir / "found.jpg"
    shutil.copy(src, added)
    subprocess.run([*edits.exiftool_cmd(), "-q", "-overwrite_original", "-Comment=found by the scanner", str(added)], check=True)
    assert client.post("/api/settings", json={"file_scanner": True, "scan_interval": 5}).status_code == 200
    st = client.get("/api/scan").json()
    assert st["enabled"] and st["interval"] == 5
    try:
        s = scanner.scan()
        assert s["error"] is None and s["added"] == 1 and s["folders"] >= 2, s  # and the root folder row, when the library had none
        con = _db(lib)
        rels = {r[0]: r[1] for r in con.execute("SELECT relativePath, id FROM Albums WHERE relativePath LIKE '/zz-scan-new%'")}
        assert set(rels) == {"/zz-scan-new", "/zz-scan-new/sub"}
        row = con.execute("SELECT id, uniqueHash, modificationDate FROM Images WHERE album = ? AND name = 'found.jpg' AND status = 1", (rels["/zz-scan-new/sub"],)).fetchone()
        assert row and row[1] and row[2]
        nid = row[0]
        assert con.execute("SELECT width FROM ImageInformation WHERE imageid = ?", (nid,)).fetchone()[0] > 0
        con.close()
        assert scanner.scan()["added"] == 0, "a second pass finds nothing new"
        # changed on disk: the modified-file scan updates size, hash and time, nothing else
        subprocess.run([*edits.exiftool_cmd(), "-q", "-overwrite_original", "-Comment=changed again, a little longer", str(added)], check=True)
        import os, time
        os.utime(added, (time.time() + 5, time.time() + 5))
        s = scanner.scan()
        assert s["modified"] == 1, s
        con = _db(lib)
        assert con.execute("SELECT fileSize FROM Images WHERE id = ?", (nid,)).fetchone()[0] == added.stat().st_size
        con.close()
        # gone from disk: status 3 and no album, as digiKam's scanner leaves it
        added.unlink()
        s = scanner.scan()
        assert s["removed"] == 1, s
        con = _db(lib)
        assert con.execute("SELECT status, album FROM Images WHERE id = ?", (nid,)).fetchone() == (3, None)
        con.close()
    finally:
        client.post("/api/settings", json={"file_scanner": False})
        shutil.rmtree(root / "zz-scan-new", ignore_errors=True)
        con = _db(lib)
        ids = [r[0] for r in con.execute("SELECT id FROM Images WHERE name = 'found.jpg'")]
        con.execute("DELETE FROM Albums WHERE relativePath LIKE '/zz-scan-new%'"); con.commit(); con.close()
        _forget(lib, ids)


def test_an_empty_or_missing_root_is_left_alone(lib, monkeypatch, tmp_path):
    from app.digikam import dkdb
    empty = tmp_path / "unmounted"
    empty.mkdir()
    con = dkdb.connect()
    try:
        roots = dkdb.root_paths(con)
    finally:
        con.close()
    monkeypatch.setattr(dkdb, "root_paths", lambda con: {k: str(empty) for k in roots})
    s = scanner.scan()
    assert s["removed"] == 0 and any("unmounted" in x or "left alone" in x for x in s["skipped"]), s
    con = sqlite3.connect(lib["db"])
    assert con.execute("SELECT count(*) FROM Images WHERE status = 1").fetchone()[0] > 100, "nothing was marked removed"
    con.close()
