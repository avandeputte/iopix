"""Thumbnail, preview and face-crop generation with an on-disk cache.

Source files are only ever opened for reading. Everything written goes to
config.CACHE_DIR.
"""
import os
import shutil
import subprocess
import threading
import zlib
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # HEIC files will fall back to a placeholder
    pillow_heif = None

from app import config

Image.MAX_IMAGE_PIXELS = 400_000_000  # a 400 MP panorama decodes; twice that is refused rather than eating the server's memory
FFMPEG = shutil.which("ffmpeg")
log = logging.getLogger(__name__)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@contextmanager
def _building(dst: Path):
    """Serialise concurrent requests for the same output file, so a thumbnail is
    decoded once even when the grid asks for it from several connections at a time.
    The lock is dropped afterwards so the table does not grow with every file ever made."""
    key = str(dst)
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        try:
            yield
        finally:
            with _locks_guard:
                _locks.pop(key, None)


def cache_path(kind: str, photo_id: int, stamp: str) -> Path:
    """kind is 't' (grid thumbnail), 'p' (preview) or 'f' (face crop)."""
    return config.CACHE_DIR / kind / f"{photo_id % 256:02x}" / f"{photo_id}-{stamp}.jpg"


def stamp_for(uhash, size, modified) -> str:
    """Changes when the file changes, so stale thumbnails are never served."""
    return f"{(uhash or 'x')[:10]}-{size or 0:x}-{zlib.crc32((modified or '').encode()) & 0xffff:x}"


def _save(im: Image.Image, dst: Path, quality: int, icc=None):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    kw = {"icc_profile": icc} if icc and len(icc) < 16384 else {}
    im.save(tmp, "JPEG", quality=quality, optimize=False, **kw)
    os.replace(tmp, dst)


def _open_oriented(src: str, want_w: int, want_h: int):
    """Open an image upright (EXIF orientation applied) in RGB or L, no smaller than
    want_w x want_h when the decoder can shrink it for free, plus its ICC profile.
    Shared by the thumbnail, analysis and caption code so every job decodes a file
    the same way."""
    im = Image.open(src)
    if im.format in ("JPEG", "MPO"):
        # ask the decoder for the smallest size that still covers the target box,
        # whichever way up the picture turns out to be
        s = max(min(want_w / im.width, want_h / im.height), min(want_w / im.height, want_h / im.width))
        if s < 1:
            im.draft("RGB", (int(im.width * s) + 1, int(im.height * s) + 1))
    icc = im.info.get("icc_profile")
    if getattr(im, "is_animated", False):
        im.seek(0)
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        elif im.mode.startswith("I;16") or im.mode in ("I", "F"):
            im = im.point(lambda v: v / 256).convert("L") if im.mode != "F" else im.convert("L")
        else:
            im = im.convert("RGB")
    return im, icc


def _video_frame(src: str, dst: Path, height: int, duration) -> bool:
    if not FFMPEG:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp.jpg")
    for seek in ((min(1.0, (duration or 0) / 4),) if duration else (1.0,)) + (0.0,):
        cmd = [FFMPEG, "-v", "error", "-nostdin", "-y", "-ss", f"{seek:.2f}", "-i", src,
               "-frames:v", "1", "-vf", f"scale=-2:'min({height},ih)'", "-q:v", "4", str(tmp)]
        try:
            subprocess.run(cmd, check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if tmp.exists() and tmp.stat().st_size > 0:
                os.replace(tmp, dst)
                return True
        except (subprocess.SubprocessError, OSError):
            pass
    tmp.unlink(missing_ok=True)
    return False


def _placeholder(dst: Path, w: int, h: int):
    scale = 240 / max(h, 1)
    im = Image.new("RGB", (max(16, int(w * scale)), 240), (58, 60, 66))
    _save(im, dst, 60)


def make(kind: str, photo: dict) -> Path:
    """Return the cached file for a photo row, creating it if needed.
    `photo` needs id, path, video, duration, w, h, hash, size, modified."""
    dst = cache_path(kind, photo["id"], stamp_for(photo["hash"], photo["size"], photo["modified"]))
    if dst.exists():
        return dst
    with _building(dst):
        if dst.exists():
            return dst
        if kind == "t":
            box_h, box_w, quality = config.THUMB_HEIGHT, config.THUMB_MAX_WIDTH, config.THUMB_QUALITY
        else:
            box_h = box_w = config.PREVIEW_SIZE
            quality = config.PREVIEW_QUALITY
        if not os.path.exists(photo["path"]):
            raise FileNotFoundError(photo["path"])
        try:
            if photo["video"]:
                if not _video_frame(photo["path"], dst, box_h if kind == "t" else 1080, photo["duration"]):
                    _placeholder(dst, photo["w"], photo["h"])
            else:
                im, icc = _open_oriented(photo["path"], box_w, box_h)
                scale = min(box_h / im.height, box_w / im.width, 1.0)
                if scale < 1.0:
                    size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
                    im = im.resize(size, Image.Resampling.LANCZOS, reducing_gap=2.0)
                _save(im, dst, quality, icc)
        except (UnidentifiedImageError, Image.DecompressionBombError, ValueError):  # not a picture we can decode: a placeholder keeps the grid going and is not retried
            _placeholder(dst, photo["w"], photo["h"])
        except OSError:  # unreadable right now (a dropped mount, a truncated copy still syncing): nothing cached, tried again later
            raise
    return dst


def make_face(tag_id: int, photo: dict, rect: tuple, size: int = 256) -> Path:
    """Square crop around a face rectangle (given in upright full-size pixels)."""
    x, y, w, h = rect
    dst = config.CACHE_DIR / "f" / f"{tag_id}-{photo['id']}-{x}-{y}-{w}.jpg"
    if dst.exists():
        return dst
    with _building(dst):
        if dst.exists():
            return dst
        factor = min(1.0, size * 1.6 / max(w, h, 1))
        pw, ph = photo["w"], photo["h"]
        if not pw or not ph or pw <= 8 or ph <= 8:  # the index's placeholder size: the file says
            pw, ph = true_size(photo["path"]) or (pw, ph)
        im, icc = _open_oriented(photo["path"], int(pw * factor) + 1, int(ph * factor) + 1)
        sx, sy = im.width / max(pw, 1), im.height / max(ph, 1)
        cx, cy = (x + w / 2) * sx, (y + h / 2) * sy
        half = max(w * sx, h * sy) * 0.85
        box = (int(max(0, cx - half)), int(max(0, cy - half)), int(min(im.width, cx + half)), int(min(im.height, cy + half)))
        im = im.crop(box)
        im = ImageOps.fit(im, (size, size), Image.Resampling.LANCZOS)
        _save(im, dst, 84, icc)
    return dst


class Prewarmer:
    """Builds every grid thumbnail in the background, newest photos first."""

    def __init__(self):
        self.total = 0
        self.done = 0
        self.running = False
        self.started = self.last = None   # unix times: job start, last finished item (progress visibility)
        self.current = {}                 # thread name -> path being worked on right now
        self._gen = 0

    def start(self, photos: list[dict]):
        self._gen += 1
        gen = self._gen
        todo = [p for p in photos
                if not cache_path("t", p["id"], stamp_for(p["hash"], p["size"], p["modified"])).exists()]
        self.total, self.done = len(todo), 0
        if not todo:
            self.running = False
            return
        self.running = True

        def work(p):
            if gen == self._gen:
                name = threading.current_thread().name
                self.current[name] = p["path"]
                t0 = time.time()
                try:
                    make("t", p)
                except Exception as e:
                    log.warning("thumbnail: %s: %s", p["path"], e)
                finally:
                    self.current.pop(name, None)
                    if time.time() - t0 > 60:
                        log.warning("thumbnail: %s took %.0f s", p["path"], time.time() - t0)
            self.done += 1
            self.last = time.time()

        def run():
            from app import jobs
            if gen != self._gen:
                return
            self.started = self.last = time.time()
            with ThreadPoolExecutor(config.PREWARM_WORKERS or jobs.threads(), thread_name_prefix="prewarm") as pool:
                list(pool.map(work, todo))
            if gen == self._gen:
                self.running = False

        from app import jobs
        jobs.submit("thumbs", run)  # one background job at a time


def forget(photo_id: int):
    """Drop what was derived from a photo whose picture just changed: the cached thumbnail and preview, the face
    crops, and its content-search embedding (computed again from the new picture)."""
    for kind in ("t", "p"):
        d = config.CACHE_DIR / kind / f"{photo_id % 256:02x}"
        try:
            for f in d.glob(f"{photo_id}-*.jpg"):
                f.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        for f in (config.CACHE_DIR / "f").glob(f"*-{photo_id}-*.jpg"):
            f.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        from app.ai import semantic
        semantic.remove_ids([int(photo_id)])
    except Exception as e:  # the embedding job is optional; a stale vector is the worst case
        log.debug("embedding not dropped for %s: %s", photo_id, e)


def true_size(path: str):
    """(width, height) of the picture as it is shown, from the file itself (the index has a placeholder 4x3 when
    digiKam does not know the size); None when it cannot be read."""
    try:
        with Image.open(path) as im:
            w, h = im.size
            try:
                o = im.getexif().get(0x0112) or 1
            except Exception:
                o = 1
        return (h, w) if o in (5, 6, 7, 8) else (w, h)
    except Exception:
        return None


def prune_previews():
    """Keep the preview cache under its size cap by deleting the oldest files."""
    root = config.CACHE_DIR / "p"
    if not root.exists():
        return
    files = []
    for f in root.rglob("*.jpg"):
        try:
            st = f.stat()
        except OSError:  # pruned or still being written by another thread
            continue
        files.append((st.st_atime, st.st_size, f))
    total = sum(s for _, s, _ in files)
    cap = config.PREVIEW_CACHE_MAX_MB * 1024 * 1024
    for _, size, f in sorted(files):
        if total <= cap:
            break
        f.unlink(missing_ok=True)
        total -= size
