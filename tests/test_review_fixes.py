"""Fixes from the code review: writes that must never wipe a photo's rows, duplicate files that keep their own
identity, and values that must not be mistaken for timestamps."""
import sqlite3

import pytest

from app.digikam import dkdb
from app.digikam.dbsync import SyncError


def test_a_failed_metadata_read_changes_nothing(client, lib, monkeypatch):
    """exiftool giving nothing back (a dropped mount, a timeout) must not rewrite the photo's rows from an empty record."""
    from app.photos import metaedit, scanner
    pid = lib["with_tags"][0] if lib.get("with_tags") else lib["ids"][0]
    con = sqlite3.connect(lib["db"])
    try:
        before = con.execute("SELECT count(*) FROM ImageTags WHERE imageid = ?", (pid,)).fetchone()[0]
        comments = con.execute("SELECT count(*) FROM ImageComments WHERE imageid = ?", (pid,)).fetchone()[0]
    finally:
        con.close()
    real_exif = scanner._exif
    monkeypatch.setattr(scanner, "_exif", lambda paths: {})
    with pytest.raises(SyncError):
        metaedit.reread(pid, check_running=False)
    monkeypatch.setattr(scanner, "_exif", real_exif)
    con = sqlite3.connect(lib["db"])
    try:
        assert con.execute("SELECT count(*) FROM ImageTags WHERE imageid = ?", (pid,)).fetchone()[0] == before
        assert con.execute("SELECT count(*) FROM ImageComments WHERE imageid = ?", (pid,)).fetchone()[0] == comments
    finally:
        con.close()

    def boom(*a, **k):
        raise OSError("exiftool: no such file")
    monkeypatch.setattr("subprocess.run", boom)
    with pytest.raises(SyncError):
        scanner.read(["/nowhere/x.jpg"])


def test_a_reread_keeps_digikams_internal_tags(client, lib):
    """digiKam's own tags (Scanned for Faces, version chains) are not in any file; a reread must not drop them."""
    from app.photos import metaedit
    pid = lib["ids"][0]
    con = sqlite3.connect(lib["db"])
    try:
        root = con.execute("SELECT id FROM Tags WHERE name = '_Digikam_Internal_Tags_' AND pid = 0").fetchone()
        if not root:
            pytest.skip("the scratch library has no internal tags root")
        tid = con.execute("SELECT id FROM Tags WHERE pid = ? AND name = 'Scanned for Faces'", (root[0],)).fetchone()
        if not tid:
            con.execute("INSERT INTO Tags (pid, name) VALUES (?, 'Scanned for Faces')", (root[0],)); tid = (con.execute("SELECT last_insert_rowid()").fetchone()[0],)
        con.execute("INSERT OR IGNORE INTO ImageTags (imageid, tagid) VALUES (?, ?)", (pid, tid[0]))
        con.commit()
    finally:
        con.close()
    metaedit.reread(pid, check_running=False)
    con = sqlite3.connect(lib["db"])
    try:
        assert con.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (pid, tid[0])).fetchone(), "the internal tag survived the reread"
    finally:
        con.close()


def test_identical_copies_keep_their_own_identity(client):
    """Two byte-identical files are two photos: neither takes the other's viewer id on a rebuild."""
    from app.data import index, state
    con = state.connect()
    try:
        con.execute("DELETE FROM photo_identity WHERE dk_id IN (900101, 900102, 900103)"); con.commit()
    finally:
        con.close()
    images = [(900101, 0, "a.jpg", 1234, "samehash"), (900102, 0, "copy of a.jpg", 1234, "samehash")]
    paths = {900101: "/lib/a.jpg", 900102: "/lib/copy of a.jpg"}
    assert index.sync_photo_ids(images, paths) == {}
    con = state.connect()
    try:
        first = dict(con.execute("SELECT dk_id, vid FROM photo_identity WHERE dk_id IN (900101, 900102)").fetchall())
    finally:
        con.close()
    assert set(first) == {900101, 900102} and first[900101] != first[900102]
    for _ in range(3):  # the same build again and again: nothing moves
        assert index.sync_photo_ids(images, paths) == {}
    con = state.connect()
    try:
        again = dict(con.execute("SELECT dk_id, vid FROM photo_identity WHERE dk_id IN (900101, 900102)").fetchall())
    finally:
        con.close()
    assert again == first
    # digiKam renumbers the first one (its old number gone): its row follows, the copy's stays
    images2 = [(900103, 0, "a.jpg", 1234, "samehash"), (900102, 0, "copy of a.jpg", 1234, "samehash")]
    paths2 = {900103: "/lib/a.jpg", 900102: "/lib/copy of a.jpg"}
    assert index.sync_photo_ids(images2, paths2) == {900101: 900103}
    con = state.connect()
    try:
        after = dict(con.execute("SELECT dk_id, vid FROM photo_identity WHERE dk_id IN (900102, 900103)").fetchall())
        con.execute("DELETE FROM photo_identity WHERE dk_id IN (900101, 900102, 900103)"); con.commit()
    finally:
        con.close()
    assert after[900103] == first[900101] and after[900102] == first[900102]


def test_only_whole_timestamps_are_timestamps():
    assert dkdb.looks_like_datetime("2024-01-01T10:00:00") and dkdb.looks_like_datetime("2024-01-01T10:00:00.123")
    assert not dkdb.looks_like_datetime("2024-01-01T10:00:00 New Year party")
    assert not dkdb.looks_like_datetime("2024-01-01T10:00:00.000 x")
