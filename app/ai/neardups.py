"""Near-duplicate photos: resized, re-saved, re-edited copies and burst neighbours that the
byte-identical check on the Duplicates page cannot see. Found with the content-search
embeddings already in the viewer database: every photo against every other, in blocks, pairs
above a similarity threshold kept. Runs as a background job and the pairs are stored in the
viewer database with the embedding generation they were computed for, so the page can say
whether they are current.
"""
import logging
import threading

try:
    import numpy as np
except ImportError:  # numpy comes with the search/objects extras; without it these features are simply off
    np = None

from app.data import index, state, vdb
from app import jobs
from app.ai.semantic import enabled, semantic
from app.digikam.dkdb import chunked

log = logging.getLogger("app.ai.neardups")
LEVELS = {"strict": 0.97, "normal": 0.94, "loose": 0.90}  # cosine similarity of the search embeddings
PER_PHOTO = 20
progress = {"running": False, "done": 0, "total": 0, "error": None}
_run = {"gen": 0}
_lock = threading.Lock()


def expected_gen(level: str) -> str:
    return f"{semantic.key}:{level}"


def status() -> dict:
    return {"gen": state.get_setting("near_dups_gen", ""), "level": state.get_setting("near_dups_level", "normal"),
            "progress": dict(progress), "enabled": _enabled()}


def _enabled() -> bool:
    if np is None:
        return False
    return enabled()


def start(level: str = "normal"):
    if level not in LEVELS:
        raise ValueError("level must be strict, normal or loose")
    if not _enabled():
        raise RuntimeError("Content search is not installed; near-duplicates need its embeddings")
    _run["gen"] += 1
    gen = _run["gen"]
    jobs.submit("neardups", lambda: _job(gen, level))


def _job(gen: int, level: str):
    threshold = LEVELS[level]
    eng = semantic.active
    eng.load()
    with eng.lock:
        ids, vecs = eng.ids, eng.vecs  # no copy: the engine replaces these arrays, it never writes into them
    n = len(ids)
    progress.update(running=True, done=0, total=n, error=None)
    total = 0
    try:
        state.set_setting("near_dups_gen", "")  # not current until every block is in
        con = vdb.connect()
        try:
            con.execute("DELETE FROM ai_similar_photos")
            con.commit()
            block = 256
            for s in range(0, n, block):
                if gen != _run["gen"]:
                    return
                with np.errstate(all="ignore"):  # Accelerate emits spurious float warnings on matmul
                    sims = vecs[s:s + block] @ vecs.T  # [block, n]
                pairs = []
                for r in range(sims.shape[0]):
                    i = s + r
                    row = sims[r]
                    row[: i + 1] = -1  # each pair once (j > i), never a photo with itself
                    js = np.nonzero(row >= threshold)[0]
                    if len(js) > PER_PHOTO:
                        js = js[np.argsort(-row[js])[:PER_PHOTO]]
                    for j in js.tolist():
                        pairs.append((int(ids[i]), int(ids[j]), float(row[j])))
                if pairs:  # this block's pairs in one transaction; the whole list never exists at once
                    con.executemany("INSERT INTO ai_similar_photos (a, b, score) VALUES (?,?,?)", pairs)
                    con.commit()
                total += len(pairs)
                progress["done"] = min(n, s + block)
        finally:
            con.close()
        state.set_setting("near_dups_gen", expected_gen(level))
        state.set_setting("near_dups_level", level)
        log.info("near-duplicates: %d pairs above %.2f among %d photos", total, threshold, n)
    except Exception as e:
        progress["error"] = str(e)[:200]
        log.warning("near-duplicates failed: %s", e)
    finally:
        progress["running"] = False


def groups(limit: int = 300) -> list:
    """Connected components of the stored pairs, each with its photos (index facts) and the strongest score."""
    with vdb.reader() as con:
        pairs = [tuple(r) for r in con.execute("SELECT a, b, score FROM ai_similar_photos")]
    if not pairs:
        return []
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    best = {}
    for a, b, s in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    for a, b, s in pairs:
        r = find(a)
        best[r] = max(best.get(r, 0), s)
    members = {}
    for x in list(parent):
        members.setdefault(find(x), []).append(x)
    comps = sorted(members.items(), key=lambda kv: (-len(kv[1]), -best.get(kv[0], 0)))[:limit]
    ids = [i for _, m in comps for i in m]
    facts = {}
    icon = index.open_index()
    try:
        for chunk in chunked(ids, 900):
            for r in icon.execute(f"SELECT p.id, p.name, p.w, p.h, p.video, p.ts, p.size, p.sharpn, p.hash, a.path FROM photos p JOIN albums a ON a.id = p.album_id "
                                  f"WHERE p.id IN ({','.join('?' * len(chunk))})", chunk):
                facts[r[0]] = {"id": r[0], "name": r[1], "w": r[2], "h": r[3], "video": r[4], "ts": r[5], "size": r[6], "sharpn": r[7], "hash": r[8], "album": r[9]}
    finally:
        icon.close()
    out = []
    for root, m in comps:
        items = [facts[i] for i in m if i in facts]
        if len(items) < 2:
            continue
        items.sort(key=lambda x: (-(x["sharpn"] or 0), -(x["size"] or 0)))  # the sharpest, largest copy first
        hashes = {(x["hash"], x["size"]) for x in items}
        out.append({"n": len(items), "score": round(best.get(root, 0), 3), "identical": len(hashes) == 1, "items": items})
    return out
