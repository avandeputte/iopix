"""Trash, move and rename: digiKam's own .dtrash layout and database states, rows keep their ids, files and
sidecars travel together, and every step is in the change log with a way back."""
import json
import os
import sqlite3
from pathlib import Path

from app.data import index


def _row(db, pid):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT album, name, status FROM Images WHERE id = ?", (pid,)).fetchone()
    finally:
        con.close()


def _path(pid):
    ic = index.open_index()
    try:
        r = ic.execute("SELECT path FROM photos WHERE id = ?", (pid,)).fetchone()
        return r[0] if r else None
    finally:
        ic.close()


def test_trash_restore_and_empty_follow_digikam(client, lib):
    pid = lib["ids"][40]
    path = _path(pid)
    side = path + ".xmp"
    Path(side).write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>\n") if not os.path.exists(side) else None
    r = client.post("/api/photos/trash", json={"ids": [pid]})
    assert r.status_code == 200 and r.json()["trashed"] == [pid], r.text
    assert not os.path.exists(path) and not os.path.exists(side)
    root = Path(lib["root"])
    recs = list((root / ".dtrash" / "info").glob("*.dtrashinfo"))
    assert recs, "digiKam's trash record"
    rec = json.loads(recs[0].read_text())
    assert rec["path"] == path and rec["imageid"] == str(pid) and rec["deletiontimestamp"][:4].isdigit()
    assert (root / ".dtrash" / "files" / (recs[0].stem + "." + os.path.basename(path).split(".", 1)[1])).exists()
    assert _row(lib["db"], pid) == (None, os.path.basename(path), 3), "status Trashed, no album, as CoreDB::removeItems does"
    assert _path(pid) is None, "gone from the index"
    t = client.get("/api/trash").json()
    item = next(i for i in t["items"] if i["id"] == pid)
    assert item["exists"] and item["name"] == os.path.basename(path) and client.get(f"/api/trash/thumb?info={item['info']}").status_code == 200
    e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
    assert e["action"] == "trashed" and "trash" in e["restore"]
    # back it comes, same id, same place, sidecar too
    r = client.post("/api/trash/restore", json={"infos": [item["info"]]})
    assert r.status_code == 200 and r.json()["restored"] == [pid], r.text
    assert os.path.exists(path) and os.path.exists(side) and _row(lib["db"], pid)[2] == 1 and _path(pid) == path
    assert not (root / ".dtrash" / "info").glob("*.dtrashinfo") or not list((root / ".dtrash" / "info").glob("*.dtrashinfo"))
    # trash again, then delete for good
    client.post("/api/photos/trash", json={"ids": [pid]})
    item = next(i for i in client.get("/api/trash").json()["items"] if i["id"] == pid)
    r = client.post("/api/trash/empty", json={"infos": [item["info"]]})
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert _row(lib["db"], pid) is None and not Path(item["trash"]).exists() and not Path(item["info"]).exists()
    lib["ids"].remove(pid)


def test_restore_from_the_change_log(client, lib):
    pid = lib["ids"][41]
    path = _path(pid)
    client.post("/api/photos/trash", json={"ids": [pid]})
    e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
    r = client.post(f"/api/history/{e['id']}/restore", json={"kind": "trash"})
    assert r.status_code == 200, r.text
    assert os.path.exists(path) and _row(lib["db"], pid)[2] == 1


def test_move_and_rename_keep_the_id(client, lib):
    pid = lib["ids"][42]
    path = _path(pid)
    side = path + ".xmp"
    if not os.path.exists(side):
        Path(side).write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>\n")
    before = _row(lib["db"], pid)
    folders = client.get("/api/folders").json()["folders"]
    other = next(f for f in folders if f["id"] != before[0] and f["rel"] != "/")
    r = client.post("/api/photos/move", json={"ids": [pid], "album": other["id"]})
    assert r.status_code == 200 and r.json()["moved"][0]["id"] == pid, r.text
    new_path = r.json()["moved"][0]["path"]
    assert os.path.exists(new_path) and os.path.exists(new_path + ".xmp") and not os.path.exists(path)
    assert _row(lib["db"], pid) == (other["id"], os.path.basename(path), 1)
    assert _path(pid) == new_path
    e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
    assert e["action"] == "moved" and "undo" in e["restore"]
    assert client.post(f"/api/history/{e['id']}/restore", json={"kind": "undo"}).status_code == 200
    assert os.path.exists(path) and _row(lib["db"], pid) == before
    # rename: the extension stays
    r = client.post("/api/photos/rename", json={"id": pid, "name": "renamed-probe.png"})
    assert r.status_code == 200, r.text
    ext = os.path.splitext(path)[1]
    assert r.json()["moved"][0]["name"] == "renamed-probe" + ext and os.path.exists(os.path.join(os.path.dirname(path), "renamed-probe" + ext))
    client.post("/api/photos/rename", json={"id": pid, "name": os.path.basename(path)})
    assert _row(lib["db"], pid) == before


def test_rename_and_move_folders_with_their_subfolders(client, lib):
    folders = client.get("/api/folders").json()["folders"]
    top = next(f for f in folders if f["rel"] == "/") if any(f["rel"] == "/" for f in folders) else folders[0]
    a = client.post("/api/folders", json={"parent": top["id"], "name": "zz-org-a"}).json()
    b = client.post("/api/folders", json={"parent": a["id"], "name": "zz-org-b"}).json()
    base = Path(a["dir"]).parent
    r = client.post(f"/api/folders/{a['id']}/rename", json={"name": "zz-org-renamed"})
    assert r.status_code == 200 and r.json()["path"].endswith("/zz-org-renamed") and r.json()["folders"] == 2, r.text
    con = sqlite3.connect(lib["db"])
    rels = dict(con.execute("SELECT id, relativePath FROM Albums WHERE id IN (?, ?)", (a["id"], b["id"])).fetchall()); con.close()
    assert rels[a["id"]].endswith("/zz-org-renamed") and rels[b["id"]].endswith("/zz-org-renamed/zz-org-b")
    assert (base / "zz-org-renamed" / "zz-org-b").is_dir() and not Path(a["dir"]).exists()
    c = client.post("/api/folders", json={"parent": top["id"], "name": "zz-org-c"}).json()
    r = client.post(f"/api/folders/{a['id']}/move", json={"parent": c["id"]})
    assert r.status_code == 200 and r.json()["path"].endswith("/zz-org-c/zz-org-renamed"), r.text
    assert (base / "zz-org-c" / "zz-org-renamed" / "zz-org-b").is_dir()
    assert client.post(f"/api/folders/{c['id']}/move", json={"parent": b["id"]}).status_code == 409, "not into itself"
    import shutil
    shutil.rmtree(base / "zz-org-c")
    con = sqlite3.connect(lib["db"]); con.execute("DELETE FROM Albums WHERE id IN (?,?,?)", (a["id"], b["id"], c["id"])); con.commit(); con.close()
    index.build()
