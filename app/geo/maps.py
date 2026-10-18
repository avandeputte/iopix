"""Map provider: OpenStreetMap tiles (default, no account) or Google Maps through the
Map Tiles API and the Geocoding API, with the owner's own API key.

The key comes from the `google_maps_key` setting (entered in Settings) or the
GOOGLE_MAPS_API_KEY environment variable; the `map_provider` setting picks the provider.
Google tiles need a session token: the server creates it with the key and hands the
browser a tile URL template. The key appears in that template, as Google's design
expects (restrict the key to this viewer's address in the Google Cloud console); the
server never prints it back in Settings.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from app.data import state

SESSION_URL = "https://tile.googleapis.com/v1/createSession"
TILE_URL = "https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_session = {"token": None, "expires": 0}
_sat_session = {"token": None, "expires": 0}
ESRI_IMAGERY = {"url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                "attribution": "Imagery © Esri, Maxar, Earthstar Geographics", "maxZoom": 19}
STREETVIEW_LINK = "https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lon}"
STREETVIEW_EMBED = "https://www.google.com/maps/embed/v1/streetview?key={key}&location={lat},{lon}&fov=80"
_lock = threading.Lock()
# the address the browser uses for this viewer, from the last /api/maps request. Google checks the
# Referer of every request against the key's "HTTP referrer" restriction, and the session and
# geocoding calls are made from here, not from the browser, so the server sends that address along.
REFERER = ""


def note_referer(headers) -> None:
    global REFERER
    ref = headers.get("referer") or headers.get("origin") or ""
    if ref:
        REFERER = ref


def _headers(extra: dict = None) -> dict:
    h = dict(extra or {})
    if REFERER:
        h["Referer"] = REFERER
    return h


def provider() -> str:
    p = state.get_setting("map_provider", "osm")
    return "google" if p == "google" and key() else "osm"


def key() -> str:
    return state.get_setting("google_maps_key", "") or os.environ.get("GOOGLE_MAPS_API_KEY", "")


def _create_session(k: str, map_type: str = "roadmap") -> dict:
    spec = {"mapType": map_type, "language": "en-US", "region": "US"}
    if map_type == "satellite":
        spec["layerTypes"] = ["layerRoadmap"]  # street and place names over the imagery
    body = json.dumps(spec).encode()
    req = urllib.request.Request(SESSION_URL + "?key=" + urllib.parse.quote(k), data=body, headers=_headers({"Content-Type": "application/json"}), method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def config() -> dict:
    """What the browser needs to draw maps: provider, tile URL templates (map and satellite), attribution,
    max zoom, and how to show Street View (embedded with a Google key, otherwise a link to Google Maps)."""
    c = _config()
    if c.get("provider") == "google":
        c["satellite"] = _satellite() or ESRI_IMAGERY
        c["streetview"] = {"embed": STREETVIEW_EMBED.replace("{key}", urllib.parse.quote(key())), "link": STREETVIEW_LINK}
    else:
        c["satellite"] = ESRI_IMAGERY
        c["streetview"] = {"embed": None, "link": STREETVIEW_LINK}
    return c


def _satellite():
    k = key()
    with _lock:
        if not _sat_session["token"] or time.time() > _sat_session["expires"] - 3600:
            try:
                s = _create_session(k, "satellite")
            except Exception:
                return None
            _sat_session.update(token=s["session"], expires=int(s.get("expiry", time.time() + 14 * 86400)))
        token = _sat_session["token"]
    return {"url": TILE_URL + "?session=" + urllib.parse.quote(token) + "&key=" + urllib.parse.quote(k), "attribution": "© Google", "maxZoom": 22}


def _config() -> dict:
    if provider() != "google":
        return {"provider": "osm", "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "attribution": "© OpenStreetMap contributors", "maxZoom": 19,
                "link": "https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=15/{lat}/{lon}", "linkLabel": "OpenStreetMap"}
    k = key()
    with _lock:
        if not _session["token"] or time.time() > _session["expires"] - 3600:
            try:
                s = _create_session(k)
            except Exception as e:
                return {"provider": "osm", "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "attribution": "© OpenStreetMap contributors", "maxZoom": 19,
                        "link": "https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=15/{lat}/{lon}", "linkLabel": "OpenStreetMap",
                        "error": f"Google Maps session failed ({str(e)[:120]}); showing OpenStreetMap. "
                                 "Check that the Map Tiles API is enabled for the key and that an HTTP-referrer restriction matches this viewer's address"
                                 + (f" ({REFERER})" if REFERER else "") + "."}
            _session.update(token=s["session"], expires=int(s.get("expiry", time.time() + 14 * 86400)))
        token = _session["token"]
    return {"provider": "google", "url": TILE_URL + "?session=" + urllib.parse.quote(token) + "&key=" + urllib.parse.quote(k), "attribution": "© Google", "maxZoom": 22,
            "link": "https://www.google.com/maps/search/?api=1&query={lat},{lon}", "linkLabel": "Google Maps"}


def geocode(q: str) -> list:
    """Google Geocoding API: addresses and landmarks, in the shape geocode.search returns."""
    req = urllib.request.Request(GEOCODE_URL + "?" + urllib.parse.urlencode({"address": q, "key": key()}), headers=_headers())
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        raise RuntimeError(data.get("error_message") or data.get("status"))
    out = []
    for x in data.get("results", [])[:8]:
        name, _, detail = x.get("formatted_address", "").partition(", ")
        loc = x["geometry"]["location"]
        kinds = set(x.get("types", []))
        out.append({"name": name, "detail": detail[:110], "lat": loc["lat"], "lon": loc["lng"],
                    "zoom": 16 if kinds & {"establishment", "point_of_interest", "premise", "street_address", "school"} else 13 if kinds & {"locality", "sublocality"} else 14})
    return out


PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ELEVATION_URL = "https://maps.googleapis.com/maps/api/elevation/json"
OPEN_METEO_ELEVATION = "https://api.open-meteo.com/v1/elevation"


def places_search(q: str) -> list:
    """Google Places API (New) text search: named places the Geocoding API does not know as addresses
    (airports, stadiums, businesses). Needs "Places API (New)" enabled for the key."""
    body = json.dumps({"textQuery": q, "maxResultCount": 8}).encode()
    req = urllib.request.Request(PLACES_URL, data=body, method="POST", headers=_headers({
        "Content-Type": "application/json", "X-Goog-Api-Key": key(),
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location,places.types"}))
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message") or str(e)
        except Exception:
            msg = str(e)
        raise RuntimeError(msg) from e
    out = []
    for x in data.get("places", [])[:8]:
        loc = x.get("location") or {}
        if "latitude" not in loc:
            continue
        addr = x.get("formattedAddress", "")
        out.append({"name": (x.get("displayName") or {}).get("text") or addr.partition(", ")[0], "detail": addr[:110],
                    "lat": loc["latitude"], "lon": loc["longitude"], "zoom": 15})
    return out


def elevation(lat: float, lon: float) -> dict:
    """Ground height in metres at a point: Google's Elevation API when Google is the provider and the
    key allows it, else Open-Meteo's (Copernicus 90 m terrain model, no key needed)."""
    errors = []
    if provider() == "google":
        try:
            req = urllib.request.Request(ELEVATION_URL + "?" + urllib.parse.urlencode({"locations": f"{lat},{lon}", "key": key()}), headers=_headers())
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.load(r)
            if data.get("status") == "OK" and data.get("results"):
                return {"alt": round(float(data["results"][0]["elevation"]), 1), "source": "Google"}
            errors.append(f"Google: {data.get('error_message') or data.get('status')}")
        except Exception as e:
            errors.append(f"Google: {e}")
    req = urllib.request.Request(OPEN_METEO_ELEVATION + "?" + urllib.parse.urlencode({"latitude": lat, "longitude": lon}), headers={"User-Agent": "iopix"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
    return {"alt": round(float(data["elevation"][0]), 1), "source": "Open-Meteo", "note": "; ".join(errors) or None}


VISION_URL = "https://vision.googleapis.com/v1/images:annotate"


def landmarks(jpeg: bytes) -> list:
    """Google Cloud Vision landmark detection: famous places recognised in a picture, with coordinates.
    Needs the Cloud Vision API enabled for the key, and a key Google accepts from a server."""
    import base64
    body = json.dumps({"requests": [{"image": {"content": base64.b64encode(jpeg).decode()}, "features": [{"type": "LANDMARK_DETECTION", "maxResults": 5}]}]}).encode()
    req = urllib.request.Request(VISION_URL + "?key=" + urllib.parse.quote(key()), data=body, method="POST", headers=_headers({"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message") or str(e)
        except Exception:
            msg = str(e)
        raise RuntimeError(msg) from e
    res = (data.get("responses") or [{}])[0]
    if res.get("error"):
        raise RuntimeError(res["error"].get("message", "error"))
    out = []
    for a in res.get("landmarkAnnotations", []):
        for loc in a.get("locations", [])[:1]:
            ll = loc.get("latLng") or {}
            if "latitude" in ll:
                out.append({"name": a.get("description", ""), "score": round(float(a.get("score", 0)), 2), "lat": ll["latitude"], "lon": ll["longitude"]})
    return out


def reset_session():
    with _lock:
        _session.update(token=None, expires=0)
