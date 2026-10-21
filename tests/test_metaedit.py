"""The metadata editor: every field listed, XMP edits in the sidecar, EXIF edits only with file writing on, the
database read again afterwards; and versions: an adjusted copy beside an untouched original, linked as digiKam links them."""
import os
import sqlite3
from pathlib import Path

from PIL import Image

from app.data import index


def _jpeg(lib):
    con = sqlite3.connect(lib["db"])
    try:
        for iid, rel, name in con.execute("SELECT i.id, a.relativePath, i.name FROM Images i JOIN Albums a ON a.id = i.album WHERE i.status = 1 AND i.category = 1 "
                                          "AND (i.name LIKE '%.jpg' OR i.name LIKE '%.JPG') ORDER BY i.id DESC"):
            p = Path(lib["root"]) / rel.strip("/") / name
            if p.exists():
                return iid, p
    finally:
        con.close()


def test_metadata_lists_fields_and_writes_xmp_to_the_sidecar(client, lib):
    pid, path = _jpeg(lib)
    m = client.get(f"/api/metadata/{pid}").json()
    groups = {f["group"] for f in m["file"]}
    assert "IFD0" in groups or "ExifIFD" in groups
    assert any(f["editable"] for f in m["file"]), "the file section has editable fields"
    r = client.post(f"/api/metadata/{pid}", json={"changes": [{"group": "XMP-dc", "tag": "Title", "value": "A title from the editor"},
                                                              {"group": "XMP-photoshop", "tag": "Headline", "value": "Editor headline"}]})
    assert r.status_code == 200, r.text
    side = Path(str(path) + ".xmp").read_text(errors="replace")
    assert "A title from the editor" in side and "Editor headline" in side
    con = sqlite3.connect(lib["db"])
    comments = dict(con.execute("SELECT type, comment FROM ImageComments WHERE imageid = ?", (pid,)).fetchall())
    con.close()
    assert comments.get(3) == "A title from the editor" and comments.get(2) == "Editor headline", "the database reads the metadata again"
    e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
    assert "Metadata edited" in e["summary"] and "sidecar" in e["restore"]
    # EXIF lives in the file: refused while file writing is off
    client.post("/api/settings", json={"write_files": False})
    r = client.post(f"/api/metadata/{pid}", json={"changes": [{"group": "IFD0", "tag": "Artist", "value": "Somebody"}]})
    assert r.status_code == 400 and "Write metadata into image files" in r.json()["detail"]
    assert client.post(f"/api/metadata/{pid}", json={"changes": [{"group": "File", "tag": "FileName", "value": "x"}]}).status_code == 400
    assert client.post(f"/api/metadata/{pid}/reread").status_code == 200


def test_adjust_saves_a_linked_version_and_leaves_the_original(client, lib):
    pid, path = _jpeg(lib)
    before = path.read_bytes()
    r = client.post("/api/photos/adjust", json={"id": pid, "exposure": 0.5, "contrast": 10, "warmth": 20, "angle": 3})
    assert r.status_code == 200, r.text
    v = r.json()
    new = path.parent / v["name"]
    try:
        assert path.read_bytes() == before, "the original is untouched"
        assert v["name"].endswith("_v1.jpg") and new.exists()
        with Image.open(new) as im:
            assert im.format == "JPEG" and im.size == (v["w"], v["h"])
        con = sqlite3.connect(lib["db"])
        assert con.execute("SELECT subject, object, type FROM ImageRelations WHERE subject = ?", (v["id"],)).fetchone() == (v["id"], pid, 1)
        tags = lambda i: {n for (n,) in con.execute("SELECT t.name FROM ImageTags it JOIN Tags t ON t.id = it.tagid WHERE it.imageid = ?", (i,))}
        assert "Current Version" in tags(v["id"]) and "Original Version" in tags(pid)
        kw = lambda i: {n for n in tags(i) if "Version" not in n}
        assert kw(v["id"]) >= {n for n in kw(pid)} - {"Unknown", "Unconfirmed"}
        assert con.execute("SELECT width, height, orientation FROM ImageInformation WHERE imageid = ?", (v["id"],)).fetchone() == (v["w"], v["h"], 1)
        con.close()
        d = client.get(f"/api/photo/{pid}").json()
        assert {x["id"] for x in d["versions"]} == {pid, v["id"]}
        assert client.post("/api/photos/adjust", json={"id": pid}).status_code == 400, "nothing to adjust"
    finally:
        con = sqlite3.connect(lib["db"])
        con.execute("DELETE FROM ImageRelations WHERE subject = ?", (v["id"],))
        for t in ("ImageInformation", "ImageMetadata", "ImagePositions", "ImageComments", "ImageCopyright", "ImageTags", "ImageTagProperties", "ImageProperties"):
            con.execute(f"DELETE FROM {t} WHERE imageid = ?", (v["id"],))
        con.execute("DELETE FROM Images WHERE id = ?", (v["id"],))
        con.execute("DELETE FROM ImageTags WHERE imageid = ? AND tagid IN (SELECT id FROM Tags WHERE name LIKE '% Version')", (pid,))
        con.commit(); con.close()
        from test_import import forget_identity
        forget_identity([v["id"]])
        new.unlink(missing_ok=True); Path(str(new) + ".xmp").unlink(missing_ok=True)
        index.build()
    assert os.path.exists(path)
