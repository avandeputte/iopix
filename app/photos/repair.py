"""Repairing a video whose wrapper is damaged while its picture and sound are intact: the same streams rewrapped,
without re-encoding, into a clean QuickTime/MP4 file.

The case it was written for: an iPhone Live Photo movie that a tool rewrote badly, leaving two movie headers, a
broken one at the start renamed "hoov" and the real "moov" at the end. Browsers and ffmpeg cannot play it. Each
candidate fix (the stray header blanked out, or the other one) is tried on a copy, remuxed with ffmpeg (-c copy) and
decoded in full; the first that plays cleanly replaces the file. The original is kept in file-backups first (the
change log's "Restore original file" puts it back), and digiKam's row is updated (size, fingerprint, duration).
"""
import os
import shutil
import subprocess
import tempfile

from app.digikam import dbsync, dkdb
from app.digikam.dbsync import SyncError
from app.photos import edits, history, scanner
from app.data import index
from app import thumbs


def _boxes(path: str) -> list:
    """[(type, offset, size)] of the file's top-level boxes."""
    out, pos, size = [], 0, os.path.getsize(path)
    with open(path, "rb") as f:
        while pos < size and len(out) < 64:
            f.seek(pos)
            hd = f.read(16)
            if len(hd) < 8:
                break
            n, kind = int.from_bytes(hd[:4], "big"), hd[4:8].decode("latin-1")
            if n == 1 and len(hd) >= 16:
                n = int.from_bytes(hd[8:16], "big")
            elif n == 0:
                n = size - pos
            if n < 8:
                break
            out.append((kind, pos, n))
            pos += n
    return out


def _decodes(ff: str, path: str) -> bool:
    res = subprocess.run([ff, "-v", "error", "-nostdin", "-i", path, "-f", "null", "-"], capture_output=True, text=True, timeout=600)
    return res.returncode == 0 and not res.stderr.strip()


def _blank(src: str, dst: str, offset: int):
    """A copy of src with the box at offset renamed "free" (its contents then ignored)."""
    shutil.copyfile(src, dst)
    with open(dst, "r+b") as f:
        f.seek(offset + 4)
        f.write(b"free")


def rewrap(path: str, work: str, ff: str):
    """(a clean copy of path in work, how it was made): the streams remuxed without re-encoding from the file as it
    is, or with one of its movie headers set aside, whichever first decodes in full. SyncError when none does."""
    heads = [(k, off) for k, off, _n in _boxes(path) if k in ("moov", "hoov")]
    tries = [("as it is", None)] + [(f"the {k} header at byte {off} set aside", off) for k, off in heads]
    ext = os.path.splitext(path)[1]
    for label, off in tries:
        src = path
        if off is not None:
            src = os.path.join(work, "patched" + ext)
            _blank(path, src, off)  # (it may only play once remuxed: the remux below decides)
        out = os.path.join(work, "fixed" + ext)
        res = subprocess.run([ff, "-v", "error", "-nostdin", "-y", "-i", src, "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                              "-map_metadata", "0", "-movflags", "+faststart+use_metadata_tags", out], capture_output=True, text=True, timeout=600)
        if res.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0 and _decodes(ff, out):
            return out, label
    raise SyncError("No repair made it play: the video data itself may be damaged; the file was left as it is")


def video(pid: int, check_running: bool = True) -> dict:
    """Repair one video in place. Returns {id, fixed_by, size_before, size_after, duration}."""
    ff = shutil.which("ffmpeg")
    if not ff:
        raise SyncError("ffmpeg is not installed on the server")
    con = index.open_index()
    try:
        r = con.execute("SELECT path, video FROM photos WHERE id = ?", (int(pid),)).fetchone()
    finally:
        con.close()
    if not r:
        raise SyncError("That photo is not in the library")
    path, is_video = r
    if not is_video:
        raise SyncError("Only videos are repaired here")
    if _decodes(ff, path):
        return {"id": pid, "fixed_by": None, "note": "it already plays: nothing to repair"}
    dbsync.preflight(dkdb.sqlite_path(), check_running, "the video")
    work = tempfile.mkdtemp(prefix=".iopix-repair-", dir=os.path.dirname(path))
    try:
        out, label = rewrap(path, work, ff)
        # keep the original's metadata (dates, place, Apple's Live Photo identifier) where ffmpeg did not carry it
        subprocess.run([*edits.exiftool_cmd(), "-q", "-m", "-overwrite_original", "-tagsfromfile", path, "-all:all", "-Keys:all", out],
                       capture_output=True, text=True, timeout=300)
        if not _decodes(ff, out):
            raise SyncError("The repaired copy did not play after its metadata was copied back; the file was left as it is")
        before = os.path.getsize(path)
        backup = edits.backup_original(path)  # the original, kept (the change log's "Restore original file")
        shutil.copystat(path, out)
        os.replace(out, path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    meta = scanner.read([path]).get(path) or {}
    rows = scanner.rows_for(path, meta, 2)
    st = dkdb.file_stamp(path)
    with dbsync.write_transaction(dkdb.sqlite_path(), check_running) as con:
        version = scanner.hash_version(con)
        con.execute("UPDATE Images SET uniqueHash = ?, fileSize = ?, modificationDate = ? WHERE id = ?",
                    (scanner.unique_hash(path, version), st[1] if st else None, st[0] if st else None, int(pid)))
        v = rows.get("video") or {}
        if v.get("duration"):
            con.execute("UPDATE VideoMetadata SET duration = ? WHERE imageid = ?", (v["duration"], int(pid)))
    thumbs.forget(int(pid))
    history.record([{"photo_id": int(pid), "action": "lossless", "path": path, "file_copy": str(backup),
                     "summary": f"Video repaired (rewrapped without re-encoding; {label})", "details": {"repair": label}}])
    return {"id": pid, "fixed_by": label, "size_before": before, "size_after": os.path.getsize(path), "duration": (rows.get("video") or {}).get("duration")}
