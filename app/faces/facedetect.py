"""Face detection on request: a photo, a folder or the whole library.

Three detectors, chosen in Maintenance › AI analysis and models (the setting face_detector), all run with
onnxruntime on a 640 px letterboxed copy of the picture:
- YuNet (OpenCV, 230 KB): the default, tiny and fast;
- SCRFD 10G (InsightFace): clearly better on small, turned and partly hidden faces, about 10x slower;
- RetinaFace ResNet-50: the classic heavy detector, the slowest.
Each returns boxes with a score and the five landmarks (eyes, nose, mouth corners) that face grouping
uses to align a face before embedding it.

Every face found is compared with the face regions the photo already has, of any kind and from any
tool (named, unknown, suggested or ignored, digiKam's or the viewer's): a detection that overlaps an
existing region is the same face, even when the two rectangles differ in size or position, and is left
alone. Only faces nothing else marked become new regions, written the way digiKam writes a face it
found but could not name (an "Unknown" region), so they show up under People › Unnamed faces, get a
"looks like" suggestion from face grouping, and can be named there or in digiKam.

Nothing runs on its own: the library job starts only from Maintenance, a folder or a photo. Each photo
checked is remembered (ai_scans, job facedetect) so a library run picks up only new and changed photos; a run
on one photo or one folder always looks again. A face removed after a run is not brought back by the
next library run, since that photo is not looked at again unless it changes.
"""
import json
import logging
import sqlite3
import threading
import time

from app.ai import analysis, models
from app.data import index, vdb, vdb as v
from app import jobs, thumbs
from app.digikam.dkdb import chunked

try:
    import numpy as np
except ImportError:  # optional: the AI packages
    np = None

log = logging.getLogger("app.faces.facedetect")
SIDE = 640           # every detector runs on a 640x640 letterboxed copy
SCORES = {"yunet": 0.85, "scrfd": 0.5, "retinaface": 0.6}  # YuNet's own default is 0.9; a little lower finds more turned and partly hidden faces
NMS_IOU = 0.3
MIN_FACE = 28        # pixels at full resolution: smaller detections are mostly noise
OVERLAP = 0.3        # a detection covering this much of an existing region (or the other way round) is that face
progress = {"running": False, "done": 0, "total": 0, "added": 0, "photos_with_new": 0, "errors": 0, "started": None, "scope": None}
_lock = threading.Lock()
_session = None
_session_path = None
_run = {"gen": 0}


def chosen() -> str:
    return models.chosen("face_detector")


def model_path():
    return models.path_for("face_detector")


def available() -> bool:
    if np is None or not model_path().exists():
        return False
    try:
        import onnxruntime
        return bool(onnxruntime.__version__)
    except ImportError:
        return False


def _sess():
    """(detector name, its onnxruntime session), read together so a switch meanwhile cannot pair one model's
    decoder with another's session."""
    global _session, _session_path
    with _lock:
        kind = chosen()
        path = models.path(kind)
        if _session is None or _session_path != path:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2  # detect() runs threads()//2 photos at once: 2 x that = the budget, not its square
            _session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
            _session_path = path
        return kind, _session


def reset_session():
    global _session
    with _lock:
        _session = None


# ---- the detectors: each takes the letterboxed 640x640 RGB array (H, W, 3, values 0..255) and returns
# [(x, y, w, h, score, [(lx, ly)] * 5)] in the 640 px frame
def _yunet(arr, sess) -> list:
    blob = np.ascontiguousarray(np.transpose(arr[:, :, ::-1], (2, 0, 1))[None])  # BGR, 0..255
    out = dict(zip([o.name for o in sess.get_outputs()], sess.run(None, {sess.get_inputs()[0].name: blob})))
    found, thr = [], SCORES["yunet"]
    for stride in (8, 16, 32):
        cols = SIDE // stride
        cls, obj, box, kps = out[f"cls_{stride}"][0, :, 0], out[f"obj_{stride}"][0, :, 0], out[f"bbox_{stride}"][0], out[f"kps_{stride}"][0]
        score = np.sqrt(np.clip(cls, 0, 1) * np.clip(obj, 0, 1))
        for i in np.nonzero(score >= thr)[0]:
            r, c = divmod(int(i), cols)
            cx, cy = (c + box[i, 0]) * stride, (r + box[i, 1]) * stride
            bw, bh = float(np.exp(box[i, 2])) * stride, float(np.exp(box[i, 3])) * stride
            pts = [((c + kps[i, 2 * k]) * stride, (r + kps[i, 2 * k + 1]) * stride) for k in range(5)]
            found.append((cx - bw / 2, cy - bh / 2, bw, bh, float(score[i]), pts))
    return found


def _scrfd(arr, sess) -> list:
    """SCRFD with keypoints: nine outputs, scores then boxes then landmarks for strides 8, 16, 32, two anchors per cell."""
    blob = np.ascontiguousarray(((np.transpose(arr, (2, 0, 1)) - 127.5) / 128.0)[None].astype(np.float32))  # RGB
    outs = sess.run(None, {sess.get_inputs()[0].name: blob})
    found, thr = [], SCORES["scrfd"]
    for k, stride in enumerate((8, 16, 32)):
        scores, boxes, kps = outs[k][:, 0], outs[k + 3] * stride, outs[k + 6] * stride
        n = SIDE // stride
        ys, xs = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        centers = np.repeat(np.stack((xs, ys), -1).reshape(-1, 2) * stride, 2, axis=0).astype(np.float32)
        for i in np.nonzero(scores >= thr)[0]:
            cx, cy = centers[i]
            x1, y1, x2, y2 = cx - boxes[i, 0], cy - boxes[i, 1], cx + boxes[i, 2], cy + boxes[i, 3]
            pts = [(cx + kps[i, 2 * j], cy + kps[i, 2 * j + 1]) for j in range(5)]
            found.append((float(x1), float(y1), float(x2 - x1), float(y2 - y1), float(scores[i]), pts))
    return found


_RF_PRIORS = None


def _rf_priors():
    """RetinaFace's anchor boxes for a 640 px input: two sizes per cell at strides 8, 16, 32 (16,800 in all), as fractions."""
    global _RF_PRIORS
    if _RF_PRIORS is None:
        rows = []
        for stride, sizes in ((8, (16, 32)), (16, (64, 128)), (32, (256, 512))):
            n = SIDE // stride
            for i in range(n):
                for j in range(n):
                    for ms in sizes:
                        rows.append(((j + 0.5) * stride / SIDE, (i + 0.5) * stride / SIDE, ms / SIDE, ms / SIDE))
        _RF_PRIORS = np.asarray(rows, dtype=np.float32)
    return _RF_PRIORS


def _retinaface(arr, sess) -> list:
    """RetinaFace (biubug6 / clibdev export): loc [1,N,4], conf [1,N,2] (softmaxed), landms [1,N,10]; input BGR minus (104,117,123)."""
    bgr = arr[:, :, ::-1].astype(np.float32) - np.asarray((104, 117, 123), dtype=np.float32)
    blob = np.ascontiguousarray(np.transpose(bgr, (2, 0, 1))[None])
    outs = {o.shape[-1]: v[0] for o, v in zip(sess.get_outputs(), sess.run(None, {sess.get_inputs()[0].name: blob}))}
    loc, conf, landms = outs[4], outs[2], outs[10]
    pri = _rf_priors()
    scores = conf[:, 1]
    found, thr = [], SCORES["retinaface"]
    for i in np.nonzero(scores >= thr)[0]:
        pcx, pcy, pw, ph = pri[i]
        cx, cy = (pcx + loc[i, 0] * 0.1 * pw) * SIDE, (pcy + loc[i, 1] * 0.1 * ph) * SIDE
        w, h = float(pw * np.exp(loc[i, 2] * 0.2)) * SIDE, float(ph * np.exp(loc[i, 3] * 0.2)) * SIDE
        pts = [(float((pcx + landms[i, 2 * j] * 0.1 * pw) * SIDE), float((pcy + landms[i, 2 * j + 1] * 0.1 * ph) * SIDE)) for j in range(5)]
        found.append((float(cx - w / 2), float(cy - h / 2), w, h, float(scores[i]), pts))
    return found


DETECTORS = {"yunet": _yunet, "scrfd": _scrfd, "retinaface": _retinaface}


def _run_square(img) -> list:
    """One pass of the chosen detector over a PIL image letterboxed into 640x640:
    [(x, y, w, h, score, landmarks)] in the image's pixels."""
    from PIL import Image
    s = min(SIDE / img.width, SIDE / img.height)
    w, h = max(1, round(img.width * s)), max(1, round(img.height * s))
    canvas = Image.new("RGB", (SIDE, SIDE))
    canvas.paste(img.convert("RGB").resize((w, h), Image.Resampling.BILINEAR), (0, 0))
    arr = np.asarray(canvas, dtype=np.float32)
    kind, sess = _sess()
    found = DETECTORS[kind](arr, sess)
    return [(x / s, y / s, bw / s, bh / s, sc, [(px / s, py / s) for px, py in pts]) for x, y, bw, bh, sc, pts in found]


def _iou(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / max(a[2] * a[3] + b[2] * b[3] - inter, 1e-9)


def _nms(boxes: list) -> list:
    keep = []
    for b in sorted(boxes, key=lambda x: -x[4]):
        if all(_iou(b, k) < NMS_IOU for k in keep):
            keep.append(b)
    return keep


def detect_image(img, landmarks: bool = False) -> list:
    """Faces in an upright PIL image: [(x, y, w, h, score)] in its pixels, each with its five landmarks
    appended when asked. Large pictures also get a 2x2 pass of overlapping tiles, so small faces in
    group photos are not lost to the 640 px input."""
    boxes = _run_square(img)
    if max(img.width, img.height) > 1.6 * SIDE:
        tw, th = int(img.width * 0.6), int(img.height * 0.6)
        for ox in (0, img.width - tw):
            for oy in (0, img.height - th):
                boxes += [(x + ox, y + oy, w, h, sc, [(px + ox, py + oy) for px, py in pts]) for x, y, w, h, sc, pts in _run_square(img.crop((ox, oy, ox + tw, oy + th)))]
    kept = _nms(boxes)
    return kept if landmarks else [b[:5] for b in kept]


def detect_photo(path: str, full_w: int, full_h: int) -> list:
    """Faces in a photo file, in upright full-resolution pixels (digiKam's region coordinates)."""
    img, _ = thumbs.open_oriented(path, 2400, 2400)
    if not full_w or not full_h or full_w <= 8 or full_h <= 8:  # the index's placeholder size: the file says
        full_w, full_h = thumbs.true_size(path) or (img.width, img.height)
    sx = full_w / img.width if full_w and full_w > 8 else 1.0
    sy = full_h / img.height if full_h and full_h > 8 else 1.0
    out = []
    for x, y, w, h, sc in detect_image(img):
        X, Y, W, H = max(0, round(x * sx)), max(0, round(y * sy)), round(w * sx), round(h * sy)
        W, H = min(W, (full_w or img.width) - X), min(H, (full_h or img.height) - Y)
        if min(W, H) >= MIN_FACE:
            out.append((X, Y, W, H, round(sc, 3)))
    return out


def same_face(a, b) -> bool:
    """Whether two rectangles mark the same face: one's centre inside the other, or a large share of the
    smaller one covered. Tools draw faces differently (tight around the features, or the whole head), so
    neither size nor position has to agree."""
    ax, ay, aw, ah = a[:4]
    bx, by, bw, bh = b[:4]
    acx, acy, bcx, bcy = ax + aw / 2, ay + ah / 2, bx + bw / 2, by + bh / 2
    if bx <= acx <= bx + bw and by <= acy <= by + bh or ax <= bcx <= ax + aw and ay <= bcy <= ay + ah:
        return True
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return ix * iy >= OVERLAP * max(1, min(aw * ah, bw * bh))


def new_faces(found: list, existing: list) -> list:
    """The detections no existing region already marks."""
    return [f for f in found if not any(same_face(f, e) for e in existing)]


# ---- which photos, what they already have
def select_photos(ids: list = None, album: int = None) -> list:
    con = index.open_index()
    con.row_factory = sqlite3.Row
    try:
        sql = "SELECT p.id, p.path, p.name, p.w, p.h, p.hash, p.size, p.modified FROM photos p WHERE p.video = 0"
        if ids is not None:
            rows = []
            for part in chunked(ids, 500):
                chunk = ",".join(str(int(x)) for x in part)
                rows += con.execute(sql + f" AND p.id IN ({chunk})").fetchall()
        elif album is not None:
            rows = con.execute(sql + " AND p.album_id IN (SELECT album FROM album_closure WHERE anc = ?) ORDER BY p.ts DESC", (int(album),)).fetchall()
        else:
            rows = con.execute(sql + " ORDER BY p.ts DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _existing(ids: list) -> dict:
    """{photo: [(x, y, w, h)]} every face region the index knows, of every kind."""
    out = {}
    con = index.open_index()
    try:
        for part in chunked(ids, 500):
            chunk = ",".join(str(int(x)) for x in part)
            for pid, x, y, w, h in con.execute(f"SELECT photo_id, x, y, w, h FROM faces WHERE photo_id IN ({chunk})"):
                out.setdefault(pid, []).append((x, y, w, h))
    finally:
        con.close()
    return out


def _scanned() -> dict:
    with vdb.reader() as con:
        return dict(con.execute("SELECT photo_id, stamp FROM ai_scans WHERE job = 'facedetect'").fetchall())


def coverage() -> int:
    if not v.exists():
        return 0
    con = v.connect()
    try:
        return con.execute("SELECT count(*) FROM ai_scans WHERE job = 'facedetect'").fetchone()[0]
    except Exception:
        return 0
    finally:
        con.close()


def reset():
    """Forget which photos were looked at, so the next library run looks at all of them again."""
    _run["gen"] += 1
    with vdb.writer() as con:
        con.execute("DELETE FROM ai_scans WHERE job = 'facedetect'")


# ---- running
def _write(found: dict) -> int:
    """{photo: [(x, y, w, h)]} new unknown faces into digiKam's database, one transaction per batch."""
    from app.faces import facemgr
    ops = [{"op": "detected", "image": pid, "rect": list(r[:4])} for pid, rects in found.items() for r in rects]
    if not ops:
        return 0
    added = 0
    for chunk in chunked(ops, 200):
        # background=True: the sidecar is left to the queue; an unknown face is not written to XMP anyway, so no
        # sidecar is created or rewritten for each of thousands of photos inside the job
        res = facemgr.apply_many(chunk, background=True)
        added += sum(1 for r in res if "error" not in r and not r.get("skipped"))
    return added


def _record(rows: list):
    if not rows:
        return
    with vdb.writer() as con:
        con.executemany("INSERT OR REPLACE INTO ai_scans (photo_id, job, stamp, detail) VALUES (?,'facedetect',?,?)",
                        [(r[0], r[1], json.dumps({"found": r[2], "added": r[3], "checked": r[4]})) for r in rows])


def detect(photos: list, gen: int = None, on_batch=None) -> dict:
    """Look for faces in these photos (rows from select_photos), write the new ones, remember the photos."""
    from concurrent.futures import ThreadPoolExecutor
    total = {"checked": 0, "found": 0, "added": 0, "photos_with_new": 0, "errors": 0}
    n = max(1, jobs.threads() // 2)
    for chunk in chunked(photos, 48):
        if gen is not None and gen != _run["gen"]:
            break
        batch = chunk
        existing = _existing([p["id"] for p in batch])

        def one(p):
            try:
                return p, detect_photo(p["path"], p["w"], p["h"])
            except Exception as e:
                log.warning("face detection: %s: %s", p["path"], e)
                return p, None
        with ThreadPoolExecutor(n, thread_name_prefix="facedetect") as pool:
            results = list(pool.map(one, batch))
        new, rows, now = {}, [], int(time.time())
        for p, found in results:
            if found is None:
                total["errors"] += 1
                continue
            fresh = new_faces(found, existing.get(p["id"], []))
            total["found"] += len(found)
            if fresh:
                new[p["id"]] = fresh
            rows.append((p["id"], analysis.stamp(p), len(found), len(fresh), now))
        added = _write(new)
        _record(rows)
        total["checked"] += len(rows)
        total["added"] += added
        total["photos_with_new"] += len(new)
        if on_batch:
            on_batch(len(batch), added, len(new), total["errors"])
    if total["added"]:
        log.info("face detection: %d new faces in %d of %d photos", total["added"], total["photos_with_new"], total["checked"])
    return total


def start(ids: list = None, album: int = None, redo: bool = False, after=None) -> int:
    """The background job for a folder or the whole library (or a large selection). Returns how many
    photos it will look at; the library run skips photos looked at since they last changed."""
    photos = select_photos(ids, album)
    if ids is None and album is None and not redo:
        seen = _scanned()
        photos = [p for p in photos if seen.get(p["id"]) != analysis.stamp(p)]
    _run["gen"] += 1
    gen = _run["gen"]
    scope = "photos" if ids is not None else "folder" if album is not None else "library"
    progress.update(running=bool(photos), done=0, total=len(photos), added=0, photos_with_new=0, errors=0, started=int(time.time()), scope=scope, error=None)
    if not photos:
        return 0

    def on_batch(n, added, with_new, errors):
        progress["done"] += n
        progress["added"] += added
        progress["photos_with_new"] += with_new
        progress["errors"] = errors

    def job():
        try:
            detect(photos, gen, on_batch)
        except Exception as e:
            log.warning("face detection stopped: %s", e, exc_info=True)
            progress["error"] = str(e)[:300]
        finally:
            if gen == _run["gen"]:
                progress["running"] = False
            if after:
                after()
    jobs.submit("facedetect", job)
    return len(photos)
