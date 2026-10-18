"""Import: staged files go into folders by the date they were taken, duplicates are left out, every new
file gets a sidecar, and the installed app's share target lands on the Import page with the files ready."""
import shutil
import sqlite3
import subprocess
from pathlib import Path

from app.digikam import dkdb
from app.photos import edits
from app.data import index


def _src(lib):
    """A JPEG the library knows: (path, photo id), read from digiKam's database so the UI test (another process) can use it too."""
    con = sqlite3.connect(lib["db"])
    try:
        for pid, rel, name in con.execute("SELECT i.id, a.relativePath, i.name FROM Images i JOIN Albums a ON a.id = i.album "
                                          "WHERE i.status = 1 AND i.fileSize > 20000 AND (i.name LIKE '%.jpg' OR i.name LIKE '%.JPG') ORDER BY i.id"):
            path = Path(lib["root"]) / rel.strip("/") / name
            if path.exists() and "zz-" not in str(path):
                return path, pid
    finally:
        con.close()
    raise AssertionError("no JPEG in the fixture library")


def _dated(lib, tmp_path, stamp="1999:12:31 10:00:00"):
    """A copy of a library photo, taken on another day (a new file: a different size and contents)."""
    p = tmp_path / "trip.jpg"
    shutil.copy(_src(lib)[0], p)
    subprocess.run([*edits.exiftool_cmd(), "-q", "-overwrite_original", f"-exif:DateTimeOriginal={stamp}", f"-exif:CreateDate={stamp}", f"-xmp:DateTimeOriginal={stamp}", f"-xmp:CreateDate={stamp}",
                    "-xmp-dc:Description=import probe", str(p)], check=True)
    return p


def forget_identity(ids):
    """Test photos removed behind the viewer's back lose their viewer ids too (the mirror test counts them)."""
    from app.data import vdb
    con = vdb.connect()
    try:
        for i in ids:
            con.execute("DELETE FROM photo_identity WHERE dk_id = ?", (int(i),))
        con.commit()
    finally:
        con.close()


def _cleanup(client, lib, rels):
    root = Path(lib["root"])
    con = sqlite3.connect(lib["db"])
    for rel in rels:
        for (aid,) in con.execute("SELECT id FROM Albums WHERE relativePath = ? OR relativePath LIKE ?", (rel, rel + "/%")).fetchall():
            forget_identity([r[0] for r in con.execute("SELECT id FROM Images WHERE album = ?", (aid,)).fetchall()])
            con.execute("DELETE FROM ImageInformation WHERE imageid IN (SELECT id FROM Images WHERE album = ?)", (aid,))
            con.execute("DELETE FROM Images WHERE album = ?", (aid,))
            con.execute("DELETE FROM Albums WHERE id = ?", (aid,))
        shutil.rmtree(root / rel.strip("/"), ignore_errors=True)
    con.commit(); con.close()
    try:
        index.build()
    except Exception:  # the UI tests' server rebuilds its own index when it notices
        pass


def test_import_sorts_by_date_leaves_duplicates_out_and_writes_sidecars(client, lib, tmp_path):
    src, src_id = _src(lib)
    new = _dated(lib, tmp_path)
    lay = client.get("/api/import/layouts").json()
    assert lay["layout"] == "year-yearmonth" and any(l["pattern"] == "{yyyy}/{yyyy}{mm}" for l in lay["layouts"])
    r = client.post("/api/import/stage", files=[("files", ("same.jpg", src.read_bytes(), "image/jpeg")), ("files", ("trip.jpg", new.read_bytes(), "image/jpeg")),
                                                ("files", ("notes.txt", b"hi", "text/plain"))])
    assert r.status_code == 200, r.text
    plan = r.json()
    by = {i["name"]: i for i in plan["items"]}
    assert by["same.jpg"]["duplicate_of"] == src_id, "same size and contents: the photo it duplicates"
    assert by["trip.jpg"]["duplicate_of"] is None and by["trip.jpg"]["when"].startswith("1999-12-31") and by["trip.jpg"]["folder"] == "/1999/199912"
    assert "not accepted" in by["notes.txt"]["error"] and plan["new"] == 1 and plan["duplicates"] == 1
    token = plan["token"]
    # another layout, looked at before importing
    assert next(i for i in client.get(f"/api/import/{token}?layout=year-date").json()["items"] if i["name"] == "trip.jpg")["folder"] == "/1999/1999-12-31"
    try:
        r = client.post(f"/api/import/{token}/run", json={"layout": "year-yearmonth"})
        assert r.status_code == 200, r.text
        res = r.json()
        assert [a["folder"] for a in res["added"]] == ["/1999/199912"] and {s["name"] for s in res["skipped"]} == {"same.jpg", "notes.txt"}
        assert res["skipped"][0].get("duplicate_of") == src_id or res["skipped"][1].get("duplicate_of") == src_id
        path = Path(res["added"][0]["path"])
        assert path.exists() and path.read_bytes() == new.read_bytes()
        side = Path(str(path) + ".xmp")
        assert side.exists(), "every imported file gets a sidecar"
        xmp = side.read_text(errors="replace")
        assert "1999-12-31" in xmp and "import probe" in xmp, "the sidecar holds the file's own metadata"
        con = sqlite3.connect(lib["db"])
        rels = dict(con.execute("SELECT relativePath, id FROM Albums WHERE relativePath IN ('/', '/1999', '/1999/199912')").fetchall())
        img = con.execute("SELECT album, name, status, modificationDate IS NOT NULL, uniqueHash IS NOT NULL FROM Images WHERE id = ?", (res["added"][0]["id"],)).fetchone()
        con.close()
        assert set(rels) == {"/", "/1999", "/1999/199912"}, "parents first, as digiKam lays them out"
        assert img == (rels["/1999/199912"], "trip.jpg", 1, 1, 1), "scanned as digiKam would: its hash and time recorded"
        e = client.get(f"/api/history?photo={res['added'][0]['id']}&limit=1").json()["entries"][0]
        assert e["action"] == "added" and "Imported into /1999/199912" in e["summary"]
        assert client.get(f"/api/photos?album={rels['/1999/199912']}").json()["count"] == 1, "in the viewer at once"
        assert client.get(f"/api/import/{token}").status_code == 404, "the staging folder is gone once everything was dealt with"
    finally:
        _cleanup(client, lib, ["/1999"])


def test_duplicates_can_be_imported_on_purpose_and_staging_can_be_discarded(client, lib, tmp_path):
    src = _src(lib)[0]
    r = client.post("/api/import/stage", files=[("files", ("again.jpg", src.read_bytes(), "image/jpeg"))])
    token = r.json()["token"]
    when = r.json()["items"][0]["when"][:7].replace("-", "")
    try:
        r = client.post(f"/api/import/{token}/run", json={"duplicates": "import", "layout": "yearmonth"})
        assert r.status_code == 200 and len(r.json()["added"]) == 1 and r.json()["added"][0]["folder"] == "/" + when, r.text
    finally:
        _cleanup(client, lib, ["/" + when])
    r = client.post("/api/import/stage", files=[("files", ("x.jpg", src.read_bytes(), "image/jpeg"))])
    token = r.json()["token"]
    assert client.post(f"/api/import/{token}/discard").status_code == 200
    assert client.get(f"/api/import/{token}").status_code == 404
    assert client.get("/api/import/not-a-token!").status_code == 404


def test_share_target_stages_the_files_and_opens_the_import_page(client, lib):
    src = _src(lib)[0]
    r = client.post("/share", files=[("files", ("shared.jpg", src.read_bytes(), "image/jpeg"))], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/#/import?staged="), r.text
    token = r.headers["location"].split("staged=")[1]
    plan = client.get(f"/api/import/{token}").json()
    assert plan["items"][0]["name"] == "shared.jpg" and plan["duplicates"] == 1
    client.post(f"/api/import/{token}/discard")
    r = client.post("/share", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#/import"
    man = client.get("/manifest.webmanifest").json()
    assert man["share_target"]["action"] == "share" and man["share_target"]["method"] == "POST" and man["share_target"]["params"]["files"][0]["name"] == "files"


def test_uploads_get_a_sidecar_too(client, lib):
    folders = client.get("/api/folders").json()["folders"]
    f = next(x for x in folders if x["rel"] != "/")
    data = _src(lib)[0].read_bytes()
    r = client.post(f"/api/folders/{f['id']}/upload", files=[("files", ("zz-side.jpg", data, "image/jpeg"))])
    assert r.status_code == 200, r.text
    path = Path(lib["root"]) / f["rel"].strip("/") / r.json()["added"][0]["name"]
    try:
        assert path.exists() and Path(str(path) + ".xmp").exists()
        e = client.get(f"/api/history?photo={r.json()['added'][0]['id']}&limit=1").json()["entries"][0]
        assert "sidecar created" in e["summary"]
    finally:
        con = sqlite3.connect(lib["db"]); con.execute("DELETE FROM Images WHERE id = ?", (r.json()["added"][0]["id"],)); con.commit(); con.close()
        forget_identity([r.json()["added"][0]["id"]])
        path.unlink(missing_ok=True); Path(str(path) + ".xmp").unlink(missing_ok=True)
        index.build()
    assert dkdb  # imported for the reader: the rows above are digiKam's
