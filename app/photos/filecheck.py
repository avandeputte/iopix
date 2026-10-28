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

The repair adds the missing end marker to the file. The file is copied first (lossless.copy_before: the true
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

from app import core, jobs, thumbs
from app.data import vdb
from app.digikam.dkdb import chunked
from app.photos import history, lossless
from app.digikam import dbsync, dkdb

log = logging.getLogger("app.photos.filecheck")
JOB = "filecheck"
JPEG_EXT = (".jpg", ".jpeg", ".jpe", ".jfif")
progress = {"running": False, "done": 0, "total": 0, "found": 0, "started": None, "finished": None}
_run = {"gen": 0}
_lock = threading.Lock()
LABELS = {"end_marker": "The end-of-image marker is missing; the picture itself is complete",
          "truncated": "The file stops early: the bottom of the picture is missing",
          "corrupt_data": "Damaged data inside the picture (it shows, with a glitch where the damage is); the file itself is complete",
          "empty": "The file is empty",
          "not_jpeg": "Named as a JPEG but is not one, and could not be read",
          "unreadable": "The picture cannot be decoded",
          "png_crc": "Damaged data inside the PNG"}


def jpegtran():
    return lossless.jpegtran()


def _jpeg_ends(data: bytes):
    return _walk(data)[0]


# markers that may follow a scan's data: tables, restart interval, another scan, end of image, application data,
# comments, frame headers (C0-CF but DHT C4, JPG C8 and DAC CC, which are tables); anything else inside a scan is a damaged byte
_AFTER_SCAN = {0xC4, 0xCC, 0xDA, 0xD9, 0xDB, 0xDC, 0xDD, 0xFE} | set(range(0xC0, 0xD0)) | set(range(0xE0, 0xF0))


def _walk(data: bytes):
    """(status, offset of the end-of-image marker or None). Walks a JPEG's markers:
      ok        an end-of-image marker closes the image data (anything after it is a trailer some cameras add)
      corrupt   damaged bytes inside the picture data (a stray marker), the file otherwise whole: it decodes with a glitch
      no_eoi    the data runs to the end of the file without an end marker: a missing marker or a file cut off
      bad       not a JPEG stream at all"""
    n = len(data)
    if n < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return "bad", None
    damaged = False
    i = 2
    while i < n:
        if data[i] != 0xFF:  # garbage between segments: libjpeg skips it too, looking for the next marker
            i += 1
            continue
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            break
        m = data[i]
        i += 1
        if m == 0xD9:
            return ("corrupt" if damaged else "ok"), i - 2
        if m in (0x01,) or 0xD0 <= m <= 0xD7:  # markers without a length
            continue
        if i + 2 > n:
            break
        seg = (data[i] << 8) | data[i + 1]
        if seg < 2:
            return "bad", None
        i += seg
        if m == 0xDA:  # start of scan: entropy-coded data up to the next real marker
            while True:
                j = data.find(b"\xff", i)
                if j < 0 or j + 1 >= n:
                    i = n
                    break
                nxt = data[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                    i = j + 1 if nxt == 0xFF else j + 2
                    continue
                if nxt not in _AFTER_SCAN:  # a damaged byte that looks like a marker: the data goes on after it
                    damaged = True
                    i = j + 2
                    continue
                i = j
                break
    # no end marker reached: a file that still ends with one has damage that broke the walk (a bad segment length),
    # not a missing end
    if data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        return "corrupt", None
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


def intact_rows(path: str):
    """For a JPEG whose bottom is missing (shown grey): (stored width, stored height, rows that decoded fully, rounded
    down to whole 16-row blocks), else None. The missing part decodes as flat mid-grey, 8 rows per pixel at 1/8 scale."""
    import numpy as np
    from PIL import Image
    try:
        with Image.open(path) as im:
            W, H = im.size
            im.draft("RGB", (max(1, W // 8), max(1, H // 8)))
            a = np.asarray(im.convert("RGB"), dtype=np.float32)
    except Exception:
        return None
    scale = H / a.shape[0]
    flat = (a.std(axis=(1, 2)) < 1.0) & (np.abs(a.mean(axis=(1, 2)) - 128) < 4)
    k = len(flat)
    while k > 0 and flat[k - 1]:
        k -= 1
    # the last row that decoded may stop part way along: grey at its right end
    while k > 0 and float(a[k - 1, -2:].std()) < 1.0 and abs(float(a[k - 1, -2:].mean()) - 128) < 4:
        k -= 1
    if k == len(flat) or k < len(flat) * 0.05:  # nothing missing, or nearly nothing left
        return None
    keep = int(k * scale) // 16 * 16
    return (W, H, keep) if keep >= 16 else None


def keep_rect(W: int, H: int, keep: int, orientation: int) -> list:
    """The upright rectangle (x, y, w, h) holding the stored picture's top `keep` rows, for lossless.crop. The origin is
    rounded up to a 16 px block so the crop (whose origin jpegtran snaps down to its blocks) keeps no grey."""
    up = lambda v: -(-v // 16) * 16  # noqa: E731
    if orientation in (3, 4):        # turned 180: the missing rows are at the top
        y = up(H - keep)
        return [0, y, W, H - y]
    if orientation in (6, 7):        # turned 90 clockwise: at the left
        x = up(H - keep)
        return [x, 0, H - x, W]
    if orientation in (5, 8):        # turned 90 anticlockwise: at the right
        return [0, 0, keep, W]
    return [0, 0, W, keep]


def keep_intact(ids: list, check_running: bool = True) -> dict:
    """Crop each photo losslessly to the part of the picture that is intact (lossless.crop: backed up first, in the
    edit log with undo). {cropped: [ids], failed: [{id, error}]}."""

    def one(con, pid, after):
        p = lossless.photo(int(pid))
        lossless.check(p)
        if os.path.splitext(p["path"])[1].lower() not in JPEG_EXT:
            raise ValueError("Only JPEGs can be cropped this way")
        r = check_file(p["path"])
        got = intact_rows(p["path"]) if r and r["problem"] in ("truncated", "corrupt_data") else None
        if not got:
            raise ValueError("No missing part at the bottom of this picture to crop away")
        W, H, keep = got
        lossless.crop(p["id"], keep_rect(W, H, keep, p["orientation"]), check_running, reencode_damaged=True, con=con, after=after)
    r = dbsync.batch([int(i) for i in ids], one, check_running, "the photos")
    return {"cropped": r["done"], "failed": r["failed"]}


def _with_keep(path: str, r: dict) -> dict:
    """A damaged JPEG whose bottom is missing gets keep: the share of the picture a crop to the intact part keeps."""
    got = intact_rows(path)
    if got:
        r["keep"] = round(got[2] / got[1], 3)
    return r


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
            if ends == "corrupt":  # never repairable here: adding an end marker would change nothing
                return _with_keep(path, {"problem": "corrupt_data", "detail": LABELS["corrupt_data"], "fixable": False})
            if ends == "no_eoi":
                whole = _complete_with_marker(path)
                if whole:
                    return {"problem": "end_marker", "detail": LABELS["end_marker"], "fixable": True}
                if whole is None:
                    return {"problem": "end_marker", "detail": LABELS["end_marker"] + " (install jpegtran to check whether the picture is complete and repair it)", "fixable": False}
                return _with_keep(path, {"problem": "truncated", "detail": LABELS["truncated"], "fixable": False})
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

CHECK_VERSION = 3  # 2: damaged data inside a scan told apart from a file cut off; 3: the intact share (keep) recorded


def _stamp(p: dict) -> str:
    return f"{thumbs.stamp_for(p['hash'], p['size'], p['modified'])}:v{CHECK_VERSION}"


def _photos() -> list:
    con = core.db()
    try:
        return [dict(r) for r in con.execute(f"SELECT {core.THUMB_COLS} FROM photos WHERE video = 0 ORDER BY ts DESC")]
    finally:
        con.close()


def _done() -> dict:
    with vdb.reader() as con:
        return {r[0]: (r[1], r[2]) for r in con.execute("SELECT photo_id, stamp, detail FROM ai_scans WHERE job = ?", (JOB,))}


def _job(gen: int, photos: list):
    have = _done()
    todo = [p for p in photos if have.get(p["id"], (None,))[0] != _stamp(p)]
    progress.update(running=True, done=0, total=len(todo), found=0, started=int(time.time()), finished=None)
    try:
        for todo_batch in chunked(todo, 50):
            if gen != _run["gen"]:
                return
            rows = []
            for p in todo_batch:
                if not os.path.isfile(p["path"]):  # not reachable now (an unmounted disk): looked at again next time
                    continue
                r = check_file(p["path"])
                if r:
                    progress["found"] += 1
                rows.append((p["id"], _stamp(p), json.dumps(r) if r else None))
            with vdb.writer() as con:
                con.executemany(f"INSERT OR REPLACE INTO ai_scans VALUES (?,'{JOB}',?,?)", rows)
            progress["done"] += len(todo_batch)
    finally:
        progress.update(running=False, finished=int(time.time()))


def start(redo: bool = False) -> int:
    """Check the files not checked yet (or changed since); with redo every file again. Returns how many are queued."""
    if redo:
        with vdb.writer() as con:
            con.execute("DELETE FROM ai_scans WHERE job = ?", (JOB,))
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

    def one(con, pid, after):  # digiKam's rows change too: the batch is refused while it runs, before any file is touched
        p = lossless.photo(int(pid))
        lossless.check(p)
        r = check_file(p["path"])
        if not r or r["problem"] != "end_marker" or not r["fixable"]:
            raise ValueError("This file does not have a repairable missing end marker (checked again just now)")
        copy = lossless.copy_before(p["path"])
        with open(p["path"], "ab") as f:
            f.write(b"\xff\xd9")
        if check_file(p["path"]):  # the repair made it no better: the copy goes back
            shutil.copy2(copy, p["path"])
            raise ValueError("Adding the end marker did not make the file sound; it was left as it was")
        size = os.path.getsize(p["path"])
        con.execute("UPDATE Images SET fileSize = ?, modificationDate = NULL WHERE id = ?", (size, p["id"]))
        lossless.forget_checksum(p["id"])
        thumbs.forget(p["id"])
        history.record([{"photo_id": p["id"], "action": "filerepair", "summary": "Missing JPEG end marker added to the file",
                         "path": p["path"], "file_copy": copy, "undo": {"filerepair": {"file_copy": copy, "fileSize": p["size"]}}}])
    r = dbsync.batch([int(i) for i in ids], one, check_running, "the photos")
    return {"repaired": r["done"], "failed": r["failed"]}


def undo(entry: dict, check_running: bool = True) -> dict:
    """Put the file back as it was before the repair."""
    u = (entry.get("undo") or {}).get("filerepair") or {}
    copy = entry.get("file_copy") if entry.get("file_copy") and os.path.isfile(entry["file_copy"]) else u.get("file_copy")
    if not copy or not os.path.isfile(copy):
        raise FileNotFoundError("The copy of the file from before the repair is gone")
    p = lossless.photo(entry["photo"])
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the photo")
    shutil.copy2(copy, p["path"])
    with dbsync.write_transaction(db_path, check_running) as con:
        con.execute("UPDATE Images SET fileSize = ?, modificationDate = NULL WHERE id = ?", (os.path.getsize(p["path"]), p["id"]))
    lossless.forget_checksum(p["id"])
    thumbs.forget(p["id"])
    history.record([{"photo_id": p["id"], "action": "restore", "summary": f"Change #{entry['id']} undone: the file is as it was before the repair", "path": p["path"]}])
    return {"undone": 1, "id": p["id"]}
