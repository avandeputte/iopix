"""The viewer's copy of the library (the lib_* tables): the index is built from it when digiKam's
database cannot be read or Sync with digiKam is off, editing keeps working meanwhile and the queued
edits reach digiKam's database when it is back, and photo_ids lets viewer data follow a photo that
digiKam renumbered."""
import shutil
import sqlite3
import time
from pathlib import Path

from helpers import need_tool
from app.digikam import dkdb
from app.photos import edits
from app.data import index, state


def _count(table: str) -> int:
    con = state.connect()
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _index_photos() -> int:
    con = index.open_index()
    try:
        return con.execute("SELECT count(*) FROM photos").fetchone()[0]
    finally:
        con.close()


def _renumber(db: Path, old: int, new: int):
    """What a re-import does to one photo: the same file under another image id, in every table."""
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("UPDATE Images SET id = ? WHERE id = ?", (new, old))
    for t in ("ImageInformation", "ImageMetadata", "ImagePositions", "ImageComments", "ImageCopyright", "ImageProperties",
              "ImageTags", "ImageTagProperties", "VideoMetadata", "ImageHistory"):
        try:
            con.execute(f"UPDATE {t} SET imageid = ? WHERE imageid = ?", (new, old))
        except sqlite3.OperationalError:
            pass
    con.commit()
    con.close()


def test_copy_follows_digikam_and_every_photo_has_a_viewer_id(client):
    index.ensure_fresh()
    m = index.mirror_info()
    assert m and m["source"] == dkdb.source_version()
    assert m["photos"] == _count("photos") > 0
    assert _count("photo_identity") >= m["photos"]  # one row per photo; rows of photos deleted by earlier tests stay (a copy never takes another photo's row)
    assert _count("tags") > 0 and _count("photo_tags") > 0 and _count("folders") > 0
    s = client.get("/api/status").json()["digikam"]
    assert s["sync"] and s["reachable"] and s["source"] == "digikam" and s["mirror"]["photos"] == m["photos"]


def test_index_builds_from_the_copy_while_digikam_is_unreachable_and_edits_still_work(client, lib, photo_id, monkeypatch):
    index.ensure_fresh()
    n = _index_photos()
    dbf = Path(lib["db"])
    gone = Path(str(dbf) + ".gone")
    shutil.move(dbf, gone)
    try:
        assert index.probe()[0] == "library" and index.offline["since"]
        assert index.ensure_fresh() is False, "the copy is what the index was last built from: nothing changed"
        stats = index.build()
        assert stats["photos"] == n and index.last_source["kind"] == "library"
        s = client.get("/api/status").json()["digikam"]
        assert s["sync"] and not s["reachable"] and s["source"] == "library"
        assert edits.digikam_tag_paths([photo_id])[photo_id] is not None, "tag paths come from the copy"
        if edits.available():
            r = client.post("/api/edit", json={"ids": [photo_id], "changes": {"rating": 4}})
            assert r.status_code == 200, r.text
            assert edits.pending().get(photo_id, {}).get("rating") == 4
    finally:
        shutil.move(gone, dbf)
    assert index.probe()[0] == "digikam" and index.offline["since"] is None
    if not edits.available():
        return
    # back online and digiKam closed (vouched, DIGIKAM_CHECK=ask): the queued edit goes into digiKam's database
    from app import main
    main._auto["at"] = 0
    monkeypatch.setattr(edits, "newest_pending", lambda: int(time.time()) - 120)
    main._refresh(initial=True)  # initial: no 'digiKam wrote seconds ago' wait, the scratch file is brand new
    assert main.state["error"] is None, main.state["error"]
    assert main._auto["at"] > 0, "the automatic apply ran"
    con = sqlite3.connect(dbf)
    assert con.execute("SELECT rating FROM ImageInformation WHERE imageid = ?", (photo_id,)).fetchone()[0] == 4
    con.close()
    index.ensure_fresh()  # digiKam now agrees, so the remembered edit is dropped
    assert photo_id not in edits.pending()


def test_sync_off_everything_works_on_the_viewers_own_library(client, lib, photo_id):
    """Sync with digiKam off: digiKam's database is never opened, and tags, folders and faces are created in the
    library tables instead; turning sync on again does not overwrite them until the owner says so."""
    from app import main

    def rebuilt():  # what the watcher does after a change: rebuild, and drop the cached answers
        index.build()
        main.state["version"] = None
        main._refresh(initial=True)
    index.ensure_fresh()
    dbf = Path(lib["db"])
    dk_before = sqlite3.connect(dbf).execute("SELECT count(*) FROM Tags").fetchone()[0]
    assert client.post("/api/settings", json={"digikam_sync": False}).status_code == 200
    try:
        assert client.get("/api/status").json()["digikam"]["sync"] is False
        con = dkdb.connect()
        assert getattr(con, "standalone", False), "digiKam's names reach the library tables"
        assert con.execute("SELECT count(*) FROM Images").fetchone()[0] == _count("photos")
        con.close()
        # a tag, a folder and a face, all without digiKam
        r = client.post("/api/tags/op", json={"op": "add", "parent": 0, "name": "zz-standalone-tag"})
        assert r.status_code == 200, r.text
        rebuilt()
        tags = client.get("/api/meta").json()["tags"]
        assert any(t["name"] == "zz-standalone-tag" for t in tags), "the new tag is in the viewer"
        assert sqlite3.connect(dbf).execute("SELECT count(*) FROM Tags").fetchone()[0] == dk_before, "digiKam's database untouched"
        assert index.mirror_info()["local_changes"] >= 1
        stats = index.build()
        assert stats["photos"] > 0 and index.last_source["kind"] == "library"
        assert isinstance(edits.digikam_tag_paths([photo_id])[photo_id], list)
        assert client.post("/api/edits/db").status_code == 409, "edits wait for digiKam while sync is off"
    finally:
        client.post("/api/settings", json={"digikam_sync": True})
    # sync back on: the library keeps the change and says so, until the owner chooses digiKam's data
    assert index.probe()[0] == "library"
    st = client.get("/api/status").json()["digikam"]
    assert st["conflict"] and st["localChanges"] >= 1
    rebuilt()
    assert any(t["name"] == "zz-standalone-tag" for t in client.get("/api/meta").json()["tags"])
    assert client.post("/api/settings", json={"library_replace": True}).status_code == 200
    assert index.probe()[0] == "digikam"
    rebuilt()
    assert not any(t["name"] == "zz-standalone-tag" for t in client.get("/api/meta").json()["tags"]), "digiKam's library again"



def test_viewer_data_follows_a_photo_digikam_renumbered(client, lib):
    index.ensure_fresh()
    pid = lib["ids"][1]
    con = state.connect()
    try:
        vid, uhash = con.execute("SELECT vid, hash FROM photo_identity WHERE dk_id = ?", (pid,)).fetchone()
        new = con.execute("SELECT max(dk_id) FROM photo_identity").fetchone()[0] + 1000
    finally:
        con.close()
    state.set_favorites([(pid, uhash)], True)
    assert pid in state.favorite_ids()
    _renumber(Path(lib["db"]), pid, new)
    try:
        index.build()
        assert new in state.favorite_ids() and pid not in state.favorite_ids(), "the favorite followed the file"
        con = state.connect()
        try:
            assert con.execute("SELECT dk_id FROM photo_identity WHERE vid = ?", (vid,)).fetchone()[0] == new, "same viewer id, new digiKam id"
            assert con.execute("SELECT count(*) FROM photo_identity WHERE dk_id = ?", (pid,)).fetchone()[0] == 0
        finally:
            con.close()
    finally:
        _renumber(Path(lib["db"]), new, pid)
        index.build()
    assert pid in state.favorite_ids()
    state.set_favorites([(pid, uhash)], False)


def test_applying_an_edit_rebuilds_so_it_no_longer_shows_as_waiting(client, photo_id):
    from app.photos import edits
    need_tool("exiftool", edits.available())
    assert client.post("/api/edit", json={"ids": [photo_id], "changes": {"caption": "Applied caption probe"}}).status_code == 200
    assert photo_id in edits.pending()
    r = client.post("/api/edits/db")
    assert r.status_code == 200 and r.json()["applied"] >= 1, r.text
    assert photo_id not in edits.pending(), "the rebuild right after the write resolves the edit"
    assert client.get(f"/api/photo/{photo_id}").json()["edited"] == 0


def test_sync_off_folders_and_faces_go_to_the_library(client, lib):
    from app.photos import edits
    from app.faces import facemgr
    from app.digikam import library
    need_tool("exiftool", edits.available())
    index.ensure_fresh()
    dbf = Path(lib["db"])
    dk = sqlite3.connect(dbf)
    albums_before = dk.execute("SELECT count(*) FROM Albums").fetchone()[0]
    faces_before = dk.execute("SELECT count(*) FROM ImageTagProperties").fetchone()[0]
    dk.close()
    con = index.open_index()
    root = con.execute("SELECT id FROM albums WHERE pid IS NULL").fetchone()[0]
    con.close()
    client.post("/api/settings", json={"digikam_sync": False})
    made = None
    try:
        r = client.post("/api/folders", json={"parent": root, "name": "zz-standalone-folder"})
        assert r.status_code == 200, r.text
        made = Path(r.json()["dir"])
        pid = lib["ids"][0]
        res = facemgr.apply({"op": "detected", "image": pid, "rect": [1, 1, 40, 40]})
        assert "error" not in res, res
        lc = library.connect()
        assert lc.execute("SELECT count(*) FROM Albums WHERE relativePath LIKE '%zz-standalone-folder'").fetchone()[0] == 1
        assert lc.execute("SELECT count(*) FROM ImageTagProperties WHERE imageid = ? AND value LIKE '<rect x=\"1\" y=\"1\"%'", (pid,)).fetchone()[0] == 1
        lc.close()
        dk = sqlite3.connect(dbf)
        assert dk.execute("SELECT count(*) FROM Albums").fetchone()[0] == albums_before
        assert dk.execute("SELECT count(*) FROM ImageTagProperties").fetchone()[0] == faces_before
        dk.close()
    finally:
        client.post("/api/settings", json={"digikam_sync": True})
        client.post("/api/settings", json={"library_replace": True})
        index.build()
        if made and made.exists():
            made.rmdir()


def test_changes_made_with_sync_off_merge_back_into_digikam(client, lib):
    """A tag, a face and a folder made with sync off; sync on: the merge writes them into digiKam's database
    (new ids there), then the library is digiKam's again with nothing left to merge."""
    from app.faces import facemgr
    from app.digikam import library
    from app import main
    from app.photos import tagmanager
    index.ensure_fresh()
    dbf = Path(lib["db"])
    pid = lib["ids"][2]
    con = index.open_index()
    root = con.execute("SELECT id FROM albums WHERE pid IS NULL").fetchone()[0]
    con.close()
    client.post("/api/settings", json={"digikam_sync": False})
    made = None
    try:
        r = client.post("/api/tags/op", json={"op": "add", "parent": 0, "name": "zz-merge-tag"}); assert r.status_code == 200, r.text
        r = client.post("/api/folders", json={"parent": root, "name": "zz-merge-folder"}); assert r.status_code == 200, r.text
        made = Path(r.json()["dir"])
        res = facemgr.apply({"op": "detected", "image": pid, "rect": [7, 7, 50, 50]}); assert "error" not in res, res
    finally:
        client.post("/api/settings", json={"digikam_sync": True})
    p = client.get("/api/library/merge").json()
    assert p["tags"]["new"] == 1 and p["folders"]["new"] == 1 and p["face_regions"]["new"] == 1, p
    assert any("1 new tags" in s for s in p["summary"])
    r = client.post("/api/library/merge"); assert r.status_code == 200, r.text
    dk = sqlite3.connect(dbf)
    tid = dk.execute("SELECT id FROM Tags WHERE name = 'zz-merge-tag'").fetchone()
    assert tid, "the tag is in digiKam"
    assert dk.execute("SELECT count(*) FROM Albums WHERE relativePath LIKE '%zz-merge-folder'").fetchone()[0] == 1
    assert dk.execute("SELECT count(*) FROM ImageTagProperties WHERE imageid = ? AND value LIKE '<rect x=\"7\" y=\"7\"%'", (pid,)).fetchone()[0] == 1
    dk.close()
    assert index.mirror_info()["local_changes"] == 0 and index.probe()[0] == "digikam"
    assert client.get("/api/library/merge").json()["total"] == 0
    # clean up the scratch library
    tagmanager.apply({"op": "delete", "id": tid[0]})
    facemgr.apply({"op": "remove", "image": pid, "rect": [7, 7, 50, 50]})
    dk = sqlite3.connect(dbf); dk.execute("DELETE FROM Albums WHERE relativePath LIKE '%zz-merge-folder'"); dk.commit(); dk.close()
    if made and made.exists():
        made.rmdir()
    main.state["version"] = None
    index.build()
    _ = library


def test_library_follows_digikam_row_by_row(client, lib):
    """Bringing the library up to date writes only the rows that differ (none when nothing changed), and
    leaves it identical to digiKam's tables, duplicates in the keyless tables included."""
    from app.digikam import dkdb, library
    from app.data import vdb
    index.refresh_library()
    src = dkdb.connect(real=True)
    try:
        src.begin_read()
        assert library.copy_from_digikam(src) == {}, "nothing changed: nothing written"
    finally:
        src.rollback(); src.close()
    con = vdb.connect()
    try:
        pid = con.execute("SELECT imageid FROM photo_information LIMIT 1").fetchone()[0]
        con.execute("UPDATE photo_information SET rating = 5 WHERE imageid = ?", (pid,))
        tag_row = con.execute("SELECT imageid, tagid FROM photo_tags LIMIT 1").fetchone()
        con.execute("DELETE FROM photo_tags WHERE imageid = ? AND tagid = ?", tuple(tag_row))
        face = con.execute("SELECT imageid, tagid, property, value FROM face_regions LIMIT 1").fetchone()
        con.execute("INSERT INTO face_regions VALUES (?,?,?,?)", tuple(face))  # a stray duplicate
        con.commit()
    finally:
        con.close()
    src = dkdb.connect(real=True)
    try:
        src.begin_read()
        written = library.copy_from_digikam(src)
    finally:
        src.rollback(); src.close()
    assert set(written) == {"photo_information", "photo_tags", "face_regions"} and sum(written.values()) <= 6, written
    a = index.read_rows(library.connect())
    src = dkdb.connect(real=True)
    b = index.read_rows(src); src.close()
    assert all(sorted(map(repr, a[k])) == sorted(map(repr, b[k])) for k in a)
