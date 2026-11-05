"""The face sidecar queue: after a face change the photo's sidecar gets its regions rewritten from
digiKam's database, in the background, one worker, in order.

The web app asks for a face change to show at once (facemgr.apply_many background=True), so the
database write returns and the sidecar follows from here. The queue is also kept in the viewer
database (vdb's pix_face_sidecar_queue table): a restart in between must not leave regions in digiKam's
rows but not in the sidecars, so resume_queue() picks it up at start. A sidecar that could not be
written is tried a few more times, a minute apart, then logged and left (the next change to that
photo rewrites it anyway). A reread of the file (metaedit) first flushes the photo's pending
rewrite, or the reread would undo the face change.
"""
import logging
import threading
import time

from app.data import vdb

log = logging.getLogger("app.faces.sidecar_queue")

BATCH = 50  # photos per exiftool run: one process start for fifty sidecars, and a restart loses at most that many
RETRIES = 3
RETRY_SECONDS = 60  # a photo on an unmounted disk or a busy file: later, not at once
FLUSH_WAIT_SECONDS = 60  # how long flush_sidecar waits for a rewrite in progress before going ahead

_side_queue = {}            # image -> db_path, waiting for its sidecar rewrite (latest database state is written)
_side_writing = set()       # images whose sidecar is being written right now (a reread waits for them)
_side_cond = threading.Condition()
_side_worker = {"thread": None}


def _sidecar_worker():
    from app.faces import facemgr  # lazy: faces.facemgr -> faces.sidecar_queue would be an import cycle
    while True:
        with _side_cond:
            while not _side_queue:
                _side_cond.wait()
            batch = list(_side_queue.items())[:BATCH]
            for image, _p in batch:
                _side_queue.pop(image, None)
            _side_writing.update(image for image, _p in batch)
        by_db = {}
        for image, path in batch:
            by_db.setdefault(path, []).append(image)
        try:
            for db_path, images in by_db.items():
                try:
                    res = facemgr.write_regions_many(images, db_path)
                    bad = [i for i, v in res.items() if v != "written"]
                    for i in res:
                        if i not in bad:
                            _tries.pop(i, None)
                    _unqueue([i for i in res if i not in bad])
                    if bad:
                        log.warning("face regions not written to the sidecars of %s: %s", bad[:10], {i: res[i] for i in bad[:3]})
                        _retry(bad, db_path)
                except Exception as e:
                    log.warning("face regions not written to the sidecars of %s: %s", images[:10], e)
                    _retry(images, db_path)
        finally:
            with _side_cond:
                _side_writing.difference_update(image for image, _p in batch)
                _side_cond.notify_all()


_tries = {}  # photo -> how often its sidecar rewrite failed


def _retry(images, db_path):
    """Put photos whose sidecar could not be written back on the queue, a few times, a minute apart."""
    again = [i for i in images if _tries.get(i, 0) < RETRIES]
    for i in images:
        _tries[i] = _tries.get(i, 0) + 1
    if again:
        t = threading.Timer(RETRY_SECONDS, queue_sidecars, args=(again, db_path))
        t.daemon = True  # a pending retry must not hold the process open at shutdown
        t.start()


def _unqueue(images: list):
    """These photos' sidecars are written: they leave the queue kept in the viewer database."""
    if not images:
        return
    try:
        with vdb.writer() as con:
            con.executemany("DELETE FROM pix_face_sidecar_queue WHERE photo_id = ?", [(int(i),) for i in images])
    except Exception as e:
        log.debug("face sidecar queue: %s", e)


def resume_queue():
    """At start: the sidecar rewrites a restart interrupted (the queue is kept in the viewer database) go on."""
    try:
        with vdb.reader() as con:
            images = [r[0] for r in con.execute("SELECT photo_id FROM pix_face_sidecar_queue")]
    except Exception as e:
        log.debug("face sidecar queue: %s", e)
        return
    if images:
        log.info("face regions: %d sidecar rewrites left from before the restart resume", len(images))
        queue_sidecars(images, remember=False)


def queue_sidecars(images: list, db_path=None, remember: bool = True):
    """Rewrite these photos' sidecar regions from the database in the background, in order, one worker. The queue is
    also kept in the viewer database, so a restart in between does not leave regions in digiKam's rows but not in
    the sidecars."""
    if remember and images:
        try:
            with vdb.writer() as con:
                con.executemany("INSERT OR REPLACE INTO pix_face_sidecar_queue (photo_id, created_at) VALUES (?,?)", [(int(i), int(time.time())) for i in images])
        except Exception as e:
            log.debug("face sidecar queue: %s", e)
    with _side_cond:
        for i in images:
            _side_queue[int(i)] = db_path
        if _side_worker["thread"] is None or not _side_worker["thread"].is_alive():
            _side_worker["thread"] = threading.Thread(target=_sidecar_worker, name="face-sidecars", daemon=True)
            _side_worker["thread"].start()
        _side_cond.notify()


def sidecars_pending() -> int:
    """How many photos still wait for their sidecar rewrite (tests and status polls)."""
    with _side_cond:
        return len(_side_queue)


def flush_sidecar(pid: int):
    """A face change of this photo still waiting for its sidecar is written now (a reread of the file would undo it);
    one being written right now is waited for first."""
    from app.faces import facemgr  # lazy: faces.facemgr -> faces.sidecar_queue would be an import cycle
    pid = int(pid)
    with _side_cond:
        for _ in range(FLUSH_WAIT_SECONDS):
            if pid not in _side_writing:
                break
            _side_cond.wait(timeout=1)
        waiting = _side_queue.pop(pid, "none")
    if waiting != "none":
        facemgr.write_regions_many([pid])
