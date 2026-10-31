"""Sync tools' and NAS folders beside the photos (GoodSync's _gsdata_, a Synology's @eaDir...) are left out of the
folder tree, the index and so every listing and job; nothing on disk or in digiKam's database is touched."""
import sqlite3

from app import config


def test_which_folders_are_system_folders():
    assert config.is_system_folder("/2017/_gsdata_/_saved_") and config.is_system_folder("/_gsdata_")
    assert config.is_system_folder("/Photos/@eaDir/x") and config.is_system_folder("/a/.thumbnails")
    assert not config.is_system_folder("/2017/201706") and not config.is_system_folder("/") and not config.is_system_folder("/gsdata")


def test_a_gsdata_folder_and_its_photos_are_left_out(client, lib):
    from app.data import index
    db = sqlite3.connect(lib["db"])
    root, rel = db.execute("SELECT albumRoot, relativePath FROM Albums WHERE relativePath != '/' ORDER BY id LIMIT 1").fetchone()
    cur = db.execute("INSERT INTO Albums (albumRoot, relativePath, date, caption, collection) VALUES (?, ?, '2017-06-07', NULL, NULL)",
                     (root, rel.rstrip("/") + "/_gsdata_/_saved_"))
    aid = cur.lastrowid
    src = db.execute("SELECT * FROM Images WHERE status = 1 AND category = 1 ORDER BY id LIMIT 1").fetchone()
    cols = [c[1] for c in db.execute("PRAGMA table_info(Images)")]
    row = dict(zip(cols, src))
    row.update(id=None, album=aid, uniqueHash=(row.get("uniqueHash") or "") + "x")
    cur = db.execute(f"INSERT INTO Images ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
    pid = cur.lastrowid
    db.execute("INSERT INTO ImageInformation (imageid, width, height) VALUES (?, 4, 3)", (pid,))
    db.commit()
    try:
        index.build()
        assert pid not in client.get("/api/photos").json()["id"]
        assert client.get(f"/api/photo/{pid}").status_code == 404
        assert not any("_gsdata_" in f["path"] for f in client.get("/api/folders").json()["folders"])
        assert db.execute("SELECT 1 FROM Images WHERE id = ?", (pid,)).fetchone()  # digiKam's rows are untouched
    finally:
        db.execute("DELETE FROM ImageInformation WHERE imageid = ?", (pid,))
        db.execute("DELETE FROM Images WHERE id = ?", (pid,))
        db.execute("DELETE FROM Albums WHERE id = ?", (aid,))
        db.commit()
        db.close()
        index.build()
