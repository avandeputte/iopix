"""Year suggestions for photos whose date is unknown, from how old the people in them look.

MiVOLO v2 (an age estimator, run with onnxruntime on the CPU) estimates the age of each named face in a
photo marked "Date unknown" (dateprecision.py), and of a sample of the same people's faces in photos
whose date is known. Each person's dated faces then calibrate the model for that person: a line from
estimated age to the year the photo was taken (or, with few dated photos, the median offset between the
two, which is their birth year as the model sees it). A birth year the user entered replaces the
offset when there are too few dated photos to learn it from. An undated photo's named faces each give
a year with an uncertainty; the photo's suggestion is their weighted mean, with a range.

The model only sees faces, so a suggestion is never more than "about 1987 (1985-1989)": it is shown,
with the people and ages it came from, and written nowhere. Using it is the date editor's year-only
date (the user's click, through /api/edit). Estimated ages are kept in the viewer database
(ai_face_ages, keyed by face rectangle like the face vectors); birth years in person_births.
"""
import logging
import math
import threading
import time

from app import config, jobs, thumbs
from app.ai import models
from app.data import index, vdb

try:
    import numpy as np
except ImportError:  # optional: the AI packages
    np = None

log = logging.getLogger(__name__)
SIDE = 384
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)
MIN_FACE = 48          # pixels (full size): smaller faces say too little about age
PER_PERSON = 60        # dated faces sampled per person, spread over the years
FIRST_REAL_YEAR = 1972  # 1970/1974 hold placeholder dates in many libraries; older dated photos are rarely dated right
progress = {"running": False, "done": 0, "total": 0}
_run = {"gen": 0}
_lock = threading.Lock()
_session = {}


def available() -> bool:
    if np is None or not models.path("mivolo").exists():
        return False
    import importlib.util
    return importlib.util.find_spec("onnxruntime") is not None


def reset_session():
    with _lock:
        _session.clear()


def _sess():
    import onnxruntime as ort
    with _lock:
        threads = jobs.threads()
        s = _session.get("s")
        if s is None or s[1] != threads:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = threads
            s = (ort.InferenceSession(str(models.path("mivolo")), sess_options=opts, providers=["CPUExecutionProvider"]), threads)
            _session["s"] = s
        return s[0]


def _letterbox(im):
    from PIL import Image
    s = SIDE / max(im.size)
    im = im.convert("RGB").resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (SIDE, SIDE))
    canvas.paste(im, ((SIDE - im.width) // 2, (SIDE - im.height) // 2))
    return canvas


def _norm(im):
    a = np.asarray(im, dtype=np.float32) / 255.0
    return ((a - np.array(MEAN, np.float32)) / np.array(STD, np.float32)).transpose(2, 0, 1)


def estimate(face_crops: list) -> list:
    """Ages in years for PIL face crops (MiVOLO without a body crop: a black one, as MiVOLO itself does)."""
    sess = _sess()
    body = _norm(np.zeros((SIDE, SIDE, 3), np.uint8))
    out = []
    for crop in face_crops:  # the export takes one face at a time
        x = np.concatenate([_norm(_letterbox(crop)), body])[None].astype(np.float32)
        out.append(round(float(sess.run(None, {sess.get_inputs()[0].name: x})[0].reshape(-1)[0]), 2))
    return out


# --------------------------------------------------------------------------
# which faces to estimate
# --------------------------------------------------------------------------

def _wanted(con) -> list:
    """[(photo id, path, w, h, x, y, fw, fh)]: every named face in a photo whose date is unknown, and for each
    person among them up to PER_PERSON of their faces in dated photos, spread evenly over the years."""
    undated = con.execute(
        "SELECT f.photo_id, p.path, p.w, p.h, f.x, f.y, f.w, f.h, f.tag_id FROM faces f JOIN photos p ON p.id = f.photo_id "
        "WHERE f.kind = 'confirmed' AND p.dprec = 3 AND p.video = 0 AND f.w >= ? AND f.h >= ?", (MIN_FACE, MIN_FACE)).fetchall()
    out = [r[:8] for r in undated]
    for tag in sorted({r[8] for r in undated}):
        dated = con.execute(
            "SELECT f.photo_id, p.path, p.w, p.h, f.x, f.y, f.w, f.h FROM faces f JOIN photos p ON p.id = f.photo_id "
            "WHERE f.kind = 'confirmed' AND f.tag_id = ? AND p.dprec IS NULL AND p.video = 0 AND p.year >= ? AND f.w >= ? AND f.h >= ? "
            "ORDER BY p.ts", (tag, FIRST_REAL_YEAR, MIN_FACE, MIN_FACE)).fetchall()
        if len(dated) > PER_PERSON:
            step = len(dated) / PER_PERSON
            dated = [dated[int(i * step)] for i in range(PER_PERSON)]
        out += dated
    seen, uniq = set(), []
    for r in out:
        k = (r[0], r[4], r[5], r[6], r[7])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def _have() -> set:
    con = vdb.connect()
    try:
        return {tuple(r) for r in con.execute("SELECT photo_id, x, y, w, h FROM ai_face_ages")}
    finally:
        con.close()


def coverage() -> tuple:
    """(faces with an age, faces wanted) for the Maintenance row."""
    try:
        con = index.open_index()
        try:
            want = {(r[0], r[4], r[5], r[6], r[7]) for r in _wanted(con)}
        finally:
            con.close()
        return len(want & _have()), len(want)
    except Exception:
        return 0, 0


def _job(gen: int):
    con = index.open_index()
    try:
        want = _wanted(con)
    finally:
        con.close()
    have = _have()
    todo = [r for r in want if (r[0], r[4], r[5], r[6], r[7]) not in have]
    progress.update(running=bool(todo), done=0, total=len(todo))
    if not todo:
        return
    by_photo = {}
    for r in todo:
        by_photo.setdefault(r[0], []).append(r)
    try:
        for pid, faces in by_photo.items():
            if gen != _run["gen"]:
                return
            _, path, pw, ph = faces[0][:4]
            try:
                im, _ = thumbs._open_oriented(path, 2400, 2400)
            except OSError as e:
                log.warning("ages: cannot read %s (%s)", path, e)
                progress["done"] += len(faces)
                continue
            except Exception as e:  # undecodable: recorded as no age, so it is not tried again
                log.warning("ages: cannot decode %s (%s)", path, e)
                ages = [None] * len(faces)
            else:
                sx, sy = im.width / max(pw or im.width, 1), im.height / max(ph or im.height, 1)
                crops = [im.crop((int(x * sx), int(y * sy), int((x + w) * sx), int((y + h) * sy))) for _p, _q, _w, _h, x, y, w, h in faces]
                try:
                    ages = estimate(crops)
                except Exception as e:
                    log.warning("ages: estimate failed on %s (%s)", path, e)
                    progress["done"] += len(faces)
                    continue
            if gen != _run["gen"]:
                return
            db = vdb.connect()
            try:
                db.executemany("INSERT OR REPLACE INTO ai_face_ages (photo_id, x, y, w, h, age) VALUES (?,?,?,?,?,?)",
                               [(pid, f[4], f[5], f[6], f[7], a) for f, a in zip(faces, ages)])
                db.commit()
            finally:
                db.close()
            progress["done"] += len(faces)
    finally:
        progress["running"] = False


def start():
    """Estimate the ages still missing (new undated photos, newly named faces)."""
    if not config.ANALYSIS or not available():
        return
    _run["gen"] += 1
    gen = _run["gen"]
    jobs.submit("ages", lambda: _job(gen))


def reset():
    _run["gen"] += 1
    con = vdb.connect()
    try:
        con.execute("DELETE FROM ai_face_ages")
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------
# birth years the user entered
# --------------------------------------------------------------------------

def births() -> dict:
    con = vdb.connect()
    try:
        return {int(r[0]): int(r[1]) for r in con.execute("SELECT tag_id, birth_year FROM person_births")}
    finally:
        con.close()


def set_birth(tag: int, year):
    con = vdb.connect()
    try:
        if year is None:
            con.execute("DELETE FROM person_births WHERE tag_id = ?", (tag,))
        else:
            con.execute("INSERT OR REPLACE INTO person_births (tag_id, birth_year, ts) VALUES (?,?,?)", (tag, int(year), int(time.time())))
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------
# the suggestions
# --------------------------------------------------------------------------

def _ages() -> dict:
    con = vdb.connect()
    try:
        return {(r[0], r[1], r[2], r[3], r[4]): r[5] for r in con.execute("SELECT photo_id, x, y, w, h, age FROM ai_face_ages WHERE age IS NOT NULL")}
    finally:
        con.close()


def calibrate(points: list, birth=None) -> dict:
    """points: [(year the photo was taken, estimated age)] of one person's dated faces. Returns
    {c0, c1, sd, n, basis, birth} with year = c0 + c1 * estimated age, or None when nothing can be said:
      fit     eight or more photos over at least four years: a straight line, its slope kept within 0.6-1.8
      dated   three or more: slope 1, the median offset (their birth year as the model sees them)
      entered the birth year the user gave, when there are too few dated photos for a line (it beats an offset
              from a handful of photos)."""
    n = len(points)
    if n >= 8 and max(p[0] for p in points) - min(p[0] for p in points) >= 4:
        ys, xs = np.array([p[0] for p in points], float), np.array([p[1] for p in points], float)
        if xs.std() > 1e-6:
            c1 = float(np.clip(np.polyfit(xs, ys, 1)[0], 0.6, 1.8))
            c0 = float(np.median(ys - c1 * xs))
            res = ys - (c0 + c1 * xs)
            sd = max(1.0, 1.4826 * float(np.median(np.abs(res - np.median(res)))))
            return {"c0": c0, "c1": c1, "sd": sd, "n": n, "basis": "fit", "birth": birth if birth else round(float(np.median(ys - xs))),
                    "entered": bool(birth), "amin": float(xs.min()), "amax": float(xs.max())}
    if n >= 3 and not birth:
        off = [y - a for y, a in points]
        c0 = float(np.median(off))
        sd = max(1.5, 1.4826 * float(np.median([abs(o - c0) for o in off])))
        return {"c0": c0, "c1": 1.0, "sd": sd, "n": n, "basis": "dated", "birth": round(c0), "entered": False}
    if birth:
        return {"c0": float(birth), "c1": 1.0, "sd": 4.0, "n": n, "basis": "entered", "birth": birth, "entered": True}
    return None


def year_for(c: dict, age: float) -> tuple:
    """(year, uncertainty) for one face of a calibrated person. Beyond the ages the line was fitted on (a
    childhood scan of someone photographed only as an adult) the line's slope is not trusted: younger than
    that, an entered birth year anchors it (birth + age); otherwise a year per year of age from the nearest
    end, and the uncertainty grows with the distance. Never before the person was born: an entered birth
    year is a hard floor, the birth year the photos imply a soft one (two years of slack)."""
    lo, hi = c.get("amin"), c.get("amax")
    if lo is None or lo <= age <= hi:
        year, sd = c["c0"] + c["c1"] * age, c["sd"]
    elif age < lo and c.get("entered") and c["basis"] == "fit":
        year, sd = c["birth"] + age, max(c["sd"], 2.0) + 0.1 * (lo - age)
    else:
        edge = lo if age < lo else hi
        year, sd = c["c0"] + c["c1"] * edge + (age - edge), c["sd"] + 0.2 * abs(age - edge)
    if c.get("birth") is not None:
        year = max(year, c["birth"] if c.get("entered") else c["birth"] - 2)
    return year, sd


def suggestions(ids: list = None) -> dict:
    """{"items": [{id, year, lo, hi, people: [{tag, name, age, year}]}], "people": [{tag, name, birth, entered, basis, n}]}
    for the photos marked "Date unknown" (or those of `ids`), best supported first. Computed on request from the
    stored ages; a photo none of whose named faces has a calibrated person gets no item."""
    if np is None:
        return {"items": [], "people": []}
    ages, entered = _ages(), births()
    con = index.open_index()
    try:
        names = {r[0]: r[1] for r in con.execute("SELECT id, name FROM tags WHERE person = 1")}
        undated = con.execute(
            "SELECT f.photo_id, f.tag_id, f.x, f.y, f.w, f.h FROM faces f JOIN photos p ON p.id = f.photo_id "
            "WHERE f.kind = 'confirmed' AND p.dprec = 3 AND p.video = 0").fetchall()
        if ids is not None:
            want = set(ids)
            undated = [r for r in undated if r[0] in want]
        tags = sorted({r[1] for r in undated})
        cal = {}
        for tag in tags:
            pts = [(ts / 31557600 + 1970, ages[(pid, x, y, w, h)]) for pid, x, y, w, h, ts in con.execute(
                "SELECT f.photo_id, f.x, f.y, f.w, f.h, p.ts FROM faces f JOIN photos p ON p.id = f.photo_id "
                "WHERE f.kind = 'confirmed' AND f.tag_id = ? AND p.dprec IS NULL AND p.year >= ?", (tag, FIRST_REAL_YEAR))
                if (pid, x, y, w, h) in ages]
            cal[tag] = calibrate(pts, entered.get(tag))
    finally:
        con.close()
    this_year = time.localtime().tm_year
    per_photo = {}
    for pid, tag, x, y, w, h in undated:
        c, age = cal.get(tag), ages.get((pid, x, y, w, h))
        if c is None or age is None:
            continue
        yr, sd = year_for(c, age)
        per_photo.setdefault(pid, []).append({"tag": tag, "name": names.get(tag, ""), "age": round(age), "year": yr, "sd": sd,
                                              "floor": c["birth"] if c.get("entered") else None})
    items = []
    for pid, est in per_photo.items():
        w = [1 / (e["sd"] ** 2) for e in est]
        mean = sum(e["year"] * k for e, k in zip(est, w)) / sum(w)
        sd = max(1.0, 1 / math.sqrt(sum(w)))
        spread = max(e["year"] for e in est) - min(e["year"] for e in est)
        half = max(1, round(max(1.5 * sd, spread / 2)))
        floor = max([e["floor"] for e in est if e["floor"] is not None], default=None)  # nobody before their entered birth year
        year = min(this_year, round(mean))
        lo = year - half
        hi = min(this_year, year + half)
        if floor is not None and lo < floor:  # pushed up by a birth year: the range moves up with it, as wide as before
            year, lo, hi = max(year, floor), floor, min(this_year, floor + (hi - lo))
        items.append({"id": pid, "year": year, "lo": lo, "hi": max(year, hi),
                      "people": [{"tag": e["tag"], "name": e["name"], "age": e["age"], "year": round(e["year"])} for e in sorted(est, key=lambda e: e["name"])]})
    items.sort(key=lambda i: (i["hi"] - i["lo"], -len(i["people"]), i["id"]))
    people = [{"tag": t, "name": names.get(t, ""), "birth": c["birth"] if c else entered.get(t), "entered": t in entered,
               "basis": c["basis"] if c else None, "n": c["n"] if c else 0} for t, c in cal.items()]
    people.sort(key=lambda p: p["name"].lower())
    return {"items": items, "people": people}
