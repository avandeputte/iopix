"""Every metadata field of a photo, as exiftool sees it, with edits written where digiKam would keep them.

read(photo) -> {"file": [...], "sidecar": [...], "writesFiles": bool}, each field {group, tag, value, editable}.
write(photo, changes) changes: [{group, tag, value}] ("" removes the field).
    XMP groups go to the sidecar (created and backed up first, as every edit here) and, when "Write metadata into
    image files" is on, into the file as well; EXIF, GPS and IPTC fields live only in the file, so they need that
    setting (and a format the viewer writes into). File writes are backed up and checked by the pixel guard.
    Afterwards the photo's rows in digiKam's database are read again from the file and sidecar (reread), so the
    database says what the metadata says. Every change is in the change log, with the sidecar and the original
    file kept for restore.
reread(photo): digiKam's "Read Metadata From File": the database rows of the photo filled again from its file
    and sidecar (camera, dates, position, comments, copyright, keywords, labels, faces).
"""
import json
import os
import subprocess

from app.digikam import dkdb
from app.photos import edits
from app.data import index
from app.digikam.dbsync import SyncError

EDITABLE = ("IFD0", "ExifIFD", "GPS", "IPTC", "XMP-dc", "XMP-xmp", "XMP-photoshop", "XMP-iptcCore", "XMP-iptcExt", "XMP-exif",
            "XMP-exifEX", "XMP-tiff", "XMP-xmpRights", "XMP-digiKam", "XMP-lr", "XMP-aux", "XMP-acdsee", "XMP-MicrosoftPhoto")
FILE_ONLY = ("IFD0", "ExifIFD", "GPS", "IPTC")
NOT_EDITABLE = {"ExifByteOrder", "ExifVersion", "FlashpixVersion", "ComponentsConfiguration", "InteropIndex", "XMPToolkit",
                "ThumbnailOffset", "ThumbnailLength", "ExifImageWidth", "ExifImageHeight", "GPSVersionID", "ApplicationRecordVersion"}


def _photo(pid: int) -> dict:
    con = index.open_index()
    try:
        r = con.execute("SELECT id, path, video FROM photos WHERE id = ?", (int(pid),)).fetchone()
    finally:
        con.close()
    if not r:
        raise SyncError("That photo is not in the library")
    return {"id": r[0], "path": r[1], "video": r[2]}


def _dump(path: str) -> list:
    if not os.path.isfile(path):
        return []
    res = subprocess.run([*edits.exiftool_cmd(), "-j", "-G1", "-a", "-s", "-struct", "-charset", "filename=utf8", "--", path],
                         capture_output=True, text=True, timeout=120)
    try:
        d = json.loads(res.stdout or "[{}]")[0]
    except ValueError:
        return []
    out = []
    for key, v in d.items():
        if ":" not in key or key == "SourceFile":
            continue
        group, tag = key.split(":", 1)
        if group in ("System", "File", "ExifTool"):
            continue
        binary = isinstance(v, str) and v.startswith("(Binary data")
        if isinstance(v, list):
            v = "; ".join(json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else str(x) for x in v)
        elif isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)
        out.append({"group": group, "tag": tag, "value": str(v), "editable": group in EDITABLE and tag not in NOT_EDITABLE and not binary
                    and not str(v).startswith("{")})
    return out


def read(pid: int) -> dict:
    p = _photo(pid)
    return {"id": p["id"], "path": p["path"], "file": _dump(p["path"]), "sidecar": _dump(p["path"] + ".xmp"),
            "writesFiles": edits.writes_files() and edits.can_write_file(p["path"]), "hasSidecar": os.path.isfile(p["path"] + ".xmp")}


def _args(changes: list) -> list:
    a = ["-sep", "; "]
    for c in changes:
        a.append(f"-{c['group']}:{c['tag']}={c.get('value') or ''}")
    return a


def write(pid: int, changes: list, check_running: bool = True) -> dict:
    from app.photos import history, pixelguard
    p = _photo(pid)
    changes = [c for c in changes if c.get("group") in EDITABLE and c.get("tag") and c["tag"] not in NOT_EDITABLE
               and all(ch.isalnum() or ch in "-_" for ch in c["tag"])]
    if not changes:
        raise SyncError("Nothing to change (only EXIF, GPS, IPTC and XMP fields can be edited)")
    file_only = [c for c in changes if c["group"] in FILE_ONLY]
    in_file = edits.writes_files() and edits.can_write_file(p["path"])
    if file_only and not in_file:
        raise SyncError("EXIF, GPS and IPTC fields live in the image file: turn on Settings › Write metadata into image files "
                        "(and this must be a JPEG, PNG, TIFF or WebP)")
    before = {f"{x['group']}:{x['tag']}": x["value"] for x in _dump(p["path"]) + _dump(p["path"] + ".xmp")}
    xmp = [c for c in changes if c["group"].startswith("XMP-")]
    side = edits._prepare(p["path"])
    snap = history.snapshot(p["id"], side)
    jobs = []
    if xmp:
        jobs.append((side, _args(xmp)))
    if in_file:
        edits.ensure_backup(p["path"])
        jobs.append((p["path"], _args(changes)))
    with pixelguard.Guard([p["path"]] if in_file else []) as g:
        bad = edits.run_exiftool(jobs)
        undone = g.verify()
    if bad or undone:
        raise SyncError("exiftool could not write " + ("the image data would have changed; the file is as it was" if undone else ", ".join(os.path.basename(b) for b in bad)))
    reread(p["id"], check_running)
    history.record([{"photo_id": p["id"], "action": "edit", "path": p["path"], "sidecar": snap,
                     "summary": "Metadata edited: " + ", ".join(f"{c['group']}:{c['tag']}" for c in changes)[:400],
                     "details": {f"{c['group']}:{c['tag']}": {"from": before.get(f"{c['group']}:{c['tag']}"), "to": c.get("value") or None} for c in changes}}])
    return {"id": p["id"], "changed": len(changes), "file": in_file}


def reread(pid: int, check_running: bool = True) -> dict:
    """The photo's rows filled again from its file and sidecar, as digiKam's Read Metadata From File does."""
    from app.digikam import dbsync
    from app.photos import scanner
    p = _photo(pid)
    meta = scanner.read([p["path"]])
    con = dkdb.connect()
    try:
        cat = con.execute("SELECT category FROM Images WHERE id = ?", (p["id"],)).fetchone()
        version = scanner.hash_version(con)
    finally:
        con.close()
    rows = scanner.rows_for(p["path"], meta.get(p["path"]) or {}, cat[0] if cat else 1)
    with dbsync.write_transaction(dkdb.sqlite_path(), check_running) as con:
        tags = scanner._Tags(con)
        try:
            from app.faces.facemgr import _special
            special = _special(con)
        except SyncError:
            special = {}
        scanner._write_rows(con, p["id"], rows, tags, special, full=True)
        st = dkdb.file_stamp(p["path"])
        con.execute("UPDATE Images SET uniqueHash = ?, fileSize = ?, modificationDate = ? WHERE id = ?",
                    (scanner.unique_hash(p["path"], version), st[1] if st else None, st[0] if st else None, p["id"]))
    edits.forget([p["id"]])
    return {"id": p["id"]}
