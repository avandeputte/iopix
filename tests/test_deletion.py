"""Deleting photos: a backup of the files, sidecars and database first, then the database rows, the files
and their sidecars, and everything the viewer keeps about them."""
import json
import os
import shutil
import sqlite3
from pathlib import Path

from app import config
from app.data import index, state


def test_delete_needs_confirmation(client, lib):
    assert client.post("/api/photos/delete", json={"ids": [lib["ids"][0]]}).status_code == 400


def test_delete_removes_file_sidecar_rows_and_viewer_data_after_a_backup(client, lib):
    index.ensure_fresh()
    con = index.open_index()
    pid, path, uhash = con.execute("SELECT id, path, hash FROM photos WHERE video = 0 AND id IN (%s) LIMIT 1" % ",".join(map(str, lib["ids"]))).fetchone()
    con.close()
    side = path + ".xmp"
    created_side = not os.path.exists(side)
    if created_side:
        Path(side).write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")
    keep = Path(str(path) + ".keep")
    shutil.copy2(path, keep)  # to put the scratch library back
    keep_side = Path(side + ".keep"); shutil.copy2(side, keep_side)
    state.set_favorites([(pid, uhash)], True)
    db = Path(lib["db"])
    dk = sqlite3.connect(db)
    dump = {t: dk.execute(f"SELECT * FROM {t} WHERE {'id' if t == 'Images' else 'imageid'} = ?", (pid,)).fetchall()
            for t in ("Images", "ImageInformation", "ImageTags", "ImageTagProperties", "ImageComments")}
    dk.close()
    r = client.post("/api/photos/delete", json={"ids": [pid], "confirm": True})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["deleted"] == 1 and not j["failed"], j
    backup = Path(j["backup"])
    m = json.loads((backup / "manifest.json").read_text())
    assert m["photos"][0]["id"] == pid and any(f.endswith(".xmp") for f in m["photos"][0]["files"])
    assert all((backup / f).exists() for f in m["photos"][0]["files"]), "the files and sidecar were copied first"
    assert not os.path.exists(path) and not os.path.exists(side), "file and sidecar are gone"
    dk = sqlite3.connect(db)
    assert not dk.execute("SELECT 1 FROM Images WHERE id = ?", (pid,)).fetchone()
    assert not dk.execute("SELECT 1 FROM ImageTags WHERE imageid = ?", (pid,)).fetchone()
    assert not dk.execute("SELECT 1 FROM ImageTagProperties WHERE imageid = ?", (pid,)).fetchone()
    # put the scratch library back as it was
    for t, rows in dump.items():
        for row in rows:
            dk.execute(f"INSERT OR REPLACE INTO {t} VALUES ({','.join('?' * len(row))})", row)
    dk.commit(); dk.close()
    assert pid not in state.favorite_ids()
    assert client.get(f"/api/photo/{pid}").status_code == 404
    shutil.move(keep, path); shutil.move(keep_side, side)
    if created_side:
        os.remove(side)
    shutil.rmtree(backup, ignore_errors=True)
    index.build()
    assert config.DATA_DIR.exists()
