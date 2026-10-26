"""One queue for the heavy background work, so only one job runs at a time
and the machine stays usable for the web app and everything else.

Thumbnails, sharpness/objects, content-search embeddings, text recognition
and captions each used to start their own thread the moment the library
changed and then compete for every core and the GPU. Now they line up in a
fixed order and run one after the other. A job submitted while it is already
queued simply keeps its place; while it is running, the job's own generation
check stops it at the next batch and the new submission runs afterwards.

How many cores a running job may use is `threads()`: the `bg_threads`
setting, else BG_THREADS from the environment, else half the cores.
"""
import threading
import time
import traceback

from app import config
from app.data import state

ORDER = ["thumbs", "pixels", "embeddings", "vision", "facedetect", "faces", "neardups", "captions", "looks", "ages"]

_lock = threading.Lock()
_wake = threading.Event()
_queue = {}          # key -> callable, at most one per key
_current = None      # key of the job running now
_started = None      # when it started
_thread = None
_last_submit = 0.0  # jobs submitted close together (a start, a library change) are picked in ORDER, not arrival order
GRACE = 3.0


def threads() -> int:
    """Cores a background job may use right now."""
    n = config.cpu_count()
    try:
        v = int(state.get_setting("bg_threads") or 0)
    except (TypeError, ValueError):
        v = 0
    if v <= 0:
        v = config.BG_THREADS
    return max(1, min(v, max(1, n - 1)))


def submit(key: str, fn):
    """Queue a job; `fn` runs on the single worker thread. Keys run in ORDER."""
    global _thread
    if key not in ORDER:
        raise ValueError(key)
    global _last_submit
    with _lock:
        _queue[key] = fn
        _last_submit = time.time()
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="jobs", daemon=True)
            _thread.start()
    _wake.set()


def status() -> dict:
    with _lock:
        return {"running": _current, "queued": [k for k in ORDER if k in _queue], "threads": threads(),
                "cores": config.cpu_count(), "since": _started}


def is_queued(key: str) -> bool:
    with _lock:
        return key in _queue


def _worker():
    global _current, _started
    while True:
        # wait until the queue has been quiet for GRACE seconds, so a burst of submissions
        # (startup, a library change) settles before the first job is picked by ORDER
        while True:
            with _lock:
                settle = GRACE - (time.time() - _last_submit)
                if not _queue or settle <= 0:
                    break
            _wake.wait(timeout=settle)
            _wake.clear()
        with _lock:
            key = next((k for k in ORDER if k in _queue), None)
            fn = _queue.pop(key) if key else None
            _current, _started = key, (time.time() if key else None)
        if fn is None:
            _wake.wait(timeout=30)
            _wake.clear()
            continue
        try:
            fn()
        except Exception:
            traceback.print_exc()
        with _lock:
            _current = _started = None
