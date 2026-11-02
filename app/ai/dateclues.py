"""Year suggestions for photos whose date is unknown, from every clue the library holds about them.

Four kinds of clue, each giving a year with an uncertainty; the suggestion is their weighted mean, with a
range, and lists the clues it came from so the reason is visible:

  ages     how old the named people look (ages.py, MiVOLO v2 calibrated per person)
  people   the years with a date in which the same named people are photographed: together, when the
           photo shows two or more of them and they appear together in enough dated photos, else each on
           their own (one person's years span their whole life, so this clue is wide on its own)
  look     the dated photos that look most like it (the content-search embeddings): film grain, colour,
           clothes and the scene itself place a print or a scan in its years
  camera   the years the same camera was used for dated photos

A folder hint ("nothing in this folder was taken after 1992", "nor before 1975"; subfolders included) narrows
every clue to that window and keeps the suggestion inside it: the people's dated years, the look-alikes and the
camera's years outside the window are not counted, and an age estimate is clipped to it.

Everything is computed on request from the index, the stored ages and the embeddings; nothing is written.
Using a suggestion is the user's click (the date editor's year-only date through /api/edit). Hints live in the
viewer database (date_hints).
"""
import logging
import math
import statistics
import threading
import time

from app.data import index, vdb
from app.faces import ages

log = logging.getLogger(__name__)
FIRST_REAL_YEAR = ages.FIRST_REAL_YEAR
TOGETHER_MIN = 3     # dated photos of the same group of people before "together" is used instead of each person
LOOK_K = 40          # dated look-alikes considered
LOOK_MIN_SIM = 0.55  # the closest dated look-alike must be at least this similar, or the look says nothing
LOOK_SD_FLOOR = 3.0  # years: look-alikes are a soft clue
PEOPLE_SD_FLOOR = 2.0
_look_cache = {"key": None, "years": {}}
_lock = threading.Lock()


# --------------------------------------------------------------------------
# folder hints
# --------------------------------------------------------------------------

def hints() -> dict:
    """{album id: {not_before, not_after}} as entered."""
    try:
        with vdb.reader() as con:
            return {int(r[0]): {"not_before": r[1], "not_after": r[2]} for r in con.execute("SELECT album_id, not_before, not_after FROM date_hints")}
    except Exception:
        return {}


def set_hint(album_id: int, not_before=None, not_after=None):
    """Keep a folder's hint (both empty: removed)."""
    from app.photos import history
    nb = int(not_before) if not_before not in (None, "") else None
    na = int(not_after) if not_after not in (None, "") else None
    for v in (nb, na):
        if v is not None and not 1800 <= v <= time.localtime().tm_year:
            raise ValueError("A year between 1800 and this year")
    if nb is not None and na is not None and nb > na:
        raise ValueError("The earliest year is after the latest")
    with vdb.writer() as con:
        if nb is None and na is None:
            con.execute("DELETE FROM date_hints WHERE album_id = ?", (int(album_id),))
        else:
            con.execute("INSERT OR REPLACE INTO date_hints (album_id, not_before, not_after, user_name, ts) VALUES (?,?,?,?,?)",
                        (int(album_id), nb, na, history.actor(), int(time.time())))


def windows(con, albums_of: dict) -> dict:
    """{photo id: (not_before or None, not_after or None)} from the hints, the nearest folder up the tree winning per
    bound. albums_of: {photo id: album id}."""
    h = hints()
    if not h or not albums_of:
        return {}
    want = sorted(set(albums_of.values()))
    anc = {}
    for part in range(0, len(want), 500):
        chunk = want[part:part + 500]
        for album, a, depth in con.execute(
                f"SELECT c.album, c.anc, length(x.path) FROM album_closure c JOIN albums x ON x.id = c.anc WHERE c.album IN ({','.join('?' * len(chunk))})", chunk):
            anc.setdefault(album, []).append((depth, a))
    out = {}
    for pid, album in albums_of.items():
        nb = na = None
        for _d, a in sorted(anc.get(album, [(0, album)]), reverse=True):  # the deepest folder with a hint decides
            x = h.get(a)
            if not x:
                continue
            if nb is None and x["not_before"] is not None:
                nb = x["not_before"]
            if na is None and x["not_after"] is not None:
                na = x["not_after"]
        if nb is not None or na is not None:
            out[pid] = (nb, na)
    return out


def _in(year, w) -> bool:
    if w is None:
        return True
    nb, na = w
    return (nb is None or year >= nb) and (na is None or year <= na)


def _clip(year, w):
    if w is None:
        return year
    nb, na = w
    if nb is not None and year < nb:
        return nb
    if na is not None and year > na:
        return na
    return year


def _spread(years: list, floor: float) -> tuple:
    """(median year, uncertainty) of a list of years: the median, and 1.4826 x the median absolute deviation,
    never below `floor` and never below a quarter of the span (a handful of years far apart stays wide)."""
    med = round(statistics.median(years))
    mad = statistics.median(abs(y - med) for y in years)
    span = max(years) - min(years)
    return med, max(floor, 1.4826 * mad, span / 4)


def _weighted_median(values: list, weights: list) -> float:
    order = sorted(range(len(values)), key=lambda i: values[i])
    total = sum(weights)
    acc = 0.0
    for i in order:
        acc += weights[i]
        if acc >= total / 2:
            return values[i]
    return values[order[-1]]


def combine(clues: list, this_year: int = None) -> dict:
    """{year, lo, hi} from [{year, sd}]: the inverse-variance weighted mean; the range the larger of 1.5 x the
    combined uncertainty and half the spread between the clues, so clues that disagree show a wide range."""
    this_year = this_year or time.localtime().tm_year
    w = [1 / (c["sd"] ** 2) for c in clues]
    mean = sum(c["year"] * k for c, k in zip(clues, w)) / sum(w)
    sd = max(1.0, 1 / math.sqrt(sum(w)))
    spread = max(c["year"] for c in clues) - min(c["year"] for c in clues)
    half = max(1, round(max(1.5 * sd, spread / 2)))
    year = min(this_year, round(mean))
    return {"year": year, "lo": year - half, "hi": min(this_year, max(year, year + half))}


# --------------------------------------------------------------------------
# the clues
# --------------------------------------------------------------------------

def _people_clues(con, undated: dict, win: dict = None) -> dict:
    """{photo id: clue} from the dated years of the named people in each undated photo. undated: {pid: [tag ids]};
    win: {pid: (not before, not after)} from the folder hints: years outside a photo's window are not counted."""
    win = win or {}
    tags = sorted({t for ts in undated.values() for t in ts})
    if not tags:
        return {}
    names = {r[0]: r[1] for r in con.execute("SELECT id, name FROM tags WHERE person = 1")}
    by_photo = {}
    for pid, tag, year in con.execute(
            "SELECT f.photo_id, f.tag_id, p.year FROM faces f JOIN photos p ON p.id = f.photo_id "
            f"WHERE f.kind = 'confirmed' AND p.dprec IS NULL AND p.year >= ? AND f.tag_id IN ({','.join('?' * len(tags))})",
            (FIRST_REAL_YEAR, *tags)):
        by_photo.setdefault(pid, (year, set()))[1].add(tag)
    years_of = {}
    for year, ts in by_photo.values():
        for t in ts:
            years_of.setdefault(t, []).append(year)
    out = {}
    for pid, ts in undated.items():
        group = set(ts)
        w = win.get(pid)
        together = [year for year, have in by_photo.values() if group <= have and _in(year, w)] if len(group) > 1 else []
        if len(together) >= TOGETHER_MIN:
            year, sd = _spread(together, PEOPLE_SD_FLOOR)
            ppl = sorted(names.get(t, "?") for t in group)
            who = ", ".join(ppl[:-1]) + " and " + ppl[-1]
            text = f"{who} are photographed together in {min(together)}–{max(together)}" if min(together) != max(together) else f"{who} are photographed together in {year}"
            out[pid] = {"kind": "people", "year": year, "sd": sd, "n": len(together), "text": text}
            continue
        parts, ests = [], []
        for t in sorted(group):
            ys = [y for y in years_of.get(t, []) if _in(y, w)]
            if not ys:
                continue
            year, sd = _spread(ys, PEOPLE_SD_FLOOR)
            ests.append((year, sd))
            parts.append(f"{names.get(t, '?')} in {min(ys)}–{max(ys)}" if min(ys) != max(ys) else f"{names.get(t, '?')} in {min(ys)}")
        if ests:
            # each person's span is wide; where they overlap the photo most likely lies
            sub = combine([{"year": y, "sd": s} for y, s in ests], this_year=9999)
            sd = max(PEOPLE_SD_FLOOR, (sub["hi"] - sub["lo"]) / 3)
            out[pid] = {"kind": "people", "year": sub["year"], "sd": sd, "n": sum(len(years_of[t]) for t in group if t in years_of),
                        "text": "Photographed: " + "; ".join(parts)}
    return out


def _look_years(con, ids: list, win: dict = None) -> dict:
    """{photo id: (year, sd, n, top similarity)} from the dated photos that look most like each; cached per
    embeddings file and index version. win: {pid: window}: only dated photos inside a photo's window are considered."""
    win = win or {}
    try:
        from app.ai.semantic import semantic, enabled
        if not enabled():
            return {}
        semantic.load()
        from app import config
        key = (semantic.key, config.INDEX_DB.stat().st_mtime if config.INDEX_DB.exists() else None, len(semantic.ids) if semantic.ids is not None else 0)
    except Exception:
        return {}
    with _lock:
        if _look_cache["key"] != key:
            _look_cache.update(key=key, years={})
        cache = _look_cache["years"]
    ck = lambda i: (i, win.get(i))  # noqa: E731  (a photo's answer depends on its window)
    missing = [i for i in ids if ck(i) not in cache]
    if missing:
        import numpy as np
        with semantic.lock:
            sids, vecs = semantic.ids, semantic.vecs
        if sids is None or not len(sids):
            return {}
        pos = {int(i): k for k, i in enumerate(sids)}
        dated = {r[0]: r[1] for r in con.execute("SELECT id, year FROM photos WHERE dprec IS NULL AND video = 0 AND year >= ?", (FIRST_REAL_YEAR,))}
        dpos = np.array([pos[i] for i in dated if i in pos], dtype=np.int64)
        dids = np.array([i for i in dated if i in pos], dtype=np.int64)
        dyears = np.array([dated[int(i)] for i in dids], dtype=np.float64)
        for pid in missing:
            k = pos.get(pid)
            w = win.get(pid)
            keep = np.ones(len(dpos), bool) if w is None else ((dyears >= (w[0] or -1e9)) & (dyears <= (w[1] or 1e9)))
            if k is None or not keep.any():
                cache[ck(pid)] = None
                continue
            with np.errstate(all="ignore"):
                scores = (vecs[dpos] @ vecs[k])[keep]
            yrs_all = dyears[keep]
            n = min(LOOK_K, len(scores))
            top = np.argpartition(-scores, n - 1)[:n]
            top = top[np.argsort(-scores[top])]
            best = float(scores[top[0]])
            if best < LOOK_MIN_SIM:
                cache[ck(pid)] = None
                continue
            wt = np.clip(scores[top] - LOOK_MIN_SIM + 0.05, 0.01, None)
            yrs = yrs_all[top]
            med = _weighted_median(list(yrs), list(wt))
            mad = _weighted_median([abs(y - med) for y in yrs], list(wt))
            cache[ck(pid)] = (float(med), max(LOOK_SD_FLOOR, 1.4826 * float(mad)), int(n), best)
    return {i: cache[ck(i)] for i in ids if cache.get(ck(i))}


def _camera_clues(con, cams: dict, win: dict = None) -> dict:
    """{photo id: clue} for undated photos whose camera is named and used for dated photos. cams: {pid: camera}."""
    win = win or {}
    want = sorted({c for c in cams.values() if c})
    if not want:
        return {}
    years = {}
    for cam, year in con.execute(f"SELECT camera, year FROM photos WHERE dprec IS NULL AND year >= ? AND camera IN ({','.join('?' * len(want))})",
                                 (FIRST_REAL_YEAR, *want)):
        years.setdefault(cam, []).append(year)
    out = {}
    for pid, cam in cams.items():
        ys = [y for y in years.get(cam, []) if _in(y, win.get(pid))]
        if not ys or len(ys) < 3:
            continue
        lo, hi = min(ys), max(ys)
        year, sd = _spread(ys, 1.0)
        out[pid] = {"kind": "camera", "year": year, "sd": max(sd, (hi - lo) / 4, 1.0), "n": len(ys),
                    "text": f"{cam} used in {lo}–{hi}" if lo != hi else f"{cam} used in {lo}"}
    return out


# --------------------------------------------------------------------------
# putting them together
# --------------------------------------------------------------------------

def suggestions(ids: list = None) -> dict:
    """{"items": [{id, year, lo, hi, clues: [{kind, text, year, sd}], people: [...]}], "people": [...]} for the photos
    marked "Date unknown" (or those of `ids`), best supported first. `people` is the age calibration list."""
    con = index.open_index()
    try:
        rows = con.execute("SELECT id, camera, album_id FROM photos WHERE dprec = 3 AND video = 0").fetchall()
        if ids is not None:
            want = set(ids)
            rows = [r for r in rows if r[0] in want]
        pids = [r[0] for r in rows]
        cams = {r[0]: r[1] for r in rows}
        win = windows(con, {r[0]: r[2] for r in rows})
        named = {}
        for pid, tag in con.execute("SELECT f.photo_id, f.tag_id FROM faces f JOIN photos p ON p.id = f.photo_id WHERE f.kind = 'confirmed' AND p.dprec = 3 AND p.video = 0"):
            if pid in cams:
                named.setdefault(pid, []).append(tag)
        try:
            age_out = ages.suggestions(pids) if ages.available() else {"items": [], "people": []}
        except Exception as e:
            log.warning("date clues: ages unavailable (%s)", e)
            age_out = {"items": [], "people": []}
        clues = {pid: [] for pid in pids}
        for it in age_out["items"]:
            text = " · ".join(f"{p['name']} looks {p['age']}" for p in it["people"])
            clues[it["id"]].append({"kind": "ages", "year": _clip(it["year"], win.get(it["id"])), "sd": max(1.0, (it["hi"] - it["lo"]) / 3), "text": text,
                                    "people": it["people"], "floor": it.get("floor")})
        for pid, c in _people_clues(con, named, win).items():
            clues[pid].append(c)
        try:
            looks = _look_years(con, pids, win)
        except Exception as e:
            log.warning("date clues: look-alikes unavailable (%s)", e)
            looks = {}
        for pid, (year, sd, n, best) in looks.items():
            clues[pid].append({"kind": "look", "year": year, "sd": sd, "n": n, "text": f"looks like photos from about {round(year)}"})
        for pid, c in _camera_clues(con, cams, win).items():
            clues[pid].append(c)
    finally:
        con.close()
    items = []
    for pid in pids:
        cs = clues[pid]
        if not cs:
            continue
        est = combine(cs)
        age_item = next((c for c in cs if c["kind"] == "ages"), None)
        floor = age_item.get("floor") if age_item else None  # an entered birth year: never before it
        item = {"id": pid, **est, "clues": [{"kind": c["kind"], "text": c["text"], "year": round(c["year"]), "sd": round(c["sd"], 1)} for c in cs],
                "people": age_item["people"] if age_item else []}
        if floor is not None and item["lo"] < floor:
            width = item["hi"] - item["lo"]
            item["year"], item["lo"], item["hi"] = max(item["year"], floor), floor, min(time.localtime().tm_year, floor + width)
        w = win.get(pid)
        if w is not None:  # the folder hint is a hard window
            item["year"], item["lo"], item["hi"] = _clip(item["year"], w), _clip(item["lo"], w), _clip(item["hi"], w)
            nb, na = w
            item["clues"].append({"kind": "hint", "text": "folder hint: " + " and ".join(x for x in (f"not before {nb}" if nb else "", f"not after {na}" if na else "") if x), "year": item["year"], "sd": 0})
        items.append(item)
    items.sort(key=lambda i: (i["hi"] - i["lo"], -len(i["clues"]), i["id"]))
    return {"items": items, "people": age_out["people"]}
