"""Face work queue: an embedding per face digiKam has not named (its "Unknown" faces and its
own suggestions), so the Faces page shows look-alikes together and they can be named or
ignored in one go, plus a few embeddings per named person (their most recent confirmed
faces, the prototypes) so each group and face can carry a "looks like" suggestion with a
one-click name. The suggestion is the viewer's own and is never written anywhere until
the owner accepts it; accepting goes through the same face operations as naming by hand.

The embeddings come from one of two models (ONNX, run locally with onnxruntime; chosen in
Maintenance, the setting face_vectors): OpenCV's SFace (the default, 128-d, on the square crops
the face queue already shows) or InsightFace's ArcFace ResNet-50 (512-d, much better at telling
people apart; each crop is first aligned on its eyes, nose and mouth, found by the chosen face
detector, the way ArcFace was trained). Vectors live in the viewer database (ai_face_embeddings
for undecided faces, ai_person_references for the named prototypes), are pruned as faces are
decided, and are all recomputed when the model (or the downloaded revision of it) changes, since vectors of
different models do not compare.
"""
import logging
import sqlite3
import threading

try:
    import numpy as np
except ImportError:  # numpy comes with the search/objects extras; without it these features are simply off
    np = None

from app import config, jobs, thumbs
from app.data import index, vdb
from app.ai import models
from app.digikam.dkdb import chunked

log = logging.getLogger("app.faces.facevecs")
SIZE = 112
# ArcFace's reference landmark positions (left eye, right eye, nose, left and right mouth corner) in its 112x112 input
ARCFACE_TEMPLATE = ((38.2946, 51.6963), (73.5318, 51.5014), (56.0252, 71.7366), (41.5493, 92.3655), (70.7299, 92.2041))
KINDS = ("unknown", "suggested")
# cosine thresholds per model: (same group, "looks like" suggestion). OpenCV's own same-identity threshold for SFace is
# 0.363, a little stricter here; ArcFace keeps different people near 0 (measured 0.05 against 0.71 for the same person)
THRESHOLDS = {"sface": (0.45, 0.40), "arcface": (0.40, 0.35)}
DIMS = {"sface": 128, "arcface": 512}
PROTOS_PER_PERSON = 12  # the most recent confirmed faces of each person become their prototypes
progress = {"running": False, "done": 0, "total": 0}
_lock = threading.Lock()
_session = None
_session_path = None
_run = {"gen": 0}


def chosen() -> str:
    return models.chosen("face_vectors")


def group_threshold() -> float:
    return THRESHOLDS.get(chosen(), THRESHOLDS["sface"])[0]


def suggest_threshold() -> float:
    return THRESHOLDS.get(chosen(), THRESHOLDS["sface"])[1]


def model_path():
    return models.path_for("face_vectors")


def available() -> bool:
    if np is None or not model_path().exists():
        return False
    try:
        import onnxruntime
        return bool(onnxruntime.__version__)
    except ImportError:
        return False


def _sess():
    global _session, _session_path
    with _lock:
        path = model_path()
        if _session is None or _session_path != path:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, jobs.threads())
            _session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
            _session_path = path
        return _session


def reset_session():
    global _session
    with _lock:
        _session = None


def _similarity(src, dst):
    """The similarity transform (scale, rotation, translation; Umeyama) taking the points src onto dst, as a 2x3 matrix."""
    src, dst = np.asarray(src, np.float64), np.asarray(dst, np.float64)
    n = len(src)
    ms, md = src.mean(0), dst.mean(0)
    s0, d0 = src - ms, dst - md
    u, sv, vt = np.linalg.svd(d0.T @ s0 / n)
    d = np.ones(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        d[1] = -1
    r = u @ np.diag(d) @ vt
    scale = (sv * d).sum() / max((s0 ** 2).sum() / n, 1e-12)
    t = md - scale * r @ ms
    return np.hstack([scale * r, t[:, None]])


def _landmarks(im):
    """The five landmarks of the face in the middle of a crop, from the chosen face detector; None when none is found."""
    from app.faces import facedetect
    if not facedetect.available():
        return None
    try:
        found = facedetect.detect_image(im, landmarks=True)
    except Exception as e:
        log.warning("faces: no landmarks (%s)", e)
        return None
    cx, cy = im.width / 2, im.height / 2
    near = [f for f in found if abs(f[0] + f[2] / 2 - cx) < im.width * 0.25 and abs(f[1] + f[3] / 2 - cy) < im.height * 0.25]
    if not near:
        return None
    return max(near, key=lambda f: f[2] * f[3])[5]


def align(im, pts):
    """The crop warped so its landmarks sit on ArcFace's template: a 112x112 RGB image. None when the
    landmarks are degenerate (all on a line, say) and no transform exists."""
    from PIL import Image
    try:
        m = np.vstack([_similarity(pts, ARCFACE_TEMPLATE), [0, 0, 1]])
        inv = np.linalg.inv(m)  # PIL wants the matrix from output pixels back to input pixels
    except (np.linalg.LinAlgError, ValueError):
        return None
    if not np.all(np.isfinite(inv)):
        return None
    return im.convert("RGB").transform((SIZE, SIZE), Image.AFFINE, tuple(inv[:2].ravel()), Image.Resampling.BICUBIC)


def _prepare(im, kind: str):
    """The model's input array (3, 112, 112) for one crop image."""
    from PIL import Image
    if kind == "arcface":
        pts = _landmarks(im)
        face = align(im, pts) if pts is not None else None
        if face is None:  # no landmarks: the middle of the crop, where make_face put the face
            m = int(im.width * 0.18)
            face = im.crop((m, m, im.width - m, im.height - m)).convert("RGB").resize((SIZE, SIZE), Image.Resampling.BILINEAR)
        arr = (np.asarray(face, dtype=np.float32) - 127.5) / 127.5  # RGB, -1..1
    else:  # sface: 112x112 BGR, pixel values as they are
        im = im.convert("RGB").resize((SIZE, SIZE), Image.Resampling.BILINEAR)
        arr = np.asarray(im, dtype=np.float32)[:, :, ::-1]
    return np.ascontiguousarray(np.transpose(arr, (2, 0, 1)))


def embed_paths(paths: list, kind: str = None, sess=None):
    """Unit vectors (128-d for SFace, 512-d for ArcFace) for face crop files. A job passes the model it
    started with, so a switch made meanwhile cannot mix two models' vectors in one batch."""
    from PIL import Image
    kind = kind or chosen()
    s = sess or _sess()
    name = s.get_inputs()[0].name
    out = []
    for p in paths:
        blob = _prepare(Image.open(p), kind)[None]
        v = s.run(None, {name: blob})[0][0]
        out.append(v / max(float(np.linalg.norm(v)), 1e-9))
    return np.asarray(out, dtype=np.float32)


def _faces():
    """Every undecided face from the index with what the crop needs: [(photo, x, y, w, h, path, pw, ph)]."""
    con = index.open_index()
    try:
        return con.execute("SELECT f.photo_id, f.x, f.y, f.w, f.h, p.path, p.w, p.h FROM faces f JOIN photos p ON p.id = f.photo_id "
                           f"WHERE f.kind IN ({','.join('?' * len(KINDS))}) AND p.video = 0", KINDS).fetchall()
    finally:
        con.close()


def _proto_faces():
    """The newest confirmed faces of every person, a few each: [(tag, photo, x, y, w, h, path, pw, ph)]."""
    con = index.open_index()
    try:
        rows = con.execute("SELECT f.tag_id, f.photo_id, f.x, f.y, f.w, f.h, p.path, p.w, p.h FROM faces f JOIN photos p ON p.id = f.photo_id "
                           "JOIN tags t ON t.id = f.tag_id WHERE f.kind = 'confirmed' AND t.person = 1 AND p.video = 0 ORDER BY f.tag_id, p.ts DESC").fetchall()
    finally:
        con.close()
    out, seen = [], {}
    for r in rows:
        if seen.get(r[0], 0) < PROTOS_PER_PERSON:
            seen[r[0]] = seen.get(r[0], 0) + 1
            out.append(r)
    return out


def _have_protos() -> dict:
    with vdb.reader() as con:
        return {(r[1], r[2], r[3], r[4], r[5]): r[0] for r in con.execute("SELECT tag_id, photo_id, x, y, w, h FROM ai_person_references")}


def _have() -> set:
    with vdb.reader() as con:
        return {tuple(r) for r in con.execute("SELECT photo_id, x, y, w, h FROM ai_face_embeddings")}


def coverage() -> tuple:
    """(faces with a vector, faces to embed) for the Maintenance row: undecided faces plus the prototypes."""
    try:
        return len(_have()) + len(_have_protos()), len(_faces()) + len(_proto_faces())
    except Exception:
        return 0, 0


def start():
    """Embed the undecided faces that have no vector yet, and drop vectors of faces since decided."""
    if not config.ANALYSIS or not available():
        return
    _run["gen"] += 1
    gen = _run["gen"]
    jobs.submit("faces", lambda: _job(gen))


def _embed_into(gen: int, todo: list, table: str, offset: int, kind: str, sess):
    """todo: [(key..., path, pw, ph)] where key is (photo, x, y, w, h) or (tag, photo, x, y, w, h); rows go to `table`."""
    tagged = table == "ai_person_references"
    done = 0
    for todo_batch in chunked(todo, 32):
        if gen != _run["gen"]:
            return False
        batch, paths = [], []
        for row in todo_batch:
            key, (path, pw, ph) = row[:-3], row[-3:]
            pid = key[1] if tagged else key[0]
            rect = key[2:6] if tagged else key[1:5]
            try:
                paths.append(str(thumbs.make_face(0, {"id": pid, "path": path, "w": pw, "h": ph}, tuple(rect))))
                batch.append(tuple(key))
            except Exception as e:
                log.warning("faces: no crop for photo %s (%s)", pid, e)
        if batch:
            try:
                vecs = embed_paths(paths, kind, sess)
            except Exception as e:
                log.warning("faces: embedding failed (%s)", e)
                continue
            if gen != _run["gen"]:  # a switch or reset happened while this batch was computed: its vectors are not kept
                return False
            with vdb.writer() as con:
                if tagged:
                    con.executemany("INSERT OR REPLACE INTO ai_person_references (tag_id, photo_id, x, y, w, h, vec) VALUES (?,?,?,?,?,?,?)",
                                    [(*key, vecs[k].astype(np.float32).tobytes()) for k, key in enumerate(batch)])
                else:
                    con.executemany("INSERT OR REPLACE INTO ai_face_embeddings (photo_id, x, y, w, h, vec) VALUES (?,?,?,?,?,?)",
                                    [(*key, vecs[k].astype(np.float32).tobytes()) for k, key in enumerate(batch)])
        done += len(todo_batch)
        progress["done"] = offset + done
    return True


def _built_key(kind: str) -> str:
    """What face_vectors_built records: model@revision (the file's sha256), so the same model downloaded again as
    another file counts as a change too; the bare name when the file is not there to hash."""
    rev = models.installed_revision(kind)
    return f"{kind}@{rev}" if rev else kind


def _same_model():
    """Vectors from another model, or another revision of it, are useless now: forget them (once) when that changed."""
    from app.data import state
    kind = chosen()
    want = _built_key(kind)
    have = state.get_setting("face_vectors_built", "sface")
    if have == want:
        return
    if have != kind:  # the bare name is what versions before revisions wrote: those vectors came from the file installed then, adopted as is
        with vdb.writer() as con:
            con.execute("DELETE FROM ai_face_embeddings")
            con.execute("DELETE FROM ai_person_references")
        log.info("faces: embeddings recomputed with %s", want)
    state.set_setting("face_vectors_built", want)


def _job(gen: int):
    _same_model()
    kind, sess = chosen(), _sess()  # the model this whole run uses
    faces, protos = _faces(), _proto_faces()
    have, have_p = _have(), _have_protos()
    todo = [f for f in faces if (f[0], f[1], f[2], f[3], f[4]) not in have]
    todo_p = [f for f in protos if (f[1], f[2], f[3], f[4], f[5]) not in have_p or have_p[(f[1], f[2], f[3], f[4], f[5])] != f[0]]
    progress.update(running=True, done=0, total=len(todo) + len(todo_p))
    try:
        if not _embed_into(gen, todo_p, "ai_person_references", 0, kind, sess):  # the prototypes first: they make the suggestions possible
            return
        if not _embed_into(gen, todo, "ai_face_embeddings", len(todo_p), kind, sess):
            return
        # faces decided since (named, ignored, removed) no longer need a vector; prototypes follow the newest faces
        current = {(f[0], f[1], f[2], f[3], f[4]) for f in faces}
        stale = [k for k in have if k not in current]
        current_p = {(f[1], f[2], f[3], f[4], f[5]) for f in protos}
        stale_p = [k for k in have_p if k not in current_p]
        if stale or stale_p:
            with vdb.writer() as con:
                if stale:
                    con.executemany("DELETE FROM ai_face_embeddings WHERE photo_id = ? AND x = ? AND y = ? AND w = ? AND h = ?", stale)
                if stale_p:
                    con.executemany("DELETE FROM ai_person_references WHERE photo_id = ? AND x = ? AND y = ? AND w = ? AND h = ?", stale_p)
    finally:
        progress["running"] = False


def reset():
    _run["gen"] += 1
    with vdb.writer() as con:
        con.execute("DELETE FROM ai_face_embeddings")
        con.execute("DELETE FROM ai_person_references")


def _prototypes() -> tuple:
    """(matrix of prototype vectors, their tag ids, {tag: name}) for the suggestions; empty when none."""
    with vdb.reader() as con:
        rows = [(r[0], np.frombuffer(r[1], dtype=np.float32)) for r in con.execute("SELECT tag_id, vec FROM ai_person_references")]
    rows = [r for r in rows if len(r[1]) == DIMS.get(chosen(), 128)]  # never a stray vector of another model
    if not rows:
        return None, None, {}
    icon = index.open_index()
    try:
        names = dict(icon.execute("SELECT id, name FROM tags WHERE person = 1").fetchall())
    finally:
        icon.close()
    rows = [r for r in rows if r[0] in names]
    if not rows:
        return None, None, {}
    return np.stack([v for _, v in rows]), np.asarray([t for t, _ in rows]), names


def _suggest(vec, pm, ptags, names):
    """The person whose prototypes match this vector best: {tag, name, score}, or None below the threshold."""
    if pm is None:
        return None
    sims = pm @ vec
    best = {}
    for t, sc in zip(ptags.tolist(), sims.tolist()):
        best[t] = max(best.get(t, -1), sc)
    t, sc = max(best.items(), key=lambda kv: kv[1])
    return {"tag": int(t), "name": names.get(int(t)), "score": round(float(sc), 3)} if sc >= suggest_threshold() else None


def _components(m, threshold: float) -> dict:
    """{root: [rows]} of the faces whose vectors (rows of m, unit length) are joined by pairs at or above the
    threshold. Compared in row blocks against the whole matrix, so the memory is n x block rather than n x n:
    this runs on a web request."""
    n = len(m)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    block = 1024
    for s in range(0, n, block):
        with np.errstate(all="ignore"):  # Accelerate emits spurious float warnings on matmul
            sims = m[s:s + block] @ m.T
        rows, cols = np.nonzero(sims >= threshold)
        keep = cols > rows + s  # each pair once (j > i), never a face with itself
        for i, j in zip((rows[keep] + s).tolist(), cols[keep].tolist()):
            a, b = find(i), find(j)
            if a != b:
                parent[b] = a
    members = {}
    for i in range(n):
        members.setdefault(find(i), []).append(i)
    return members


def groups(kind: str = "unknown", threshold: float = None, tag: int = None) -> dict:
    """Undecided faces of one kind, look-alikes together: {groups: [{faces: [...], n, suggest}], ungrouped: [...],
    missing: faces without a vector yet}. Groups are connected components of pairs above the threshold; each group
    and each face carries the viewer's own "looks like" suggestion when a named person's prototypes match."""
    con = index.open_index()
    con.row_factory = sqlite3.Row
    try:
        where, params = "f.kind = ? AND p.video = 0", [kind]
        if tag:
            where += " AND f.tag_id = ?"
            params.append(tag)
        rows = con.execute(f"SELECT f.photo_id AS photo, f.tag_id AS tag, f.x, f.y, f.w, f.h, p.ts, p.name AS file, p.w AS pw, p.h AS ph, "
                           f"(SELECT name FROM tags t WHERE t.id = f.tag_id) AS name FROM faces f JOIN photos p ON p.id = f.photo_id "
                           f"WHERE {where} ORDER BY p.ts DESC", params).fetchall()
    finally:
        con.close()
    faces = [dict(r) for r in rows]
    vcon = vdb.connect()
    try:
        vecs = {(r[0], r[1], r[2], r[3], r[4]): np.frombuffer(r[5], dtype=np.float32) for r in vcon.execute("SELECT photo_id, x, y, w, h, vec FROM ai_face_embeddings")}
    finally:
        vcon.close()
    dim = DIMS.get(chosen(), 128)
    keyed = [(f, vecs.get((f["photo"], f["x"], f["y"], f["w"], f["h"]))) for f in faces]
    keyed = [(f, v if v is not None and len(v) == dim else None) for f, v in keyed]  # a vector of another model counts as missing
    missing = [f for f, v in keyed if v is None]
    have = [(f, v) for f, v in keyed if v is not None]
    threshold = threshold or group_threshold()
    out = {"groups": [], "ungrouped": [], "missing": missing, "total": len(faces), "threshold": threshold}
    if not have:
        return out
    pm, ptags, names = _prototypes()
    out["people"] = len(set(ptags.tolist())) if ptags is not None else 0
    m = np.stack([v for _, v in have])
    for root, idx in _components(m, threshold).items():
        if len(idx) == 1:
            f = dict(have[idx[0]][0])
            f["suggest"] = _suggest(m[idx[0]], pm, ptags, names)
            out["ungrouped"].append(f)
            continue
        c = m[idx].mean(axis=0)
        c = c / max(float(np.linalg.norm(c)), 1e-9)
        order = sorted(idx, key=lambda k: -float(m[k] @ c))  # the most typical face first
        out["groups"].append({"n": len(idx), "faces": [have[k][0] for k in order], "suggest": _suggest(c, pm, ptags, names)})
    out["groups"].sort(key=lambda g: -g["n"])
    return out
