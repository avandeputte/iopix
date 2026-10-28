"""Address and landmark search for the location dialog ("Mt Lebanon High School" ->
coordinates). Online it asks OpenStreetMap's Nominatim, the same project the map tiles
come from, under its usage policy: an identifying User-Agent, at most one request per
second, results cached. Without a connection it falls back to the offline GeoNames town
list (data/geonames, populated by enable-places.sh), which knows towns but not streets
or landmarks. Nothing about the library ever leaves the machine: only the typed query.
"""
import json
import threading
import time
import urllib.parse
import urllib.request
import logging
from collections import OrderedDict

from app.geo import maps, places

URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "iopix/1.0 (https://github.com/avandeputte/iopix)"
_cache = OrderedDict()
_lock = threading.Lock()       # the cache
_net_lock = threading.Lock()   # the rate limit and the request itself, so a cached answer never waits behind a slow one
_last = [0.0]
_offline_until = [0.0]
log = logging.getLogger("app.geo.geocode")


def _fetch(q: str) -> list:
    """Raw Nominatim rows for a query (network)."""
    with _net_lock:  # Nominatim's policy: one request per second, from one client at a time
        wait = _last[0] + 1.05 - time.time()
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(URL + "?" + urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": 8, "addressdetails": 0}),
                                     headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                rows = json.load(r)
        finally:
            _last[0] = time.time()
    return rows


def _nominatim(q: str) -> list:
    return _reshape(_fetch(q))


def _reshape(rows: list) -> list:
    out = []
    for x in rows:
        name, _, detail = x.get("display_name", "").partition(", ")
        kind = (x.get("type") or "").replace("_", " ")
        out.append({"name": name, "detail": (kind + " · " if kind and kind not in name.lower() else "") + detail[:110], "lat": float(x["lat"]), "lon": float(x["lon"]),
                    "zoom": 16 if x.get("category") in ("amenity", "building", "tourism", "shop", "leisure") or x.get("type") in ("house", "school") else 13 if x.get("type") in ("city", "town", "village", "suburb") else 14})
    return out


def _offline(q: str) -> list:
    """Towns from the GeoNames file whose name starts with the query (no landmarks offline)."""
    path = places.DIR / "cities1000.txt"
    if not path.exists():
        return []
    ql = q.lower()
    hits = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.split("\t")
            if len(p) < 15:
                continue
            if p[1].lower().startswith(ql) or p[2].lower().startswith(ql):
                try:
                    hits.append((int(p[14] or 0), {"name": p[1], "detail": f"{p[10]}, {p[8]}".strip(", "), "lat": float(p[4]), "lon": float(p[5]), "zoom": 12}))
                except ValueError:
                    pass
    hits.sort(key=lambda h: -h[0])  # biggest towns first
    return [h[1] for h in hits[:8]]


def _online(q: str):
    """Google (Geocoding, then Places for named places such as airports) when it is the provider, and
    OpenStreetMap whenever Google finds nothing or refuses (a key restricted to browser referrers is
    refused by Google's server-side APIs). None when no service could be reached."""
    notes = []
    if maps.provider() == "google":
        for name, fn in (("Geocoding API", maps.geocode), ("Places API (New)", maps.places_search)):
            try:
                rows = fn(q)
                if rows:
                    return {"results": rows, "source": "google"}
            except Exception as e:
                notes.append(f"Google {name}: {str(e)[:160]}")
        if notes:
            log.warning("place search %r: %s; using OpenStreetMap", q, "; ".join(notes))
    try:
        rows = _nominatim(q)
    except Exception as e:
        log.warning("place search %r: OpenStreetMap unreachable (%s)", q, e)
        return None
    return {"results": rows, "source": "osm", "note": ("Google could not search (" + "; ".join(notes) + "); these results are from OpenStreetMap.") if notes else None}


def search(q: str) -> dict:
    key = q.strip().lower()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    if time.time() > _offline_until[0]:
        res = _online(q)
        if res is None:
            _offline_until[0] = time.time() + 60  # don't hammer a dead connection; retry a minute later
            res = {"results": _offline(q), "source": "offline"}
    else:
        res = {"results": _offline(q), "source": "offline"}
    with _lock:
        _cache[key] = res
        while len(_cache) > 200:
            _cache.popitem(last=False)
    return res
