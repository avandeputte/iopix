"""Turns filter parameters (the same ones the web UI puts in its URLs) into SQL
against the app's index. Shared by the web API and the MCP server."""
import re

SORTS = {
    "new": "ts DESC, id DESC", "old": "ts ASC, id ASC", "added": "mts DESC, id DESC",
    "name": "name COLLATE NOCASE, id", "size": "size DESC, id", "rating": "rating DESC, ts DESC, id DESC",
    "sharp": "sharpn DESC, id", "blur": "sharpn IS NULL, sharpn ASC, id", "burst": "burst, burst_best DESC, ts, name",
    "looks": "aesth IS NULL, aesth DESC, id",
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


class _Where:
    """The clauses collected so far, and the query-string values they are built from."""

    def __init__(self, items):
        self.multi: dict[str, list[str]] = {}
        for k, v in items:
            if v != "":
                self.multi.setdefault(k, []).append(v)
        self.where, self.params = [], []

    def one(self, key):
        return self.multi.get(key, [None])[0]

    def all(self, key):
        return self.multi.get(key, [])

    def add(self, sql, *params):
        self.where.append(sql)
        self.params += params

    def ids(self, ids):
        """An explicit list of photo ids (ints only, so safe to inline)."""
        self.add(f"id IN ({','.join(map(str, ids[:50000])) or 'NULL'})")


def _text(w):
    """q: the text index, plus (qids, aligned with q, filled in by main.resolve) the photos the
    content model labels with a matching concept, so "audience" finds what Detected content shows."""
    qids = w.all("qids")
    for i, text in enumerate(w.all("q")):
        fq = fts_query(text)
        extra = [x for x in (_ints([qids[i]]) if i < len(qids) else []) if x > 0]  # "0": no concept photos
        clauses, params = [], []
        if fq:
            clauses.append("id IN (SELECT rowid FROM fts WHERE fts MATCH ?)")
            params.append(fq)
        if extra:
            clauses.append(f"id IN ({','.join(map(str, extra[:50000]))})")
        if clauses:
            w.add("(" + " OR ".join(clauses) + ")", *params)


def _selection(w):
    if "ids" in w.multi:  # an explicit selection
        w.ids(_ints(w.all("ids")))


def _favorites(w):
    """Favorites and the viewer's albums live in the viewer database (SQLite or MySQL): inlined as ids."""
    if w.one("fav") == "1":
        from app.data import state
        w.ids(state.favorite_ids())


def _viewer_albums(w):
    ualbums = _ints(w.all("ualbum"))
    if ualbums:
        from app.data import state
        w.ids(state.album_item_ids(ualbums))


def _flag(key, clauses: dict):
    """A switch whose value picks one fixed clause: blurry=1, exposure=dark, type=video..."""
    def f(w):
        sql = clauses.get(w.one(key))
        if sql:
            w.add(sql)
    return f


def _each(key, sql, allowed=None, cast=None):
    """One clause per value of a repeatable parameter: reject=blur&reject=eyes, tag=3&tag=9..."""
    def f(w):
        for v in (_ints(w.all(key)) if cast is int else w.all(key)):
            if allowed is None or v in allowed:
                w.add(sql, v if cast is None or cast is int else cast(v))
    return f


def _people_count(w):
    v = w.one("npeople")
    if v is None:
        return
    if v.endswith("+") and v[:-1].isdigit():
        w.add("npeople >= ?", int(v[:-1]))
    elif v.isdigit():
        w.add("npeople = ?", int(v))


def _faces(w):
    if w.one("faces") in ("unknown", "suggested", "ignored", "confirmed"):  # photos with faces of that kind
        w.add("id IN (SELECT photo_id FROM faces WHERE kind = ?)", w.one("faces"))


def _albums(w):
    albums = _ints(w.all("album"))
    if albums:
        w.add(f"album_id IN (SELECT album FROM album_closure WHERE anc IN ({','.join('?' * len(albums))}))", *albums)


def _any_of_text(key, col):
    """camera=X&camera=Y: any of the values; "~" stands for no value at all."""
    def f(w):
        vals = w.all(key)
        if not vals:
            return
        parts, params = [], []
        for v in vals:
            if v == "~":
                parts.append(f"{col} IS NULL")
            else:
                parts.append(f"{col} = ?")
                params.append(v)
        w.add("(" + " OR ".join(parts) + ")", *params)
    return f


def _any_of_int(key, col):
    def f(w):
        vals = _ints(w.all(key))
        if vals:
            w.add(f"{col} IN ({','.join('?' * len(vals))})", *vals)
    return f


def _date(key, sql, pattern=DATE_RE):
    def f(w):
        v = w.one(key)
        if v and pattern.match(v):
            w.add(sql, v)
    return f


def _rating(w):
    if _num(w.one("rating")) is not None:
        w.add("rating >= ?", int(_num(w.one("rating"))))


def _bbox(w):
    if not w.one("bbox"):
        return
    b = [_num(x) for x in w.one("bbox").split(",")]
    if len(b) != 4 or None in b:
        return
    s, west, n, e = b
    w.add("lat BETWEEN ? AND ?", s, n)
    if west <= e:
        w.add("lon BETWEEN ? AND ?", west, e)
    else:  # box crosses the antimeridian
        w.add("(lon >= ? OR lon <= ?)", west, e)


def _range(key, col):
    """iso_min / iso_max and the like."""
    def f(w):
        lo, hi = _num(w.one(key + "_min")), _num(w.one(key + "_max"))
        if lo is not None:
            w.add(f"{col} >= ?", lo)
        if hi is not None:
            w.add(f"{col} <= ?", hi)
    return f


# every filter the query string can carry, in the order their clauses appear in the SQL
FILTERS = [
    _text, _selection, _favorites, _viewer_albums,
    _flag("blurry", {"1": "sharpn < 0.1"}),  # the least sharp tenth of the library
    _flag("sharpest", {"1": "sharpn >= 0.8"}),
    _flag("exposure", {"dark": "(bright < 0.2 OR clip_lo > 0.5)", "bright": "(bright > 0.8 OR clip_hi > 0.3)"}),
    _flag("rejects", {"1": "reject IS NOT NULL"}),  # photo quality: suggested rejects (index.likely_rejects), any reason or one
    _each("reject", "(',' || reject || ',') LIKE ?", allowed=("blur", "eyes", "burst", "exposure"), cast=lambda v: f"%,{v},%"),
    _flag("looks", {"top": "aesthn >= 0.9"}),  # the best-looking tenth by the aesthetic score
    _flag("eyes", {"closed": "eyes_closed > 0"}),  # someone's eyes closed (even when most faces are fine)
    _flag("hastext", {"1": "ocr IS NOT NULL AND length(ocr) >= 3"}),
    _flag("bursts", {"1": "burst IS NOT NULL"}),
    _flag("bestonly", {"1": "(burst IS NULL OR burst_best = 1)"}),  # collapse each burst to its best frame
    _each("burst", "burst = ?", cast=int),
    _each("obj", "objects LIKE ?", cast=lambda v: f"%,{v},%"),
    _people_count, _faces,
    _flag("edited", {"1": "edited = 1"}),
    _flag("dupes", {"1": "(hash, size) IN (SELECT hash, size FROM photos WHERE hash IS NOT NULL AND hash != '' "
                         "GROUP BY hash, size HAVING count(*) > 1)"}),
    # every tag must match; child tags count
    _each("tag", "id IN (SELECT photo_id FROM photo_tags WHERE tag_id IN (SELECT tag FROM tag_closure WHERE anc = ?))", cast=int),
    _each("nottag", "id NOT IN (SELECT photo_id FROM photo_tags WHERE tag_id IN (SELECT tag FROM tag_closure WHERE anc = ?))", cast=int),
    _albums,
    *(_any_of_text(k, k) for k in ("camera", "lens", "fmt", "creator", "place", "region", "country")),
    *(_any_of_int(k, k) for k in ("pick", "color", "year", "month")),
    _date("from", "day >= ?"), _date("to", "day <= ?"), _date("md", "md = ?", re.compile(r"^\d\d-\d\d$")),
    _rating,
    _flag("type", {"video": "video = 1", "photo": "video = 0"}),
    _flag("geo", {"1": "lat IS NOT NULL", "0": "lat IS NULL"}),
    _flag("untagged", {"1": "ntags = 0"}),
    _bbox,
    _range("iso", "iso"), _range("ap", "aperture"), _range("fl", "coalesce(focal35, focal)"), _range("exp", "exposure"),
]


def build_where(items) -> tuple[str, list]:
    """items: iterable of (key, value) pairs from the query string."""
    w = _Where(items)
    for f in FILTERS:
        f(w)
    return (" AND ".join(w.where) or "1"), w.params
