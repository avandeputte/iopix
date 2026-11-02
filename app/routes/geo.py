"""Place lookups: geocoding, nearby landmarks and elevation."""

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from app import access, thumbs, users
from app.geo import geocode, maps, placeguess
from app.http import jresp, json_body
from app.runtime import db



router = APIRouter()


@router.get("/api/geocode", dependencies=[access.signed_in])
async def api_geocode(request: Request, q: str = ""):
    """Address or landmark search for the location dialog: OpenStreetMap's Nominatim (the same
    project the map tiles come from), with the offline GeoNames towns as a fallback."""
    if users.is_signed_in(request.state.user):
        maps.note_referer(request.headers)  # a visitor's Referer must not steer the key's referrer restriction
    q = q.strip()
    if len(q) < 2:
        return {"results": [], "source": None}
    return await run_in_threadpool(geocode.search, q)


@router.get("/api/location/suggest", dependencies=[access.public])
def api_location_suggest(id: int, hours: float = 6):
    """Where a photo was probably taken, from the photos nearest in time that have a position: the closest
    one before and after, and others the same day, nearest first (with how far apart in time)."""
    con = db()
    try:
        me = con.execute("SELECT id, ts, camera FROM photos WHERE id = ?", (id,)).fetchone()
        if not me:
            raise HTTPException(404)
        span = int(max(0.25, min(hours, 72)) * 3600)
        rows = con.execute("SELECT id, name, ts, lat, lon, alt, place, region, country, camera FROM photos "
                           "WHERE lat IS NOT NULL AND id != ? AND ts BETWEEN ? AND ? ORDER BY abs(ts - ?) LIMIT 400",
                           (id, me["ts"] - span, me["ts"] + span, me["ts"])).fetchall()
    finally:
        con.close()
    out, seen = [], []
    for r in rows:  # distinct places only: the nearest photo in time stands for each spot within ~150 m
        if any(abs(r["lat"] - s[0]) < 0.0015 and abs(r["lon"] - s[1]) < 0.002 for s in seen):
            continue
        seen.append((r["lat"], r["lon"]))
        out.append({"id": r["id"], "name": r["name"], "minutes": round((r["ts"] - me["ts"]) / 60), "lat": r["lat"], "lon": r["lon"], "alt": r["alt"],
                    "place": ", ".join(x for x in (r["place"], r["region"], r["country"]) if x), "same_camera": r["camera"] == me["camera"]})
        if len(out) >= 6:
            break
    return {"suggestions": out, "landmarks": maps.provider() == "google"}


@router.get("/api/placesuggest", dependencies=[access.editor])
def api_placesuggest(request: Request, offset: int = 0, limit: int = 100, ids: str = ""):
    """Where the photos without a position were probably taken (placeguess.py), surest first:
    {total, items: [{id, lat, lon, km, place, region, country, basis, text, from: [ids]}]}. Nothing is written; using
    one is /api/edit {gps: {lat, lon, accuracy: km * 1000}}."""
    want = [int(x) for x in ids.split(",") if x.strip().isdigit()] if ids else None
    items = placeguess.suggestions(want)["items"]
    return jresp(request, {"total": len(items), "offset": offset, "items": items[max(0, offset):max(0, offset) + max(1, min(limit, 1000))]})


@router.post("/api/location/landmark", dependencies=[access.editor_write])
async def api_location_landmark(request: Request):
    """Body: {id}. Google Cloud Vision looks for a famous landmark in the photo (a preview is sent, not the original)."""
    body = await json_body(request)
    pid = int(body.get("id") or 0)
    if not maps.key():
        raise HTTPException(409, "Landmark recognition uses Google Cloud Vision: add a Google API key in Settings › Map provider")
    con = db()
    try:
        row = con.execute("SELECT path, w, h, video FROM photos WHERE id = ?", (pid,)).fetchone()
    finally:
        con.close()
    if not row or row["video"]:
        raise HTTPException(404, "No such photo")

    def work():
        import io
        im, _ = thumbs.open_oriented(row["path"], 1024, 1024)
        im = im.convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return maps.landmarks(buf.getvalue())
    if users.is_signed_in(request.state.user):
        maps.note_referer(request.headers)  # a visitor's Referer must not steer the key's referrer restriction
    try:
        found = await run_in_threadpool(work)
    except RuntimeError as e:
        raise HTTPException(502, f"Google Cloud Vision: {e}. It needs the Cloud Vision API enabled for the key, and a key not restricted to website addresses (Google refuses those from a server).")
    return {"landmarks": found}


@router.get("/api/elevation", dependencies=[access.editor])
async def api_elevation(request: Request, lat: float, lon: float):
    """Ground height at a point for the location dialog's altitude field (Google Elevation, else Open-Meteo)."""
    if users.is_signed_in(request.state.user):
        maps.note_referer(request.headers)  # a visitor's Referer must not steer the key's referrer restriction
    if abs(lat) > 90 or abs(lon) > 180:
        raise HTTPException(400, "Not a position")
    try:
        return await run_in_threadpool(maps.elevation, lat, lon)
    except Exception as e:
        raise HTTPException(502, f"No elevation service could be reached: {str(e)[:160]}")
