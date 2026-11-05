"""Who is in a photo, from what they wear: people matched by clothing within one event.

At a game, a party or a day out, each person wears the same clothes all day. Object detection (detector.py)
gives the people in each photo as boxes; the content-search image model turns each person box into an
embedding, which mostly describes the clothes, build and surroundings of that person. Within one event
(the photos of one folder on one day) the boxes that look alike are grouped. A group with a named face in
it is that person; the group's other boxes, from behind, too far or too small for a face, suggest that
the person is in those photos too: "probably Sam, dressed as in 6 photos where the face is named".

A suggestion adds the person's tag to the photo when the user clicks it, as a person present without a face
region, and is written nowhere before that. The embeddings are kept in the viewer database (pix_ai_person_looks,
keyed by the person box like the jersey numbers) and are computed again when the search model changes.

The job's bookkeeping (progress, the start rule, reset, coverage) is the shared ScanJob (scanjob.py); this module
keeps the to-do query, the embedding loop and the stamp rule (the file's stamp and the model's name, in pix_ai_scans).
"""
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from app import jobs, thumbs
from app.ai import detector, dismissed
from app.ai.analysis import stamp
from app.ai.scanjob import ScanJob
from app.data import index, vdb
from app.digikam.dkdb import chunked, in_chunks

try:
    import numpy as np
except ImportError:  # optional: the AI packages
    np = None
from app.ai.semantic import enabled, semantic

log = logging.getLogger(__name__)
MIN_BOX = 0.015      # of the frame
MIN_HEIGHT = 0.1
MAX_BOXES = 12
MIN_SIM = 0.78       # an unnamed person box must look at least this much like one of the person's named boxes
MARGIN = 0.05        # and lead the next-best person by this much (on named faces, the lead alone kept 9 in 10 right)
ALONE = 0.88         # with a single person named in the event there is nobody to lead: a high bar instead


def available() -> bool:
    return np is not None and enabled()


def _engine():
    return semantic.active


def _model_key() -> str:
    try:
        return _engine().name
    except Exception:
        return ""


def _todo(con, photos: list) -> list:
    """Still photos with people detected whose boxes are not embedded with the current model, or whose file changed."""
    model = _model_key()
    have = {r[0]: r[1] for r in con.execute("SELECT photo_id, stamp FROM pix_ai_scans WHERE job = 'clothing'")}
    objects = {r[0] for r in con.execute("SELECT photo_id FROM pix_ai_scans WHERE job = 'objects'")}
    return [p for p in photos if not p["video"] and p["id"] in objects and have.get(p["id"]) != f"{stamp(p)}:{model}"]


def _counts(con) -> tuple:
    """(photos embedded with the current model, photos with object detection)."""
    model = _model_key()
    return (con.execute("SELECT count(*) FROM pix_ai_scans WHERE job = 'clothing' AND stamp LIKE ?", (f"%:{model}",)).fetchone()[0],
            con.execute("SELECT count(*) FROM pix_ai_scans WHERE job = 'objects'").fetchone()[0])


def crops(im, boxes: list) -> list:
    """The person crops of one upright image, each padded a little, for the image model."""
    W, H = im.size
    out = []
    for x, y, w, h in boxes[:MAX_BOXES]:
        m = 0.05
        box = (max(0, int((x - m * w) * W)), max(0, int((y - m * h) * H)), min(W, int((x + w + m * w) * W)), min(H, int((y + h + m * h) * H)))
        out.append(im.crop(box))
    return out


def _work(gen: int, todo: list):
    engine = _engine()
    model = engine.name
    with ThreadPoolExecutor(jobs.batch_threads(), thread_name_prefix="clothing") as pool:
        for chunk in chunked(todo, 8):
            if progress.cancelled(gen):
                return
            boxes = detector.person_boxes([p["id"] for p in chunk], MIN_BOX, MIN_HEIGHT)

            def prepare(p):
                bs = boxes.get(p["id"], [])
                if not bs:
                    return p, [], []
                try:
                    im, _ = thumbs.open_oriented(p["path"], 1600, 1600)
                except OSError as e:
                    log.warning("clothing: cannot read %s (%s)", p["path"], e)
                    return None
                except Exception as e:
                    log.warning("clothing: cannot decode %s (%s)", p["path"], e)
                    return p, [], []
                return p, bs[:MAX_BOXES], crops(im, bs)
            ready = [r for r in pool.map(prepare, chunk) if r]
            flat = [(p, b, c) for p, bs, cs in ready for b, c in zip(bs, cs)]
            vecs = []
            if flat:
                try:
                    vecs = engine.embed_images([c for _p, _b, c in flat])
                except Exception as e:
                    log.warning("clothing: embedding failed on a batch (%s)", e)
                    continue
            if progress.cancelled(gen):
                return
            with vdb.writer() as con:
                con.executemany("DELETE FROM pix_ai_person_looks WHERE photo_id = ?", [(p["id"],) for p, _bs, _cs in ready])
                con.executemany("INSERT OR REPLACE INTO pix_ai_person_looks (photo_id, px, py, pw, ph, vec) VALUES (?,?,?,?,?,?)",
                                [(p["id"], detector.thousandths(b[0]), detector.thousandths(b[1]), detector.thousandths(b[2]), detector.thousandths(b[3]), np.asarray(v, np.float32).tobytes()) for (p, b, _c), v in zip(flat, vecs)])
                con.executemany("INSERT OR REPLACE INTO pix_ai_scans VALUES (?,'clothing',?,NULL)", [(p["id"], f"{stamp(p)}:{model}") for p, _bs, _cs in ready])
            progress.step(len(chunk))


job = ScanJob("clothing", _todo, _work, _counts, available, tables=("pix_ai_person_looks",))
progress, start, reset, coverage = job.progress, job.start, job.reset, job.coverage


# --------------------------------------------------------------------------
# matching within an event
# --------------------------------------------------------------------------

def _events(con, ids: list = None) -> dict:
    """{(album, day): [photo ids]} for the photos with embedded people (or those among `ids` and their event mates)."""
    if ids is not None:  # one photo's event: its folder and day, and the photos of that day with embedded people
        keys = {(r[0], r[1]) for r in in_chunks(con, "SELECT album_id, day FROM photos WHERE dprec IS NULL AND id IN ({ids})", map(int, ids))}
        events = defaultdict(list)
        for album, day in keys:
            mates = [r[0] for r in con.execute("SELECT id FROM photos WHERE album_id = ? AND day = ? AND dprec IS NULL", (album, day))]
            if not mates:
                continue
            with vdb.reader() as vcon:
                have = {r[0] for r in in_chunks(vcon, "SELECT DISTINCT photo_id FROM pix_ai_person_looks WHERE photo_id IN ({ids})", mates)}
            events[(album, day)] = [i for i in mates if i in have]
        return events
    with vdb.reader() as vcon:
        have = sorted({r[0] for r in vcon.execute("SELECT DISTINCT photo_id FROM pix_ai_person_looks")})
    meta = {}
    for pid, album, day in in_chunks(con, "SELECT id, album_id, day FROM photos WHERE dprec IS NULL AND id IN ({ids})", have):  # an approximate date has no day to make an event of
        meta[pid] = (album, day)
    events = defaultdict(list)
    for pid, key in meta.items():
        events[key].append(pid)
    return events


def _boxes_of(ids: list) -> list:
    """[(photo, (x, y, w, h), vec)] for the photos given."""
    out = []
    with vdb.reader() as con:
        for pid, px, py, pw, ph, vec in in_chunks(con, "SELECT photo_id, px, py, pw, ph, vec FROM pix_ai_person_looks WHERE photo_id IN ({ids})", ids):
            out.append((pid, (px / 1000, py / 1000, pw / 1000, ph / 1000), np.frombuffer(vec, np.float32)))
    return out


def _named_faces(con, ids: list) -> dict:
    """{photo: [(tag, cx, cy)]} with the face centres as fractions of the frame."""
    out, dims = {}, {}
    for part in chunked(ids, 500):
        chunk = ",".join(str(i) for i in part)
        for pid, w, h in con.execute(f"SELECT id, w, h FROM photos WHERE id IN ({chunk})"):
            dims[pid] = (w or 1, h or 1)
        for pid, tag, x, y, w, h in con.execute(f"SELECT photo_id, tag_id, x, y, w, h FROM faces WHERE kind = 'confirmed' AND photo_id IN ({chunk})"):
            W, H = dims.get(pid, (1, 1))
            out.setdefault(pid, []).append((tag, (x + w / 2) / W, (y + h / 2) / H))
    return out


def _tag_in_box(faces: list, box: tuple):
    x, y, w, h = box
    inside = [t for t, cx, cy in faces if x <= cx <= x + w and y <= cy <= y + 0.4 * h]
    return inside[0] if len(inside) == 1 else None


def match_event(boxes: list, faces: dict, tagged: dict, min_sim: float = MIN_SIM, margin: float = MARGIN, alone: float = ALONE) -> list:
    """Suggestions for one event: boxes [(photo, box, vec)], faces {photo: [(tag, cx, cy)]}, tagged {photo: {tags}}.
    Each unnamed box is compared with every named box of other photos; the person whose named box it is most like is
    suggested when that similarity is at least `min_sim` and leads the next person's by `margin` (or, when only one
    person is named in the event, reaches `alone`). Nothing is suggested in a photo where the person is tagged or has a
    named face already."""
    if len(boxes) < 2:
        return []
    names = [_tag_in_box(faces.get(b[0], []), b[1]) for b in boxes]
    named = [i for i, t in enumerate(names) if t is not None]
    if not named:
        return []
    m = np.stack([boxes[i][2] for i in named])
    ntags = np.array([names[i] for i in named])
    npids = np.array([boxes[i][0] for i in named])
    people = sorted(set(ntags.tolist()))
    out = []
    for i, (pid, box, vec) in enumerate(boxes):
        if names[i] is not None:
            continue
        with np.errstate(all="ignore"):
            sims = m @ vec
        sims = np.where(npids == pid, -1.0, sims)  # never a box of the same photo
        best = {t: float(sims[ntags == t].max()) for t in people}
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        tag, score = ranked[0]
        if score < 0:
            continue
        lead = score - ranked[1][1] if len(ranked) > 1 else None
        if not ((lead is not None and score >= min_sim and lead >= margin) or (lead is None and score >= alone)):
            continue
        if tag in tagged.get(pid, set()) or any(t == tag for t, _x, _y in faces.get(pid, [])):
            continue
        n = int(((ntags == tag) & (npids != pid)).sum())
        out.append({"id": pid, "tag": int(tag), "n": n, "score": round(score, 3), "lead": round(lead, 3) if lead is not None else None,
                    "box": {"x": box[0], "y": box[1], "w": box[2], "h": box[3]}})
    return out


def suggestions(ids: list = None) -> dict:
    """{"items": [{id, tag, name, n, score, box, event, text}]}: people probably present, by what they wear, surest first."""
    if np is None:
        return {"items": []}
    con = index.open_index()
    try:
        events = _events(con, ids)
        names = {r[0]: r[1] for r in con.execute("SELECT id, name FROM tags WHERE person = 1")}
        albums = {r[0]: r[1] for r in con.execute("SELECT id, name FROM albums")}
        no = dismissed.ids("clothing")
        items = []
        for (album, day), pids in events.items():
            if len(pids) < 2:
                continue
            boxes = _boxes_of(pids)
            faces = _named_faces(con, pids)
            tagged = defaultdict(set)
            for pid, tag in in_chunks(con, "SELECT photo_id, tag_id FROM photo_tags WHERE photo_id IN ({ids})", pids):
                tagged[pid].add(tag)
            seen = set()
            for s in match_event(boxes, faces, tagged):
                if s["tag"] not in names or (s["id"], s["tag"]) in seen or (ids is not None and s["id"] not in set(ids)) or (s["id"], s["tag"]) in no:
                    continue
                seen.add((s["id"], s["tag"]))
                s.update(name=names[s["tag"]], event=f"{albums.get(album, '')} · {day}".strip(" ·"),
                         text=f"dressed like {names[s['tag']]} in {s['n']} {'photo' if s['n'] == 1 else 'photos'} of {albums.get(album, 'this folder')} where the face is named")
                items.append(s)
    finally:
        con.close()
    items.sort(key=lambda i: (i["name"].lower(), -i["score"], i["id"]))
    return {"items": items}


def for_photo(photo_id: int) -> int:
    """How many people in a photo have a clothing embedding (the info panel)."""
    try:
        with vdb.reader() as con:
            return int(con.execute("SELECT count(*) FROM pix_ai_person_looks WHERE photo_id = ?", (photo_id,)).fetchone()[0])
    except Exception:
        return 0
