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


def test_old_password_hashes_are_upgraded_at_sign_in(client, fresh):
    """A hash in the first format (scrypt$salt$hash, n=2^14) still signs in and is replaced by the current one."""
    import base64, hashlib, secrets
    from app import users
    from app.data import state
    u = users.create("legacy-probe", "longpassword1", "viewer")
    salt = secrets.token_bytes(16)
    old = "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(hashlib.scrypt(b"longpassword1", salt=salt, n=2 ** 14, r=8, p=1, dklen=32)).decode()
    con = state.connect(); con.execute("UPDATE user_accounts SET hash = ? WHERE id = ?", (old, u["id"])); con.commit(); con.close()
    assert users.needs_rehash(old)
    assert client.post("/api/auth/login", json={"name": "legacy-probe", "password": "longpassword1"}).status_code == 200
    con = state.connect(); h = con.execute("SELECT hash FROM user_accounts WHERE id = ?", (u["id"],)).fetchone()[0]; con.close()
    assert h.startswith("scrypt2$65536$8$1$") and not users.needs_rehash(h)
    assert client.post("/api/auth/login", json={"name": "legacy-probe", "password": "longpassword1"}).status_code == 200
    client.post("/api/auth/logout", json={})


def test_sign_out_everywhere_ends_the_other_sessions(client, fresh):
    from app import users
    users.create("multi-probe", "longpassword1", "viewer")
    r = client.post("/api/auth/login", json={"name": "multi-probe", "password": "longpassword1"})
    phone = r.cookies.get(users.COOKIE)  # the same account on another device
    assert phone
    client.post("/api/auth/logout_all", json={})
    assert client.get("/api/auth/me").json()["user"]["name"] == "multi-probe", "this session was renewed"
    client.cookies.clear(); client.cookies.set(users.COOKIE, phone)
    assert client.get("/api/auth/me").json()["user"] is None, "the other device's session is over"
    client.cookies.clear()


def test_sidecar_naming_follows_digikam(tmp_path, monkeypatch):
    from app.photos import edits
    photo = tmp_path / "IMG_1.jpg"; photo.write_bytes(b"x")
    monkeypatch.delenv("SIDECAR_COMPAT", raising=False)
    assert edits.sidecar_path(str(photo)) == str(photo) + ".xmp", "digiKam's default name for a new sidecar"
    (tmp_path / "IMG_1.xmp").write_text("<x/>")
    assert edits.sidecar_path(str(photo)) == str(tmp_path / "IMG_1.xmp"), "the compatible name when only that one exists"
    (tmp_path / "IMG_1.cr2").write_bytes(b"y")
    assert edits.sidecar_path(str(photo)) == str(photo) + ".xmp", "IMG_1.xmp would be shared with IMG_1.cr2: not this photo's"
    (tmp_path / "IMG_1.jpg.xmp").write_text("<x/>")
    assert edits.sidecar_path(str(photo)) == str(photo) + ".xmp", "the default name wins when both exist"
    photo2 = tmp_path / "IMG_2.jpg"; photo2.write_bytes(b"x")
    monkeypatch.setenv("SIDECAR_COMPAT", "1")
    assert edits.sidecar_path(str(photo2)) == str(tmp_path / "IMG_2.xmp"), "new sidecars in the compatible style when asked"


def test_only_people_with_a_face_box_are_protected_by_the_sync(client, lib):
    """A person tagged without a face box (a video, or a tag-only person) syncs like any keyword; one with a box stays."""
    from app.photos import consistency
    con = sqlite3.connect(lib["db"])
    try:
        boxed = con.execute("SELECT imageid, tagid FROM ImageTagProperties WHERE property = 'tagRegion' LIMIT 1").fetchone()
        if not boxed:
            pytest.skip("no named face in the scratch library")
        pid, tid = boxed
        path = con.execute("SELECT name FROM Tags WHERE id = ?", (tid,)).fetchone()[0]
        with_box = {r[0] for r in con.execute("SELECT DISTINCT imageid FROM ImageTagProperties")}
        other = next(i for i in lib["ids"] if i != pid and i not in with_box)  # a photo with no face box at all
        con.execute("INSERT OR IGNORE INTO ImageTags (imageid, tagid) VALUES (?, ?)", (other, tid)); con.commit()  # the same person, no box
    finally:
        con.close()
    b = consistency._boxed_people([pid, other])
    assert any(p.endswith(path) for p in b.get(pid, ())), "the person with the box is protected on that photo"
    assert not b.get(other), "the same person without a box is an ordinary tag on the other photo"
    con = sqlite3.connect(lib["db"]); con.execute("DELETE FROM ImageTags WHERE imageid = ? AND tagid = ?", (other, tid)); con.commit(); con.close()
