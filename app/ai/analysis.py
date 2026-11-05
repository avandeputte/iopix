"""Background image analysis, stored in the viewer's own database (vdb.py).

  quality   sharpness and exposure, measured on a ~1000 px decode of each photo
  vision    text in the picture (OCR) and face quality. Portable engine: RapidOCR
            (onnxruntime) for text, and face quality measured as the sharpness of
            each face region digiKam knows, so it runs anywhere, Docker included.
  objects   everyday objects with boxes (see detector.py), when that model is installed
  captions  a one-sentence description of each photo from a local captioning model
            (BLIP base, see caption.py), so "boy holding a soccer ball" is searchable
  looks     photo quality (see looks.py): an aesthetic score, focus on the sharpest part of the
            frame, and closed eyes on faces; each part only when its model is installed

Everything is computed from the original files, which are only read, and is
keyed by a stamp of the file so a changed photo is analysed again. The index
build folds the results into the search index; nothing goes to digiKam.

Each analysis is one job in the shared background queue (jobs.py), with its own
Progress. The five are started and reset together, so start() and reset() cancel
every Progress at once, and a job checks its own `progress[key].cancelled(gen)`
between batches: the running job stops so the new submission can take over.
"""
from PIL import UnidentifiedImageError
import importlib
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from app import config, jobs, thumbs
from app.digikam.dkdb import chunked, in_chunks
from app.data import vdb
from app.ai import caption, detector as det, looks

try:
    import numpy as np
except ImportError:  # numpy comes with the optional AI features; without it the jobs record nothing
    np = None

log = logging.getLogger(__name__)
JOBS = ("quality", "vision", "objects", "captions", "looks")
progress = {key: jobs.Progress(f"analysis:{key}") for key in JOBS}
_started_with = {"ids": set()}  # the photos the running jobs were started with, for start()'s "nothing new" rule
_ocr = None  # the OCR engine once loaded, False when none could be loaded
on_progress = None  # set by the web app: called now and then so the index picks up new results


READ_TIMEOUT = 30  # a read here may wait behind a job's long write transaction: longer than vdb.reader's default


def init():
    pass  # the analysis tables are part of the schema vdb.ensure_schema keeps (state.init)


def _counts() -> dict:
    """{job: photos analysed so far}, or None when the database is missing or unreadable."""
    if not vdb.ready():
        return None
    try:
        with vdb.reader(READ_TIMEOUT) as con:
            have = dict(con.execute("SELECT job, count(*) FROM ai_scans GROUP BY job").fetchall())
        return {key: int(have.get(key, 0)) for key in JOBS}
    except Exception:
        return None


_epoch = {"n": 0}  # bumped when a job's run ends: the one moment its results are folded into the index


def bump():
    """A background job finished a run (jobs.finished_hook): the next version check rebuilds the index once."""
    _epoch["n"] += 1


jobs.finished_hook = bump


def _drop_models():
    """The queue ran dry: the loaded models go, so an idle server is small; the next job loads them again in seconds."""
    global _ocr
    with _ocr_lock:
        _ocr = None
    for mod, fn in (("app.ai.detector", "reset"), ("app.ai.looks", "reset_sessions"), ("app.ai.caption", "reset"), ("app.faces.facedetect", "reset_session"),
                    ("app.faces.facevecs", "reset_session"), ("app.faces.ages", "reset_session"), ("app.ai.videos", "reset_sessions")):
        try:
            getattr(importlib.import_module(mod), fn)()
        except Exception as e:
            log.debug("idle unload %s: %s", mod, e)


jobs.idle_hooks.append(_drop_models)


def signature() -> str:
    """Part of the index version: the results accumulate in steps of 2000 while jobs run (a rebuild every so often,
    never one per batch) and are folded in exactly once when a run ends (the epoch). A job's running flag is
    deliberately not part of it: a version that changed at every start and every end rebuilt the index in a loop."""
    counts = _counts()
    if counts is None:
        return "a0"
    from app.ai import jerseys  # lazy: ai.jerseys -> ai.analysis would be an import cycle
    j = jerseys.coverage()[0]
    return f"a{_epoch['n']}." + ".".join(str(n // 2000 * 2000) for n in counts.values()) + f".j{j // 2000 * 2000}"


def coverage() -> dict:
    """How many photos each analysis has covered so far."""
    return _counts() or {key: 0 for key in JOBS}


def stamp(p: dict) -> str:
    return thumbs.stamp_for(p["hash"], p["size"], p["modified"])


def _todo(job: str, photos: list, con=None) -> list:
    """The still photos this job has not looked at, or looked at in an older version of the file (ai_scans)."""
    if con is None:
        with vdb.reader(READ_TIMEOUT) as con:
            return _todo(job, photos, con)
    have = dict(con.execute("SELECT photo_id, stamp FROM ai_scans WHERE job = ?", (job,)).fetchall())
    return [p for p in photos if not p["video"] and have.get(p["id"]) != stamp(p)]


def _notify(last: float, every: float) -> float:
    """Call on_progress at most every `every` seconds; returns the new timestamp to carry."""
    if on_progress and time.time() - last > every:
        on_progress()
        return time.time()
    return last


def _finish(*keys):
    for key in keys:
        progress[key]["running"] = False
    if any(progress[key]["done"] for key in keys):  # a cancelled run that did nothing changes nothing
        bump()
    if on_progress:
        on_progress()


def laplacian_var(a) -> float:
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
    return (laplacian_var(c), float(a.mean() / 255.0), float((a < 8).mean()), float((a > 247).mean()))


def _analyse_one(p: dict, want_quality: bool, detector):
    """Decode once; quality and object detection both work from the same ~1100 px image."""
    im, _ = thumbs.open_oriented(p["path"], 1100, 1100)
    out = {}
    if want_quality:
        out["quality"] = measure(im)
    if detector is not None:
        out["objects"] = detector.detect(im)
    return out


def _pixel_job(photos: list, gens: dict):
    """One decode serves two analyses, so this job reports as both "quality" and "objects" and stops when either is cancelled."""
    def cancelled() -> bool:
        return any(progress[k].cancelled(gens[k]) for k in ("quality", "objects"))
    workers = config.ANALYSIS_WORKERS or jobs.threads()
    detector = det.load(max(1, jobs.threads() // workers))  # None until the model is installed
    with vdb.reader(READ_TIMEOUT) as con:
        q_todo = {p["id"] for p in _todo("quality", photos, con)}
        o_todo = {p["id"] for p in _todo("objects", photos, con)} if detector else set()
    todo = [p for p in photos if p["id"] in q_todo or p["id"] in o_todo]
    progress["quality"].update(running=bool(q_todo), done=0, total=len(q_todo))
    progress["objects"].update(running=bool(o_todo), done=0, total=len(o_todo))
    if not todo:
        return

    try:
        def work(p):
            if cancelled():
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
            for chunk in chunked(todo, 64):
                if cancelled():
                    return
                qrows, orows, done = [], [], []
                for res in pool.map(work, chunk):
                    if not res:
                        continue
                    p, out = res
                    st = stamp(p)
                    if "quality" in out:
                        qrows.append((p["id"], st, *out["quality"]))  # (photo, stamp, sharp, bright, clip_lo, clip_hi)
                    if p["id"] in o_todo:
                        done.append((p["id"], st))
                        orows += [(p["id"], *o) for o in out.get("objects", [])]
                if cancelled():  # reset while this batch was computed: its results are of the model before
                    return
                if qrows:
                    with vdb.writer() as con:
                        con.executemany("INSERT OR REPLACE INTO ai_quality VALUES (?,?,?,?,?)", [(r[0], *r[2:]) for r in qrows])
                        con.executemany("INSERT OR REPLACE INTO ai_scans VALUES (?,'quality',?,NULL)", [(r[0], r[1]) for r in qrows])
                    progress["quality"]["done"] += len(qrows)
                if done:
                    with vdb.writer() as con:
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
        return [(t, c) for t, c, _b in self.boxes(image)]

    def boxes(self, image) -> list:
        """[(text, confidence, (x0, y0, x1, y1))] with each line's box in the image's pixels."""
        def box(pts):
            xs, ys = [float(p[0]) for p in pts], [float(p[1]) for p in pts]
            return (min(xs), min(ys), max(xs), max(ys))
        if self.modern:
            r = self.engine(image)
            if r is None or not getattr(r, "txts", None):
                return []
            bx = getattr(r, "boxes", None)
            bx = list(bx) if bx is not None else []  # an ndarray: never tested for truth
            return [(t, c, box(bx[i]) if i < len(bx) else None) for i, (t, c) in enumerate(zip(r.txts, r.scores or [1.0] * len(r.txts)))]
        res, _elapsed = self.engine(image)
        return [(x[1], x[2], box(x[0]) if x[0] is not None else None) for x in (res or []) if len(x) > 2]


_ocr_lock = threading.Lock()


def ocr_engine():
    """RapidOCR, if installed. None otherwise; the import is tried once, by one thread (a second caller waits for the
    load instead of seeing "no OCR" while it is in progress)."""
    with _ocr_lock:
        return _load_ocr()


def _load_ocr():
    global _ocr
    if _ocr is None:
        _ocr = False
        k = max(1, jobs.threads() // _vision_workers())
        # rapidocr 3.x uses headless OpenCV (good for Docker); rapidocr_onnxruntime is the older package
        for mod in ("rapidocr", "rapidocr_onnxruntime"):
            try:
                cls = importlib.import_module(mod).RapidOCR
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
        for pid, x, y, w, h in in_chunks(con, "SELECT photo_id, x, y, w, h FROM faces WHERE photo_id IN ({ids})", map(int, photo_ids)):
            out.setdefault(pid, []).append((x, y, w, h))
    finally:
        con.close()
    return out


def _portable_batch(batch: list) -> list:
    """OCR with RapidOCR and face quality from the sharpness of digiKam's face regions."""
    ocr = ocr_engine()
    regions = face_regions([p["id"] for p in batch])
    rows = []
    for p in batch:
        try:
            im, _ = thumbs.open_oriented(p["path"], 1600, 1600)
        except UnidentifiedImageError as e:
            log.warning("vision: cannot decode %s (%s)", p["path"], e)
            rows.append((p["id"], stamp(p), "", 0, None))  # recorded so it is not retried forever
            continue
        except OSError as e:  # unreadable right now (a dropped mount): nothing recorded, tried again on the next pass
            log.warning("vision: cannot read %s (%s)", p["path"], e)
            continue
        except Exception as e:
            log.warning("vision: cannot analyse %s (%s)", p["path"], e)
            rows.append((p["id"], stamp(p), "", 0, None))
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
            quals.append(float(min(1.0, (laplacian_var(np.asarray(crop, dtype=np.float32)) / 400.0) ** 0.5)))
        rows.append((p["id"], stamp(p), text, len(faces), max(quals) if quals else None))
    return rows


def vision_available() -> bool:
    """RapidOCR is installed (the ocr dependency group)."""
    return ocr_engine() is not None



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
            for chunk in chunked(batches, n):
                if progress["vision"].cancelled(gen):
                    return
                for rows in pool.map(recognise, chunk):  # (photo, stamp, text, nfaces, faceq)
                    if progress["vision"].cancelled(gen):
                        return
                    with vdb.writer() as con:
                        con.executemany("INSERT OR REPLACE INTO ai_text VALUES (?,?)", [(r[0], r[2]) for r in rows])
                        con.executemany("INSERT OR REPLACE INTO ai_face_count VALUES (?,?,?)", [(r[0], r[3], r[4]) for r in rows])
                        con.executemany("INSERT OR REPLACE INTO ai_scans VALUES (?,'vision',?,NULL)", [(r[0], r[1]) for r in rows])
                    progress["vision"]["done"] += len(rows)
                last = _notify(last, 60)
    finally:
        _finish("vision")


def _caption_job(photos: list, gen: int):
    if not caption.available():
        return
    todo = _todo("captions", photos)
    progress["captions"].update(running=bool(todo), done=0, total=len(todo))
    if not todo:
        return
    try:
        last = time.time()
        for chunk in chunked(todo, 8):  # one thread: the GPU serialises anyway, and the other jobs keep the CPU
            if progress["captions"].cancelled(gen):
                return
            batch = chunk
            try:
                texts = caption.describe([p["path"] for p in batch])
            except Exception as e:  # the batch is not recorded, so these photos are tried again on the next run
                log.warning("captions: batch starting at %s failed (%s)", batch[0]["path"], e)
                progress["captions"]["done"] += len(batch)
                continue
            rows = [(p["id"], stamp(p), (t or "")[:500]) for p, t in zip(batch, texts)]
            if progress["captions"].cancelled(gen):
                return
            with vdb.writer() as con:
                con.executemany("INSERT OR REPLACE INTO ai_descriptions VALUES (?,?)", [(r[0], r[2]) for r in rows])
                con.executemany("INSERT OR REPLACE INTO ai_scans VALUES (?,'captions',?,NULL)", [(r[0], r[1]) for r in rows])
            progress["captions"]["done"] += len(batch)
            last = _notify(last, 120)
    finally:
        _finish("captions")


# --------------------------------------------------------------------------
# photo quality: aesthetic score, focus, closed eyes (looks.py)
# --------------------------------------------------------------------------

def _looks_parts() -> dict:
    """Which parts of the photo quality job can run here: focus always, the others with their models."""
    # each part's value is the version of its measure recorded with the results, or False when it cannot run here
    return {"f": looks.VERSION, "a": 1 if looks.aesthetic_available() else False, "e": looks.EYES_VERSION if looks.eyes_available() else False}


def _looks_todo(photos: list, parts: dict, con=None) -> list:
    """[(photo, {parts it still needs})]: a changed file needs them all; otherwise only a part whose model
    arrived since (or whose measure changed) is filled in, and the rest is kept."""
    import json
    if con is None:
        with vdb.reader(READ_TIMEOUT) as con:
            return _looks_todo(photos, parts, con)
    have = {r[0]: (r[1], r[2]) for r in con.execute("SELECT photo_id, stamp, detail FROM ai_scans WHERE job = 'looks'")}
    out = []
    for p in photos:
        if p["video"]:
            continue
        st, detail = have.get(p["id"], (None, None))
        if st != stamp(p):
            need = {k for k, v in parts.items() if v}
        else:
            try:
                done = json.loads(detail or "{}")
            except ValueError:
                done = {}
            need = {k for k, v in parts.items() if v and done.get(k) != v}
        if need:
            out.append((p, need))
    return out


def _looks_faces(ids: list) -> dict:
    """{photo id: [(x, y, w, h)]}: the faces worth judging for closed eyes (named, unnamed or suggested; not
    the ones marked ignored, which are usually strangers in the background)."""
    out = {}
    if not ids or not config.INDEX_DB.exists():
        return out
    con = sqlite3.connect(f"file:{quote(str(config.INDEX_DB))}?mode=ro", uri=True)
    try:
        for pid, x, y, w, h in in_chunks(con, "SELECT photo_id, x, y, w, h FROM faces WHERE kind != 'ignored' AND photo_id IN ({ids})", map(int, ids)):
            out.setdefault(pid, []).append((x, y, w, h))
    finally:
        con.close()
    return out


def _looks_job(photos: list, gen: int):
    import json
    if np is None:
        return
    parts = _looks_parts()
    todo = _looks_todo(photos, parts)
    progress["looks"].update(running=bool(todo), done=0, total=len(todo))
    if not todo:
        return
    n = jobs.threads()
    try:
        last = time.time()
        with ThreadPoolExecutor(n, thread_name_prefix="looks") as pool:
            for chunk in chunked(todo, 16):
                if progress["looks"].cancelled(gen):
                    return
                batch = chunk
                faces = _looks_faces([p["id"] for p, need in batch if "e" in need])

                def prepare(item):
                    p, need = item
                    try:
                        im, _ = thumbs.open_oriented(p["path"], 1600, 1600)
                    except OSError as e:  # unreadable right now (a dropped mount): tried again on the next pass
                        log.warning("photo quality: cannot read %s (%s)", p["path"], e)
                        return None
                    except Exception as e:  # cannot be decoded: recorded so it is not tried forever
                        log.warning("photo quality: cannot decode %s (%s)", p["path"], e)
                        return p, need, {}, None
                    out = {}
                    try:
                        if "f" in need:
                            out["focus"] = looks.focus(im)
                        if "e" in need:
                            big = [f for f in faces.get(p["id"], []) if min(f[2], f[3]) >= 0.05 * min(p.get("w") or im.width, p.get("h") or im.height)]
                            out["eyes"] = looks.eyes(im, big, p.get("w"), p.get("h")) if big else (0, 0)
                        clip = looks.clip_input(im) if "a" in need else None
                    except Exception as e:
                        log.warning("photo quality: cannot analyse %s (%s)", p["path"], e)
                        return p, need, {}, None
                    return p, need, out, clip

                ready = [r for r in pool.map(prepare, batch) if r]
                with_clip = [r for r in ready if r[3] is not None]
                if with_clip:
                    try:
                        for r, score in zip(with_clip, looks.aesthetic([r[3] for r in with_clip], n)):
                            r[2]["aesthetic"] = score
                    except Exception as e:
                        log.warning("photo quality: the aesthetic model failed on a batch (%s)", e)
                if progress["looks"].cancelled(gen):
                    return
                with vdb.writer() as con:
                    done = [int(r[0]["id"]) for r in ready]
                    old = {r[0]: r for r in in_chunks(con, "SELECT photo_id, aesthetic, focus, eyes_checked, eyes_closed FROM ai_looks WHERE photo_id IN ({ids})", done)}
                    have = {r[0]: (r[1], r[2]) for r in in_chunks(con, "SELECT photo_id, stamp, detail FROM ai_scans WHERE job = 'looks' AND photo_id IN ({ids})", done)}
                    rows, scans = [], []
                    for p, need, out, _clip in ready:
                        st = stamp(p)
                        prev = old.get(p["id"])
                        was, detail = have.get(p["id"], (None, None))
                        try:
                            done = json.loads(detail or "{}") if was == st else {}
                        except ValueError:
                            done = {}
                        if was != st:  # a new or changed file: nothing measured before is kept
                            prev = None
                        a = out.get("aesthetic", prev[1] if prev else None)
                        f = out.get("focus", prev[2] if prev else None)
                        ec, ex = out["eyes"] if "eyes" in out else ((prev[3], prev[4]) if prev else (None, None))
                        rows.append((p["id"], a, f, ec, ex))
                        for k, field in (("f", "focus"), ("a", "aesthetic"), ("e", "eyes")):
                            if k in need and (field in out or not out):  # undecodable (no out): marked done until the file changes
                                done[k] = parts[k]
                        scans.append((p["id"], st, json.dumps(done, sort_keys=True)))
                    con.executemany("INSERT OR REPLACE INTO ai_looks VALUES (?,?,?,?,?)", rows)
                    con.executemany("INSERT OR REPLACE INTO ai_scans VALUES (?,'looks',?,?)", scans)
                progress["looks"]["done"] += len(batch)
                last = _notify(last, 120)
    finally:
        _finish("looks")


def start(photos: list):
    """Analyse whatever is new or changed, newest first. photos: rows with id, path, video, hash, size, modified."""
    if not config.ANALYSIS:
        return
    init()
    ids = {p["id"] for p in photos}
    if any(progress[k]["running"] for k in JOBS) and ids <= _started_with["ids"]:
        return  # nothing new for the running jobs: they carry on (a library change used to restart them at every batch)
    _started_with["ids"] = ids
    gens = {k: progress[k].cancel() for k in JOBS}  # the running jobs stop at their next batch; these runs take over
    # one at a time, through the shared job queue, so the machine stays usable
    jobs.submit("pixels", lambda: _pixel_job(photos, gens))
    jobs.submit("vision", lambda: _vision_job(photos, gens["vision"]))
    if config.CAPTIONS:
        jobs.submit("captions", lambda: _caption_job(photos, gens["captions"]))
    jobs.submit("looks", lambda: _looks_job(photos, gens["looks"]))
    from app.ai import jerseys  # lazy: ai.jerseys -> ai.analysis would be an import cycle
    jerseys.start(photos)  # numbers on shirts, after object detection has found the people
    from app.ai import clothing  # lazy: ai.clothing -> ai.analysis would be an import cycle
    clothing.start(photos)  # what each detected person wears, for matches within an event
    from app.ai import rotation  # lazy: ai.rotation -> data.index -> ai.analysis would be an import cycle
    rotation.start()  # photos stored on their side, from the search embeddings
    from app.ai import videos  # lazy: ai.videos -> ai.analysis would be an import cycle
    videos.start(photos)  # cover frame, chapters and speech for videos


def results() -> dict:
    """{photo_id: {...}} for the index build."""
    if not vdb.ready():
        return {}
    out = {}
    try:
        with vdb.reader(READ_TIMEOUT) as con:
            for r in con.execute("SELECT photo_id, sharp, bright, clip_lo, clip_hi FROM ai_quality"):
                out.setdefault(r[0], {}).update(sharp=r[1], bright=r[2], clip_lo=r[3], clip_hi=r[4])
            for r in con.execute("SELECT photo_id, text FROM ai_text"):
                out.setdefault(r[0], {}).update(ocr=r[1] or None)
            for r in con.execute("SELECT photo_id, nfaces, faceq FROM ai_face_count"):
                out.setdefault(r[0], {}).update(vfaces=r[1], faceq=r[2])
            for r in con.execute("SELECT photo_id FROM ai_scans WHERE job = 'objects'"):
                out.setdefault(r[0], {}).update(objects={}, npeople=0)
            for r in con.execute("SELECT photo_id, aesthetic, focus, eyes_checked, eyes_closed FROM ai_looks"):
                out.setdefault(r[0], {}).update(aesth=r[1], focus=r[2], eyes_checked=r[3], eyes_closed=r[4])
            for r in con.execute("SELECT photo_id, text FROM ai_descriptions WHERE text != ''"):  # descriptions stored before the filler was trimmed get it trimmed here
                out.setdefault(r[0], {}).update(caption_ai=caption.tidy(r[1]))
            for pid, x, y, w, h in con.execute("SELECT photo_id, x, y, w, h FROM ai_objects WHERE cls = 'person' AND w * h >= 0.01"):
                out.setdefault(pid, {}).setdefault("persons", []).append((x, y, w, h))  # for the focus point of a cropped thumbnail (smartcrop.py)
            for pid, cls, n in con.execute("SELECT photo_id, cls, count(*) FROM ai_objects GROUP BY 1, 2"):
                d = out.setdefault(pid, {})
                d.setdefault("objects", {})[cls] = n
                if cls == "person":
                    d["npeople"] = n
    except Exception as e:
        log.warning("analysis: results unreadable (%s)", e)
    return out


def boxes(photo_id: int) -> list:
    """Detected objects of one photo for the info panel: [{cls, score, x, y, w, h}], best first."""
    if not vdb.ready():
        return []
    try:
        with vdb.reader(READ_TIMEOUT) as con:
            return [dict(r) for r in con.execute(
                "SELECT cls, round(score, 2) AS score, x, y, w, h FROM ai_objects WHERE photo_id = ? ORDER BY score DESC", (photo_id,))]
    except Exception:
        return []


def reset(job: str):
    """Throw away one analysis so it is computed again from scratch."""
    tables = {"quality": ["ai_quality"], "vision": ["ai_text", "ai_face_count"], "objects": ["ai_objects"], "captions": ["ai_descriptions"], "looks": ["ai_looks"]}.get(job, [])
    for k in JOBS:  # stop the running jobs (all of them: they run as one set, and the caller restarts the set)
        progress[k].cancel()
    with vdb.writer() as con:
        for t in tables:
            con.execute(f"DELETE FROM {t}")
        if job in JOBS:
            con.execute("DELETE FROM ai_scans WHERE job = ?", (job,))
