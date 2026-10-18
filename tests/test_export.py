"""Export: resized and converted copies with their metadata, the location removed on request, files named by
date; and renaming library files by date with undo."""
import io
import sqlite3
import subprocess
import zipfile
from pathlib import Path

from PIL import Image

from app.photos import edits, export


def _ids_with_gps(client, lib, n=3):
    return [i for i in lib["with_gps"] if i not in lib["videos"]][:n]


def _zip(client, ids, **opts):
    q = {"ids": ",".join(map(str, ids)), "name": "t", **{k: str(v) for k, v in opts.items()}}
    r = client.post("/api/zip", data=q)
    assert r.status_code == 200, r.text[:300]
    return zipfile.ZipFile(io.BytesIO(r.content))


def _meta(data: bytes, tmp_path, name="x.jpg"):
    import json
    p = tmp_path / name
    p.write_bytes(data)
    out = subprocess.run([*edits.exiftool_cmd(), "-j", "-n", "-GPSLatitude", "-Model", "-DateTimeOriginal", str(p)], capture_output=True, text=True).stdout
    return json.loads(out)[0]


def test_resized_jpeg_copies_keep_metadata_and_can_lose_the_location(client, lib, tmp_path):
    ids = _ids_with_gps(client, lib)
    z = _zip(client, ids, xsize=800, xfmt="jpeg", xname="date", flat=1)
    names = z.namelist()
    assert len(names) == len(ids) and all("/" not in n and n.endswith(".jpg") and n[:4].isdigit() and n[10] == "_" for n in names), names
    data = z.read(names[0])
    im = Image.open(io.BytesIO(data))
    assert max(im.size) <= 800 and im.format == "JPEG"
    m = _meta(data, tmp_path)
    assert m.get("GPSLatitude") and m.get("Model") and m.get("DateTimeOriginal"), ("location, camera and date come along", m)
    z = _zip(client, ids, xsize=800, xfmt="jpeg", nogps=1)
    data = z.read(z.namelist()[0])
    m = _meta(data, tmp_path)
    assert "GPSLatitude" not in m and m.get("Model"), ("no location left, the rest kept", m)
    # untouched originals when nothing is asked for: the same bytes as the library file
    z = _zip(client, ids[:1])
    con = sqlite3.connect(lib["db"])
    rel, name = con.execute("SELECT a.relativePath, i.name FROM Images i JOIN Albums a ON a.id = i.album WHERE i.id = ?", (ids[0],)).fetchone()
    con.close()
    assert z.read(z.namelist()[0]) == (Path(lib["root"]) / rel.strip("/") / name).read_bytes()


def test_names_are_unique_by_pattern():
    used = set()
    a = export.name_for("date", 1_700_000_000, "IMG_1.jpg", used)
    b = export.name_for("date", 1_700_000_000, "IMG_2.jpg", used)
    assert a == "2023-11-14_221320.jpg" and b == "2023-11-14_221320-2.jpg"
    assert export.name_for("date-name", 1_700_000_000, "IMG_3.HEIC", used, ".jpg") == "2023-11-14_IMG_3.jpg"
    assert export.name_for("date-seq", 1_700_000_000, "x.jpg", set()) == "2023-11-14_001.jpg"
    assert export.name_for("keep", 1_700_000_000, "x.jpg", set()) == "x.jpg"


def test_rename_by_date_keeps_ids_and_undo_puts_names_back(client, lib):
    ids = [i for i in lib["ids"] if i not in lib["videos"]][60:63]
    con = sqlite3.connect(lib["db"])
    before = {i: con.execute("SELECT name FROM Images WHERE id = ?", (i,)).fetchone()[0] for i in ids}
    con.close()
    r = client.post("/api/photos/rename-pattern", json={"ids": ids, "pattern": "date-name"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert not res["failed"] and len(res["renamed"]) + res["unchanged"] == len(ids)
    con = sqlite3.connect(lib["db"])
    for x in res["renamed"]:
        name = con.execute("SELECT name FROM Images WHERE id = ?", (x["id"],)).fetchone()[0]
        assert name == x["name"] and name[:4].isdigit() and name.endswith(before[x["id"]]) and x["was"] == before[x["id"]]
    con.close()
    for x in res["renamed"]:
        assert client.post("/api/photos/rename", json={"id": x["id"], "name": x["was"]}).status_code == 200
    con = sqlite3.connect(lib["db"])
    assert {i: con.execute("SELECT name FROM Images WHERE id = ?", (i,)).fetchone()[0] for i in ids} == before
    con.close()
    assert client.post("/api/photos/rename-pattern", json={"ids": ids, "pattern": "nonsense"}).status_code == 409
