"""Phone-friendly copies of videos a phone cannot play as they are (AVI, WMV, MPEG-TS, odd audio): H.264 video
and AAC audio in an MP4 with its index at the front, made with ffmpeg on first request and kept in the cache
(served with Range support, so a player can seek). One conversion runs at a time."""
import logging
import os
import subprocess
import threading
from pathlib import Path

from app import config, thumbs

log = logging.getLogger(__name__)
DIR = config.CACHE_DIR / "v"
MAX_BYTES = 8 * 1024 ** 3  # the oldest copies go once the folder holds more than this
PLAYABLE_EXT = {"mp4", "m4v", "mov"}
PLAYABLE_VIDEO = {"", "h264", "avc1", "hevc", "h265", "hvc1"}
PLAYABLE_AUDIO = {"", "none", "aac", "mp3", "alac", "ac3", "eac3", "pcm_s16le", "pcm_s24le"}

_lock = threading.Lock()
_running: set = set()
_failed: dict = {}  # path of a copy that could not be made -> ffmpeg's last words


def playable(name: str, vcodec, acodec) -> bool:
    """Whether iPhones, iPads and Macs (AVPlayer) play the original as it is: an MP4 or QuickTime file with H.264 or
    HEVC video and AAC (or another audio Apple plays). Unknown codecs in such a file count as playable."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in PLAYABLE_EXT and (vcodec or "").lower() in PLAYABLE_VIDEO and (acodec or "").lower() in PLAYABLE_AUDIO


def copy_path(row: dict) -> Path:
    return DIR / f"{row['id'] % 256:02x}" / f"{row['id']}-{thumbs.stamp_for(row['hash'], row['size'], row['modified'])}.mp4"


def status(row: dict) -> str:
    """ready, converting, failed or missing (not asked for yet)."""
    dst = copy_path(row)
    if dst.exists():
        return "ready"
    if str(dst) in _running:
        return "converting"
    return "failed" if str(dst) in _failed else "missing"


def start(row: dict) -> str:
    """Begin making the copy (in the background) unless it exists or is under way; returns the status."""
    st = status(row)
    if st != "missing":
        return st
    if not thumbs.FFMPEG:
        _failed[str(copy_path(row))] = "ffmpeg is not installed"
        return "failed"
    _running.add(str(copy_path(row)))
    threading.Thread(target=_convert, args=(dict(row),), daemon=True).start()
    return "converting"


def error(row: dict) -> str:
    return _failed.get(str(copy_path(row)), "")


def _convert(row: dict):
    dst = copy_path(row)
    tmp = dst.with_name(f".{dst.name}.tmp.mp4")
    try:
        with _lock:  # one at a time: a conversion keeps every core busy
            dst.parent.mkdir(parents=True, exist_ok=True)
            cmd = [thumbs.FFMPEG, "-v", "error", "-nostdin", "-y", "-i", row["path"], "-map", "0:v:0", "-map", "0:a:0?",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                   "-vf", "scale='min(1920,iw)':-2", "-c:a", "aac", "-b:a", "160k", "-ac", "2",
                   "-movflags", "+faststart", "-map_metadata", "0", str(tmp)]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600)
            if p.returncode or not tmp.exists() or not tmp.stat().st_size:
                _failed[str(dst)] = (p.stderr or "ffmpeg failed").strip()[-400:]
                log.warning("video copy of %s failed: %s", row["path"], _failed[str(dst)])
                return
            os.replace(tmp, dst)
            _prune()
    except (OSError, subprocess.SubprocessError) as e:
        _failed[str(dst)] = str(e)
        log.warning("video copy of %s failed: %s", row["path"], e)
    finally:
        tmp.unlink(missing_ok=True)
        _running.discard(str(dst))


def _prune():
    files = sorted((p for p in DIR.glob("*/*.mp4")), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for p in files:
        if total <= MAX_BYTES:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
