"""Turns filter parameters (the same ones the web UI puts in its URLs) into SQL
against the app's index. Shared by the web API and the MCP server."""
import re

SORTS = {
    "new": "ts DESC, id DESC", "old": "ts ASC, id ASC", "added": "mts DESC, id DESC",
    "name": "name COLLATE NOCASE, id", "size": "size DESC, id", "rating": "rating DESC, ts DESC, id DESC",
    "sharp": "sharpn DESC, id", "blur": "sharpn IS NULL, sharpn ASC, id", "burst": "burst, burst_best DESC, ts, name",
}
WORD_RE = re.compile(r"\w+")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fts_query(text: str):
    words = WORD_RE.findall(text or "")
    return " ".join(f'"{w}"*' for w in words[:12]) if words else None


def _ints(values):
    out = []
    for v in values:
        for part in str(v).split(","):
            try:
                out.append(int(part))
            except ValueError:
                pass
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_where(items) -> tuple[str, list]:
    """items: iterable of (key, value) pairs from the query string."""
    multi: dict[str, list[str]] = {}
    for k, v in items:
        if v != "":
            multi.setdefault(k, []).append(v)
    one = lambda k: multi.get(k, [None])[0]
    where, params = [], []

    # q: the text index, plus (qids, aligned with q, filled in by main.resolve) the photos the
    # content model labels with a matching concept, so "audience" finds what Detected content shows
    qids = multi.get("qids", [])
    for i, text in enumerate(multi.get("q", [])):
        fq = fts_query(text)
        extra = [x for x in (_ints([qids[i]]) if i < len(qids) else []) if x > 0]  # "0": no concept photos
        clauses = []
        if fq:
            clauses.append("id IN (SELECT rowid FROM fts WHERE fts MATCH ?)")
            params.append(fq)
        if extra:
            clauses.append(f"id IN ({','.join(map(str, extra[:50000]))})")
        if clauses:
            where.append("(" + " OR ".join(clauses) + ")")
    ids = _ints(multi.get("ids", []))
    if "ids" in multi:  # an explicit selection (ints only, so safe to inline)
        where.append(f"id IN ({','.join(map(str, ids[:50000])) or 'NULL'})")
    if one("fav") == "1":  # favorites and the viewer's albums live in the viewer database (SQLite or MySQL): inlined as ids
        from app.data import state
        where.append(f"id IN ({','.join(map(str, state.favorite_ids()[:50000])) or 'NULL'})")
    ualbums = _ints(multi.get("ualbum", []))
    if ualbums:
        from app.data import state
        where.append(f"id IN ({','.join(map(str, state.album_item_ids(ualbums)[:50000])) or 'NULL'})")
    if one("blurry") == "1":   # the least sharp tenth of the library
        where.append("sharpn < 0.1")
    if one("sharpest") == "1":
        where.append("sharpn >= 0.8")
    if one("exposure") == "dark":
        where.append("(bright < 0.2 OR clip_lo > 0.5)")
    elif one("exposure") == "bright":
        where.append("(bright > 0.8 OR clip_hi > 0.3)")
    if one("hastext") == "1":
        where.append("ocr IS NOT NULL AND length(ocr) >= 3")
    if one("bursts") == "1":
        where.append("burst IS NOT NULL")
    if one("bestonly") == "1":  # collapse each burst to its best frame
        where.append("(burst IS NULL OR burst_best = 1)")
    for b in _ints(multi.get("burst", [])):
        where.append("burst = ?")
        params.append(b)
    for obj in multi.get("obj", []):
        where.append("objects LIKE ?")
        params.append(f"%,{obj},%")
    if one("npeople") is not None:
        v = one("npeople")
        if v.endswith("+") and v[:-1].isdigit():
            where.append("npeople >= ?")
            params.append(int(v[:-1]))
        elif v.isdigit():
            where.append("npeople = ?")
            params.append(int(v))
    if one("faces") in ("unknown", "suggested", "ignored", "confirmed"):  # photos with faces of that kind
        where.append("id IN (SELECT photo_id FROM faces WHERE kind = ?)")
        params.append(one("faces"))
    if one("edited") == "1":
        where.append("edited = 1")
    if one("dupes") == "1":
        where.append("(hash, size) IN (SELECT hash, size FROM photos WHERE hash IS NOT NULL AND hash != '' "
                     "GROUP BY hash, size HAVING count(*) > 1)")
    for tid in _ints(multi.get("tag", [])):  # every tag must match; child tags count
        where.append("id IN (SELECT photo_id FROM photo_tags WHERE tag_id IN "
                     "(SELECT tag FROM tag_closure WHERE anc = ?))")
        params.append(tid)
    for tid in _ints(multi.get("nottag", [])):
        where.append("id NOT IN (SELECT photo_id FROM photo_tags WHERE tag_id IN "
                     "(SELECT tag FROM tag_closure WHERE anc = ?))")
        params.append(tid)
    albums = _ints(multi.get("album", []))
    if albums:
        where.append(f"album_id IN (SELECT album FROM album_closure WHERE anc IN ({','.join('?' * len(albums))}))")
        params += albums
    for key, col in (("camera", "camera"), ("lens", "lens"), ("fmt", "fmt"), ("creator", "creator"),
                     ("place", "place"), ("region", "region"), ("country", "country")):
        vals = multi.get(key)
        if vals:
            parts = []
            for v in vals:
                if v == "~":
                    parts.append(f"{col} IS NULL")
                else:
                    parts.append(f"{col} = ?")
                    params.append(v)
            where.append("(" + " OR ".join(parts) + ")")
    for key, col in (("pick", "pick"), ("color", "color"), ("year", "year"), ("month", "month")):
        vals = _ints(multi.get(key, []))
        if vals:
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            params += vals
    if one("from") and DATE_RE.match(one("from")):
        where.append("day >= ?")
        params.append(one("from"))
    if one("to") and DATE_RE.match(one("to")):
        where.append("day <= ?")
        params.append(one("to"))
    if one("md") and re.match(r"^\d\d-\d\d$", one("md")):
        where.append("md = ?")
        params.append(one("md"))
    if _num(one("rating")) is not None:
        where.append("rating >= ?")
        params.append(int(_num(one("rating"))))
    if one("type") == "video":
        where.append("video = 1")
    elif one("type") == "photo":
        where.append("video = 0")
    if one("geo") == "1":
        where.append("lat IS NOT NULL")
    elif one("geo") == "0":
        where.append("lat IS NULL")
    if one("untagged") == "1":
        where.append("ntags = 0")
    if one("bbox"):
        b = [_num(x) for x in one("bbox").split(",")]
        if len(b) == 4 and None not in b:
            s, w, n, e = b
            where.append("lat BETWEEN ? AND ?")
            params += [s, n]
            if w <= e:
                where.append("lon BETWEEN ? AND ?")
                params += [w, e]
            else:  # box crosses the antimeridian
                where.append("(lon >= ? OR lon <= ?)")
                params += [w, e]
    for key, col in (("iso", "iso"), ("ap", "aperture"), ("fl", "coalesce(focal35, focal)"), ("exp", "exposure")):
        lo, hi = _num(one(key + "_min")), _num(one(key + "_max"))
        if lo is not None:
            where.append(f"{col} >= ?")
            params.append(lo)
        if hi is not None:
            where.append(f"{col} <= ?")
            params.append(hi)
    return (" AND ".join(where) or "1"), params
