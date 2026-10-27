"""Damaged files: a check of every photo's file, and the repair of the one kind of damage that can be undone safely.

What is checked (only reading the files):
  JPEG   the marker structure is walked from the start of the file to its end-of-image marker (FFD9); a file whose
         data stops before it is damaged. jpegtran (libjpeg) then decodes the file with an end marker added: if
         that decodes without a "premature end" warning, all of the picture's data is there and only the marker is
         missing (some phones' burst frames end this way), which is repairable; otherwise the file was cut off and
         the bottom of the picture is lost, which nothing can bring back.
  PNG    every chunk's checksum (Pillow's verify).
  other  the picture is decoded (Pillow); a file that cannot be is reported with the decoder's message.
Videos are not checked. Each result is kept in ai_scans (job filecheck) under the file's stamp, so a later run
looks only at new and changed files; the detail of a damaged one is a small JSON note.

The repair adds the missing end marker to the file. The file is copied first (lossless._copy_before: the true
original once, plus a copy from just before this change), the change is in the edit log, and Undo puts the copy
back. digiKam's row gets the new file size with its modification date cleared, so digiKam (and iopix's own
scanner) read the file again, as after any edit of the file.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

from app import jobs, thumbs
from app.data import vdb

log = logging.getLogger("app.photos.filecheck")
JOB = "filecheck"
JPEG_EXT = (".jpg", ".jpeg", ".jpe", ".jfif")
progress = {"running": False, "done": 0, "total": 0, "found": 0, "started": None, "finished": None}
_run = {"gen": 0}
_lock = threading.Lock()
LABELS = {"end_marker": "The end-of-image marker is missing; the picture itself is complete",
          "truncated": "The file stops early: the bottom of the picture is missing",
          "empty": "The file is empty",
          "not_jpeg": "Named as a JPEG but is not one, and could not be read",
          "unreadable": "The picture cannot be decoded",
          "png_crc": "Damaged data inside the PNG"}


def jpegtran():
    from app.photos import lossless
    return lossless.jpegtran()


def _jpeg_ends(data: bytes):
    return _walk(data)[0]


def _walk(data: bytes):
    """(status, offset of the end-of-image marker or None). Walk a JPEG's markers. Returns 'ok' when an end-of-image marker closes the image data (anything after it is a
    trailer some cameras add), 'no_eoi' when the data runs to the end of the file without one, 'bad' when it is not a
    JPEG stream at all."""
    n = len(data)
    if n < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return "bad", None
    i = 2
    while i < n:
        if data[i] != 0xFF:  # garbage between segments: libjpeg skips it too, looking for the next marker
            i += 1
            continue
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            return "no_eoi", None
        m = data[i]
        i += 1
        if m == 0xD9:
            return "ok", i - 2
        if m in (0x01,) or 0xD0 <= m <= 0xD7:  # markers without a length
            continue
        if i + 2 > n:
            return "no_eoi", None
        seg = (data[i] << 8) | data[i + 1]
        if seg < 2:
            return "bad", None
        i += seg
        if m == 0xDA:  # start of scan: entropy-coded data up to the next real marker
            while True:
                j = data.find(b"\xff", i)
                if j < 0 or j + 1 >= n:
                    return "no_eoi", None
                nxt = data[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                    i = j + 1 if nxt == 0xFF else j + 2
                    continue
                i = j
                break
    return "no_eoi", None


def _complete_with_marker(path: str):
    """True when the JPEG's picture data is all there (it decodes cleanly once an end marker is added), False when
    it is cut off, None when jpegtran is not installed to tell."""
    jt = jpegtran()
    if not jt:
        return None
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "x.jpg")
        shutil.copyfile(path, tmp)
        with open(tmp, "ab") as f:
            f.write(b"\xff\xd9")
        res = subprocess.run([jt, "-copy", "none", "-outfile", os.devnull, tmp], capture_output=True, text=True, timeout=120)
    msg = (res.stderr or "") + (res.stdout or "")
    return res.returncode == 0 and "Premature end" not in msg and "Corrupt" not in msg


def check_file(path: str):
    """None when the file looks sound, else {problem, detail, fixable}."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None  # gone or unreadable right now: the scanner deals with missing files
    if size == 0:
        return {"problem": "empty", "detail": LABELS["empty"], "fixable": False}
    ext = os.path.splitext(path)[1].lower()
    from PIL import Image
    try:
        if ext in JPEG_EXT:
            with open(path, "rb") as f:
                data = f.read()
            ends = _jpeg_ends(data)
            if ends == "no_eoi":
                whole = _complete_with_marker(path)
                if whole:
                    return {"problem": "end_marker", "detail": LABELS["end_marker"], "fixable": True}
                if whole is None:
                    return {"problem": "end_marker", "detail": LABELS["end_marker"] + " (install jpegtran to check whether the picture is complete and repair it)", "fixable": False}
                return {"problem": "truncated", "detail": LABELS["truncated"], "fixable": False}
            if ends == "bad":
                try:  # a PNG or HEIC with a .jpg name is not damage
                    with Image.open(path) as im:
                        im.load()
                    return None
                except Exception:
                    return {"problem": "not_jpeg", "detail": LABELS["not_jpeg"], "fixable": False}
            return None
        if ext == ".png":
            with Image.open(path) as im:
                im.verify()
            return None
        with Image.open(path) as im:
            im.draft("RGB", (max(1, im.width // 8), max(1, im.height // 8)))
            im.load()
        return None
    except MemoryError:
        return None
    except Exception as e:
        if ext == ".png":
            return {"problem": "png_crc", "detail": f"{LABELS['png_crc']} ({str(e)[:120]})", "fixable": False}
        if type(e).__name__ == "UnidentifiedImageError" and ext not in (".png", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif") + JPEG_EXT:
            return None  # a raw file or another format Pillow does not know: nothing to say about it
        return {"problem": "unreadable", "detail": f"{LABELS['unreadable']} ({str(e)[:120]})", "fixable": False}


# --------------------------------------------------------------------------
# the library run
# --------------------------------------------------------------------------

def _stamp(p: dict) -> str:
    return thumbs.stamp_for(p["hash"], p["size"], p["modified"])


def _photos() -> list:
    from app import core
    con = core.db()
    try:
        return [dict(r) for r in con.execute(f"SELECT {core.THUMB_COLS} FROM photos WHERE video = 0 ORDER BY ts DESC")]
    finally:
        con.close()


def _done() -> dict:
    con = vdb.connect()
    try:
        return {r[0]: (r[1], r[2]) for r in con.execute("SELECT photo_id, stamp, detail FROM ai_scans WHERE job = ?", (JOB,))}
    finally:
        con.close()


def _job(gen: int, photos: list):
    have = _done()
    todo = [p for p in photos if have.get(p["id"], (None,))[0] != _stamp(p)]
    progress.update(running=True, done=0, total=len(todo), found=0, started=int(time.time()), finished=None)
    try:
        for i in range(0, len(todo), 50):
            if gen != _run["gen"]:
                return
            rows = []
            for p in todo[i:i + 50]:
                if not os.path.isfile(p["path"]):  # not reachable now (an unmounted disk): looked at again next time
                    continue
                r = check_file(p["path"])
                if r:
                    progress["found"] += 1
                rows.append((p["id"], _stamp(p), json.dumps(r) if r else None))
            con = vdb.connect()
            try:
                con.executemany(f"INSERT OR REPLACE INTO ai_scans VALUES (?,'{JOB}',?,?)", rows)
                con.commit()
            finally:
                con.close()
            progress["done"] += len(todo[i:i + 50])
    finally:
        progress.update(running=False, finished=int(time.time()))


def start(redo: bool = False) -> int:
    """Check the files not checked yet (or changed since); with redo every file again. Returns how many are queued."""
    if redo:
        con = vdb.connect()
        try:
            con.execute("DELETE FROM ai_scans WHERE job = ?", (JOB,))
            con.commit()
        finally:
            con.close()
    photos = _photos()
    have = _done()
    n = sum(1 for p in photos if have.get(p["id"], (None,))[0] != _stamp(p))
    _run["gen"] += 1
    gen = _run["gen"]
    jobs.submit(JOB, lambda: _job(gen, photos))
    return n


def report() -> dict:
    """{checked, total, found: [{photo, name, path, problem, detail, fixable}], progress}: the damage found so far,
    for the photos still in the library and unchanged since they were checked."""
    photos = {p["id"]: p for p in _photos()}
    have = _done()
    found = []
    checked = 0
    for pid, (st, detail) in have.items():
        p = photos.get(pid)
        if not p or st != _stamp(p):
            continue
        checked += 1
        if detail:
            d = json.loads(detail)
            found.append({"photo": pid, "name": os.path.basename(p["path"]), "folder": os.path.dirname(p["path"]), **d})
    found.sort(key=lambda x: (not x["fixable"], x["folder"], x["name"]))
    return {"checked": checked, "total": len(photos), "found": found, "progress": dict(progress), "jpegtran": bool(jpegtran())}


# --------------------------------------------------------------------------
# the repair: an end-of-image marker added, with a copy kept for Undo
# --------------------------------------------------------------------------

def repair(ids: list, check_running: bool = True) -> dict:
    """Add the missing end marker to these photos' JPEGs (only where the check found the picture complete).
    {repaired: [ids], failed: [{id, error}]}."""
    from app.digikam import dbsync, dkdb
    from app.photos import history, lossless
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the photos")  # digiKam's rows change too: refused while it runs, before any file is touched
    done, failed = [], []
    for pid in ids:
        try:
            p = lossless._photo(int(pid))
            lossless._check(p)
            r = check_file(p["path"])
            if not r or r["problem"] != "end_marker" or not r["fixable"]:
                raise ValueError("This file does not have a repairable missing end marker (checked again just now)")
            copy = lossless._copy_before(p["path"])
            with open(p["path"], "ab") as f:
                f.write(b"\xff\xd9")
            if check_file(p["path"]):  # the repair made it no better: the copy goes back
                shutil.copy2(copy, p["path"])
                raise ValueError("Adding the end marker did not make the file sound; it was left as it was")
            size = os.path.getsize(p["path"])
            with dbsync.write_transaction(db_path, check_running) as con:
                con.execute("UPDATE Images SET fileSize = ?, modificationDate = NULL WHERE id = ?", (size, p["id"]))
            lossless._forget_checksum(p["id"])
            thumbs.forget(p["id"])
            history.record([{"photo_id": p["id"], "action": "filerepair", "summary": "Missing JPEG end marker added to the file",
                             "path": p["path"], "file_copy": copy, "undo": {"filerepair": {"file_copy": copy, "fileSize": p["size"]}}}])
            done.append(p["id"])
        except Exception as e:
            failed.append({"id": pid, "error": str(e)[:300]})
    return {"repaired": done, "failed": failed}


def undo(entry: dict, check_running: bool = True) -> dict:
    """Put the file back as it was before the repair."""
    from app.digikam import dbsync, dkdb
    from app.photos import history, lossless
    u = (entry.get("undo") or {}).get("filerepair") or {}
    copy = entry.get("file_copy") if entry.get("file_copy") and os.path.isfile(entry["file_copy"]) else u.get("file_copy")
    if not copy or not os.path.isfile(copy):
        raise FileNotFoundError("The copy of the file from before the repair is gone")
    p = lossless._photo(entry["photo"])
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the photo")
    shutil.copy2(copy, p["path"])
    with dbsync.write_transaction(db_path, check_running) as con:
        con.execute("UPDATE Images SET fileSize = ?, modificationDate = NULL WHERE id = ?", (os.path.getsize(p["path"]), p["id"]))
    lossless._forget_checksum(p["id"])
    thumbs.forget(p["id"])
    history.record([{"photo_id": p["id"], "action": "restore", "summary": f"Change #{entry['id']} undone: the file is as it was before the repair", "path": p["path"]}])
    return {"undone": 1, "id": p["id"]}
