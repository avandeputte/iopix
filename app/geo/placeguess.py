"""Place suggestions for photos without a position, from the photos around them that have one.

A photo without GPS is usually from a camera that has none, shot among phone photos that do. Three ways
to place it, tried in this order:

  between   a photo with a position shortly before and one shortly after, close to each other: the
            position is interpolated between them by time (a walk, a stroll through town)
  near      one photo with a position within an hour, when nothing brackets it: that spot
  day       the places where photos were taken that day, grouped within a couple of kilometres: one
            group takes it; among several, the one its content embedding says it looks most like
            (a beach, a gym, a living room), else the group nearest in time
  stay      no position that day at all, but the day before and the day after both have photos at one
            place: a stay there (a trip); among several such places, the one it looks most like

Every suggestion carries how far it may be off (the distance between the photos it came from, or the
spread of the group), and is written nowhere: Use writes it as a GPS edit with that accuracy, like any
location edit (sidecar, file if enabled, digiKam on the next apply).
"""
import logging
import math
import threading

from app.data import index

log = logging.getLogger(__name__)
GROUP_KM = 2.0         # positions this close on one day are one place
BETWEEN_S = 3 * 3600   # a photo before and one after, each within this, bracket the photo
BETWEEN_KM = 5.0       # and this close to each other
NEAR_S = 3600          # one positioned photo within this stands for the spot
LOOK_MARGIN = 0.03     # mean similarity lead the chosen group needs over the next one
_look_cache = {"key": None, "sims": {}}
_lock = threading.Lock()


def km(lat1, lon1, lat2, lon2) -> float:
    return math.hypot(lat1 - lat2, (lon1 - lon2) * math.cos(math.radians(lat1))) * 111.2


def _groups(rows: list) -> list:
    """Positions of one day grouped within GROUP_KM: [{lat, lon, radius_km, members: [row]}], largest first."""
    groups = []
    for r in sorted(rows, key=lambda r: r["ts"]):
        for g in groups:
            if km(r["lat"], r["lon"], g["lat"], g["lon"]) <= GROUP_KM:
                g["members"].append(r)
                n = len(g["members"])
                g["lat"] += (r["lat"] - g["lat"]) / n
                g["lon"] += (r["lon"] - g["lon"]) / n
                break
        else:
            groups.append({"lat": r["lat"], "lon": r["lon"], "members": [r]})
    for g in groups:
        g["radius_km"] = max((km(m["lat"], m["lon"], g["lat"], g["lon"]) for m in g["members"]), default=0.0)
    groups.sort(key=lambda g: -len(g["members"]))
    return groups


def _place_of(members: list) -> dict:
    """The place name most of a group's photos carry (the index geocoded them)."""
    from collections import Counter
    names = Counter((m["place"], m["region"], m["country"]) for m in members if m["place"])
    place, region, country = names.most_common(1)[0][0] if names else (None, None, None)
    return {"place": place, "region": region, "country": country}


def _by_time(me: dict, rows: list):
    """A position from the positioned photos of the same day nearest in time, or None."""
    before = [r for r in rows if r["ts"] <= me["ts"]]
    after = [r for r in rows if r["ts"] > me["ts"]]
    b = max(before, key=lambda r: r["ts"]) if before else None
    a = min(after, key=lambda r: r["ts"]) if after else None
    if b and a and me["ts"] - b["ts"] <= BETWEEN_S and a["ts"] - me["ts"] <= BETWEEN_S:
        d = km(b["lat"], b["lon"], a["lat"], a["lon"])
        if d <= BETWEEN_KM:
            span = max(a["ts"] - b["ts"], 1)
            f = (me["ts"] - b["ts"]) / span
            lat, lon = b["lat"] + f * (a["lat"] - b["lat"]), b["lon"] + f * (a["lon"] - b["lon"])
            mins = round(max(me["ts"] - b["ts"], a["ts"] - me["ts"]) / 60)
            return {"lat": lat, "lon": lon, "km": max(0.05, d), "basis": "between", "from": [b["id"], a["id"]],
                    "text": f"between two photos with a position taken within {mins} min" + (f", {d:.1f} km apart" if d >= 0.15 else ""), **_place_of([b, a])}
    near = min(rows, key=lambda r: abs(r["ts"] - me["ts"]))
    gap = abs(near["ts"] - me["ts"])
    if gap <= NEAR_S:
        return {"lat": near["lat"], "lon": near["lon"], "km": 0.1 + gap / 3600 * 1.0, "basis": "near", "from": [near["id"]],
                "text": f"a photo with a position taken {max(1, round(gap / 60))} min {'earlier' if near['ts'] <= me['ts'] else 'later'}", **_place_of([near])}
    return None


def _look_sims(ids: list, others: list) -> dict:
    """{(id, other id): cosine similarity} from the content embeddings, for the pairs given; {} without them."""
    try:
        from app.ai.semantic import semantic, enabled
        if not enabled():
            return {}
        semantic.load()
        import numpy as np
        with semantic.lock:
            sids, vecs = semantic.ids, semantic.vecs
        if sids is None or not len(sids):
            return {}
        pos = {int(i): k for k, i in enumerate(sids)}
        out = {}
        for pid in ids:
            k = pos.get(pid)
            if k is None:
                continue
            want = [o for o in others if o in pos]
            if not want:
                continue
            with np.errstate(all="ignore"):
                s = vecs[[pos[o] for o in want]] @ vecs[k]
            for o, v in zip(want, s):
                out[(pid, o)] = float(v)
        return out
    except Exception as e:
        log.debug("place suggestions: no look-alikes (%s)", e)
        return {}


def _by_look(me: dict, groups: list, sims: dict, need_margin: bool):
    """The group the photo looks most like (mean similarity to its members), when that lead is clear."""
    scored = []
    for g in groups:
        vals = [sims[(me["id"], m["id"])] for m in g["members"] if (me["id"], m["id"]) in sims]
        if vals:
            scored.append((sum(vals) / len(vals), g))
    if not scored:
        return None
    scored.sort(key=lambda s: -s[0])
    if len(scored) > 1 and scored[0][0] - scored[1][0] < LOOK_MARGIN:
        return None
    if need_margin and (len(scored) < 2 and len(groups) > 1):
        return None
    return scored[0][1]


def _stays(day: str, groups_of: dict) -> list:
    """Places photographed both the day before and the day after `day` (within GROUP_KM of each other), as merged
    groups: where someone was staying, when the day itself has no position at all."""
    from datetime import date, timedelta
    dd = date.fromisoformat(day)
    before = groups_of.get((dd - timedelta(days=1)).isoformat(), [])
    after = groups_of.get((dd + timedelta(days=1)).isoformat(), [])
    out = []
    for a in before:
        for b in after:
            if km(a["lat"], a["lon"], b["lat"], b["lon"]) <= GROUP_KM:
                members = a["members"] + b["members"]
                lat = sum(m["lat"] for m in members) / len(members)
                lon = sum(m["lon"] for m in members) / len(members)
                out.append({"lat": lat, "lon": lon, "members": members, "radius_km": max(km(m["lat"], m["lon"], lat, lon) for m in members)})
    out.sort(key=lambda g: -len(g["members"]))
    return out


def suggestions(ids: list = None) -> dict:
    """{"items": [{id, lat, lon, km, place, region, country, basis, text, from: [ids]}]} for the still photos with a
    known date and no position (or those of `ids`), the surest first."""
    import sqlite3
    from datetime import date, timedelta
    con = index.open_index()
    con.row_factory = sqlite3.Row
    try:
        sql = "SELECT id, ts, day FROM photos WHERE lat IS NULL AND video = 0 AND dprec IS NULL AND year > 1971 AND ts IS NOT NULL"
        if ids is not None:
            sql += f" AND id IN ({','.join(str(int(i)) for i in ids) or 'NULL'})"
        want = [dict(r) for r in con.execute(sql)]
        if not want:
            return {"items": []}
        days = sorted({r["day"] for r in want})
        around = set()
        for d in days:
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                continue
            around.update((dd + timedelta(days=k)).isoformat() for k in (-1, 0, 1))
        positioned = {}
        for part in range(0, len(around), 500):
            chunk = sorted(around)[part:part + 500]
            for r in con.execute(f"SELECT id, ts, day, lat, lon, place, region, country FROM photos WHERE lat IS NOT NULL AND day IN ({','.join('?' * len(chunk))})", chunk):
                positioned.setdefault(r["day"], []).append(dict(r))
    finally:
        con.close()
    groups_of = {d: _groups(rows) for d, rows in positioned.items()}
    # look-alikes are only needed where a day (or the stay around it) has more than one place
    pairs = {}
    for me in want:
        same = groups_of.get(me["day"], [])
        nearby = [] if same else _stays(me["day"], groups_of)
        if len(same) > 1 or len(nearby) > 1:
            pairs[me["id"]] = [m["id"] for g in (same or nearby) for m in g["members"][:40]]
    sims = {}
    if pairs:
        flat = sorted({o for os in pairs.values() for o in os})
        sims = _look_sims(list(pairs), flat)
    items = []
    for me in want:
        rows = positioned.get(me["day"], [])
        hit = _by_time(me, rows) if rows else None
        if hit is None and rows:
            groups = groups_of[me["day"]]
            g = groups[0] if len(groups) == 1 else _by_look(me, groups, sims, need_margin=False)
            basis = "day"
            if g is None:  # several places that day and no clear look-alike: the one nearest in time
                g = min(groups, key=lambda g: min(abs(m["ts"] - me["ts"]) for m in g["members"]))
                basis = "day-time"
            hit = {"lat": g["lat"], "lon": g["lon"], "km": max(0.2, g["radius_km"]), "basis": basis, "from": [m["id"] for m in g["members"][:6]],
                   "text": (f"the place of {len(g['members'])} photos with a position that day" if len(groups) == 1 else
                            f"of the {len(groups)} places photographed that day, the one it looks most like" if basis == "day" else
                            f"of the {len(groups)} places photographed that day, the one nearest in time"), **_place_of(g["members"])}
        if hit is None and not rows:
            stays = _stays(me["day"], groups_of)
            g = stays[0] if len(stays) == 1 else (_by_look(me, stays, sims, need_margin=True) if stays else None)
            if g is not None:
                hit = {"lat": g["lat"], "lon": g["lon"], "km": max(1.0, g["radius_km"] * 2), "basis": "stay", "from": [m["id"] for m in g["members"][:6]],
                       "text": "photos with a position were taken there both the day before and the day after" + (", and it looks most like them" if len(stays) > 1 else ""),
                       **_place_of(g["members"])}
        if hit:
            hit.update(id=me["id"], lat=round(hit["lat"], 6), lon=round(hit["lon"], 6), km=round(hit["km"], 2))
            items.append(hit)
    order = {"between": 0, "near": 1, "day": 2, "day-time": 3, "stay": 4}
    items.sort(key=lambda i: (order[i["basis"]], i["km"], i["id"]))
    return {"items": items}
