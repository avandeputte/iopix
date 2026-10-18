"""The change log: every change is recorded with before/after, and can be undone or restored from backups."""
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from app.photos import edits
from app.data import index


def test_an_edit_is_logged_and_can_be_undone(client, lib):
    if not edits.available():
        pytest.skip("exiftool not available")
    index.ensure_fresh()
    con = index.open_index()
    pid, title = con.execute("SELECT id, title FROM photos WHERE video = 0 AND id IN (%s) LIMIT 1" % ",".join(map(str, lib["ids"][3:]))).fetchone()
    con.close()
    assert client.post("/api/edit", json={"ids": [pid], "changes": {"title": "History probe"}}).status_code == 200
    h = client.get("/api/history", params={"photo": pid}).json()
    e = h["entries"][0]
    assert e["action"] == "edit" and "History probe" in e["summary"] and "undo" in e["restore"], e
    assert "sidecar" in e["restore"] or not os.path.exists(e.get("path", "") + ".xmp")
    r = client.post(f"/api/history/{e['id']}/restore", json={"kind": "undo"})
    assert r.status_code == 200, r.text
    assert (client.get(f"/api/photo/{pid}").json()["title"] or "") == (title or ""), "the title is back (the edit resolved itself: digiKam agrees again)"
    assert client.get("/api/history", params={"photo": pid}).json()["entries"][0]["action"] == "restore"
    client.post("/api/edit/revert", json={"ids": [pid]})


def test_a_deleted_photo_can_be_restored(client, lib):
    index.ensure_fresh()
    con = index.open_index()
    # a still nobody else relies on: no faces, no GPS, the lowest id (the restored photo gets a new id)
    # a still no other test names: not among the first ids (the fixtures), not one with faces; the restored
    # photo gets a new id, which takes the old one's place in lib["ids"] for the tests that follow
    candidates = [i for i in lib["ids"][4:] if i not in lib["with_faces"][:2]]
    row = con.execute(f"SELECT id, path, album_id FROM photos WHERE video = 0 AND id IN ({','.join(map(str, candidates))}) ORDER BY id DESC LIMIT 1").fetchone()
    pid, path, album = row
    con.close()
    keep = Path(path + ".keep"); shutil.copy2(path, keep)
    r = client.post("/api/photos/delete", json={"ids": [pid], "confirm": True})
    assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
    backup = r.json()["backup"]
    e = client.get("/api/history", params={"photo": pid, "action": "deleted"}).json()["entries"][0]
    assert "deleted" in e["restore"], e
    r = client.post(f"/api/history/{e['id']}/restore", json={"kind": "deleted"})
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    assert os.path.exists(path)
    db = sqlite3.connect(lib["db"])
    assert db.execute("SELECT album, name FROM Images WHERE id = ?", (new_id,)).fetchone()[0] == album
    db.close()
    lib["ids"][lib["ids"].index(pid)] = new_id
    if pid in lib["with_faces"]:
        lib["with_faces"][lib["with_faces"].index(pid)] = new_id
    acts = [x["action"] for x in client.get("/api/history", params={"photo": new_id}).json()["entries"]]
    assert acts[:2] == ["restore", "deleted"], "the history follows the photo to its new id"
    keep.unlink()
    shutil.rmtree(backup, ignore_errors=True)


def _face_rows(db, pid):
    import sqlite3 as _s
    con = _s.connect(db)
    try:
        return sorted(con.execute("SELECT tagid, property, value FROM ImageTagProperties WHERE imageid = ?", (pid,)).fetchall())
    finally:
        con.close()


@pytest.mark.parametrize("op", ["ignore", "unknown", "remove", "move"])
def test_face_changes_can_be_undone(client, lib, op):
    """Every face change in the change log offers Undo, which puts the photo's faces back as they were."""
    import re
    from app.faces import facemgr
    pid = lib["with_faces"][4]
    before = _face_rows(lib["db"], pid)
    val = next(v for t, p, v in before if p == "tagRegion")
    x, y, w, h = map(int, re.search(r'x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', val).groups())
    body = {"op": op, "image": pid, "rect": [x, y, w, h]} | ({"new_rect": [x + 5, y, w, h]} if op == "move" else {})
    assert client.post("/api/faces/op", json=body).status_code == 200
    assert _face_rows(lib["db"], pid) != before
    e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
    assert "undo" in e["restore"], e
    r = client.post(f"/api/history/{e['id']}/restore", json={"kind": "undo"})
    assert r.status_code == 200, r.text
    assert _face_rows(lib["db"], pid) == before
    for _ in range(100):
        if not facemgr.sidecars_pending():
            break
        time.sleep(0.1)


def test_older_face_entries_without_undo_data_are_undone_from_their_details(client, lib):
    from app.faces import facemgr
    assert facemgr.inverse_ops({"op": "remove", "was": "confirmed", "from_tag": 26, "rect": [1, 2, 3, 4]}) == [{"op": "add", "rect": [1, 2, 3, 4], "tag": 26}]
    assert facemgr.inverse_ops({"op": "name", "tag": 194, "was": "confirmed", "from_tag": 89, "rect": [1, 2, 3, 4]}) == [{"op": "name", "rect": [1, 2, 3, 4], "tag": 89}]
    assert facemgr.inverse_ops({"op": "add", "tag": 26, "rect": [1, 2, 3, 4]}) == [{"op": "remove", "rect": [1, 2, 3, 4]}]
    assert facemgr.inverse_ops({"op": "confirm", "was": "suggested", "from_tag": 7, "rect": [1, 2, 3, 4]})[-1] == {"op": "suggest", "rect": [1, 2, 3, 4], "tag": 7}
    assert facemgr.inverse_ops({"op": "move", "rect": [1, 2, 3, 4]}) == [], "an old move without its new rectangle cannot be undone"


def test_backed_up_image_file_can_be_restored_from_any_change(client, lib):
    """A change whose photo has its original in file-backups offers "Restore original file", whatever kind of change it is."""
    import shutil
    from app.photos import edits, history as h
    pid = lib["ids"][7]
    ic = __import__("app.data.index", fromlist=["open_index"]).open_index()
    path = ic.execute("SELECT path FROM photos WHERE id = ?", (pid,)).fetchone()[0]; ic.close()
    b = edits.file_backup_path(path)
    b.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, b)
    h.record([{"photo_id": pid, "action": "face", "summary": "Face region removed", "details": {"op": "remove", "rect": [1, 2, 3, 4]}}])
    e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
    assert "original" in e["restore"] and e["path"] == path, e
    with open(path, "ab") as f:
        f.write(b"changed")
    assert client.post(f"/api/history/{e['id']}/restore", json={"kind": "original"}).status_code == 200
    assert open(path, "rb").read() == open(b, "rb").read()
    b.unlink()


def test_edit_log_search_by_words_person_user_and_time(client, lib):
    import time
    from app.photos import history
    pid = lib["with_faces"][0]
    history.record([{"photo_id": pid, "action": "edit", "summary": "Searchable zebra change", "user": "tester-x"}])
    r = client.get("/api/history?q=zebra").json()
    assert r["total"] >= 1 and all("zebra" in e["summary"].lower() for e in r["entries"]) and "tester-x" in r["users"]
    assert client.get("/api/history?q=zebra&user=tester-x").json()["total"] >= 1
    assert client.get("/api/history?q=zebra&user=nobody-at-all").json()["total"] == 0
    assert client.get(f"/api/history?q=zebra&since={int(time.time()) + 3600}").json()["total"] == 0
    tag = next(f["tag"] for f in client.get(f"/api/photo/{pid}").json()["faces"])
    by_person = client.get(f"/api/history?person={tag}&q=zebra").json()
    assert by_person["total"] >= 1 and all(e["photo"] for e in by_person["entries"])
    other = next(i for i in lib["ids"] if i not in lib["with_faces"])
    history.record([{"photo_id": other, "action": "edit", "summary": "zebra on a photo without that person", "user": "tester-x"}])
    assert all(e["photo"] != other for e in client.get(f"/api/history?person={tag}&q=zebra").json()["entries"])
