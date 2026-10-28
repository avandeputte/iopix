"""digiKam folders: creating one, and putting new files into one.

A digiKam folder ("album" in its own vocabulary) is a directory under an album root plus a
row in Albums. digiKam's own New Album (AlbumManager::createPAlbum) makes the directory and
calls addAlbum(albumRoot, relativePath, caption, date, collection); this does the same, in a
guarded transaction with a backup first, like every other write to digiKam's database here.

An upload copies the file into the folder and adds the Images row with modificationDate
NULL, plus the basic ImageInformation exiftool can read (date, size, orientation, format) so
the viewer shows the photo at once. A null modificationDate is digiKam's own signal for "do a
full scan of this file" (CollectionScanner::scanFileNormal), so on its next run digiKam
reads the metadata and fills every table exactly as it does for its own imports, keeping
the id. Nothing else is guessed on digiKam's behalf.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from app.digikam import dbsync, dkdb
from app.photos import edits
from app.digikam.dbsync import SyncError

IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "tif", "tiff", "webp", "heic", "heif", "avif", "bmp", "jp2", "psd",
             "dng", "cr2", "cr3", "nef", "nrw", "arw", "raf", "orf", "rw2", "pef", "srw", "x3f"}
VIDEO_EXT = {"mov", "mp4", "m4v", "avi", "mkv", "webm", "3gp", "mts", "m2ts", "mpg", "mpeg", "wmv"}
FORMATS = {"jpg": "JPG", "jpeg": "JPG", "png": "PNG", "gif": "GIF", "tif": "TIFF", "tiff": "TIFF", "webp": "WEBP",
           "heic": "HEIF", "heif": "HEIF", "avif": "AVIF", "bmp": "BMP", "jp2": "JP2K", "psd": "PSD"}
MAX_UPLOAD = 2 * 1024 ** 3


def _album(con, album_id: int) -> dict:
    r = con.execute("SELECT id, albumRoot, relativePath FROM Albums WHERE id = ?", (album_id,)).fetchone()
    if not r:
        raise SyncError("That folder no longer exists in digiKam.")
    roots = dkdb.root_paths(con)
    if r[1] not in roots:
        raise SyncError("The album root of that folder is not mounted here (Settings › Album roots).")
    rel = r[2]
    return {"id": r[0], "root": r[1], "rel": rel, "dir": Path(roots[r[1]]) / rel.strip("/")}


def clean_name(name: str, what: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or name.startswith(".") or len(name) > 200 \
            or any(ord(c) < 32 for c in name):
        raise SyncError(f"That is not a usable {what} name.")
    return name


def list_all() -> list:
    """Every folder digiKam knows, empty ones included (the index drops those), as a tree."""
    with dkdb.reader() as con:
        roots = {r[0]: r[1] for r in con.execute("SELECT id, label FROM AlbumRoots")}
        single = len(roots) == 1
        rows = con.execute("SELECT id, albumRoot, relativePath, caption, date, collection, icon FROM Albums ORDER BY albumRoot, relativePath").fetchall()
    by_path = {(r[1], r[2]): r[0] for r in rows}
    out = []
    for aid, root, rel, caption, adate, collection, icon in rows:
        if root not in roots:
            continue
        if rel == "/":
            pid, name = None, roots[root] or "Library"
        else:
            pid, name = by_path.get((root, rel.rsplit("/", 1)[0] or "/")), rel.rsplit("/", 1)[1]
        out.append({"id": aid, "pid": pid, "name": name, "root": root, "rel": rel,
                    "path": rel if single else f"/{roots[root]}{'' if rel == '/' else rel}",
                    "caption": caption or "", "date": str(adate)[:10] if adate else "", "category": collection or "", "icon": icon})
    return out


def set_properties(album_id: int, props: dict, check_running: bool = True) -> dict:
    """digiKam's Album Properties: caption, date (YYYY-MM-DD), category (Albums.collection) and cover (Albums.icon,
    an image id; None for automatic). Only the keys given change."""
    import re as _re
    with dkdb.reader() as con:
        _album(con, album_id)
        if props.get("icon") is not None and not con.execute("SELECT 1 FROM Images WHERE id = ? AND status = 1", (int(props["icon"]),)).fetchone():
            raise SyncError("That photo is not in the library")
    sets, vals = [], []
    if "caption" in props:
        sets.append("caption = ?"); vals.append((props["caption"] or "").strip()[:2000] or None)
    if "date" in props:
        d = (props["date"] or "").strip()
        if d and not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            raise SyncError("The date must look like 2026-10-17")
        sets.append("date = ?"); vals.append(d or None)
    if "category" in props:
        sets.append("collection = ?"); vals.append((props["category"] or "").strip()[:200] or None)
    if "icon" in props:
        sets.append("icon = ?"); vals.append(int(props["icon"]) if props["icon"] is not None else None)
    if not sets:
        return {"id": album_id}
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "folders")
    with dbsync.write_transaction(db_path, check_running) as con:
        con.execute(f"UPDATE Albums SET {', '.join(sets)} WHERE id = ?", (*vals, album_id))
    return {"id": album_id, **{k: props[k] for k in ("caption", "date", "category", "icon") if k in props}}


def create(parent_id: int, name: str, check_running: bool = True) -> dict:
    """mkdir under the parent folder and the Albums row digiKam's New Album would add."""
    name = clean_name(name, "folder")
    with dkdb.reader() as con:
        parent = _album(con, parent_id)
        rel = ("" if parent["rel"] == "/" else parent["rel"]) + "/" + name
        if con.execute("SELECT 1 FROM Albums WHERE albumRoot = ? AND relativePath = ?", (parent["root"], rel)).fetchone():
            raise SyncError("A folder with that name already exists there.")
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "folders")
    target = parent["dir"] / name
    try:
        target.mkdir(exist_ok=True)  # the directory may already be there (made by hand); digiKam just did not know it yet
    except OSError as e:
        raise SyncError(f"Could not create the folder on disk: {e}")
    dkdb.backup(db_path)
    with dbsync.write_transaction(db_path, check_running) as con:
        cur = con.execute("INSERT INTO Albums (albumRoot, relativePath, date, caption, collection) VALUES (?, ?, ?, NULL, NULL)",
                          (parent["root"], rel, time.strftime("%Y-%m-%d")))
        new_id = cur.lastrowid
    return {"id": new_id, "path": rel, "dir": str(target)}


def category(ext: str) -> int:
    if ext in IMAGE_EXT:
        return 1
    if ext in VIDEO_EXT:
        return 2
    raise SyncError(f"Files of type .{ext} are not accepted (photos and videos only).")


def free_name(d: Path, name: str) -> str:
    if not (d / name).exists():
        return name
    stem, ext = os.path.splitext(name)
    for i in range(2, 1000):
        cand = f"{stem} ({i}){ext}"
        if not (d / cand).exists():
            return cand
    raise SyncError("Too many files with that name already.")


def probe(path: Path) -> dict:
    """What exiftool can tell about a new file: creation date, orientation, size, duration."""
    try:
        # -d formats the dates (it is a print conversion, so no global -n); Orientation# stays numeric
        res = subprocess.run([*edits.exiftool_cmd(), "-j", "-m", "-q", "-d", "%Y-%m-%dT%H:%M:%S",
                              "-DateTimeOriginal", "-Keys:CreationDate", "-CreateDate", "-MediaCreateDate", "-Orientation#", "-ImageWidth", "-ImageHeight",
                              "-ExifImageWidth", "-ExifImageHeight", "--", str(path)], capture_output=True, text=True, timeout=60)
        d = json.loads(res.stdout or "[{}]")[0]
    except (OSError, ValueError, subprocess.SubprocessError):
        d = {}
    when = next((d[k] for k in ("DateTimeOriginal", "CreationDate", "CreateDate", "MediaCreateDate") if d.get(k) and str(d[k])[:4].isdigit()), None)
    if not when:
        when = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))
    return {"when": str(when)[:19] + ".000", "orientation": int(d.get("Orientation") or 1),
            "w": int(d.get("ImageWidth") or d.get("ExifImageWidth") or 0), "h": int(d.get("ImageHeight") or d.get("ExifImageHeight") or 0)}


def staged_entry(path: Path) -> tuple:
    """(name, category, extension, path, probe info) for a file already in place."""
    name = path.name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return (name, category(ext), ext, path, probe(path))


def album_for_dir(directory: Path):
    """The digiKam folder (Albums id) for a directory on disk, or None."""
    with dkdb.reader() as con:
        roots = dkdb.root_paths(con)
        d = str(directory).rstrip("/")
        for rid, rpath in roots.items():
            rp = str(rpath).rstrip("/")
            if d == rp or d.startswith(rp + "/"):
                rel = "/" + d[len(rp):].strip("/") if d != rp else "/"
                row = con.execute("SELECT id FROM Albums WHERE albumRoot = ? AND relativePath = ?", (rid, rel)).fetchone()
                if row:
                    return row[0]
        return None


def upload(album_id: int, files: list, check_running: bool = True) -> dict:
    """files: [(name, bytes or an open binary file)]. Each goes into the folder (a taken name gets ' (2)'), then one
    transaction adds their rows; an open file is copied in chunks. Returns {added: [{id, name}], skipped: [{name, reason}]}."""
    with dkdb.reader() as con:
        folder = _album(con, album_id)
    if not folder["dir"].is_dir():
        raise SyncError(f"The folder is not on disk here: {folder['dir']}")
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the photo list")
    staged, skipped = [], []
    for name, data in files:
        try:
            name = clean_name(os.path.basename(name or ""), "file")
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            cat = category(ext)
            if isinstance(data, (bytes, bytearray)):
                size = len(data)
            else:
                data.seek(0, os.SEEK_END); size = data.tell(); data.seek(0)
            if size > MAX_UPLOAD or not size:
                raise SyncError("Empty file, or larger than 2 GB.")
            final = free_name(folder["dir"], name)
            tmp = folder["dir"] / (".upload-" + final)
            if isinstance(data, (bytes, bytearray)):
                tmp.write_bytes(data)
            else:
                with open(tmp, "wb") as out:
                    shutil.copyfileobj(data, out, 1024 * 1024)
            tmp.replace(folder["dir"] / final)
            path = folder["dir"] / final
            from app.photos import importer
            importer.make_sidecar(str(path))  # the file's own metadata in a sidecar, as digiKam would write one
            staged.append((final, cat, ext, path, probe(path)))  # exiftool runs here, not inside the write lock below
        except (SyncError, OSError) as e:
            skipped.append({"name": name, "reason": str(e)})
    if not staged:
        return {"added": [], "skipped": skipped}
    added = register(album_id, staged, check_running)
    from app.photos import scanner
    scanner.complete([a["id"] for a in added], check_running)  # camera, place, keywords, faces, digiKam's hash, as digiKam's scan would
    from app.photos import importer
    importer.sidecars_from_db([a["id"] for a in added])
    from app.photos import history
    history.record([{"photo_id": a["id"], "action": "added", "summary": f"Uploaded into {folder.get('rel') or folder['dir']} (sidecar created)", "path": str(folder["dir"] / a["name"])} for a in added])
    return {"added": added, "skipped": skipped}


def register(album_id: int, staged: list, check_running: bool = True, con=None) -> list:
    """Files already in the folder on disk -> their Images and ImageInformation rows, as an upload makes them.
    staged: [(file name, category, extension, path, probe info)]. With `con`, the rows go into the caller's own
    write transaction (which took the backup) instead of one of their own."""
    if con is not None:
        return register_rows(con, album_id, staged)
    db_path = dkdb.sqlite_path()
    dkdb.backup(db_path)
    with dbsync.write_transaction(db_path, check_running) as con:
        return register_rows(con, album_id, staged)


def register_rows(con, album_id: int, staged: list) -> list:
    added = []
    for final, cat, ext, path, info in staged:
        cur = con.execute("INSERT INTO Images (album, name, status, category, modificationDate, fileSize, uniqueHash) "
                          "VALUES (?, ?, 1, ?, NULL, ?, NULL)", (album_id, final, cat, path.stat().st_size))
        iid = cur.lastrowid
        fmt = FORMATS.get(ext, ("RAW-" + ext.upper()) if ext in IMAGE_EXT else ext.upper())
        con.execute("INSERT INTO ImageInformation (imageid, rating, creationDate, digitizationDate, orientation, width, height, format, colorDepth, colorModel) "
                    "VALUES (?, -1, ?, ?, ?, ?, ?, ?, 8, 1)", (iid, info["when"], info["when"], info["orientation"], info["w"], info["h"], fmt))
        added.append({"id": iid, "name": final})
    return added
