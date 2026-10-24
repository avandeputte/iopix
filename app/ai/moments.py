"""Bursts (near-identical frames shot in quick succession, with a best pick) and
events / trips (groupings by time and place). Pure arithmetic on data the
index already has, plus the content embeddings when they exist."""
import math
import statistics
from collections import Counter, defaultdict
from datetime import date

BURST_GAP = 2          # seconds between consecutive frames
BURST_SIMILARITY = 0.80
AWAY_KM = 75
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _similarity():
    """(id, id) -> cosine similarity from the content embeddings, or None when there are none
    (then bursts are decided by time and device alone)."""
    try:
        from app.ai.semantic import semantic, enabled
        if not enabled():
            return None
        import numpy as np
        semantic.load()
        if semantic.ids is None or not len(semantic.ids):
            return None
        with semantic._lock:  # ids and vecs as one pair (a batch being appended replaces both)
            ids, vecs = semantic.ids, semantic.vecs
        pos = {int(i): k for k, i in enumerate(ids)}
    except Exception:
        return None

    def sim(a, b):
        if a not in pos or b not in pos:
            return None
        with np.errstate(all="ignore"):
            return float(vecs[pos[a]] @ vecs[pos[b]])
    return sim


def find_bursts(rows: list) -> dict:
    """rows: [(id, ts, name, device key, sharp, faceq, video)].
    Returns {photo id: (burst number, 1 if it is the best frame else 0)}."""
    vec_of = _similarity()
    photos = sorted((r for r in rows if not r[6]), key=lambda r: (r[3], r[1], r[2]))
    groups, cur = [], []
    for r in photos:
        if cur:
            prev = cur[-1]
            close = r[3] == prev[3] and 0 <= r[1] - prev[1] <= BURST_GAP
            if close and vec_of:
                sim = vec_of(prev[0], r[0])
                close = sim is None or sim >= BURST_SIMILARITY
            if not close:
                if len(cur) > 1:
                    groups.append(cur)
                cur = []
        cur.append(r)
    if len(cur) > 1:
        groups.append(cur)

    out = {}
    for n, g in enumerate(groups, 1):
        sharps = [r[4] for r in g if r[4] is not None]
        top = max(sharps) if sharps else None

        def score(r):
            s = (r[4] / top) if (top and r[4] is not None) else 0.0   # sharpest frame in the burst = 1
            return s + 0.6 * (r[5] or 0.0)                           # faces looking good count too
        best = max(g, key=score)[0] if sharps else g[0][0]
        for r in g:
            out[r[0]] = (n, 1 if r[0] == best else 0)
    return out


def _km(lat1, lon1, lat2, lon2):
    return math.hypot(lat1 - lat2, (lon1 - lon2) * math.cos(math.radians(lat1))) * 111.2


def _range_label(d1: str, d2: str) -> str:
    y1, m1, a = int(d1[:4]), int(d1[5:7]), int(d1[8:])
    y2, m2, b = int(d2[:4]), int(d2[5:7]), int(d2[8:])
    if d1 == d2:
        return f"{MONTHS[m1]} {a}, {y1}"
    if (y1, m1) == (y2, m2):
        return f"{MONTHS[m1]} {a}–{b}, {y1}"
    if y1 == y2:
        return f"{MONTHS[m1]} {a} – {MONTHS[m2]} {b}, {y1}"
    return f"{MONTHS[m1]} {a}, {y1} – {MONTHS[m2]} {b}, {y2}"


def events_and_trips(con) -> list:
    """con: connection to the index. Returns [{kind, title, when, from, to, n, cover, places}], newest first."""
    rows = con.execute(
        "SELECT id, day, lat, lon, place, region, country, title, rating, pick, video FROM photos "
        "WHERE year > 1971 ORDER BY ts").fetchall()
    if not rows:
        return []
    geo = [r for r in rows if r["lat"] is not None]
    home, top_place = None, None
    if geo:
        top_place = Counter((r["place"], r["region"]) for r in geo if r["place"]).most_common(1)
        at_home = [r for r in geo if top_place and (r["place"], r["region"]) == top_place[0][0]] or geo
        home = (statistics.median(r["lat"] for r in at_home), statistics.median(r["lon"] for r in at_home))

    days = defaultdict(list)
    for r in rows:
        days[r["day"]].append(r)
    order = sorted(days)

    def away(day):
        g = [r for r in days[day] if r["lat"] is not None]
        if not home or len(g) < 2:
            return False
        far = sum(1 for r in g if _km(r["lat"], r["lon"], *home) > AWAY_KM)
        return far >= max(2, 0.6 * len(g))

    def gap(a, b):
        return (date.fromisoformat(b) - date.fromisoformat(a)).days

    def cover(photos):
        still = [r for r in photos if not r["video"]] or photos
        best = max(still, key=lambda r: (r["rating"] or 0))
        return (best if (best["rating"] or 0) > 0 else still[len(still) // 2])["id"]

    out, used = [], set()
    # trips: runs of days spent away from where most photos are taken
    away_days = [d for d in order if away(d)]
    run = []
    for d in away_days + [None]:
        if run and (d is None or gap(run[-1], d) > 3):
            span = [x for x in order if run[0] <= x <= run[-1]]
            photos = [r for x in span for r in days[x]]
            places = Counter((r["place"], r["country"]) for r in photos
                             if r["place"] and r["lat"] is not None and _km(r["lat"], r["lon"], *home) > AWAY_KM)
            countries = Counter()
            for (_, c), n in places.items():
                countries[c] += n
            if len(photos) >= 8 and places:
                country = countries.most_common(1)[0][0]
                big = [c for c, n in countries.most_common(3) if n >= 0.15 * sum(countries.values())]
                top = [p for (p, c), _ in places.most_common(6) if c == country][:2]
                regions = Counter(r["region"] for r in photos if r["region"] and r["country"] == country)
                where = country if country != "United States" or not regions else regions.most_common(1)[0][0]
                if len(big) > 1:      # a trip across countries is named by the countries
                    title = " & ".join(big[:3])
                elif top and top[0] != where:
                    title = f"{' & '.join(top)}, {where}"
                else:
                    title = where
                out.append({"kind": "trip", "title": title,
                            "when": _range_label(run[0], run[-1]), "from": run[0], "to": run[-1], "n": len(photos),
                            "days": gap(run[0], run[-1]) + 1, "cover": cover(photos),
                            "places": [p for (p, _), _ in places.most_common(6)]})
                used.update(span)
            run = []
        if d is not None:
            run.append(d)

    # events: busy days at home, named by the title most of their photos share
    pending = None
    for d in order:
        if d in used or len(days[d]) < 20:
            continue
        titles = Counter(r["title"] for r in days[d] if r["title"])
        title = None
        if titles and titles.most_common(1)[0][1] >= 0.3 * len(days[d]):
            title = titles.most_common(1)[0][0]
        if pending and title and pending["title"] == title and gap(pending["to"], d) <= 1:
            pending["to"], pending["n"] = d, pending["n"] + len(days[d])
            pending["_p"] += days[d]
            continue
        if pending:
            out.append(pending)
        place = Counter(r["place"] for r in days[d] if r["place"]).most_common(1)
        if not title:  # no shared title: fall back to the day's main keyword tag, then the town
            tag = con.execute(
                "SELECT t.name, count(*) c FROM photos p JOIN photo_tags pt ON pt.photo_id = p.id JOIN tags t ON t.id = pt.tag_id "
                "WHERE p.day = ? AND t.person = 0 GROUP BY t.id ORDER BY c DESC LIMIT 1", (d,)).fetchone()
            if tag and tag[1] >= 0.3 * len(days[d]):
                title = tag[0]
            elif place and place[0][0] != (top_place[0][0][0] if geo and top_place else None):
                title = place[0][0]
            else:
                title = f"{len(days[d])} photos"
        pending = {"kind": "event", "title": title, "from": d, "to": d,
                   "n": len(days[d]), "_p": list(days[d]), "places": [place[0][0]] if place else [], "days": 1}
    if pending:
        out.append(pending)
    for e in out:
        if "_p" in e:
            e["cover"] = cover(e.pop("_p"))
            e["when"] = _range_label(e["from"], e["to"])
            e["days"] = gap(e["from"], e["to"]) + 1
    return sorted(out, key=lambda e: e["from"], reverse=True)
