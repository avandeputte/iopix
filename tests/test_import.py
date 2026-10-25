"""Import: staged files go into folders by the date they were taken, duplicates are left out, every new
file gets a sidecar, and the installed app's share target lands on the Import page with the files ready."""
import sqlite3
from pathlib import Path

from app.digikam import dkdb
from app.data import index
from helpers import forget_identity, _src, _dated, _cleanup


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


def test_an_app_can_name_its_own_folder_pattern():
    """The Mac and iPhone apps send a folder pattern of their own instead of a layout key."""
    from app.photos import importer
    import pytest
    assert importer.folder_for("2024-07-04T10:00:00", "{yyyy}/{yyyy}{mm}") == "/2024/202407"
    assert importer.folder_for("2024-07-04T10:00:00", "Uploads") == "/Uploads"
    assert importer.folder_for("2024-07-04T10:00:00", "Phone/{yyyy}-{mm}-{dd}") == "/Phone/2024-07-04"
    assert importer.folder_for("2024-07-04T10:00:00", "year-date") == "/2024/2024-07-04", "layout keys still work"
    for bad in ("", "/Uploads", "Uploads/", "a//b", "a/../b", ".", "{year}", "{yyyy", "a\\b", "x" * 201, "a\x01b"):
        with pytest.raises(ValueError):
            importer.check_pattern(bad)


def test_import_run_takes_a_pattern(client, lib, tmp_path):
    import io, sqlite3
    from PIL import Image
    assert client.get("/api/import/layouts").json().get("patterns") is True, "apps can tell the server takes patterns"
    buf = io.BytesIO(); Image.new("RGB", (40, 30), (10, 200, 30)).save(buf, "JPEG"); buf.seek(0)
    r = client.post("/api/import/stage", files=[("files", ("pattern-test.jpg", buf.getvalue(), "image/jpeg"))], data={"pattern": "Phone/{yyyy}"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    item = next(i for i in r.json()["items"] if i["name"] == "pattern-test.jpg")
    assert item["folder"].startswith("/Phone/") and r.json()["layout"] == "Phone/{yyyy}"
    assert client.get(f"/api/import/{token}?pattern=/bad").status_code == 400
    assert client.get(f"/api/import/{token}?pattern={{year}}").status_code == 400
    assert client.get(f"/api/import/{token}?pattern=Uploads").json()["items"][0]["folder"] == "/Uploads"
    r = client.post(f"/api/import/{token}/run", json={"pattern": "{nope}"})
    assert r.status_code == 400 and "placeholder" in r.json()["detail"]
    r = client.post(f"/api/import/{token}/run", json={"pattern": "Uploads/{yyyy}"})
    if r.status_code == 409:
        import pytest; pytest.skip("digiKam is running")
    assert r.status_code == 200, r.text
    added = r.json()["added"]
    assert added, r.json()
    try:
        dk = sqlite3.connect(lib["db"])
        assert dk.execute("SELECT count(*) FROM Albums WHERE relativePath LIKE '/Uploads/%'").fetchone()[0] >= 1, "the folder rows reached digiKam's database"
        dk.close()
    finally:  # leave the scratch library as the other tests know it: no extra photo, no Uploads folder
        import os
        assert client.post("/api/photos/delete", json={"ids": [a["id"] for a in added], "confirm": True}).status_code == 200
        dk = sqlite3.connect(lib["db"])
        rels = [r[0] for r in dk.execute("SELECT relativePath FROM Albums WHERE relativePath = '/Uploads' OR relativePath LIKE '/Uploads/%'")]
        dk.execute("DELETE FROM Albums WHERE relativePath = '/Uploads' OR relativePath LIKE '/Uploads/%'"); dk.commit(); dk.close()
        for rel in sorted(rels, key=len, reverse=True):
            d = os.path.join(lib["root"], rel.lstrip("/"))
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)


def test_one_request_import_and_the_already_there_check(client, lib, tmp_path):
    """POST /api/import stages and imports in one request (the apps' background uploads), duplicates left out;
    POST /api/import/check answers which files (size and SHA-256) the library already has."""
    import hashlib
    src, src_id = _src(lib)
    new = _dated(lib, tmp_path)
    sha = lambda b: hashlib.sha256(b).hexdigest()
    have = client.post("/api/import/check", json={"files": [{"size": len(src.read_bytes()), "sha256": sha(src.read_bytes())},
                                                              {"size": len(new.read_bytes()), "sha256": sha(new.read_bytes())}]})
    assert have.status_code == 200 and have.json()["present"] == [{"sha256": sha(src.read_bytes()), "id": src_id}], have.text
    assert client.post("/api/import/check", json={"files": [{"size": 1, "sha256": "nope"}]}).status_code == 400
    try:
        r = client.post("/api/import", data={"pattern": "{yyyy}/{yyyy}{mm}"},
                        files=[("files", ("same.jpg", src.read_bytes(), "image/jpeg")), ("files", ("trip.jpg", new.read_bytes(), "image/jpeg"))])
        assert r.status_code == 200, r.text
        res = r.json()
        assert [a["folder"] for a in res["added"]] == ["/1999/199912"] and res["skipped"][0]["duplicate_of"] == src_id
        assert Path(res["added"][0]["path"]).read_bytes() == new.read_bytes()
        again = client.post("/api/import/check", json={"files": [{"size": len(new.read_bytes()), "sha256": sha(new.read_bytes())}]}).json()["present"]
        assert again == [{"sha256": sha(new.read_bytes()), "id": res["added"][0]["id"]}], "the new file counts once imported"
    finally:
        _cleanup(client, lib, ["/1999"])
