"""Wrongly rotated photos: scans and old files stored on their side or upside down, found and offered the lossless turn.

No model is downloaded for this. The content-search model's embeddings carry the picture's orientation, and the
library itself teaches what upright looks like: a sample of its photos is embedded as they are and turned by
90, 180 and 270 degrees, and a small softmax classifier learns to tell the four from the embedding. It is then
run over every photo's existing embedding (the one search already uses), and each photo it does not read as
upright is checked properly: its thumbnail is embedded in all four turns, and the four verdicts have to agree
on the same answer (a photo on its side reads as upright once turned back). Only those agreed on are listed,
under Manage › Wrongly rotated and in the info panel, each with the turn that would right it.

Nothing is changed until the user clicks Rotate, which is the ordinary lossless rotate (lossless.py: jpegtran
for JPEGs, the orientation flag for the rest, file and digiKam row updated, undo in the edit log). The
classifier is kept in data/models/rotation-<search model>.npz and learnt again when the search model changes;
the findings are in the viewer database (ai_rotation) and a dismissed one stays dismissed (quality_accepted,
check "rotation") until the file changes.
"""
import logging
import sqlite3
import threading
import time

from app import config, jobs, thumbs
from app.data import index, vdb

try:
    import numpy as np
except ImportError:  # optional: the AI packages
    np = None

log = logging.getLogger(__name__)
SAMPLE = 500          # photos the classifier learns from (each in four turns)
SCREEN_MIN = 0.5      # a photo read as turned with at least this probability is checked in all four turns
CONFIRM_MIN = 0.9     # the four-turn vote has to be this sure
TURN_OP = {1: "left", 2: "180", 3: "right"}  # class -> the lossless op that rights it (1: turned 90° clockwise, 3: counterclockwise)
TURN_TEXT = {1: "on its side, turned to the right", 2: "upside down", 3: "on its side, turned to the left"}
progress = jobs.Progress("rotation", phase="")
_lock = threading.Lock()


def available() -> bool:
    from app.ai.semantic import enabled
    return np is not None and enabled()


def _engine():
    from app.ai.semantic import semantic
    return semantic.active


def model_path(name: str):
    return config.DATA_DIR / "models" / f"rotation-{name}.npz"


# --------------------------------------------------------------------------
# the classifier: softmax regression on unit embeddings, trained with plain gradient descent
# --------------------------------------------------------------------------

SCALE = 10.0  # unit embeddings are small numbers; scaled so the logits can grow in a few hundred steps


def train(X, Y, epochs: int = 1500, lr: float = 1.0, l2: float = 1e-4) -> dict:
    """{W, b} from embeddings X (rows) and classes Y (0 upright, 1 turned clockwise, 2 upside down, 3 counterclockwise)."""
    X = np.asarray(X, np.float32) * SCALE
    Y = np.asarray(Y)
    W = np.zeros((X.shape[1], 4), np.float32)
    b = np.zeros(4, np.float32)
    n = len(Y)
    with np.errstate(all="ignore"):  # macOS's Accelerate BLAS raises spurious matmul warnings
        for _ in range(epochs):
            z = X @ W + b
            z -= z.max(1, keepdims=True)
            p = np.exp(z)
            p /= p.sum(1, keepdims=True)
            p[np.arange(n), Y] -= 1
            W -= lr * (X.T @ p / n + l2 * W)
            b -= lr * p.mean(0)
    return {"W": W, "b": b}


def predict(clf: dict, X):
    """Class probabilities (rows of 4) for embeddings X."""
    with np.errstate(all="ignore"):
        z = np.asarray(X, np.float32) * SCALE @ clf["W"] + clf["b"]
    z -= z.max(1, keepdims=True)
    p = np.exp(z)
    return p / p.sum(1, keepdims=True)


def vote(probs) -> tuple:
    """(turn, probability) from the four turns of one photo: probs[k] is predict() for the picture turned k quarter
    turns clockwise. Under the hypothesis that the file is turned by t, turn k of it reads as class (t + k) mod 4;
    the hypothesis whose four readings agree best wins."""
    scores = []
    for t in range(4):
        scores.append(sum(float(np.log(probs[k][(t + k) % 4] + 1e-9)) for k in range(4)))
    s = np.array(scores)
    p = np.exp(s - s.max())
    p /= p.sum()
    return int(p.argmax()), float(p.max())


def turns(im) -> list:
    """The picture turned 0, 90, 180 and 270 degrees clockwise (class order)."""
    return [im, im.rotate(270, expand=True), im.rotate(180, expand=True), im.rotate(90, expand=True)]


# --------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------

def _load_clf(name: str):
    p = model_path(name)
    if not p.exists():
        return None
    try:
        with np.load(p) as z:
            return {"W": z["W"], "b": z["b"]}
    except Exception as e:
        log.warning("rotation: classifier unreadable (%s)", e)
        return None


def _thumb(con, pid: int):
    """The cached grid thumbnail of a photo as a PIL image, or None."""
    from PIL import Image
    r = con.execute("SELECT id, path, video, duration, w, h, hash, size, modified FROM photos WHERE id = ?", (pid,)).fetchone()
    if not r:
        return None
    try:
        return Image.open(thumbs.make("t", dict(r))).convert("RGB")
    except Exception as e:
        log.debug("rotation: no thumbnail for %s (%s)", pid, e)
        return None


def _learn(engine, con, gen: int):
    """Train the classifier on a sample of the library in four turns, and save it."""
    rows = con.execute("SELECT id FROM photos WHERE video = 0 AND w >= 200 ORDER BY random() LIMIT ?", (SAMPLE,)).fetchall()
    progress.update(phase="learning", done=0, total=len(rows))
    X, Y, batch = [], [], []
    for (pid,) in rows:
        if progress.cancelled(gen):
            return None
        im = _thumb(con, pid)
        if im is None:
            progress.step()
            continue
        batch += [(t, k) for k, t in enumerate(turns(im))]
        if len(batch) >= 64:
            X += list(engine.embed_images([b[0] for b in batch]))
            Y += [b[1] for b in batch]
            batch = []
        progress.step()
    if batch:
        X += list(engine.embed_images([b[0] for b in batch]))
        Y += [b[1] for b in batch]
    if len(X) < 200:
        log.warning("rotation: too few thumbnails to learn from (%d)", len(X))
        return None
    clf = train(X, Y)
    p = model_path(engine.name)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez(p, W=clf["W"], b=clf["b"], n=len(X), trained=int(time.time()))
    return clf


def _job(gen: int):
    if not available():
        return
    engine = _engine()
    engine.load()
    con = index.open_index()
    con.row_factory = sqlite3.Row
    try:
        clf = _load_clf(engine.name)
        if clf is None:
            clf = _learn(engine, con, gen)
            if clf is None:
                return
        with engine.lock:
            ids, vecs = engine.ids, engine.vecs
        if ids is None or not len(ids):
            return
        stamps = {r[0]: r[1] for r in con.execute("SELECT id, hash || ':' || coalesce(size, 0) || ':' || coalesce(modified, '') FROM photos WHERE video = 0")}
        key = f"{engine.name}"
        with vdb.reader() as v:
            done = {r[0]: r[1] for r in v.execute("SELECT photo_id, stamp FROM ai_scans WHERE job = 'rotation'")}
        todo_pos = [i for i, pid in enumerate(ids.tolist()) if pid in stamps and done.get(pid) != f"{stamps[pid]}:{key}"]
        progress.update(phase="screening", done=0, total=len(todo_pos))
        if not todo_pos:
            return
        probs = predict(clf, vecs[todo_pos])
        cand = [(int(ids[i]), int(probs[j].argmax()), float(probs[j].max())) for j, i in enumerate(todo_pos) if probs[j].argmax() != 0 and probs[j].max() >= SCREEN_MIN]
        clean = [int(ids[i]) for j, i in enumerate(todo_pos) if not (probs[j].argmax() != 0 and probs[j].max() >= SCREEN_MIN)]
        with vdb.writer() as v:
            v.executemany("DELETE FROM ai_rotation WHERE photo_id = ?", [(p,) for p in clean])
            v.executemany("INSERT OR REPLACE INTO ai_scans VALUES (?,'rotation',?,NULL)", [(p, f"{stamps[p]}:{key}") for p in clean])
        progress.update(phase="checking", done=0, total=len(cand))
        for pid, _cls, _p in cand:
            if progress.cancelled(gen):
                return
            im = _thumb(con, pid)
            found = None
            if im is not None:
                try:
                    pr = predict(clf, engine.embed_images(turns(im)))
                    t, sure = vote(pr)
                    if t != 0 and sure >= CONFIRM_MIN:
                        found = (t, sure)
                except Exception as e:
                    log.warning("rotation: could not check %s (%s)", pid, e)
            with vdb.writer() as v:
                v.execute("DELETE FROM ai_rotation WHERE photo_id = ?", (pid,))
                if found:
                    v.execute("INSERT INTO ai_rotation (photo_id, turn, score, stamp) VALUES (?,?,?,?)", (pid, found[0], round(found[1], 4), stamps[pid]))
                v.execute("INSERT OR REPLACE INTO ai_scans VALUES (?,'rotation',?,NULL)", (pid, f"{stamps[pid]}:{key}"))
            progress.step()
    finally:
        con.close()
        progress.finish(gen)


def start():
    if not config.ANALYSIS or not available():
        return
    if progress["running"]:
        return  # it screens every embedding anyway; a new start would only make it begin again
    gen = progress.cancel()
    jobs.submit("rotation", lambda: _job(gen))


def reset():
    progress.cancel()
    try:
        model_path(_engine().name).unlink(missing_ok=True)
    except Exception:
        pass
    with vdb.writer() as con:
        con.execute("DELETE FROM ai_rotation")
        con.execute("DELETE FROM ai_scans WHERE job = 'rotation'")


def coverage() -> tuple:
    try:
        with vdb.reader() as con:
            done = con.execute("SELECT count(*) FROM ai_scans WHERE job = 'rotation'").fetchone()[0]
        from app.ai.semantic import semantic
        total = len(semantic.ids) if available() and semantic.ids is not None else 0
        return int(done), int(total)
    except Exception:
        return 0, 0


# --------------------------------------------------------------------------
# the findings
# --------------------------------------------------------------------------

def dismiss(photos: list, on: bool = True) -> int:
    """These photos are fine as they are (on: false takes it back); a dismissal lasts until the file changes."""
    from app.photos import history
    want = [int(p) for p in photos]
    with vdb.writer() as con:
        if on:
            stamps = dict(con.execute(f"SELECT photo_id, stamp FROM ai_rotation WHERE photo_id IN ({','.join(str(p) for p in want) or 'NULL'})").fetchall())
            con.executemany("INSERT OR REPLACE INTO quality_accepted (photo_id, check_name, signature, user_name, ts) VALUES (?,'rotation',?,?,?)",
                            [(p, stamps[p], history.actor(), int(time.time())) for p in want if p in stamps])
            return sum(1 for p in want if p in stamps)
        con.executemany("DELETE FROM quality_accepted WHERE photo_id = ? AND check_name = 'rotation'", [(p,) for p in want])
        return len(want)


def findings(ids: list = None) -> dict:
    """{"items": [{id, turn, op, text, score}], "dismissed": n}: the photos read as turned, surest first, the dismissed
    ones left out (unless their file changed since)."""
    where = f" WHERE photo_id IN ({','.join(str(int(i)) for i in ids) or 'NULL'})" if ids is not None else ""
    with vdb.reader() as con:
        rows = [tuple(r) for r in con.execute("SELECT photo_id, turn, score, stamp FROM ai_rotation" + where)]
        acc = dict(con.execute("SELECT photo_id, signature FROM quality_accepted WHERE check_name = 'rotation'" + where.replace(" WHERE", " AND")).fetchall())
    items, dismissed = [], 0
    for pid, turn, score, stamp in rows:
        if acc.get(pid) == stamp:
            dismissed += 1
            continue
        items.append({"id": pid, "turn": turn, "op": TURN_OP.get(turn), "text": f"Looks {TURN_TEXT.get(turn, 'turned')}", "score": score})
    items.sort(key=lambda i: (-i["score"], i["id"]))
    return {"items": items, "dismissed": dismissed}
