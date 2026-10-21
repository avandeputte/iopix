"""Background image analysis, stored in the viewer's own database (vdb.py).

  quality   sharpness and exposure, measured on a ~1000 px decode of each photo
  vision    text in the picture (OCR) and face quality. Portable engine: RapidOCR
            (onnxruntime) for text, and face quality measured as the sharpness of
            each face region digiKam knows, so it runs anywhere, Docker included.
  objects   everyday objects with boxes (see detector.py), when that model is installed
  captions  a one-sentence description of each photo from a local captioning model
            (BLIP base, see caption.py), so "boy holding a soccer ball" is searchable

Everything is computed from the original files, which are only read, and is
keyed by a stamp of the file so a changed photo is analysed again. The index
build folds the results into the search index; nothing goes to digiKam.

Each analysis is one job in the shared background queue (jobs.py). A job checks
`_run["gen"]` between batches: start() and reset() bump it, which makes the
running job stop so the new submission can take over.
"""
from PIL import UnidentifiedImageError
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import quote

from app import config, jobs, thumbs

try:
    import numpy as np
except ImportError:  # numpy comes with the optional AI features; without it the jobs record nothing
    np = None

log = logging.getLogger(__name__)
JOBS = ("quality", "vision", "objects", "captions")
progress = {key: {"running": False, "done": 0, "total": 0} for key in JOBS}
_write_lock = threading.Lock()  # also taken by consistency.py, which shares this database
_run = {"gen": 0}
_ocr = None  # the OCR engine once loaded, False when none could be loaded
on_progress = None  # set by the web app: called now and then so the index picks up new results


def connect():
    from app.data import vdb
    return vdb.connect(timeout=30)


@contextmanager
def _writer():
    """One serialised write transaction: committed on success, always closed."""
    with _write_lock:
        con = connect()
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init():
    pass  # the analysis tables are part of the schema vdb.ensure_schema keeps (state.init)


def _counts() -> dict:
    """{job: photos analysed so far}, or None when the database is missing or unreadable."""
    from app.data import vdb
    if not vdb.exists():
        return None
    con = connect()
    try:
        have = dict(con.execute("SELECT job, count(*) FROM ai_scans GROUP BY job").fetchall())
        return {key: int(have.get(key, 0)) for key in JOBS}
    except Exception:
        return None
    finally:
        con.close()


def signature() -> str:
    """Part of the index version: changes as analysis results accumulate (in steps
    while a job is running, exactly once it has finished)."""
    counts = _counts()
    if counts is None:
        return "a0"
    return "a" + ".".join(str(n // 2000 * 2000 if progress[key]["running"] else n) for key, n in counts.items())


def coverage() -> dict:
    """How many photos each analysis has covered so far."""
    return _counts() or {key: 0 for key in JOBS}


def _stamp(p: dict) -> str:
    return thumbs.stamp_for(p["hash"], p["size"], p["modified"])


def _todo(job: str, photos: list, con=None) -> list:
    """The still photos this job has not looked at, or looked at in an older version of the file (ai_scans)."""
    own = con is None
    con = con or connect()
    try:
        have = dict(con.execute("SELECT photo_id, stamp FROM ai_scans WHERE job = ?", (job,)).fetchall())
    finally:
        if own:
            con.close()
    return [p for p in photos if not p["video"] and have.get(p["id"]) != _stamp(p)]


def _save(sql: str, rows: list):
    with _writer() as con:
        con.executemany(sql, rows)


def _notify(last: float, every: float) -> float:
    """Call on_progress at most every `every` seconds; returns the new timestamp to carry."""
    if on_progress and time.time() - last > every:
        on_progress()
        return time.time()
    return last


def _finish(*keys):
    for key in keys:
        progress[key]["running"] = False
    if on_progress:
        on_progress()


def _laplacian_var(a) -> float:
    """Variance of the 4-neighbour Laplacian of a float32 greyscale array: higher is sharper."""
    lap = a[1:-1, 1:-1] * 4 - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    return float(lap.var())


# --------------------------------------------------------------------------
# quality (and object detection, which shares the decoded image)
# --------------------------------------------------------------------------

def measure(im) -> tuple:
    """(sharpness, brightness, fraction of crushed blacks, fraction of blown highlights).
    Sharpness is the variance of a Laplacian over the central 70% of the frame at a fixed
    size, so it is comparable between photos of the same scene (e.g. within a burst)."""
    g = im.convert("L")
    scale = 1024 / max(g.size)
    if scale < 1:
        g = g.resize((max(1, round(g.width * scale)), max(1, round(g.height * scale))))
    a = np.asarray(g, dtype=np.float32)
    h, w = a.shape
    c = a[int(h * 0.15):int(h * 0.85), int(w * 0.15):int(w * 0.85)]
    return (_laplacian_var(c), float(a.mean() / 255.0), float((a < 8).mean()), float((a > 247).mean()))


def _analyse_one(p: dict, want_quality: bool, detector):
    """Decode once; quality and object detection both work from the same ~1100 px image."""
    im, _ = thumbs._open_oriented(p["path"], 1100, 1100)
    out = {}
    if want_quality:
        out["quality"] = measure(im)
    if detector is not None:
        out["objects"] = detector.detect(im)
    return out


def _pixel_job(photos: list, gen: int):
    from app.ai import detector as det
    workers = config.ANALYSIS_WORKERS or jobs.threads()
    detector = det.load(max(1, jobs.threads() // workers))  # None until the model is installed
    con = connect()
    try:
        q_todo = {p["id"] for p in _todo("quality", photos, con)}
        o_todo = {p["id"] for p in _todo("objects", photos, con)} if detector else set()
    finally:
        con.close()
    todo = [p for p in photos if p["id"] in q_todo or p["id"] in o_todo]
    progress["quality"].update(running=bool(q_todo), done=0, total=len(q_todo))
    progress["objects"].update(running=bool(o_todo), done=0, total=len(o_todo))
    if not todo:
        return

    try:
        def work(p):
            if gen != _run["gen"]:
                return None
            try:
                return p, _analyse_one(p, p["id"] in q_todo, detector if p["id"] in o_todo else None)
            except UnidentifiedImageError as e:
                log.warning("analysis: cannot decode %s (%s)", p["path"], e)
                # record the failure so the file is not decoded again on every pass
                return p, {"quality": (None, None, None, None)} if p["id"] in q_todo else {}
            except OSError as e:  # unreadable right now (a dropped mount): nothing recorded, tried again on the next pass
                log.warning("analysis: cannot read %s (%s)", p["path"], e)
                return None
            except Exception as e:
                log.warning("analysis: cannot analyse %s (%s)", p["path"], e)
                return p, {"quality": (None, None, None, None)} if p["id"] in q_todo else {}

        last = time.time()
        with ThreadPoolExecutor(workers, thread_name_prefix="analyse") as pool:
            for i in range(0, len(todo), 64):
                if gen != _run["gen"]:
                    return
                qrows, orows, done = [], [], []
                for res in pool.map(work, todo[i:i + 64]):
                    if not res:
                        continue
                    p, out = res
                    st = _stamp(p)
                    if "quality" in out:
                        qrows.append((p["id"], st, *out["quality"]))  # (photo, stamp, sharp, bright, clip_lo, clip_hi)
                    if p["id"] in o_todo:
                        done.append((p["id"], st))
                        orows += [(p["id"], *o) for o in out.get("objects", [])]
                if qrows:
                    with _writer() as con:
                        con.executemany("INSERT OR REPLACE INTO ai_quality VALUES (?,?,?,?,?)", [(r[0], *r[2:]) for r in qrows])
                        con.executemany("INSERT OR REPLACE INTO ai_scans VALUES (?,'quality',?,NULL)", [(r[0], r[1]) for r in qrows])
                    progress["quality"]["done"] += len(qrows)
                if done:
                    with _writer() as con:
                        con.executemany("DELETE FROM ai_objects WHERE photo_id = ?", [(d[0],) for d in done])
                        con.executemany("INSERT INTO ai_objects VALUES (?,?,?,?,?,?,?)", orows)
                        con.executemany("INSERT OR REPLACE INTO ai_scans VALUES (?,'objects',?,NULL)", done)
                    progress["objects"]["done"] += len(done)
                last = _notify(last, 60)
    finally:
        _finish("quality", "objects")


# --------------------------------------------------------------------------
# text recognition and face quality
# --------------------------------------------------------------------------

class _OCR:
    """One call shape for both RapidOCR generations: returns [(text, confidence)]."""

    def __init__(self, engine, modern: bool):
        self.engine, self.modern = engine, modern

    def __call__(self, image) -> list:
        if self.modern:
            r = self.engine(image)
            if r is None or not getattr(r, "txts", None):
                return []
            return list(zip(r.txts, r.scores or [1.0] * len(r.txts)))
        res, _elapsed = self.engine(image)
        return [(x[1], x[2]) for x in (res or []) if len(x) > 2]


def _ocr_engine():
    """RapidOCR, if installed (./enable-ocr.sh). None otherwise; the import is tried once."""
    global _ocr
    if _ocr is None:
        _ocr = False
        k = max(1, jobs.threads() // _vision_workers())
        # rapidocr 3.x uses headless OpenCV (good for Docker); rapidocr_onnxruntime is the older package
        for mod in ("rapidocr", "rapidocr_onnxruntime"):
            try:
                cls = __import__(mod, fromlist=["RapidOCR"]).RapidOCR
                try:
                    # log_level: the engine logs every model load at INFO and resets its logger's level itself
                    engine = cls(params={"EngineConfig.onnxruntime.intra_op_num_threads": k, "Global.log_level": "error"})
                except TypeError:  # the older package has no params argument
                    engine = cls()
                _ocr = _OCR(engine, modern=(mod == "rapidocr"))
                break
            except ImportError:
                continue
            except Exception as e:
                log.warning("analysis: %s failed to load (%s)", mod, e)
    return _ocr or None


def face_regions(photo_ids: list) -> dict:
    """{photo id: [(x, y, w, h)]} from the index: the faces digiKam has placed, upright pixels."""
    out = {}
    if not photo_ids or not config.INDEX_DB.exists():
        return out
    con = sqlite3.connect(f"file:{quote(str(config.INDEX_DB))}?mode=ro", uri=True)
    try:
        for i in range(0, len(photo_ids), 500):
            chunk = ",".join(str(int(x)) for x in photo_ids[i:i + 500])
            for pid, x, y, w, h in con.execute(f"SELECT photo_id, x, y, w, h FROM faces WHERE photo_id IN ({chunk})"):
                out.setdefault(pid, []).append((x, y, w, h))
    finally:
        con.close()
    return out


def _portable_batch(batch: list) -> list:
    """OCR with RapidOCR and face quality from the sharpness of digiKam's face regions."""
    ocr = _ocr_engine()
    regions = face_regions([p["id"] for p in batch])
    rows = []
    for p in batch:
        try:
            im, _ = thumbs._open_oriented(p["path"], 1600, 1600)
        except Exception as e:
            log.warning("vision: cannot read %s (%s)", p["path"], e)
            rows.append((p["id"], _stamp(p), "", 0, None))  # recorded so it is not retried forever
            continue
        text = ""
        if ocr is not None:
            try:
                # a PIL image: RapidOCR converts it to BGR itself; a 3-channel ndarray would be taken as BGR already
                lines = [t.strip() for t, c in ocr(im.convert("RGB")) if c >= 0.5 and t.strip()]
                text = "\n".join(lines)[:4000]
            except Exception as e:
                log.warning("vision: OCR failed on %s (%s)", p["path"], e)
        faces = regions.get(p["id"], [])
        quals = []
        # face rectangles are in full-size upright pixels; the decode may be smaller
        sx, sy = im.width / max(p.get("w") or im.width, 1), im.height / max(p.get("h") or im.height, 1)
        for x, y, w, h in faces:
            box = (int(x * sx), int(y * sy), int((x + w) * sx), int((y + h) * sy))
            if box[2] - box[0] < 12 or box[3] - box[1] < 12:
                continue
            crop = im.crop(box).convert("L").resize((96, 96))
            # 0..1: a crisp face at this size has a Laplacian variance around 400+
            quals.append(float(min(1.0, (_laplacian_var(np.asarray(crop, dtype=np.float32)) / 400.0) ** 0.5)))
        rows.append((p["id"], _stamp(p), text, len(faces), max(quals) if quals else None))
    return rows


def vision_available() -> bool:
    """RapidOCR is installed (the ocr dependency group)."""
    return _ocr_engine() is not None



def _vision_workers() -> int:
    """Photo batches recognised at once: two when the core budget allows it."""
    return 2 if jobs.threads() >= 4 else 1


def _vision_job(photos: list, gen: int):
    if not vision_available():
        return
    todo = _todo("vision", photos)
    progress["vision"].update(running=bool(todo), done=0, total=len(todo))
    if not todo:
        return
    batches = [todo[i:i + 24] for i in range(0, len(todo), 24)]
    try:
        recognise = _portable_batch
        last = time.time()
        n = _vision_workers()
        with ThreadPoolExecutor(n, thread_name_prefix="vision") as pool:
            for i in range(0, len(batches), n):
                if gen != _run["gen"]:
                    return
                for rows in pool.map(recognise, batches[i:i + n]):  # (photo, stamp, text, nfaces, faceq)
                    with _writer() as con:
                        con.executemany("INSERT OR REPLACE INTO ai_text VALUES (?,?)", [(r[0], r[2]) for r in rows])
                        con.executemany("INSERT OR REPLACE INTO ai_face_count VALUES (?,?,?)", [(r[0], r[3], r[4]) for r in rows])
                        con.executemany("INSERT OR REPLACE INTO ai_scans VALUES (?,'vision',?,NULL)", [(r[0], r[1]) for r in rows])
                    progress["vision"]["done"] += len(rows)
                last = _notify(last, 60)
    finally:
        _finish("vision")


def _caption_job(photos: list, gen: int):
    from app.ai import caption
    if not caption.available():
        return
    todo = _todo("captions", photos)
    progress["captions"].update(running=bool(todo), done=0, total=len(todo))
    if not todo:
        return
    try:
        last = time.time()
        for i in range(0, len(todo), 8):  # one thread: the GPU serialises anyway, and the other jobs keep the CPU
            if gen != _run["gen"]:
                return
            batch = todo[i:i + 8]
            try:
                texts = caption.describe([p["path"] for p in batch])
            except Exception as e:  # the batch is not recorded, so these photos are tried again on the next run
                log.warning("captions: batch starting at %s failed (%s)", batch[0]["path"], e)
                progress["captions"]["done"] += len(batch)
                continue
            rows = [(p["id"], _stamp(p), (t or "")[:500]) for p, t in zip(batch, texts)]
            with _writer() as con:
                con.executemany("INSERT OR REPLACE INTO ai_descriptions VALUES (?,?)", [(r[0], r[2]) for r in rows])
                con.executemany("INSERT OR REPLACE INTO ai_scans VALUES (?,'captions',?,NULL)", [(r[0], r[1]) for r in rows])
            progress["captions"]["done"] += len(batch)
            last = _notify(last, 120)
    finally:
        _finish("captions")


def start(photos: list):
    """Analyse whatever is new or changed, newest first. photos: rows with id, path, video, hash, size, modified."""
    if not config.ANALYSIS:
        return
    init()
    _run["gen"] += 1
    gen = _run["gen"]
    # one at a time, through the shared job queue, so the machine stays usable
    jobs.submit("pixels", lambda: _pixel_job(photos, gen))
    jobs.submit("vision", lambda: _vision_job(photos, gen))
    if config.CAPTIONS:
        jobs.submit("captions", lambda: _caption_job(photos, gen))


def results() -> dict:
    """{photo_id: {...}} for the index build."""
    from app.data import vdb
    if not vdb.exists():
        return {}
    con = connect()
    out = {}
    try:
        for r in con.execute("SELECT photo_id, sharp, bright, clip_lo, clip_hi FROM ai_quality"):
            out.setdefault(r[0], {}).update(sharp=r[1], bright=r[2], clip_lo=r[3], clip_hi=r[4])
        for r in con.execute("SELECT photo_id, text FROM ai_text"):
            out.setdefault(r[0], {}).update(ocr=r[1] or None)
        for r in con.execute("SELECT photo_id, nfaces, faceq FROM ai_face_count"):
            out.setdefault(r[0], {}).update(vfaces=r[1], faceq=r[2])
        for r in con.execute("SELECT photo_id FROM ai_scans WHERE job = 'objects'"):
            out.setdefault(r[0], {}).update(objects={}, npeople=0)
        for r in con.execute("SELECT photo_id, text FROM ai_descriptions WHERE text != ''"):
            out.setdefault(r[0], {}).update(caption_ai=r[1])
        for pid, cls, n in con.execute("SELECT photo_id, cls, count(*) FROM ai_objects GROUP BY 1, 2"):
            d = out.setdefault(pid, {})
            d.setdefault("objects", {})[cls] = n
            if cls == "person":
                d["npeople"] = n
    except Exception as e:
        log.warning("analysis: results unreadable (%s)", e)
    finally:
        con.close()
    return out


def boxes(photo_id: int) -> list:
    """Detected objects of one photo for the info panel: [{cls, score, x, y, w, h}], best first."""
    from app.data import vdb
    if not vdb.exists():
        return []
    con = connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT cls, round(score, 2) AS score, x, y, w, h FROM ai_objects WHERE photo_id = ? ORDER BY score DESC", (photo_id,))]
    except Exception:
        return []
    finally:
        con.close()


def reset(job: str):
    """Throw away one analysis so it is computed again from scratch."""
    tables = {"quality": ["ai_quality"], "vision": ["ai_text", "ai_face_count"], "objects": ["ai_objects"], "captions": ["ai_descriptions"]}.get(job, [])
    _run["gen"] += 1  # stop the running jobs; the caller restarts them
    with _writer() as con:
        for t in tables:
            con.execute(f"DELETE FROM {t}")
        if job in JOBS:
            con.execute("DELETE FROM ai_scans WHERE job = ?", (job,))
