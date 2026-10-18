"""Lossless rotate, flip and crop: JPEG pixels through jpegtran with the orientation reset, other files by their
orientation flag, face regions turning with the picture in digiKam's database and the sidecar, a copy kept so
Undo brings the file and the rows back exactly."""
import io
import os
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from app.data import index
from app.photos import lossless


def _face_jpeg(lib):
    """(photo id, path, (tagid, rect), (w, h, orientation)) of a JPEG with a confirmed face region."""
    con = sqlite3.connect(lib["db"])
    try:
        for pid in lib["with_faces"]:
            row = con.execute("SELECT i.name, a.relativePath, ii.width, ii.height, ii.orientation FROM Images i JOIN Albums a ON a.id = i.album "
                              "LEFT JOIN ImageInformation ii ON ii.imageid = i.id WHERE i.id = ? AND i.status = 1", (pid,)).fetchone()
            if not row or not row[0].lower().endswith((".jpg", ".jpeg")) or not row[2]:
                continue
            reg = con.execute("SELECT tagid, value FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion'", (pid,)).fetchone()
            if not reg:
                continue
            path = Path(lib["root"]) / row[1].strip("/") / row[0]
            if path.exists():
                m = index.RECT_RE.search(reg[1])
                return pid, path, (reg[0], tuple(map(int, m.groups()))), (row[2], row[3], row[4] or 1)
    finally:
        con.close()
    pytest.skip("no JPEG with a face region in the fixture")


def _info(db, pid):
    con = sqlite3.connect(db)
    try:
        ii = con.execute("SELECT width, height, orientation FROM ImageInformation WHERE imageid = ?", (pid,)).fetchone()
        im = con.execute("SELECT fileSize, modificationDate FROM Images WHERE id = ?", (pid,)).fetchone()
        regs = con.execute("SELECT tagid, value FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion'", (pid,)).fetchall()
        return ii, im, {t: tuple(map(int, index.RECT_RE.search(v).groups())) for t, v in regs}
    finally:
        con.close()


def _vid(pid):
    from app.data import vdb
    con = vdb.connect()
    try:
        r = con.execute("SELECT vid FROM photo_identity WHERE dk_id = ?", (pid,)).fetchone()
        return r[0] if r else None
    finally:
        con.close()


def test_geometry():
    assert lossless.compose(1, "right") == 6 and lossless.compose(6, "right") == 3 and lossless.compose(3, "right") == 8 and lossless.compose(8, "right") == 1
    assert lossless.compose(1, "left") == 8 and lossless.compose(6, "left") == 1 and lossless.compose(1, "fliph") == 2 and lossless.compose(1, "flipv") == 4
    assert lossless.compose(6, "fliph") == 5 or lossless.compose(6, "fliph") == 7  # a transposition either way
    r = (10, 20, 30, 40)
    once = lossless.turn_rect(r, "right", 100, 200)
    assert once == (200 - 20 - 40, 10, 40, 30)
    assert lossless.turn_rect(once, "right", 200, 100) == lossless.turn_rect(r, "180", 100, 200)
    assert lossless.turn_rect(lossless.turn_rect(r, "right", 100, 200), "left", 200, 100) == r
    assert lossless.crop_rect(r, (5, 5, 50, 60)) == (5, 15, 30, 40) and lossless.crop_rect(r, (0, 0, 15, 25)) is None
    assert lossless.crop_rect(r, (38, 58, 50, 50)) is None, "nothing worth keeping"


@pytest.mark.skipif(not lossless.jpegtran(), reason="jpegtran is not installed here")
def test_rotate_jpeg_pixels_turn_faces_turn_and_undo_puts_everything_back(client, lib):
    pid, path, (tag, rect), (w, h, o) = _face_jpeg(lib)
    original = path.read_bytes()
    W, H = lossless.upright_size(w, h, o)
    vid_before = _vid(pid)
    r = client.post("/api/photos/transform", json={"ids": [pid], "op": "right"})
    assert r.status_code == 200 and not r.json()["failed"], r.text
    d = r.json()["done"][0]
    assert d["orientation"] == 1 and d["upright"] == [H, W]
    with Image.open(path) as im:
        assert im.size == (H, W), "the pixels themselves were turned"
    ii, im_row, regs = _info(lib["db"], pid)
    assert ii == (H, W, 1) and im_row[1] is None and im_row[0] == path.stat().st_size, "digiKam's rows: new size, upright, a full rescan due"
    assert regs[tag] == lossless.turn_rect(rect, "right", W, H), "the face turned with the picture"
    xmp = Path(str(path) + ".xmp").read_text(errors="replace")
    assert "tiff:Orientation>1<" in xmp or 'tiff:Orientation="1"' in xmp
    assert "RegionInfo" in xmp or "Regions" in xmp, "the regions are in the sidecar too"
    assert _vid(pid) == vid_before, "the rewritten file keeps its viewer id (favorites and albums stay attached)"
    assert client.get(f"/thumb/{pid}").status_code == 200
    e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
    assert e["action"] == "lossless" and "undo" in e["restore"] and e["file_copy"] and os.path.isfile(e["file_copy"])
    assert "Rotated right" in e["summary"]
    r = client.post(f"/api/history/{e['id']}/restore", json={"kind": "undo"})
    assert r.status_code == 200, r.text
    assert path.read_bytes() == original, "the file is byte for byte what it was"
    ii, im_row, regs = _info(lib["db"], pid)
    assert ii == (w, h, o) and regs[tag] == rect and im_row[0] == len(original)


@pytest.mark.skipif(not lossless.jpegtran(), reason="jpegtran is not installed here")
def test_crop_jpeg_keeps_the_face_where_it_is_and_undo(client, lib):
    pid, path, (tag, rect), (w, h, o) = _face_jpeg(lib)
    original = path.read_bytes()
    W, H = lossless.upright_size(w, h, o)
    box = [17, 9, W - 40, H - 20]
    r = client.post("/api/photos/crop", json={"id": pid, "rect": box})
    assert r.status_code == 200, r.text
    d = r.json()
    with Image.open(path) as im:
        assert im.size == (d["w"], d["h"]) and d["w"] <= W and d["h"] <= H
    bx = d["box"]
    assert bx[0] <= 17 and bx[1] <= 9 and bx[0] + bx[2] == 17 + W - 40 and bx[1] + bx[3] == 9 + H - 20, "the origin snaps to the block grid, the far edges stay"
    ii, im_row, regs = _info(lib["db"], pid)
    assert ii == (d["w"], d["h"], 1)
    want = lossless.crop_rect(rect, tuple(bx))
    if want is None:
        assert tag not in regs and d["faces_dropped"] >= 1
    else:
        assert regs[tag] == want
    e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
    assert e["action"] == "lossless" and "Cropped" in e["summary"]
    assert client.post(f"/api/history/{e['id']}/restore", json={"kind": "undo"}).status_code == 200
    assert path.read_bytes() == original
    ii, im_row, regs = _info(lib["db"], pid)
    assert ii == (w, h, o) and regs[tag] == rect
    assert client.post("/api/photos/crop", json={"id": pid, "rect": [0, 0, W, H]}).status_code == 400, "the whole photo is no crop"


def test_other_formats_turn_by_their_orientation_flag(client, lib):
    pid0, path, _reg, _dims = _face_jpeg(lib)
    folders = client.get("/api/folders").json()["folders"]
    f = next(x for x in folders if x["rel"] != "/")
    buf = io.BytesIO()
    with Image.open(path) as im:
        im.convert("RGB").resize((120, 80)).save(buf, format="PNG")
    r = client.post(f"/api/folders/{f['id']}/upload", files=[("files", ("zz-turn.png", buf.getvalue(), "image/png"))])
    assert r.status_code == 200, r.text
    pid = r.json()["added"][0]["id"]
    png = Path(lib["root"]) / f["rel"].strip("/") / "zz-turn.png"
    try:
        r = client.post("/api/photos/transform", json={"ids": [pid], "op": "right"})
        assert r.status_code == 200 and not r.json()["failed"], r.text
        d = r.json()["done"][0]
        assert d["orientation"] == 6 and d["upright"] == [80, 120]
        with Image.open(png) as im:
            assert im.size == (120, 80), "the pixels are untouched"
        assert _info(lib["db"], pid)[0] == (120, 80, 6)
        r = client.post("/api/photos/transform", json={"ids": [pid], "op": "right"})
        assert r.json()["done"][0]["orientation"] == 3
        r = client.post("/api/photos/transform", json={"ids": [pid], "op": "flipv"})
        assert r.json()["done"][0]["orientation"] == 2, "upside down then flipped top to bottom is a mirror"
        xmp = Path(str(png) + ".xmp").read_text(errors="replace")
        assert "tiff:Orientation>2<" in xmp or 'tiff:Orientation="2"' in xmp
        # a video is left alone
        if lib["videos"]:
            r = client.post("/api/photos/transform", json={"ids": [lib["videos"][0]], "op": "right"})
            assert r.status_code == 200 and r.json()["failed"] and not r.json()["done"]
    finally:
        con = sqlite3.connect(lib["db"])
        con.execute("DELETE FROM ImageInformation WHERE imageid = ?", (pid,)); con.execute("DELETE FROM Images WHERE id = ?", (pid,)); con.commit(); con.close()
        from test_import import forget_identity
        forget_identity([pid])
        png.unlink(missing_ok=True); Path(str(png) + ".xmp").unlink(missing_ok=True)
        index.build()


def test_heic_crops_by_re_encoding(client, lib):
    pytest.importorskip("pillow_heif")
    _pid0, path, _reg, _dims = _face_jpeg(lib)
    folders = client.get("/api/folders").json()["folders"]
    f = next(x for x in folders if x["rel"] != "/")
    buf = io.BytesIO()
    with Image.open(path) as im:
        small = im.convert("RGB").resize((400, 300))
    small = Image.frombytes("RGB", small.size, small.tobytes())  # pixels only: the source's orientation flag must not travel along
    small.save(buf, format="HEIF", quality=92)
    r = client.post(f"/api/folders/{f['id']}/upload", files=[("files", ("zz-crop.heic", buf.getvalue(), "image/heic"))])
    assert r.status_code == 200, r.text
    pid = r.json()["added"][0]["id"]
    heic = Path(lib["root"]) / f["rel"].strip("/") / "zz-crop.heic"
    original = heic.read_bytes()
    try:
        assert ".heic" in client.get("/api/status").json()["lossless"]["crop"]
        r = client.post("/api/photos/crop", json={"id": pid, "rect": [20, 10, 200, 150]})
        assert r.status_code == 200, r.text
        d = r.json()
        assert (d["w"], d["h"], d["how"]) == (200, 150, "re-encoded")
        with Image.open(heic) as im:
            assert im.size == (200, 150) and im.format == "HEIF"
        assert _info(lib["db"], pid)[0] == (200, 150, 1)
        e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
        assert "re-encoded" in e["summary"] and "undo" in e["restore"]
        assert client.post(f"/api/history/{e['id']}/restore", json={"kind": "undo"}).status_code == 200
        assert heic.read_bytes() == original
        # rotation of a HEIC is by the flag, the file untouched
        r = client.post("/api/photos/transform", json={"ids": [pid], "op": "left"})
        assert r.json()["done"][0]["orientation"] == 8 and heic.read_bytes() == original
    finally:
        con = sqlite3.connect(lib["db"])
        con.execute("DELETE FROM ImageInformation WHERE imageid = ?", (pid,)); con.execute("DELETE FROM Images WHERE id = ?", (pid,)); con.commit(); con.close()
        from test_import import forget_identity
        forget_identity([pid])
        heic.unlink(missing_ok=True); Path(str(heic) + ".xmp").unlink(missing_ok=True)
        index.build()


def test_undoing_a_rotation_keeps_face_changes_made_since(client, lib):
    """A face added after a rotation turns back with the picture when the rotation is undone, instead of the boxes
    saved at rotation time coming back over it (which lost the new face and left its person without a box)."""
    import sqlite3
    pid, path, (tag, rect), (w, h, o) = _face_jpeg(lib)
    W, H = lossless.upright_size(w, h, o)
    r = client.post("/api/photos/transform", json={"ids": [pid], "op": "right"})
    assert r.status_code == 200 and not r.json()["failed"], r.text
    e = client.get(f"/api/history?photo={pid}&limit=1").json()["entries"][0]
    con = sqlite3.connect(lib["db"])
    person = con.execute("SELECT tagid FROM TagProperties WHERE property = 'person' AND value != '' AND tagid != ? LIMIT 1", (tag,)).fetchone()[0]
    con.close()
    new_rect = [10, 10, 60, 60]  # in the turned picture (H x W upright)
    assert client.post("/api/faces/op", json={"op": "add", "image": pid, "rect": new_rect, "tag": person}).status_code == 200
    r = client.post(f"/api/history/{e['id']}/restore", json={"kind": "undo"})
    assert r.status_code == 200, r.text
    _ii, _im, regs = _info(lib["db"], pid)
    assert regs[tag] == rect, "the face that was there turned back"
    assert regs[person] == lossless.turn_rect(tuple(new_rect), "left", H, W), "the face added since turned back too"
    con = sqlite3.connect(lib["db"])
    assert con.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (pid, person)).fetchone()
    con.close()
    client.post("/api/faces/op", json={"op": "remove", "image": pid, "rect": list(regs[person])})
