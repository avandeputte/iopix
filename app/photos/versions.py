"""Adjusted copies saved as versions, the way digiKam's image editor saves "as a new version".

adjust(photo, params) renders the photo (upright) with the adjustments, saves it as <name>_v<n>.jpg in the same folder
(JPEG at quality 95, its colour profile kept), copies the original's metadata into it (exiftool) and its sidecar beside
it, and registers it in digiKam's database with the original's attributes (rating, labels, keywords, captions,
position, copyright; face regions too unless the picture was straightened). The two are linked as digiKam links
versions: an ImageRelations row (the version DerivedFrom the original) and digiKam's internal version tags
(Original / Intermediate / Current Version). The original file and its rows are not changed.

params: exposure (EV, -2..2), contrast and saturation (-100..100 %), warmth (-100..100), angle (degrees, -15..15,
cropped to the largest upright rectangle), autolevel (bool).
"""
import math
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

from app.digikam import dbsync, dkdb
from app.photos import edits, folders, history, scanner
from app.data import index
from app import thumbs
from app.digikam.dbsync import SyncError
from app.faces import facemgr

INTERNAL = "_Digikam_Internal_Tags_"
DERIVED_FROM = 1  # DatabaseRelation::DerivedFrom
HEIC = thumbs.pillow_heif is not None


def _clamp(v, lo, hi):
    try:
        return max(lo, min(hi, float(v or 0)))
    except (TypeError, ValueError):
        return 0.0


def clean(params: dict) -> dict:
    return {"exposure": _clamp(params.get("exposure"), -2, 2), "contrast": _clamp(params.get("contrast"), -100, 100),
            "saturation": _clamp(params.get("saturation"), -100, 100), "warmth": _clamp(params.get("warmth"), -100, 100),
            "angle": _clamp(params.get("angle"), -15, 15), "autolevel": bool(params.get("autolevel"))}


def _inner(w, h, angle):
    """The largest axis-aligned rectangle inside a w x h picture turned by angle degrees (centered)."""
    a = math.radians(abs(angle))
    if a == 0:
        return w, h
    sin, cos = math.sin(a), math.cos(a)
    long_, short = max(w, h), min(w, h)
    if short <= 2 * sin * cos * long_ or abs(sin - cos) < 1e-10:
        x = 0.5 * short
        wr, hr = (x / sin, x / cos) if w >= h else (x / cos, x / sin)
    else:
        cos2 = cos * cos - sin * sin
        wr, hr = (w * cos - h * sin) / cos2, (h * cos - w * sin) / cos2
    return int(wr), int(hr)


def render(path: str, p: dict) -> Image.Image:
    with Image.open(path) as im:
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im).convert("RGB")
    if p["autolevel"]:
        im = ImageOps.autocontrast(im, cutoff=0.5, preserve_tone=True)
    if p["exposure"]:
        im = ImageEnhance.Brightness(im).enhance(2 ** p["exposure"])
    if p["contrast"]:
        im = ImageEnhance.Contrast(im).enhance(1 + p["contrast"] / 100)
    if p["saturation"]:
        im = ImageEnhance.Color(im).enhance(1 + p["saturation"] / 100)
    if p["warmth"]:
        k = p["warmth"] / 400  # +100: reds up 25 %, blues down 25 %
        r, g, b = im.split()
        r = r.point(lambda v: min(255, int(v * (1 + k))))
        b = b.point(lambda v: max(0, min(255, int(v * (1 - k)))))
        im = Image.merge("RGB", (r, g, b))
    if p["angle"]:
        w, h = im.size
        im = im.rotate(-p["angle"], resample=Image.Resampling.BICUBIC, expand=False)
        cw, ch = _inner(w, h, p["angle"])
        left, top = (w - cw) // 2, (h - ch) // 2
        im = im.crop((left, top, left + cw, top + ch))
    im.info["icc_profile"] = icc
    return im


def _version_name(directory: str, name: str) -> str:
    base = os.path.splitext(name)[0]
    n = 1
    while os.path.exists(os.path.join(directory, f"{base}_v{n}.jpg")):
        n += 1
    return f"{base}_v{n}.jpg"


def _internal_tag(con, name: str) -> int:
    root = con.execute("SELECT id FROM Tags WHERE name = ? AND pid = 0", (INTERNAL,)).fetchone()
    pid = root[0] if root else con.execute("INSERT INTO Tags (pid, name) VALUES (0, ?)", (INTERNAL,)).lastrowid
    r = con.execute("SELECT id FROM Tags WHERE name = ? AND pid = ?", (name, pid)).fetchone()
    if r:
        return r[0]
    tid = con.execute("INSERT INTO Tags (pid, name) VALUES (?, ?)", (pid, name)).lastrowid
    con.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?, 'internalTag', NULL)", (tid,))
    return tid


def adjust(pid: int, params: dict, check_running: bool = True) -> dict:
    p = clean(params)
    if not any(p[k] for k in ("exposure", "contrast", "saturation", "warmth", "angle")) and not p["autolevel"]:
        raise SyncError("Nothing to adjust")
    con = index.open_index()
    try:
        row = con.execute("SELECT id, path, name, album_id, video FROM photos WHERE id = ?", (int(pid),)).fetchone()
    finally:
        con.close()
    if not row:
        raise SyncError("That photo is not in the library")
    if row[4]:
        raise SyncError("Videos cannot be adjusted here")
    src, name, album = row[1], row[2], row[3]
    if os.path.splitext(name)[1].lower() in (".heic", ".heif") and not HEIC:
        raise SyncError("Reading HEIC needs the pillow-heif package on the server")
    dbsync.preflight(dkdb.sqlite_path(), check_running, "the photo")
    directory = os.path.dirname(src)
    new_name = _version_name(directory, name)
    dst = os.path.join(directory, new_name)
    im = render(src, p)
    tmp = os.path.join(directory, ".iopix-" + new_name)
    try:
        im.save(tmp, "JPEG", quality=95, icc_profile=im.info.get("icc_profile"), optimize=True)
        subprocess.run([*edits.exiftool_cmd(), "-q", "-m", "-overwrite_original", "-charset", "filename=utf8", "-tagsfromfile", src, "-all:all", "-xmp:all",
                        "-unsafe", "-Orientation#=1", "-IFD1:all=", "-ThumbnailImage=", "-PreviewImage=", f"-ExifImageWidth={im.size[0]}", f"-ExifImageHeight={im.size[1]}",
                        "-XMP-mwg-rs:RegionInfo=", "-XMP-MP:RegionInfoMP=", tmp], capture_output=True, text=True, timeout=300)  # regions: rewritten below
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # the sidecar: the original's (what digiKam's database says), upright, regions dropped if straightened
    if os.path.exists(edits.sidecar_path(src)):
        shutil.copy2(edits.sidecar_path(src), edits.sidecar_path(dst))
        subprocess.run([*edits.exiftool_cmd(), "-q", "-m", "-overwrite_original", "-XMP-tiff:Orientation#=1",
                        *(["-XMP-mwg-rs:RegionInfo=", "-XMP-MP:RegionInfoMP="] if p["angle"] else []), edits.sidecar_path(dst)], capture_output=True, text=True, timeout=120)
    w, h = im.size
    version = scanner.hash_version()
    # one transaction registers the file, copies the original's attributes and links the two as versions: if any of
    # it is refused, nothing is in the database and the new file goes again
    db_path = dkdb.sqlite_path()
    dkdb.backup(db_path)
    try:
        with dbsync.write_transaction(db_path, check_running) as con:
            vid = folders.register(album, [folders.staged_entry(Path(dst))], check_running, con=con)[0]["id"]
            scanner.copy_attributes(con, int(pid), vid)
            con.execute("UPDATE ImageInformation SET width = ?, height = ?, orientation = 1, format = 'JPG', colorDepth = 8, colorModel = 5 WHERE imageid = ?", (w, h, vid))
            if p["angle"]:
                con.execute("DELETE FROM ImageTagProperties WHERE imageid = ?", (vid,))
            st = dkdb.file_stamp(dst)
            con.execute("UPDATE Images SET uniqueHash = ?, fileSize = ?, modificationDate = ? WHERE id = ?", (scanner.unique_hash(dst, version), st[1], st[0], vid))
            if not dkdb.standalone():  # the viewer's own library keeps no version links or version tags
                con.execute("INSERT INTO ImageRelations (subject, object, type) VALUES (?, ?, ?)", (vid, int(pid), DERIVED_FROM))
                current, original, inter = (_internal_tag(con, n) for n in ("Current Version", "Original Version", "Intermediate Version"))
                derived = con.execute("SELECT 1 FROM ImageRelations WHERE subject = ? AND type = ?", (int(pid), DERIVED_FROM)).fetchone()
                con.execute("DELETE FROM ImageTags WHERE imageid = ? AND tagid IN (?, ?, ?)", (vid, current, original, inter))
                con.execute("DELETE FROM ImageTags WHERE imageid = ? AND tagid = ?", (int(pid), current))
                con.execute("INSERT OR IGNORE INTO ImageTags (imageid, tagid) VALUES (?, ?)", (int(pid), inter if derived else original))
                con.execute("INSERT OR IGNORE INTO ImageTags (imageid, tagid) VALUES (?, ?)", (vid, current))
    except BaseException:
        for f in (dst, edits.sidecar_path(dst)):
            try:
                os.unlink(f)
            except OSError:
                pass
        raise
    # the original's edits not yet in digiKam's database are the version's too (its sidecar is a copy of the
    # original's); then its sidecar regions and tag list are written from its own rows: upright, at its own size
    e = edits.pending().get(int(pid))
    if e:
        edits.save_pending([(vid, e)])
    facemgr.write_regions_many([vid])
    what = ", ".join(k for k in ("exposure", "contrast", "saturation", "warmth", "angle") if p[k]) + (", auto levels" if p["autolevel"] else "")
    history.record([{"photo_id": vid, "action": "added", "path": dst, "summary": f"New version of {name} ({what.strip(', ')})", "details": {"from": int(pid), **p}},
                    {"photo_id": int(pid), "action": "edit", "path": src, "summary": f"Version saved as {new_name}; this file is unchanged", "details": {"version": vid}}])
    return {"id": vid, "name": new_name, "from": int(pid), "w": w, "h": h}


def versions_of(pid: int) -> list:
    """[{id, role}] of the photo's version family (digiKam's ImageRelations, DerivedFrom), the photo included."""
    try:
        con = dkdb.connect()
    except Exception:
        return []
    try:
        seen, todo, out = set(), [int(pid)], []
        while todo:
            x = todo.pop()
            if x in seen:
                continue
            seen.add(x)
            for (o,) in con.execute("SELECT object FROM ImageRelations WHERE subject = ? AND type = ?", (x, DERIVED_FROM)):
                todo.append(o)
            for (s,) in con.execute("SELECT subject FROM ImageRelations WHERE object = ? AND type = ?", (x, DERIVED_FROM)):
                todo.append(s)
        if len(seen) == 1:
            return []
        q = ",".join(map(str, seen))
        status = dict(con.execute(f"SELECT id, status FROM Images WHERE id IN ({q})").fetchall())
        parents = {s for (s,) in con.execute(f"SELECT subject FROM ImageRelations WHERE subject IN ({q}) AND type = ?", (DERIVED_FROM,))}
        for x in sorted(seen):
            if status.get(x) == 1:
                out.append({"id": x, "role": "version" if x in parents else "original"})
        return out
    except Exception:
        return []
    finally:
        con.close()
