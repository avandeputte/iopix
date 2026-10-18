"""Lossless rotate, flip and crop.

Rotate and flip
  JPEG, with jpegtran at hand: the file's pixels are transformed in the compressed domain (nothing is
  decoded or re-encoded, so nothing is lost), first to upright when an orientation flag was set, then as
  asked; the orientation flag becomes normal. Edge blocks that cannot be transformed exactly are kept
  rather than trimmed (jpegtran without -perfect), as digiKam's own lossless rotation does.
  Anything else (PNG, TIFF, WebP, HEIC, RAW..., or no jpegtran): the orientation flag changes, in digiKam's
  database, in the sidecar and, when the file is one the viewer writes into, in the file, under the pixel
  guard. The picture itself is never re-encoded.
Crop
  JPEG: jpegtran -crop after the pixels were made upright. The crop's origin snaps to the JPEG block grid
  (8 or 16 pixels), so the result can be a few pixels larger than asked; the right and bottom edges are kept.
  PNG, TIFF and WebP: Pillow (PNG and TIFF without loss, WebP at quality 95), metadata copied back from the
  original. HEIC/HEIF (Apple's format): decoded and re-encoded at quality 92, as Apple Photos does on export,
  since HEVC cannot be cut without re-encoding; the change log says "re-encoded". RAW and the rest are refused.
Every edit: the true original is kept once in file-backups and a copy of the file as it was just before
this edit is kept beside it (Undo puts it back); digiKam's rows change (ImageInformation width, height,
orientation; Images fileSize, with modificationDate cleared so digiKam fully rescans the file next time,
recomputing its hash and thumbnails); face regions turn, flip or shift with the picture (ImageTagProperties
and the sidecar's mwg-rs and MP regions), the sidecar's orientation follows, the viewer's cached thumbnails
of the photo are dropped, and the change log has an entry with Undo.
"""
import logging
import os
import shutil
import subprocess
import time

from PIL import Image, ImageOps

from app import config, thumbs
from app.digikam import dkdb
from app.photos import edits
from app.faces import facemgr
from app.data import index
from app.digikam.dbsync import SyncError

log = logging.getLogger("app.photos.lossless")

OPS = {"right": "Rotated right", "left": "Rotated left", "180": "Turned upside down", "fliph": "Flipped horizontally", "flipv": "Flipped vertically"}
_JT_OP = {"right": ["-rotate", "90"], "left": ["-rotate", "270"], "180": ["-rotate", "180"], "fliph": ["-flip", "horizontal"], "flipv": ["-flip", "vertical"]}
# the jpegtran transform that makes a file with this Exif orientation upright
_JT_UPRIGHT = {2: ["-flip", "horizontal"], 3: ["-rotate", "180"], 4: ["-flip", "vertical"], 5: ["-transpose"], 6: ["-rotate", "90"], 7: ["-transverse"], 8: ["-rotate", "270"]}
_T = Image.Transpose
_PIL_UPRIGHT = {2: _T.FLIP_LEFT_RIGHT, 3: _T.ROTATE_180, 4: _T.FLIP_TOP_BOTTOM, 5: _T.TRANSPOSE, 6: _T.ROTATE_270, 7: _T.TRANSVERSE, 8: _T.ROTATE_90}
_PIL_OP = {"right": _T.ROTATE_270, "left": _T.ROTATE_90, "180": _T.ROTATE_180, "fliph": _T.FLIP_LEFT_RIGHT, "flipv": _T.FLIP_TOP_BOTTOM}
JPEG_EXT = {".jpg", ".jpeg", ".jpe"}
PIL_CROP_EXT = {".png", ".tif", ".tiff", ".webp"}
HEIF_EXT = {".heic", ".heif"}
LOSSY_EXT = {".webp", *HEIF_EXT}  # re-encoded on crop
MIN_CROP = 16


def jpegtran():
    """The jpegtran binary (libjpeg-turbo), or None: then JPEGs are turned by their orientation flag only."""
    for c in (shutil.which("jpegtran"), "/opt/local/bin/jpegtran", "/opt/homebrew/bin/jpegtran", "/usr/local/bin/jpegtran", "/usr/bin/jpegtran"):
        if c and os.access(c, os.X_OK):
            return c
    return None


def available() -> bool:
    return edits.available() and config.DB_WRITE


def _heif() -> bool:
    import importlib.util
    return importlib.util.find_spec("pillow_heif") is not None


def capabilities() -> dict:
    return {"jpegtran": bool(jpegtran()), "crop": sorted(JPEG_EXT if jpegtran() else set()) + sorted(PIL_CROP_EXT) + (sorted(HEIF_EXT) if _heif() else []),
            "reencoded": sorted(LOSSY_EXT)}


# ---- geometry
def compose(orientation: int, op: str) -> int:
    """The Exif orientation that shows a file (stored with `orientation`) the way `op` turns its upright view."""
    base = Image.new("L", (5, 3))
    base.putdata(list(range(15)))
    o = orientation if orientation in _PIL_UPRIGHT else 1
    up = base.transpose(_PIL_UPRIGHT[o]) if o != 1 else base
    want = up.transpose(_PIL_OP[op])
    want = (want.size, want.tobytes())
    for code in range(1, 9):
        cand = base.transpose(_PIL_UPRIGHT[code]) if code != 1 else base
        if (cand.size, cand.tobytes()) == want:
            return code
    raise ValueError("orientation")


def turn_rect(r, op: str, W: int, H: int):
    """An upright rectangle after the upright picture (W x H) is turned or flipped by op."""
    x, y, w, h = r
    if op == "right":
        return (H - y - h, x, h, w)
    if op == "left":
        return (y, W - x - w, h, w)
    if op == "180":
        return (W - x - w, H - y - h, w, h)
    if op == "fliph":
        return (W - x - w, y, w, h)
    if op == "flipv":
        return (x, H - y - h, w, h)
    raise ValueError(op)


def crop_rect(r, box):
    """An upright rectangle after the crop to box (x, y, w, h): shifted and trimmed; None when (nearly) nothing is left."""
    x, y, w, h = r
    cx, cy, cw, ch = box
    nx, ny = max(0, x - cx), max(0, y - cy)
    nw, nh = min(x + w - cx, cw) - nx, min(y + h - cy, ch) - ny
    if nw < 8 or nh < 8:
        return None
    return (nx, ny, nw, nh)


def upright_size(w: int, h: int, orientation: int):
    return (h, w) if orientation in (5, 6, 7, 8) else (w, h)


# ---- the photo in digiKam's database
def _photo(pid: int) -> dict:
    con = dkdb.connect()
    try:
        row = con.execute(
            "SELECT i.id, i.name, i.category, i.fileSize, a.albumRoot, a.relativePath, ii.width, ii.height, ii.orientation "
            "FROM Images i JOIN Albums a ON a.id = i.album LEFT JOIN ImageInformation ii ON ii.imageid = i.id WHERE i.id = ? AND i.status = 1", (int(pid),)).fetchone()
        if not row:
            raise SyncError("That photo is not in the library")
        roots = dkdb.root_paths(con)
        if row[4] not in roots:
            raise SyncError("The album root of that photo is not mounted here")
        path = roots[row[4]] + row[5].rstrip("/") + "/" + row[1]
        regions = [(r[0], r[1], r[2]) for r in con.execute(
            "SELECT tagid, property, value FROM ImageTagProperties WHERE imageid = ? AND value LIKE '%<rect%'", (int(pid),))]
    finally:
        con.close()
    return {"id": row[0], "name": row[1], "category": row[2], "size": row[3], "path": path, "w": row[6] or 0, "h": row[7] or 0,
            "orientation": row[8] or 1, "ext": os.path.splitext(row[1])[1].lower(), "regions": regions}


def _check(p: dict):
    if p["category"] != 1:
        raise SyncError("Only photos can be edited this way")
    if not os.path.isfile(p["path"]):
        raise FileNotFoundError(f"The file is not there: {p['path']}")
    if not os.access(p["path"], os.W_OK):
        raise SyncError("The file is read-only")
    if not p["w"] or not p["h"]:
        with Image.open(p["path"]) as im:
            p["w"], p["h"] = im.size


def _copy_before(path: str) -> str:
    """The file as it is now, kept for Undo (and the true original, kept once)."""
    edits.backup_original(path)
    base = edits.file_backup_path(path)
    dst = base.with_name(base.name + f".before-{int(time.time())}-{os.getpid() % 1000}")
    size = os.stat(path).st_size
    if shutil.disk_usage(str(config.DATA_DIR)).free < size + edits.MIN_FREE_BYTES:
        raise OSError(f"Not enough free disk space to keep a copy of {os.path.basename(path)} before editing it")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)
    return str(dst)


def _run(cmd: list, what: str):
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise SyncError(f"{what} failed: {(res.stderr or res.stdout).strip()[:300]}")


def _jt(jt: str, args: list, src: str, dst: str):
    _run([jt, *args, "-copy", "all", "-outfile", dst, src], "jpegtran")


def _tmp(path: str, n: int) -> str:
    return os.path.join(os.path.dirname(path), f".iopix-{n}-{os.path.basename(path)}")


def _file_orientation(path: str, orientation: int, w: int = None, h: int = None, only_existing: bool = False):
    """The orientation (and, after a pixel change, the Exif size, without the stale embedded thumbnail) in the file."""
    args = [*edits.exiftool_cmd(), "-q", "-m", "-overwrite_original", "-charset", "filename=utf8"]
    if only_existing:
        args.append("-wm")
        args.append("w")
    args.append(f"-Orientation#={orientation}")
    if w:
        args += ["-IFD1:all=", f"-ExifImageWidth={w}", f"-ExifImageHeight={h}"]
    res = subprocess.run([*args, path], capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        log.warning("orientation not written into %s: %s", path, (res.stderr or "").strip()[:200])
    # an XMP orientation already in the file must not contradict the Exif one
    subprocess.run([*edits.exiftool_cmd(), "-q", "-m", "-overwrite_original", "-wm", "w", "-charset", "filename=utf8", f"-XMP-tiff:Orientation#={orientation}", path],
                   capture_output=True, text=True, timeout=300)


def _sidecar(path: str, orientation: int):
    side = edits._prepare(path)
    res = subprocess.run([*edits.exiftool_cmd(), "-q", "-m", "-overwrite_original", "-charset", "filename=utf8", f"-XMP-tiff:Orientation#={orientation}", side],
                         capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        log.warning("orientation not written into %s: %s", side, (res.stderr or "").strip()[:200])


def _new_values(regions: list, fn) -> tuple:
    """(kept [(tagid, property, old, new)], dropped [(tagid, property, old)]) with every rectangle in the values mapped by fn."""
    kept, dropped = [], []
    for tagid, prop, val in regions:
        gone = []

        def sub(m):
            r = fn(tuple(map(int, m.groups())))
            if r is None:
                gone.append(1)
                return m.group(0)
            return f'x="{r[0]}" y="{r[1]}" width="{r[2]}" height="{r[3]}"'
        new = index.RECT_RE.sub(sub, val)
        if gone:
            dropped.append((tagid, prop, val))
        elif new != val:
            kept.append((tagid, prop, val, new))
    return kept, dropped


def _write_db(pid: int, w: int, h: int, orientation: int, size: int, kept: list, dropped: list, check_running: bool, restore_regions: list = None):
    from app.digikam import dbsync
    db_path = dkdb.sqlite_path()
    dkdb.backup(db_path)
    with dbsync.write_transaction(db_path, check_running) as con:
        con.execute("UPDATE ImageInformation SET width = ?, height = ?, orientation = ? WHERE imageid = ?", (w, h, orientation, pid))
        con.execute("UPDATE Images SET fileSize = ?, modificationDate = NULL WHERE id = ?", (size, pid))
        if restore_regions is not None:
            con.execute("DELETE FROM ImageTagProperties WHERE imageid = ? AND value LIKE '%<rect%'", (pid,))
            for tagid, prop, val in restore_regions:
                con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?, ?, ?, ?)", (pid, tagid, prop, val))
        for tagid, prop, old, new in kept:
            con.execute("UPDATE ImageTagProperties SET value = ? WHERE imageid = ? AND tagid = ? AND property = ? AND value = ?", (new, pid, tagid, prop, old))
        for tagid, prop, old in dropped:
            con.execute("DELETE FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = ? AND value = ?", (pid, tagid, prop, old))


def _forget_checksum(pid: int):
    try:
        from app.data import vdb
        con = vdb.connect()
        try:
            con.execute("DELETE FROM image_data_checksums WHERE photo_id = ?", (int(pid),))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _finish(p: dict, orientation: int, summary: str, before: dict, copy: str, w: int, h: int, action_detail: dict):
    from app.photos import history
    _sidecar(p["path"], orientation)
    facemgr.write_regions_many([p["id"]])
    thumbs.forget(p["id"])
    _forget_checksum(p["id"])
    history.record([{"photo_id": p["id"], "action": "lossless", "summary": summary, "path": p["path"], "file_copy": copy,
                     "details": action_detail, "undo": {"lossless": {**before, "file_copy": copy}}}])
    return {"id": p["id"], "w": w, "h": h, "orientation": orientation, "upright": list(upright_size(w, h, orientation))}


# ---- rotate and flip
def transform_one(pid: int, op: str, check_running: bool = True) -> dict:
    from app.digikam import dbsync
    if op not in OPS:
        raise SyncError("Unknown transform")
    p = _photo(pid)
    _check(p)
    dbsync.preflight(dkdb.sqlite_path(), check_running, "the photo")
    W, H = upright_size(p["w"], p["h"], p["orientation"])
    before = {"width": p["w"], "height": p["h"], "orientation": p["orientation"], "fileSize": p["size"], "regions": p["regions"]}
    copy = _copy_before(p["path"])
    kept, dropped = _new_values(p["regions"], lambda r: turn_rect(r, op, W, H))
    jt = jpegtran()
    if p["ext"] in JPEG_EXT and jt:
        src, t1, t2 = p["path"], _tmp(p["path"], 1), _tmp(p["path"], 2)
        try:
            if p["orientation"] in _JT_UPRIGHT:
                _jt(jt, _JT_UPRIGHT[p["orientation"]], src, t1)
                src = t1
            _jt(jt, _JT_OP[op], src, t2)
            os.replace(t2, p["path"])
        finally:
            for t in (t1, t2):
                if os.path.exists(t):
                    os.unlink(t)
        with Image.open(p["path"]) as im:
            w, h = im.size
        orientation = 1
        _file_orientation(p["path"], 1, w, h)
        how = "pixels transformed losslessly"
    else:
        w, h = p["w"], p["h"]
        orientation = compose(p["orientation"], op)
        if edits.can_write_file(p["path"]):
            from app.photos.pixelguard import Guard
            with Guard([p["path"]]) as g:
                _file_orientation(p["path"], orientation)
                if g.verify():
                    raise SyncError("The orientation could not be written into the file without changing its image data; nothing was changed")
        how = "orientation flag"
    size = os.stat(p["path"]).st_size
    _write_db(p["id"], w, h, orientation, size, kept, dropped, check_running)
    return _finish(p, orientation, f"{OPS[op]} ({how})", before, copy, w, h, {"lossless": op, "how": how})


def transform(ids: list, op: str, check_running: bool = True) -> dict:
    done, failed = [], []
    for pid in ids:
        try:
            done.append(transform_one(int(pid), op, check_running))
        except (SyncError, OSError, ValueError, subprocess.SubprocessError) as e:
            failed.append({"id": pid, "error": str(e)})
    return {"done": done, "failed": failed}


# ---- crop
def crop(pid: int, rect: list, check_running: bool = True) -> dict:
    from app.digikam import dbsync
    p = _photo(pid)
    _check(p)
    W, H = upright_size(p["w"], p["h"], p["orientation"])
    try:
        x, y, w, h = [int(round(float(v))) for v in rect]
    except (TypeError, ValueError):
        raise SyncError("The crop rectangle is not valid")
    x, y = max(0, x), max(0, y)
    w, h = min(w, W - x), min(h, H - y)
    if w < MIN_CROP or h < MIN_CROP:
        raise SyncError(f"The crop must keep at least {MIN_CROP} x {MIN_CROP} pixels")
    if (x, y, w, h) == (0, 0, W, H):
        raise SyncError("That crop keeps the whole photo")
    jt = jpegtran()
    if p["ext"] in JPEG_EXT and not jt:
        raise SyncError("Cropping a JPEG without loss needs jpegtran (libjpeg-turbo) on the server")
    if p["ext"] in HEIF_EXT and not _heif():
        raise SyncError("Cropping HEIC needs the pillow-heif package on the server")
    if p["ext"] not in JPEG_EXT and p["ext"] not in PIL_CROP_EXT and p["ext"] not in HEIF_EXT:
        raise SyncError("Cropping here works for JPEG, PNG, TIFF, WebP and HEIC files")
    dbsync.preflight(dkdb.sqlite_path(), check_running, "the photo")
    before = {"width": p["w"], "height": p["h"], "orientation": p["orientation"], "fileSize": p["size"], "regions": p["regions"]}
    copy = _copy_before(p["path"])
    if p["ext"] in JPEG_EXT:
        src, t1, t2 = p["path"], _tmp(p["path"], 1), _tmp(p["path"], 2)
        try:
            if p["orientation"] in _JT_UPRIGHT:
                _jt(jt, _JT_UPRIGHT[p["orientation"]], src, t1)
                src = t1
            _jt(jt, ["-crop", f"{w}x{h}+{x}+{y}"], src, t2)
            os.replace(t2, p["path"])
        finally:
            for t in (t1, t2):
                if os.path.exists(t):
                    os.unlink(t)
        with Image.open(p["path"]) as im:
            nw, nh = im.size
        # jpegtran keeps the right and bottom edges and moves the origin down to the block grid
        box = (x + w - nw, y + h - nh, nw, nh)
        _file_orientation(p["path"], 1, nw, nh)
    else:
        t2 = _tmp(p["path"], 2)
        try:
            with Image.open(p["path"]) as im:
                fmt = im.format
                up = ImageOps.exif_transpose(im) if p["orientation"] != 1 else im
                out = up.crop((x, y, x + w, y + h))
                kw = {"icc_profile": im.info.get("icc_profile")} if im.info.get("icc_profile") else {}
                if fmt == "WEBP":
                    kw["quality"] = 95
                if p["ext"] in HEIF_EXT:
                    fmt, kw = "HEIF", {"quality": 92}  # HEVC: decoded and encoded again, as Apple Photos does on export
                    out.info["icc_profile"] = im.info.get("icc_profile")
                out.save(t2, format=fmt, **kw)
            os.replace(t2, p["path"])
        finally:
            if os.path.exists(t2):
                os.unlink(t2)
        nw, nh = w, h
        box = (x, y, w, h)
        # Pillow keeps no metadata: it comes back from the copy, with the new orientation and size
        subprocess.run([*edits.exiftool_cmd(), "-q", "-m", "-overwrite_original", "-charset", "filename=utf8", "-tagsfromfile", copy, "-all:all", "-xmp:all",
                        "-Orientation#=1", "-IFD1:all=", f"-ExifImageWidth={nw}", f"-ExifImageHeight={nh}", p["path"]], capture_output=True, text=True, timeout=300)
    kept, dropped = _new_values(p["regions"], lambda r: crop_rect(r, box))
    size = os.stat(p["path"]).st_size
    _write_db(p["id"], nw, nh, 1, size, kept, dropped, check_running)
    how = "lossless" if p["ext"] in JPEG_EXT or p["ext"] in (".png", ".tif", ".tiff") else "re-encoded"
    r = _finish(p, 1, f"Cropped to {nw} x {nh} ({how})", before, copy, nw, nh, {"lossless": "crop", "box": list(box), "faces_dropped": len(dropped), "how": how})
    r["box"] = list(box)
    r["faces_dropped"] = len(dropped)
    r["how"] = how
    return r


# ---- undo
def undo(entry: dict, check_running: bool = True) -> dict:
    """The file, digiKam's rows and the regions as they were before this edit."""
    from app.digikam import dbsync
    from app.photos import history
    u = (entry.get("undo") or {}).get("lossless")
    if not u:
        raise ValueError("This change has nothing to undo")
    copy = entry.get("file_copy") if entry.get("file_copy") and os.path.isfile(entry["file_copy"]) else u.get("file_copy")
    if not copy or not os.path.isfile(copy):
        raise FileNotFoundError("The copy of the file from before this edit is gone")
    p = _photo(entry["photo"])
    _check(p)
    dbsync.preflight(dkdb.sqlite_path(), check_running, "the photo")
    shutil.copy2(copy, p["path"])
    size = os.stat(p["path"]).st_size
    regions = [tuple(r) for r in u.get("regions") or []]
    _write_db(p["id"], u["width"], u["height"], u["orientation"], size, [], [], check_running, restore_regions=regions)
    _sidecar(p["path"], u["orientation"])
    facemgr.write_regions_many([p["id"]])
    thumbs.forget(p["id"])
    _forget_checksum(p["id"])
    history.record([{"photo_id": p["id"], "action": "restore", "summary": f"Change #{entry['id']} undone: the file is as it was before", "path": p["path"]}])
    return {"undone": 1, "id": p["id"]}
