"""Metadata editing: sidecars, image files when enabled, digiKam's database on request; the consistency report; GPX tracks; place tags and keyword tags."""
import json
import logging
import re
import secrets
import time

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from app import config
from app.photos import consistency, edits, metaedit, placetags, tagmanager
from app.digikam import dbsync, dkdb
from app.geo import gpx
from app.data import state as appstate
from app.core import _form_items, _kick, _rebuild_after_write, db, DB_ERRORS, resolve
from app.digikam.dkdb import chunked


router = APIRouter()

# --------------------------------------------------------------------------
# Metadata editing (sidecars, image files when enabled, digiKam's database on request)
# --------------------------------------------------------------------------

@router.get("/api/metadata/{photo_id}")
def api_metadata(photo_id: int):
    """Every metadata field of the photo's file and sidecar (exiftool, grouped), with which ones can be edited."""
    try:
        return metaedit.read(photo_id)
    except dbsync.SyncError as e:
        raise HTTPException(404, str(e))


@router.post("/api/metadata/{photo_id}")
async def api_metadata_write(photo_id: int, request: Request):
    """Body {changes: [{group, tag, value}]}: write fields (XMP to the sidecar, EXIF/GPS/IPTC to the file), then read the
    photo's database rows again from its metadata."""
    if not (config.DB_WRITE and edits.available()):
        raise HTTPException(403, "Editing is turned off")
    body = await request.json()
    try:
        r = await run_in_threadpool(metaedit.write, photo_id, list(body.get("changes") or [])[:200])
    except dbsync.SyncError as e:
        raise HTTPException(409 if "digiKam is running" in str(e) else 400, str(e))
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/metadata/{photo_id}/reread")
async def api_metadata_reread(photo_id: int):
    """digiKam's Read Metadata From File: the photo's database rows filled again from its file and sidecar."""
    if not config.DB_WRITE:
        raise HTTPException(403, "Writing to digiKam's database is turned off (DB_WRITE=0)")
    try:
        r = await run_in_threadpool(metaedit.reread, photo_id)
    except dbsync.SyncError as e:
        raise HTTPException(409 if "digiKam is running" in str(e) else 400, str(e))
    await run_in_threadpool(_rebuild_after_write)
    return r


def _current_tag_paths(tag_ids: list, fallback: dict) -> list:
    """Tag paths as digiKam's database has them right now (a tag renamed or moved a moment ago, before the index caught
    up, must not be written under its old path); the index's paths when digiKam's database cannot be read."""
    try:
        src = dkdb.connect(write=False)
        try:
            tree = dkdb.tag_tree(src)
        finally:
            src.close()
        return [dkdb.tag_path(tree, t) if t in tree else fallback[t] for t in tag_ids]
    except Exception:
        return [fallback[t] for t in tag_ids]


@router.post("/api/edit")
async def api_edit(request: Request):
    """Change metadata for one or more photos. Written to XMP sidecars (and into the image
    files when that setting is on); digiKam's database only on a later explicit apply.
    Body: {ids: [...], changes: {rating, pick, color, title, caption, taken, shift, gps, template,
    add_tags: [tag ids], remove_tags: [tag ids]}}"""
    if not edits.available():
        raise HTTPException(503, "Editing is not available")
    body = await request.json()
    ids = [int(i) for i in body.get("ids", [])][:20000]
    raw = body.get("changes") or {}
    ch = {}
    if "rating" in raw:
        ch["rating"] = max(0, min(5, int(raw["rating"])))
    if "pick" in raw:
        ch["pick"] = max(0, min(3, int(raw["pick"])))
    if "color" in raw:
        ch["color"] = max(0, min(9, int(raw["color"])))
    for k in ("title", "caption"):
        if k in raw:
            ch[k] = str(raw[k] or "").strip()[:4000]
    per_photo = {}
    if raw.get("taken") and raw.get("date_precision") is None:
        t = str(raw["taken"])[:19]
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", t):
            raise HTTPException(400, "Date must be YYYY-MM-DDTHH:MM:SS")
        ch["taken"] = t
    prec = raw.get("date_precision")
    if prec is not None:
        from app.photos import dateprecision
        if prec not in dateprecision.CHOICES:
            raise HTTPException(400, f"date_precision is one of {', '.join(dateprecision.CHOICES)}")
        if raw.get("shift"):
            raise HTTPException(400, "A date shift and a change of how well the date is known go in separate edits")
        known = str(raw.get("taken") or "")[:19]  # "YYYY", "YYYY-MM" or a full date: the part that is known
        if known and not re.match(r"^\d{4}(-\d{2}(-\d{2}T\d{2}:\d{2}:\d{2})?)?$", known):
            raise HTTPException(400, "Date must be YYYY, YYYY-MM or YYYY-MM-DDTHH:MM:SS")
        ch.pop("taken", None)
    if raw.get("shift"):  # seconds, applied to each photo's own latest date inside edits.apply (under its lock)
        try:
            ch["shift"] = int(raw["shift"])
        except (TypeError, ValueError):
            raise HTTPException(400, "Shift must be a number of seconds")
    if "gps" in raw:
        g = raw["gps"]
        if g:
            try:
                lat, lon = float(g["lat"]), float(g["lon"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, "Location needs lat and lon")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise HTTPException(400, "Latitude or longitude out of range")
            alt = g.get("alt")
            ch["gps"] = {"lat": round(lat, 7), "lon": round(lon, 7), "alt": (round(float(alt), 1) if alt not in (None, "") else None)}
            try:  # an approximate position (only the town is known): how far off it may be, in metres
                acc = float(g.get("accuracy") or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "accuracy is a number of metres")
            if acc > 0:
                ch["gps"]["accuracy"] = round(min(acc, 1_000_000), 1)
        else:
            ch["gps"] = None
    if isinstance(raw.get("template"), dict):
        ch["template"] = {k: str(v or "").strip()[:1000] for k, v in raw["template"].items() if k in edits.TEMPLATE_FIELDS}
    con = db()
    try:
        for k in ("add_tags", "remove_tags"):
            try:
                tag_ids = [int(t) for t in raw.get(k, [])]
            except (TypeError, ValueError):
                raise HTTPException(400, "Tags are given by id")
            if tag_ids:
                rows = con.execute(f"SELECT id, full FROM tags WHERE person = 0 AND id IN ({','.join(map(str, tag_ids))})").fetchall()
                if len(rows) != len(set(tag_ids)):
                    raise HTTPException(400, "Only existing keyword tags can be edited here (people tags stay in digiKam)")
                ch[k] = _current_tag_paths([r[0] for r in rows], {r[0]: r[1] for r in rows})
        photos = [dict(r) for r in con.execute(
            f"SELECT id, path FROM photos WHERE id IN ({','.join(map(str, ids)) or 'NULL'})")]
    finally:
        con.close()
    if prec is not None and photos:
        # how well the date is known: its tag (created in digiKam first if need be), the known part in XMP, and a full
        # placeholder date (1 January, or the 1st of the month) built from the date given or each photo's own
        try:
            await run_in_threadpool(dateprecision.ensure_tags, prec)
            dateprecision.prepare(photos, prec, known, ch, per_photo, _current_dates([p["id"] for p in photos]) if len(known) < 19 else {})
        except dbsync.SyncError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
    if not (ch or per_photo) or not photos:
        raise HTTPException(400, "Nothing to change")
    result = await run_in_threadpool(edits.apply, photos, ch, per_photo)
    _kick.set()
    return result


def _current_dates(ids: list) -> dict:
    """{photo: "YYYY-MM-DDTHH:MM:SS"}: the date each photo has now, a pending edit's before the index's."""
    con = db()
    try:
        out = {}
        for chunk in chunked(ids, 500):
            out.update({r[0]: r[1] for r in con.execute(f"SELECT id, taken FROM photos WHERE id IN ({','.join(str(int(x)) for x in chunk)})")})
    finally:
        con.close()
    pend = edits.pending()
    out.update({i: pend[i]["taken"] for i in ids if pend.get(i, {}).get("taken")})
    return out


@router.post("/api/edit/revert")
async def api_edit_revert(request: Request):
    """Undo the viewer's edits to these photos by restoring the sidecar backup."""
    body = await request.json()
    ids = [int(i) for i in body.get("ids", [])][:20000]
    # edited in the index, or edited a moment ago and not in the index yet (an undo right after the edit)
    fresh = [i for i in ids if edits.pending_one(i)] if len(ids) <= 200 else list(set(ids) & set(edits.pending()))
    con = db()
    try:
        photos = [dict(r) for r in con.execute(
            f"SELECT id, path FROM photos WHERE (edited = 1 OR id IN ({','.join(map(str, fresh)) or 'NULL'})) AND id IN ({','.join(map(str, ids)) or 'NULL'})")]
    finally:
        con.close()
    result = await run_in_threadpool(edits.revert, photos)  # runs exiftool; keep the event loop free
    _kick.set()
    return result



# --------------------------------------------------------------------------
# Consistency report and writing pending edits to digiKam's database
# --------------------------------------------------------------------------

@router.get("/api/consistency")
def api_consistency(field: str = None, offset: int = 0, limit: int = 100):
    """The metadata consistency report: photos whose database, sidecar and file disagree."""
    return {"progress": consistency.progress, "summary": consistency.summary(),
            **consistency.report(field or None, max(0, offset), max(1, min(limit, 5000)))}


@router.post("/api/consistency/scan")
async def api_consistency_scan(request: Request):
    """Body: the usual filters (form-encoded) to scan a subset, or nothing for the whole library;
    rescan_all=0 only compares photos not checked since they last changed."""
    items = await _form_items(request)
    d = dict(items)

    def rows_for():  # the whole library's rows: not on the event loop
        where, params, _ = resolve([(k, v) for k, v in items if k != "rescan_all"])
        con = db()
        try:
            return [dict(r) for r in con.execute(
                f"SELECT id, path, name, video, hash, size, modified FROM photos WHERE {where} ORDER BY ts DESC", params)]
        finally:
            con.close()
    rows = await run_in_threadpool(rows_for)
    consistency.scan(rows, rescan_all=d.get("rescan_all", "1") != "0")
    return {"queued": len(rows)}


@router.post("/api/consistency/sync")
async def api_consistency_sync(request: Request):
    """Body: {ids, source: db|sidecar|file, targets: [...], fields: [...]}."""
    body = await request.json()
    ids = [int(i) for i in body.get("ids", [])][:20000]
    try:
        res = await run_in_threadpool(consistency.sync, ids, body.get("source"), body.get("targets") or [], body.get("fields"))
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    _kick.set()
    return res


@router.get("/api/edits/db")
def api_edits_db_status():
    return {"pending": edits.pending_count(), "digikamRunning": dbsync.digikam_running(), "enabled": config.DB_WRITE,
            "ask": config.DIGIKAM_CHECK == "none"}


@router.post("/api/edits/db")
async def api_edits_db_apply():
    """Write the pending edits into digiKam's database. Refused while digiKam is running."""
    if not config.DB_WRITE:
        raise HTTPException(403, "Writing to the digiKam database is turned off (DB_WRITE=0)")
    log = logging.getLogger("app.sync")
    if dkdb.standalone() and not dkdb.no_digikam():
        raise HTTPException(409, "Sync with digiKam is off: the edits stay in the sidecars and in the viewer, and go into digiKam's database when sync is turned on again")
    try:
        result = await run_in_threadpool(dbsync.apply_pending)  # every pending edit, with a backup first: minutes on a large backlog
    except dbsync.SyncError as e:
        log.warning("apply to digiKam's database refused: %s", e)
        raise HTTPException(409, str(e))
    except DB_ERRORS as e:
        log.error("apply to digiKam's database failed and was rolled back: %s", e)
        raise HTTPException(500, f"The update failed and was rolled back: {e}")
    log.info("edits applied to digiKam's database (on request): %d photos, %d skipped%s", result["applied"], len(result["skipped"]),
             "".join(f"; {s['id']}: {s['reason']}" for s in result["skipped"][:5]))
    await run_in_threadpool(_rebuild_after_write)
    return result



# --------------------------------------------------------------------------
# Positions from GPX tracks
# --------------------------------------------------------------------------

_gpx_tracks: dict = {}  # token -> parsed points, kept in memory for this server run


@router.post("/api/gpx/preview")
async def api_gpx_preview(request: Request):
    """Multipart: file (.gpx), ids (comma-separated), utc_offset, clock_shift, max_gap (seconds),
    only_missing (1 = skip photos that already have a position). Returns matches without writing."""
    form = await request.form()
    up = form.get("file")
    if up is None:
        raise HTTPException(400, "Upload a .gpx file")
    data = await run_in_threadpool(up.file.read, 50 * 1024 * 1024 + 1)  # no more than the cap is ever read into memory
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(400, "GPX file is larger than 50 MB")
    try:
        points = await run_in_threadpool(gpx.parse, data)  # up to 50 MB of XML; keep the event loop free
    except Exception as e:
        raise HTTPException(400, f"Could not read the GPX file: {e}")
    if not points:
        raise HTTPException(400, "The GPX file has no timed track points")
    ids = [int(x) for x in str(form.get("ids", "")).split(",") if x.strip().isdigit()][:20000]
    utc_offset, clock_shift = int(form.get("utc_offset") or 0), int(form.get("clock_shift") or 0)
    max_gap = max(10, min(int(form.get("max_gap") or 600), 86400))
    only_missing = str(form.get("only_missing", "1")) == "1"
    con = db()
    try:
        rows = con.execute(f"SELECT id, ts, lat, name FROM photos WHERE video = 0 AND id IN ({','.join(map(str, ids)) or 'NULL'})").fetchall()
    finally:
        con.close()
    photos = [{"id": r["id"], "ts": r["ts"], "name": r["name"]} for r in rows if not (only_missing and r["lat"] is not None)]
    matches = gpx.correlate(points, photos, utc_offset, clock_shift, max_gap)
    token = secrets.token_hex(8)
    now = time.time()  # a few recent tracks only: a 50 MB track parsed is a lot of points to keep for the server's lifetime
    for k in [k for k, (t, _p) in _gpx_tracks.items() if now - t > 1800]:
        del _gpx_tracks[k]
    while len(_gpx_tracks) >= 5:
        del _gpx_tracks[next(iter(_gpx_tracks))]
    _gpx_tracks[token] = (now, points)
    names = {p["id"]: p["name"] for p in photos}
    matched = [dict(m, name=names[m["id"]]) for m in matches if m["matched"]]
    return {"token": token, "track": gpx.summary(points), "considered": len(photos), "skipped_with_position": len(rows) - len(photos),
            "matched": len(matched), "matches": matched[:2000], "unmatched": len(matches) - len(matched)}


@router.post("/api/gpx/apply")
async def api_gpx_apply(request: Request):
    """Body: {token, ids, utc_offset, clock_shift, max_gap, only_missing}. Writes the matched
    positions as GPS edits (sidecar, file if enabled; database on the next apply)."""
    body = await request.json()
    points = (_gpx_tracks.get(str(body.get("token"))) or (None, None))[1]
    if not points:
        raise HTTPException(409, "The track preview has expired; load the GPX file again")
    ids = [int(i) for i in body.get("ids", [])][:20000]
    con = db()
    try:
        rows = con.execute(f"SELECT id, ts, lat, path FROM photos WHERE video = 0 AND id IN ({','.join(map(str, ids)) or 'NULL'})").fetchall()
    finally:
        con.close()
    only_missing = bool(body.get("only_missing", True))
    photos = [{"id": r["id"], "ts": r["ts"], "path": r["path"]} for r in rows if not (only_missing and r["lat"] is not None)]
    matches = gpx.correlate(points, photos, int(body.get("utc_offset") or 0), int(body.get("clock_shift") or 0), max(10, min(int(body.get("max_gap") or 600), 86400)))
    per_photo = {m["id"]: {"gps": {"lat": m["lat"], "lon": m["lon"], "alt": m["alt"]}} for m in matches if m["matched"]}
    targets = [p for p in photos if p["id"] in per_photo]
    if not targets:
        raise HTTPException(400, "No photo matches the track")
    result = await run_in_threadpool(edits.apply, targets, {}, per_photo)
    _kick.set()
    return result



# --------------------------------------------------------------------------
# Place tags and keyword tags (digiKam's database; digiKam must be closed)
# --------------------------------------------------------------------------

@router.get("/api/placetags")
def api_placetags_plan(ids: str = None):
    """What creating place tags would do, for the whole library or a comma-separated id list."""
    sel = [int(x) for x in ids.split(",") if x.strip().isdigit()] if ids else None
    p = placetags.plan(sel)
    p.pop("_todo", None)
    p.pop("_untag", None)
    return {**p, "progress": placetags.progress, "digikamRunning": dbsync.digikam_running(), "root": placetags.ROOT}


@router.post("/api/placetags")
async def api_placetags_run(request: Request):
    """Create missing Places/Country/Region/Town tags (digiKam closed) and assign them."""
    if not (config.DB_WRITE and edits.available()):
        raise HTTPException(403, "Needs editing and database writes enabled")
    if placetags.progress["running"]:
        raise HTTPException(409, "Already running")
    body = await request.json()
    sel = [int(i) for i in body.get("ids", [])] or None
    p = await run_in_threadpool(placetags.plan, sel)
    if p["missing_tags"] and dbsync.digikam_running():
        raise HTTPException(409, "digiKam is running. Quit digiKam first; new place tags are created in its database.")
    await run_in_threadpool(placetags.run, sel)
    return {"to_tag": p["to_tag"], "creating": len(p["missing_tags"])}


@router.post("/api/tags/recent")
async def api_tags_recent(request: Request):
    """Remember the tags just assigned, newest first (shown as a quick row like digiKam's)."""
    body = await request.json()
    ids = [int(i) for i in body.get("ids", [])][:20]
    old = [int(i) for i in json.loads(appstate.get_setting("recent_tags", "[]"))]
    new = ids + [i for i in old if i not in ids]
    appstate.set_setting("recent_tags", json.dumps(new[:12]))
    return {"recent": new[:12]}



@router.post("/api/tags/op")
def api_tag_op(op: dict):
    """Add, rename, move or delete a keyword tag (writes digiKam's database; digiKam must be closed)."""
    if not (config.DB_WRITE and edits.available()):
        raise HTTPException(403, "Tag management is turned off (needs DB_WRITE and EDITING)")
    try:
        result = tagmanager.apply(op)
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    except (*DB_ERRORS, ValueError) as e:
        raise HTTPException(500, f"The change failed and was rolled back: {e}")
    _kick.set()
    return result
