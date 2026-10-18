"""Browsing: photos, metadata, facets, map, events, status, detected content and duplicates."""
import gzip
import json
import re
import sqlite3
from collections import Counter
from functools import lru_cache
from urllib.parse import parse_qsl

import orjson
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.responses import Response

from app.ai import analysis
from app.ai import concepts
from app import config
from app.photos import edits
from app.faces import facedetect
from app.data import index
from app.photos import lossless
from app.ai import moments
from app.geo import places
from app.photos import tagmanager
from app.data import state as appstate
from app import jobs as bgjobs
from app import users
from app import updates
from app import __version__ as APP_VERSION
from app.ai.semantic import semantic
from app.ai.semantic import enabled as semantic_enabled
from app.data.query import SORTS
from app.core import state
from app.core import prewarmer
from app.core import db
from app.core import resolve
from app.core import _public_version
from app.core import jresp
from app.core import app_name
from app.core import _app_stamp
from app.core import CACHES

from app.core import _tag_covers

router = APIRouter()

# --------------------------------------------------------------------------
# Browsing: photos, metadata, facets, map, events
# --------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _photos_cached(version: str, key: str) -> bytes:
    items = parse_qsl(key.split("|", 1)[1], keep_blank_values=True)
    where, params, rank = resolve(items)
    d = dict(items)
    sort = SORTS.get(d.get("sort", "new"), SORTS["new"])
    con = db()
    try:
        rows = con.execute(
            f"SELECT id, ts, w, h, video, duration, lat IS NOT NULL, "
            f"EXISTS (SELECT 1 FROM faces f WHERE f.photo_id = photos.id AND f.kind = 'confirmed') FROM photos WHERE {where} ORDER BY {sort}", params).fetchall()
    finally:
        con.close()
    if rank and "sort" not in d:
        rows.sort(key=lambda r: rank.get(r[0], 1 << 30))
    data = {
        "version": version, "count": len(rows), "ranked": bool(rank),
        "id": [r[0] for r in rows], "ts": [r[1] for r in rows],
        "w": [r[2] for r in rows], "h": [r[3] for r in rows],
        # videos only, as {index: seconds}, to keep the payload small
        "video": {str(i): (round(r[5]) if r[5] else 0) for i, r in enumerate(rows) if r[4]},
        # positions of photos that carry GPS coordinates, for the badge on their tiles
        "geo": [i for i, r in enumerate(rows) if r[6]],
        "faces": [i for i, r in enumerate(rows) if r[7]],
    }
    return gzip.compress(orjson.dumps(data), compresslevel=3)


@router.get("/api/random")
def api_random(request: Request, n: int = 12):
    """Explore › Rediscover: photos picked at random from the whole library (stills, not rejected): {id, w, h, ts}."""
    where, params, _rank = resolve([("type", "photo"), ("norejected", "1")])
    con = db()
    try:
        rows = con.execute(f"SELECT id, w, h, ts FROM photos WHERE {where} ORDER BY random() LIMIT ?", [*params, max(1, min(int(n), 60))]).fetchall()
    finally:
        con.close()
    return {"id": [r[0] for r in rows], "w": [r[1] for r in rows], "h": [r[2] for r in rows], "ts": [r[3] for r in rows]}


@router.get("/api/thenandnow")
def api_then_and_now(request: Request, n: int = 3, person: int = None):
    """Explore › Then and now: people photographed years apart, with their earliest and latest named face:
    {pairs: [{tag, name, then: {photo, ts, rect}, now: {...}, years}]}. person: that one person; else random ones."""
    con = db()
    try:
        rows = con.execute(
            "SELECT f.tag_id, t.name, min(p.ts), max(p.ts), count(*) FROM faces f JOIN photos p ON p.id = f.photo_id JOIN tags t ON t.id = f.tag_id "
            "WHERE f.kind = 'confirmed' AND t.person = 1 AND p.video = 0 AND p.pick != 1 GROUP BY f.tag_id HAVING max(p.ts) - min(p.ts) >= ? "
            + ("AND f.tag_id = ? " if person else "") + "ORDER BY random() LIMIT ?", [3 * 365 * 86400] + ([int(person)] if person else []) + [max(1, min(int(n), 12))]).fetchall()
        pairs = []
        for tag, name, t0, t1, cnt in rows:
            ends = []
            for order in ("ASC", "DESC"):
                r = con.execute("SELECT f.photo_id, p.ts, f.x, f.y, f.w, f.h FROM faces f JOIN photos p ON p.id = f.photo_id "
                                f"WHERE f.tag_id = ? AND f.kind = 'confirmed' AND p.video = 0 AND p.pick != 1 ORDER BY p.ts {order}, f.w * f.h DESC LIMIT 1", (tag,)).fetchone()
                ends.append({"photo": r[0], "ts": r[1], "rect": [r[2], r[3], r[4], r[5]]})
            pairs.append({"tag": tag, "name": name, "then": ends[0], "now": ends[1], "years": round((t1 - t0) / (365.25 * 86400), 1), "photos": cnt})
    finally:
        con.close()
    return {"pairs": pairs}


@router.get("/api/photos")
def api_photos(request: Request):
    """The whole result set in compact column form; the browser lays it out and
    fetches thumbnails lazily, so scrolling never waits on the server."""
    try:
        # the prefix makes cached results expire when favorites, albums or embeddings change
        # an album filter depends on who asks (private albums), so the cached answer is kept per user then
        who = f".u{(request.state.user or {}).get('id')}" if "ualbum" in request.url.query else ""
        body = _photos_cached(state["version"], f"{appstate.rev}.{semantic.key}{who}|{request.url.query}")
    except sqlite3.OperationalError as e:
        raise HTTPException(400, f"Bad query: {e}")
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(body, media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Cache-Control": "no-store"})
    return Response(gzip.decompress(body), media_type="application/json")




@lru_cache(maxsize=2)
def _meta(version: str) -> dict:
    con = db()
    try:
        q = con.execute
        covers = {r[0]: r[1] for r in q(
            "SELECT c.anc, p.id, max(p.rating * 1e11 + p.ts - (p.pick = 1) * 1e13) FROM tag_closure c "
            "JOIN photo_tags pt ON pt.tag_id = c.tag JOIN photos p ON p.id = pt.photo_id "
            "WHERE p.video = 0 GROUP BY c.anc")}
        covers.update(_tag_covers)
        # a thumbnail chosen in digiKam (or here) beats the automatic pick
        covers.update({r[0]: r[1] for r in q("SELECT id, icon FROM tags WHERE icon IS NOT NULL")})
        # whose face to show for a people tag: its own, or its most-photographed member's
        faces = {r[0]: r[1] for r in q(
            "SELECT c.anc, f.tag_id, max(cnt) FROM tag_closure c JOIN (SELECT tag_id, count(*) AS cnt FROM faces "
            "GROUP BY tag_id) f ON f.tag_id = c.tag GROUP BY c.anc")}
        own = {r[0] for r in q("SELECT DISTINCT tag_id FROM faces")}
        tags = [{"id": r["id"], "pid": r["pid"], "name": r["name"], "full": r["full"], "person": r["person"],
                 "n": r["n"], "nAll": r["n_all"], "cover": covers.get(r["id"]), "icon": r["icon"],
                 "face": r["id"] if r["id"] in own else (faces.get(r["id"]) if r["full"].split("/")[0] == "People" else None)}
                for r in q("SELECT * FROM tags ORDER BY name COLLATE NOCASE")]
        albums = [{"id": r["id"], "pid": r["pid"], "path": r["path"], "name": r["name"], "caption": r["caption"],
                   "n": r["n"], "nAll": r["n_all"], "cover": r["cover"], "tsMin": r["ts_min"], "tsMax": r["ts_max"],
                   "date": r["adate"], "category": r["category"], "icon": r["icon"]}
                  for r in q("SELECT * FROM albums ORDER BY path COLLATE NOCASE")]

        def counts(col, where="1"):
            return [[r[0], r[1]] for r in q(
                f"SELECT {col}, count(*) FROM photos WHERE {where} GROUP BY 1 ORDER BY 2 DESC")]
        tot = q("SELECT count(*), sum(video), sum(lat IS NOT NULL), min(ts), max(ts), sum(size) FROM photos").fetchone()
        return {
            "version": version, "tags": tags, "albums": albums,
            "cameras": counts("camera", "camera IS NOT NULL"), "lenses": counts("lens", "lens IS NOT NULL"),
            "formats": counts("fmt"), "creators": counts("creator", "creator IS NOT NULL"),
            "years": sorted(counts("year"), reverse=True), "ratings": counts("rating"),
            "picks": counts("pick"), "colors": counts("color"),
            "total": tot[0], "videos": tot[1] or 0, "geotagged": tot[2] or 0,
            "tsMin": tot[3], "tsMax": tot[4], "bytes": tot[5] or 0,
            "colorNames": index.COLORS,
            "places": [[r[0], r[1], r[2], r[3], r[4]] for r in q(
                "SELECT place, region, country, count(*), id, max(ts - (pick = 1) * 1e11 - video * 1e11) FROM photos "
                "WHERE place IS NOT NULL GROUP BY place, region, country ORDER BY 4 DESC")],
            "countries": counts("country", "country IS NOT NULL"),
        }
    finally:
        con.close()


@router.get("/api/meta")
def api_meta(request: Request):
    return jresp(request, {**_meta(state["version"]), "version": _public_version()})



@router.get("/api/status")
def api_status(request: Request):
    user = request.state.user
    return {"version": _public_version(), "error": state["error"], "rev": appstate.rev, "app": _app_stamp(),
            "thumbs": {"running": prewarmer.running, "done": prewarmer.done, "total": prewarmer.total, "since": prewarmer.started, "last": prewarmer.last,
                       "current": list(prewarmer.current.values())[:3] if users.is_signed_in(user) else []},  # file paths: not for anonymous clients
            "semantic": {"enabled": semantic_enabled(), "running": semantic.running, "done": semantic.done,
                         "total": semantic.total, "indexed": 0 if semantic.ids is None else int(len(semantic.ids))},
            "places": places.available(), "editing": edits.available() and users.can_write(user), "pendingEdits": edits.pending_count(),
            "dbWrite": config.DB_WRITE and not appstate.read_only() and users.can_write(user), "writeFiles": edits.writes_files(),
            "user": ({"id": user["id"], "name": user["name"], "role": user["role"], "person": user.get("person")} if user else None), "usersExist": users.any_users(),
            "isAdmin": users.is_admin(user), "signedIn": users.is_signed_in(user), "requireLogin": users.require_login(),
            "updateAvailable": updates.available() if users.is_admin(user) and updates.auto_check_enabled() else None,
            "readOnly": appstate.read_only(), "readOnlyLocked": config.READ_ONLY, "digikamCheck": config.DIGIKAM_CHECK,
            "digikam": index.status(), "faceDetect": facedetect.available(), "lossless": lossless.capabilities() if config.DB_WRITE and edits.available() else None,
            "appVersion": APP_VERSION, "commit": updates.local_commit()[:7], "appName": app_name(),
            "tagJobs": {**tagmanager.progress, "pending": tagmanager.pending_jobs()},
            "analysis": analysis.progress, "jobs": bgjobs.status()}


@router.get("/api/facets")
def api_facets(request: Request):
    """Counts of every metadata value within the current result set."""
    where, params, _ = resolve(request.query_params.multi_items())
    con = db()
    try:
        # keyed, so the twenty "id IN (SELECT id FROM sel)" probes below use the b-tree
        # instead of each building its own (halves the time on the whole library)
        con.execute("CREATE TEMP TABLE sel (id INTEGER PRIMARY KEY)")
        con.execute(f"INSERT INTO sel SELECT id FROM photos WHERE {where}", params)
        q = con.execute

        def counts(col, limit=60):
            return [[r[0], r[1]] for r in q(
                f"SELECT {col}, count(*) FROM photos WHERE id IN (SELECT id FROM sel) GROUP BY 1 "
                f"ORDER BY 2 DESC LIMIT {limit}")]
        tag_counts = [[r[0], r[1]] for r in q(
            "SELECT pt.tag_id, count(*) FROM photo_tags pt WHERE pt.photo_id IN (SELECT id FROM sel) "
            "GROUP BY 1 ORDER BY 2 DESC")]
        rng = q("SELECT count(*), min(ts), max(ts), sum(size), sum(video), sum(lat IS NOT NULL), "
                "min(iso), max(iso), min(aperture), max(aperture), "
                "min(coalesce(focal35, focal)), max(coalesce(focal35, focal)) "
                "FROM photos WHERE id IN (SELECT id FROM sel)").fetchone()
        return jresp(request, {
            "count": rng[0], "tsMin": rng[1], "tsMax": rng[2], "bytes": rng[3] or 0,
            "videos": rng[4] or 0, "geotagged": rng[5] or 0,
            "iso": [rng[6], rng[7]], "aperture": [rng[8], rng[9]], "focal": [rng[10], rng[11]],
            "tags": tag_counts, "years": sorted(counts("year", 300), reverse=True),
            "cameras": counts("camera"), "lenses": counts("lens"), "formats": counts("fmt"),
            "ratings": counts("rating"), "picks": counts("pick"), "colors": counts("color"),
            "albums": counts("album_id", 400), "creators": counts("creator"),
            "objects": _object_counts(q), "npeople": [c for c in counts("npeople") if c[0] is not None],
            "extras": dict(q("SELECT 'blurry', count(*) FROM photos WHERE sharpn < 0.1 AND id IN (SELECT id FROM sel) UNION ALL "
                             "SELECT 'sharpest', count(*) FROM photos WHERE sharpn >= 0.8 AND id IN (SELECT id FROM sel) UNION ALL "
                             "SELECT 'hastext', count(*) FROM photos WHERE length(ocr) >= 3 AND id IN (SELECT id FROM sel) UNION ALL "
                             "SELECT 'bursts', count(*) FROM photos WHERE burst IS NOT NULL AND id IN (SELECT id FROM sel) UNION ALL "
                             "SELECT 'dark', count(*) FROM photos WHERE (bright < 0.2 OR clip_lo > 0.5) AND id IN (SELECT id FROM sel) UNION ALL "
                             "SELECT 'bright', count(*) FROM photos WHERE (bright > 0.8 OR clip_hi > 0.3) AND id IN (SELECT id FROM sel)").fetchall()),
            "places": [[r[0], r[1], r[2], r[3]] for r in q(
                "SELECT place, region, country, count(*) FROM photos WHERE place IS NOT NULL AND id IN (SELECT id FROM sel) "
                "GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 200")],
            "countries": [c for c in counts("country") if c[0]],
        })
    except sqlite3.OperationalError as e:
        raise HTTPException(400, f"Bad query: {e}")
    finally:
        con.close()


def _object_counts(q) -> list:
    c = Counter()
    for (objs,) in q("SELECT objects FROM photos WHERE objects IS NOT NULL AND id IN (SELECT id FROM sel)"):
        c.update(o for o in objs.split(",") if o)
    return [[k, v] for k, v in c.most_common(80)]


@lru_cache(maxsize=2)
def _events(version: str) -> list:
    con = db()
    try:
        return moments.events_and_trips(con)
    finally:
        con.close()


@router.get("/api/events")
def api_events(request: Request):
    """Trips (runs of days away from home) and busy days, worked out from dates, places and titles."""
    return jresp(request, {"events": _events(state["version"])})


@router.get("/api/bursts")
def api_bursts(request: Request, offset: int = 0, limit: int = 40):
    """Bursts within the current filters, newest first, each with its frames and best pick."""
    items = [(k, v) for k, v in request.query_params.multi_items() if k not in ("offset", "limit")]
    where, params, _ = resolve(items)
    con = db()
    try:
        ids = [r[0] for r in con.execute(
            f"SELECT burst FROM photos WHERE burst IS NOT NULL AND {where} GROUP BY burst ORDER BY max(ts) DESC", params)]
        page = ids[max(0, offset):max(0, offset) + max(1, min(limit, 200))]
        rows = con.execute(
            f"SELECT id, burst, burst_best, w, h, ts, sharpn, faceq, pick, rating FROM photos "
            f"WHERE burst IN ({','.join(map(str, page)) or 'NULL'}) ORDER BY ts, name").fetchall()
        total_photos = con.execute(f"SELECT count(*) FROM photos WHERE burst IS NOT NULL AND {where}", params).fetchone()[0]
    except sqlite3.OperationalError as e:
        raise HTTPException(400, f"Bad query: {e}")
    finally:
        con.close()
    groups = {b: [] for b in page}
    for r in rows:
        groups[r["burst"]].append({"id": r["id"], "best": r["burst_best"], "w": r["w"], "h": r["h"], "ts": r["ts"],
                                   "sharpn": r["sharpn"], "faceq": r["faceq"], "pick": r["pick"], "rating": r["rating"]})
    return jresp(request, {"bursts": len(ids), "photos": total_photos, "offset": offset,
                           "groups": [{"burst": b, "items": groups[b]} for b in page]})


@router.get("/api/trail")
def api_trail(request: Request, day: str):
    """The day's geotagged photos in time order, to draw the route taken."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise HTTPException(400, "day must be YYYY-MM-DD")
    con = db()
    try:
        rows = con.execute("SELECT id, ts, lat, lon, alt FROM photos WHERE day = ? AND lat IS NOT NULL ORDER BY ts", (day,)).fetchall()
    finally:
        con.close()
    return jresp(request, {"day": day, "id": [r[0] for r in rows], "ts": [r[1] for r in rows],
                           "lat": [round(r[2], 6) for r in rows], "lon": [round(r[3], 6) for r in rows], "alt": [r[4] for r in rows]})


@router.get("/api/map")
def api_map(request: Request):
    where, params, _ = resolve(request.query_params.multi_items())
    con = db()
    try:
        rows = con.execute(
            f"SELECT id, lat, lon FROM photos WHERE lat IS NOT NULL AND {where} ORDER BY ts DESC", params).fetchall()
    except sqlite3.OperationalError as e:
        raise HTTPException(400, f"Bad query: {e}")
    finally:
        con.close()
    return jresp(request, {"id": [r[0] for r in rows], "lat": [round(r[1], 5) for r in rows],
                           "lon": [round(r[2], 5) for r in rows]})


@router.get("/api/photo/{photo_id}")
def api_photo(photo_id: int, request: Request):
    con = db()
    try:
        row = con.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        d = dict(row)
        d["extra"] = json.loads(d["extra"]) if d["extra"] else {}
        album = con.execute("SELECT id, path, name FROM albums WHERE id = ?", (d["album_id"],)).fetchone()
        d["album"] = {"id": album["id"], "path": album["path"], "name": album["name"]} if album else None
        d["tags"] = [dict(r) for r in con.execute(
            "SELECT t.id, t.name, t.full, t.person FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
            "WHERE pt.photo_id = ? ORDER BY t.person DESC, t.full COLLATE NOCASE", (photo_id,))]
        d["faces"] = [dict(r) for r in con.execute(
            "SELECT f.tag_id AS tag, t.name, f.x, f.y, f.w, f.h FROM faces f JOIN tags t ON t.id = f.tag_id "
            "WHERE f.photo_id = ? AND f.kind = 'confirmed' ORDER BY f.x", (photo_id,))]
        d["faces_other"] = [dict(r) for r in con.execute(
            "SELECT f.tag_id AS tag, f.kind, f.x, f.y, f.w, f.h, (SELECT name FROM tags t WHERE t.id = f.tag_id) AS name "
            "FROM faces f WHERE f.photo_id = ? AND f.kind != 'confirmed' ORDER BY f.x", (photo_id,))]
        d.pop("hash", None)
        d["ualbums"] = appstate.albums_of(photo_id)
        d["similar"] = semantic_enabled()
        d["concepts"] = concepts.for_photo(photo_id) if semantic_enabled() else []
        d["boxes"] = analysis.boxes(photo_id)
        from app.photos import versions
        d["versions"] = versions.versions_of(photo_id)  # digiKam's version family (ImageRelations), when there is one
        if d.get("burst"):
            d["burst_size"] = con.execute("SELECT count(*) FROM photos WHERE burst = ?", (d["burst"],)).fetchone()[0]
            best = con.execute("SELECT id, name FROM photos WHERE burst = ? AND burst_best = 1", (d["burst"],)).fetchone()
            d["burst_best_id"], d["burst_best_name"] = (best[0], best[1]) if best else (None, None)
        return jresp(request, d)
    finally:
        con.close()



# --------------------------------------------------------------------------
# Detected content and duplicates
# --------------------------------------------------------------------------

@router.get("/api/concepts")
def api_concepts(request: Request):
    """What the content-search model sees: everyday subjects with how many photos match each."""
    if not semantic_enabled():
        return jresp(request, {"enabled": False, "concepts": []})
    con = db()
    try:
        valid = [r[0] for r in con.execute("SELECT id FROM photos")]
    finally:
        con.close()
    return jresp(request, {"enabled": True, "indexing": semantic.running, "concepts": concepts.summary(valid)})


@router.get("/api/duplicates")
def api_duplicates(request: Request):
    """Groups of files with identical content (same digiKam file hash and size)."""
    con = db()
    try:
        rows = con.execute(
            "SELECT p.id, p.hash, p.size, p.w, p.h, p.video, p.ts, p.name, a.path FROM photos p "
            "JOIN albums a ON a.id = p.album_id WHERE (p.hash, p.size) IN (SELECT hash, size FROM photos "
            "WHERE hash IS NOT NULL AND hash != '' GROUP BY hash, size HAVING count(*) > 1) "
            "ORDER BY p.size DESC, p.hash, a.path, p.name").fetchall()
    finally:
        con.close()
    groups, cur = [], None
    for r in rows:
        if cur is None or cur["key"] != (r[1], r[2]):
            cur = {"key": (r[1], r[2]), "size": r[2], "items": []}
            groups.append(cur)
        cur["items"].append({"id": r[0], "w": r[3], "h": r[4], "video": r[5], "ts": r[6], "name": r[7], "album": r[8]})
    for g in groups:
        del g["key"]
    return jresp(request, {"groups": groups, "wasted": sum(g["size"] * (len(g["items"]) - 1) for g in groups)})



@router.get("/manifest.webmanifest")
def api_manifest():
    """The PWA manifest, with the configured name (the file in static/ is the template)."""
    data = json.loads((config.STATIC_DIR / "manifest.webmanifest").read_text())
    name = app_name()
    data["name"] = name
    if len(name) <= 12:
        data["short_name"] = name
    return JSONResponse(data, media_type="application/manifest+json")


CACHES.extend([_photos_cached, _meta, _events])
