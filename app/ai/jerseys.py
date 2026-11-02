"""Jersey numbers: read on each detected person, and matched to the people who wear them.

Object detection (detector.py) gives the people in a photo as boxes. On the upper part of each box, where a
shirt number sits, text recognition (RapidOCR, the same engine as "text in photos") reads one- or two-digit
numbers. A number read on a person whose face is named teaches which player wears it, in that folder (a
season's or a game's photos) and in that year. A number read on a person without a named face, from behind
or too small for a face, then suggests the player: "probably Sam (number 10, as in 14 photos of this folder)".

The suggestion adds the person's tag to the photo when the user clicks it (no face region: digiKam's own
"person in this photo without a face" tag), and is written nowhere before that. Numbers are kept in the
viewer database (ai_jerseys, keyed by the person box) and searchable as "jersey:10".
"""
import logging
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from app import config, jobs, thumbs
from app.data import index, vdb
from app.digikam.dkdb import chunked

log = logging.getLogger(__name__)
MIN_BOX = 0.02        # of the frame: smaller people carry no readable number
MIN_HEIGHT = 0.12     # of the frame's height
TORSO = 0.62          # the upper part of the person box that is searched
CROP_WIDTH = 320      # the crop is enlarged to at least this width for the recogniser
MIN_CONF = 0.6
MAX_BOXES = 12
NUMBER = re.compile(r"\d{1,2}")
LEARN_MIN_FOLDER = 2  # named sightings of a number in one folder before it names a player there
LEARN_MIN_YEAR = 3    # and in one year
AGREE = 0.8           # the share of sightings the leading player needs
progress = jobs.Progress("jerseys")


def available() -> bool:
    from app.ai.analysis import vision_available
    return vision_available()


def _k(v: float) -> int:
    return int(round(v * 1000))


def read_numbers(im, boxes: list, ocr) -> list:
    """[(box, number, confidence)] for the person boxes (fractions of the upright frame) with a number on their shirt."""
    out = []
    W, H = im.size
    for x, y, w, h in boxes[:MAX_BOXES]:
        crop = im.crop((int(x * W), int(y * H), int((x + w) * W), int((y + TORSO * h) * H)))
        if crop.width < 24 or crop.height < 24:
            continue
        s = CROP_WIDTH / crop.width
        if s > 1:
            crop = crop.resize((int(crop.width * s), int(crop.height * s)))
        best = None
        for text, conf in ocr(crop.convert("RGB")):
            t = text.strip().replace(" ", "")
            if conf >= MIN_CONF and NUMBER.fullmatch(t) and (best is None or conf > best[1]):
                best = (t, conf)
        if best:
            out.append(((x, y, w, h), best[0], round(float(best[1]), 3)))
    return out


def _person_boxes(ids: list) -> dict:
    out = {}
    with vdb.reader() as con:
        for part in chunked(ids, 500):
            for r in con.execute(f"SELECT photo_id, x, y, w, h FROM ai_objects WHERE cls = 'person' AND photo_id IN ({','.join(str(int(i)) for i in part)}) ORDER BY w * h DESC"):
                if r[3] * r[4] >= MIN_BOX and r[4] >= MIN_HEIGHT:
                    out.setdefault(r[0], []).append((r[1], r[2], r[3], r[4]))
    return out


def _todo(photos: list) -> list:
    """Still photos with people detected that this job has not read yet (or whose file or detection changed since)."""
    with vdb.reader() as con:
        have = {r[0]: r[1] for r in con.execute("SELECT photo_id, stamp FROM ai_scans WHERE job = 'jerseys'")}
        objects = {r[0]: r[1] for r in con.execute("SELECT photo_id, stamp FROM ai_scans WHERE job = 'objects'")}
    from app.ai.analysis import stamp
    return [p for p in photos if not p["video"] and p["id"] in objects and have.get(p["id"]) != stamp(p)]


def coverage() -> tuple:
    try:
        with vdb.reader() as con:
            done = con.execute("SELECT count(*) FROM ai_scans WHERE job = 'jerseys'").fetchone()[0]
            total = con.execute("SELECT count(*) FROM ai_scans WHERE job = 'objects'").fetchone()[0]
        return int(done), int(total)
    except Exception:
        return 0, 0


def _job(photos: list, gen: int):
    from app.ai.analysis import _ocr_engine, stamp
    ocr = _ocr_engine()
    if ocr is None:
        return
    todo = _todo(photos)
    progress.update(running=bool(todo), done=0, total=len(todo))
    if not todo:
        return
    try:
        n = max(1, min(4, jobs.threads()))
        with ThreadPoolExecutor(n, thread_name_prefix="jerseys") as pool:
            for chunk in chunked(todo, 32):
                if progress.cancelled(gen):
                    return
                boxes = _person_boxes([p["id"] for p in chunk])

                def work(p):
                    bs = boxes.get(p["id"], [])
                    if not bs:
                        return p, []
                    try:
                        im, _ = thumbs.open_oriented(p["path"], 2000, 2000)
                    except OSError as e:  # unreadable now: tried again next time
                        log.warning("jerseys: cannot read %s (%s)", p["path"], e)
                        return None
                    try:
                        return p, read_numbers(im, bs, ocr)
                    except Exception as e:
                        log.warning("jerseys: failed on %s (%s)", p["path"], e)
                        return p, []
                ready = [r for r in pool.map(work, chunk) if r]
                if progress.cancelled(gen):
                    return
                with vdb.writer() as con:
                    con.executemany("DELETE FROM ai_jerseys WHERE photo_id = ?", [(p["id"],) for p, _ in ready])
                    con.executemany("INSERT OR REPLACE INTO ai_jerseys (photo_id, px, py, pw, ph, number, score) VALUES (?,?,?,?,?,?,?)",
                                    [(p["id"], _k(b[0]), _k(b[1]), _k(b[2]), _k(b[3]), num, sc) for p, found in ready for b, num, sc in found])
                    con.executemany("INSERT OR REPLACE INTO ai_scans VALUES (?,'jerseys',?,NULL)", [(p["id"], stamp(p)) for p, _ in ready])
                progress.step(len(chunk))
    finally:
        progress.finish(gen)


def start(photos: list):
    """Read the numbers still missing. photos: index rows with id, path, video, hash, size, modified."""
    if not config.ANALYSIS or not available():
        return
    gen = progress.cancel()
    jobs.submit("jerseys", lambda: _job(photos, gen))


def reset():
    progress.cancel()
    with vdb.writer() as con:
        con.execute("DELETE FROM ai_jerseys")
        con.execute("DELETE FROM ai_scans WHERE job = 'jerseys'")


def results() -> dict:
    """{photo id: "10 7"}: the numbers read in each photo, for the index."""
    out = defaultdict(list)
    try:
        with vdb.reader() as con:
            for pid, num in con.execute("SELECT photo_id, number FROM ai_jerseys ORDER BY photo_id, score DESC"):
                if num not in out[pid]:
                    out[pid].append(num)
    except Exception as e:
        log.warning("jerseys: results unreadable (%s)", e)
    return {pid: " ".join(nums) for pid, nums in out.items()}


# --------------------------------------------------------------------------
# who wears which number
# --------------------------------------------------------------------------

def _sightings(con, rows: list) -> list:
    """[(photo, album, year, box, number, score, named tag or None)]: each number read, with the named face standing
    in that person box (its centre within the box, in the box's upper third) when there is one."""
    ids = sorted({r[0] for r in rows})
    faces, meta = {}, {}
    for part in chunked(ids, 500):
        chunk = ",".join(str(i) for i in part)
        for pid, tag, x, y, w, h in con.execute(f"SELECT photo_id, tag_id, x, y, w, h FROM faces WHERE kind = 'confirmed' AND photo_id IN ({chunk})"):
            faces.setdefault(pid, []).append((tag, x + w / 2, y + h / 2))
        for pid, album, year, pw, ph in con.execute(f"SELECT id, album_id, year, w, h FROM photos WHERE id IN ({chunk})"):
            meta[pid] = (album, year, pw or 1, ph or 1)
    out = []
    for pid, px, py, pw, ph, number, score in rows:
        album, year, W, H = meta.get(pid, (None, None, 1, 1))
        x, y, w, h = px / 1000, py / 1000, pw / 1000, ph / 1000
        inside = [t for t, cx, cy in faces.get(pid, []) if x <= cx / W <= x + w and y <= cy / H <= y + 0.35 * h]
        out.append((pid, album, year, (x, y, w, h), number, score, inside[0] if len(inside) == 1 else None))
    return out


def _roster(sightings: list) -> tuple:
    """({(album, number): tag}, {(year, number): tag}, counts) from the named sightings, where one player clearly wears it."""
    by_album, by_year = defaultdict(Counter), defaultdict(Counter)
    for pid, album, year, _box, number, _s, tag in sightings:
        if tag is None:
            continue
        by_album[(album, number)][tag] += 1
        by_year[(year, number)][tag] += 1

    def decide(table, least):
        out, counts = {}, {}
        for key, c in table.items():
            tag, n = c.most_common(1)[0]
            if n >= least and n >= AGREE * sum(c.values()):
                out[key], counts[key] = tag, n
        return out, counts
    album_map, album_n = decide(by_album, LEARN_MIN_FOLDER)
    year_map, year_n = decide(by_year, LEARN_MIN_YEAR)
    return album_map, year_map, {"album": album_n, "year": year_n}


_cache = {"key": None, "value": None}


def _all_sightings(con, rows: list) -> list:
    """_sightings over every number read, cached while neither the index nor the numbers change (the info panel asks per photo)."""
    key = (config.INDEX_DB.stat().st_mtime if config.INDEX_DB.exists() else None, len(rows), rows[-1] if rows else None)
    if _cache["key"] != key:
        _cache.update(key=key, value=_sightings(con, rows))
    return _cache["value"]


def suggestions(ids: list = None) -> dict:
    """{"items": [{id, number, tag, name, basis: folder|year, n, box: {x, y, w, h}}], "players": [{tag, name, numbers}]}:
    photos where a number is read on someone whose face is not named, and that number names a player in the same folder
    (else the same year) who is not yet tagged on the photo. Computed on request; nothing is written."""
    with vdb.reader() as vcon:
        rows = [tuple(r) for r in vcon.execute("SELECT photo_id, px, py, pw, ph, number, score FROM ai_jerseys")]
    con = index.open_index()
    try:
        sightings = _all_sightings(con, rows)
        album_map, year_map, counts = _roster(sightings)
        names = {r[0]: r[1] for r in con.execute("SELECT id, name FROM tags WHERE person = 1")}
        albums = {r[0]: r[1] for r in con.execute("SELECT id, name FROM albums")}
        candidates = [s for s in sightings if s[6] is None and (ids is None or s[0] in set(ids))]
        tagged = defaultdict(set)
        for part in chunked(sorted({s[0] for s in candidates}), 500):
            for pid, tag in con.execute(f"SELECT photo_id, tag_id FROM photo_tags WHERE photo_id IN ({','.join(str(i) for i in part)})"):
                tagged[pid].add(tag)
    finally:
        con.close()
    items, seen = [], set()
    for pid, album, year, box, number, score, _t in candidates:
        tag, basis, n = None, None, 0
        if (album, number) in album_map:
            tag, basis, n = album_map[(album, number)], "folder", counts["album"][(album, number)]
        elif (year, number) in year_map:
            tag, basis, n = year_map[(year, number)], "year", counts["year"][(year, number)]
        if tag is None or tag in tagged[pid] or (pid, tag) in seen or tag not in names:
            continue
        seen.add((pid, tag))
        where = f"in {albums.get(album, 'this folder')}" if basis == "folder" else f"in {year}"
        items.append({"id": pid, "number": number, "tag": tag, "name": names[tag], "basis": basis, "n": n,
                      "text": f"number {number}, worn by {names[tag]} in {n} named photos {where}",
                      "box": {"x": box[0], "y": box[1], "w": box[2], "h": box[3]}})
    items.sort(key=lambda i: (i["name"].lower(), -i["n"], i["id"]))
    players = defaultdict(Counter)
    for (album, number), tag in album_map.items():
        players[tag][number] += counts["album"][(album, number)]
    for (year, number), tag in year_map.items():
        players[tag][number] += counts["year"][(year, number)]
    plist = [{"tag": t, "name": names.get(t, ""), "numbers": [n for n, _ in c.most_common()]} for t, c in players.items() if t in names]
    plist.sort(key=lambda p: p["name"].lower())
    return {"items": items, "players": plist, "read": len(rows), "named": sum(1 for s in sightings if s[6] is not None)}


def for_photo(photo_id: int) -> list:
    """[{number, score, box}] read in one photo, for the info panel."""
    try:
        with vdb.reader() as con:
            return [{"number": r[4], "score": r[5], "box": {"x": r[0] / 1000, "y": r[1] / 1000, "w": r[2] / 1000, "h": r[3] / 1000}}
                    for r in con.execute("SELECT px, py, pw, ph, number, score FROM ai_jerseys WHERE photo_id = ? ORDER BY score DESC", (photo_id,))]
    except Exception:
        return []

