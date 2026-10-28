"""A check that writing metadata into an image file never touches the picture itself.

Before the viewer rewrites a file's metadata (Settings › Write metadata into image files), exiftool
hashes the file's image data only (ImageDataHash, SHA-256: the compressed pixels of a JPEG, the image
streams of PNG, TIFF, WebP, HEIC..., metadata excluded) and a copy of the file is put aside. After the
write the hash is taken again. If it differs, the write is undone: the copy goes back in place, the
write counts as failed, and the event is logged and recorded in the photo's change log. Each verified
hash is kept in image_data_checksums, with the time it was last confirmed.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time

from app import config

log = logging.getLogger("app.photos.pixelguard")
_warned = [False]


def hashes(paths: list) -> dict:
    """{path: SHA-256 of the image data, or None when exiftool cannot hash that file}."""
    if not paths:
        return {}
    from app.photos import edits
    with tempfile.NamedTemporaryFile("w", suffix=".args", dir=config.DATA_DIR, delete=False, encoding="utf-8") as f:
        f.write("\n".join(paths) + "\n")
        argfile = f.name
    try:
        res = subprocess.run([*edits.exiftool_cmd(), "-j", "-api", "ImageHashType=SHA256", "-ImageDataHash", "-charset", "filename=utf8", "-@", argfile],
                             capture_output=True, text=True, timeout=1800)
        rows = json.loads(res.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log.warning("image data hashes could not be taken: %s", e)
        return {p: None for p in paths}
    finally:
        os.unlink(argfile)
    out = {r.get("SourceFile"): r.get("ImageDataHash") for r in rows}
    if not any(out.values()) and not _warned[0]:
        _warned[0] = True
        log.warning("this exiftool cannot hash image data (needs 12.58 or newer); in-file writes are not verified")
    return {p: out.get(p) for p in paths}


class Guard:
    """Around one batch of in-file writes: hashes and copies before, verification after."""

    def __init__(self, targets: list):
        self.targets = [t for t in targets if not t.endswith(".xmp") and os.path.isfile(t)]
        self.before, self.copies = {}, {}
        self.dir = None

    def __enter__(self):
        if not self.targets:
            return self
        self.before = hashes(self.targets)
        self.dir = tempfile.mkdtemp(prefix="guard-", dir=config.DATA_DIR)
        for i, t in enumerate(self.targets):
            if self.before.get(t):
                c = os.path.join(self.dir, f"{i}{os.path.splitext(t)[1]}")
                shutil.copy2(t, c)
                self.copies[t] = c
        return self

    def verify(self) -> list:
        """After the write: the targets whose image data changed (they are put back as they were)."""
        if not self.copies:
            return []
        after = hashes(list(self.copies))
        bad, now, keep = [], int(time.time()), []
        for t, c in self.copies.items():
            if after.get(t) and after[t] == self.before[t]:
                keep.append((t, after[t], now))
                continue
            if after.get(t) is None and not os.path.isfile(t):
                pass  # the file vanished: put the copy back
            os.replace(c, t)
            bad.append(t)
            _log_refused(t)
            log.error("the image data of %s changed while its metadata was written; the file was put back as it was", t)
        _store(keep)
        return bad

    def __exit__(self, *exc):
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)


def _log_refused(path: str):
    try:
        from app.photos import history
        from app.data import index
        con = index.open_index()
        try:
            row = con.execute("SELECT id FROM photos WHERE path = ?", (path,)).fetchone()
        finally:
            con.close()
        if row:
            history.record([{"photo_id": row[0], "action": "guard", "path": path,
                             "summary": "Metadata write into the file undone: it would have changed the image data; the file is as it was"}])
    except Exception:
        pass


def _photo_ids(paths: list) -> dict:
    """{path: photo id} from the index, for the checksum rows (which are kept by photo, so moves and renames keep them)."""
    from app.data import index
    out = {}
    try:
        con = index.open_index()
        try:
            for i in range(0, len(paths), 400):
                chunk = paths[i:i + 400]
                for pid, path in con.execute(f"SELECT id, path FROM photos WHERE path IN ({','.join('?' * len(chunk))})", chunk):
                    out[path] = pid
        finally:
            con.close()
    except Exception:
        pass
    return out


def _store(rows: list):
    if not rows:
        return
    try:
        from app.data import vdb
        ids = _photo_ids([p for p, _h, _t in rows])
        with vdb.writer() as con:
            con.executemany("INSERT OR REPLACE INTO image_data_checksums (photo_id, hash, algorithm, verified) VALUES (?,?,?,?)",
                            [(ids[p], h, "SHA-256", t) for p, h, t in rows if p in ids])
    except Exception as e:  # bookkeeping only
        log.warning("image data checksums were not stored: %s", e)
