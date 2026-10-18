"""digiKam folders: a new folder is a directory plus the Albums row digiKam's New Album adds;
an upload puts the file in the folder and registers it for digiKam's full scan (null
modificationDate) with the basics the viewer shows meanwhile. Writes go to the scratch copy."""
import time

from app.digikam import dkdb


def _wait_meta_album(client, path, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if any(a["path"] == path for a in client.get("/api/meta").json()["albums"]):
            return True
        time.sleep(0.3)
    return False


def test_folder_list_includes_empty_folders_and_the_tree(client):
    r = client.get("/api/folders").json()
    assert r["folders"] and "canManage" in r
    tops = [f for f in r["folders"] if f["pid"] is None]
    assert tops, "folders whose parent digiKam does not know are top-level"
    assert all(f["pid"] is not None for f in r["folders"] if f not in tops)


def _cleanup(disk):
    """Take the test folder and its rows out again, so the other tests see the library they expect."""
    import shutil
    con = dkdb.connect(write=True)
    try:
        con.begin_write()
        ids = [r[0] for r in con.execute("SELECT id FROM Albums WHERE relativePath LIKE '%/zz-upload-test%'")]
        for aid in ids:
            for iid, in con.execute("SELECT id FROM Images WHERE album = ?", (aid,)).fetchall():
                con.execute("DELETE FROM ImageInformation WHERE imageid = ?", (iid,))
                con.execute("DELETE FROM Images WHERE id = ?", (iid,))
            con.execute("DELETE FROM Albums WHERE id = ?", (aid,))
        con.commit()
    finally:
        con.close()
    shutil.rmtree(disk / "zz-upload-test", ignore_errors=True)


def test_create_folder_and_upload(client, lib, tmp_path):
    top = next(f for f in client.get("/api/folders").json()["folders"] if f["pid"] is None)
    root, base = top["id"], top["rel"].rstrip("/")
    disk = lib["root"] / base.strip("/")
    try:
        _create_and_upload(client, lib, root, base, disk)
    finally:
        _cleanup(disk)
        _wait_gone(client, base + "/zz-upload-test")


def _wait_gone(client, path, timeout=10):
    from app import main as appmain
    appmain._kick.set()
    end = time.time() + timeout
    while time.time() < end:
        if not any(a["path"] == path for a in client.get("/api/meta").json()["albums"]):
            return
        time.sleep(0.3)


def _create_and_upload(client, lib, root, base, disk):
    # names digiKam would not accept either
    for bad in ("", "../x", "a/b", ".hidden"):
        assert client.post("/api/folders", json={"parent": root, "name": bad}).status_code == 400, bad
    r = client.post("/api/folders", json={"parent": root, "name": "zz-upload-test"})
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    assert (disk / "zz-upload-test").is_dir()
    con = dkdb.connect()
    row = con.execute("SELECT albumRoot, relativePath, date, modificationDate FROM Albums WHERE id = ?", (fid,)).fetchone()
    con.close()
    assert row[1] == base + "/zz-upload-test" and row[2] == time.strftime("%Y-%m-%d") and row[3] is None, "digiKam scans a folder without a modification date"
    assert client.post("/api/folders", json={"parent": root, "name": "zz-upload-test"}).status_code == 400, "exists"
    assert any(f["id"] == fid and f["nAll"] == 0 for f in client.get("/api/folders").json()["folders"]), "listed while still empty"
    # a sub-folder
    r = client.post("/api/folders", json={"parent": fid, "name": "sub"})
    assert r.status_code == 200 and r.json()["path"] == base + "/zz-upload-test/sub"
    # upload a JPEG from the library and a file digiKam would ignore
    src = next(p for p in (lib["root"]).rglob("*") if p.suffix.lower() in (".jpg", ".jpeg") and "zz-upload" not in str(p))
    data = src.read_bytes()
    r = client.post(f"/api/folders/{fid}/upload", files=[("files", ("New Photo.JPG", data, "image/jpeg")), ("files", ("notes.txt", b"hi", "text/plain")),
                                                        ("files", ("New Photo.JPG", data, "image/jpeg"))])
    assert r.status_code == 200, r.text
    res = r.json()
    assert [a["name"] for a in res["added"]] == ["New Photo.JPG", "New Photo (2).JPG"]
    assert res["skipped"][0]["name"] == "notes.txt" and "not accepted" in res["skipped"][0]["reason"]
    assert (disk / "zz-upload-test" / "New Photo.JPG").read_bytes() == data
    con = dkdb.connect()
    iid = res["added"][0]["id"]
    img = con.execute("SELECT album, name, status, category, modificationDate, fileSize, uniqueHash FROM Images WHERE id = ?", (iid,)).fetchone()
    info = con.execute("SELECT rating, creationDate, orientation, width, height, format, colorDepth, colorModel FROM ImageInformation WHERE imageid = ?", (iid,)).fetchone()
    con.close()
    assert img[:4] == (fid, "New Photo.JPG", 1, 1) and img[4] and img[5] == len(data) and img[6], "scanned at once as digiKam would: time, size and digiKam's hash"
    assert info[0] == -1 and info[1] and info[1][4] == "-" and info[3] > 0 and info[4] > 0 and info[5] == "JPG" and info[6:] == (8, 5)  # YCbCr for a JPEG, as digiKam records it
    # the viewer shows the folder and the photos as soon as the index catches up
    assert _wait_meta_album(client, base + "/zz-upload-test")
    end = time.time() + 8
    while time.time() < end:
        aid = next((a["id"] for a in client.get("/api/meta").json()["albums"] if a["path"] == base + "/zz-upload-test"), None)
        d = client.get(f"/api/photos?album={aid}").json() if aid else {"count": 0}
        if d.get("count") == 2:
            break
        time.sleep(0.3)
    assert d["count"] == 2 and iid in d["id"]
    assert client.get(f"/thumb/{iid}").status_code == 200
    assert client.get("/api/folders/999999/upload").status_code in (404, 405)


def test_folder_properties_are_digikams_album_fields(client, lib):
    import sqlite3
    folders = client.get("/api/folders").json()["folders"]
    f = next(x for x in folders if x["rel"] != "/" and x["nAll"])
    pid = client.get(f"/api/photos?album={f['id']}").json()["id"][-1]
    r = client.post(f"/api/folders/{f['id']}/properties", json={"caption": "Our trip", "date": "2024-07-04", "category": "Holidays", "icon": pid})
    assert r.status_code == 200, r.text
    con = sqlite3.connect(lib["db"])
    row = con.execute("SELECT caption, date, collection, icon FROM Albums WHERE id = ?", (f["id"],)).fetchone()
    con.close()
    assert row[0] == "Our trip" and str(row[1])[:10] == "2024-07-04" and row[2] == "Holidays" and row[3] == pid
    g = next(x for x in client.get("/api/folders").json()["folders"] if x["id"] == f["id"])
    assert g["caption"] == "Our trip" and g["category"] == "Holidays" and g["date"] == "2024-07-04" and g["icon"] == pid
    a = next(x for x in client.get("/api/meta").json()["albums"] if x["id"] == f["id"])
    assert a["cover"] == pid and a["caption"] == "Our trip", "the chosen cover is the folder's picture"
    assert client.post(f"/api/folders/{f['id']}/properties", json={"date": "July 4"}).status_code == 400
    assert client.post(f"/api/folders/{f['id']}/properties", json={"icon": 99999999}).status_code == 400
    client.post(f"/api/folders/{f['id']}/properties", json={"caption": "", "date": "", "category": "", "icon": None})
    con = sqlite3.connect(lib["db"])
    assert con.execute("SELECT caption, date, collection, icon FROM Albums WHERE id = ?", (f["id"],)).fetchone() == (None, None, None, None)
    con.close()
