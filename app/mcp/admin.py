"""The maintenance tools, for admins: what the web app's Maintenance and Explore pages
offer, so an assistant can run the same reports, suggestions, edits and background jobs. Each tool
calls the module the page calls, with the same checks (read-only mode, digiKam closed, editing on),
so a change made here is logged and undoable exactly like one made in the browser."""
import threading
import time
from typing import Literal, Optional

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app import config, events, jobs
from app.photos import consistency, datecheck, dateprecision, edits, organize, tagmanager
from app.data import index, state as appstate
from app.ai.semantic import enabled as semantic_enabled
from app.digikam.dkdb import chunked, in_chunks
from app.digikam import dbsync
from app.ai import clothing, dateclues, jerseys, rotation
from app.faces import ages, facemgr, facequality
from app.geo import placeguess
from app.digikam.dbsync import SyncError
from app.http import MAX_IDS
from app.mcp.base import READ_ONLY, WRITES, clamp_limit, db, need_admin, resolve_album, server
from app.routes.editing import current_dates
from app.routes.maintenance import start_analysis

# a job that starts a background run but changes nothing by itself
STARTS_JOB = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
REWRITE_SIDECARS_MAX = 5000  # one exiftool run over that many sidecars takes minutes; a client pages beyond it
PRECISION_BATCH = 100  # photos per edits.apply call in the date-precision job: progress is reported per batch
TAG_WAIT_SECONDS = 120  # how long tag_op waits for the previous change's sidecar rewrites before giving up


@server.tool(annotations=READ_ONLY)
def consistency_report(field: str = "", offset: int = 0, limit: int = 500) -> dict:
    """The metadata consistency report (Maintenance › Metadata consistency): photos whose rating, title,
    caption or tags differ between digiKam's database, the XMP sidecar and the image file, with each
    source's value. field narrows it to one of rating|title|caption|tags. Admins only."""
    need_admin()
    return {"progress": dict(consistency.progress), "summary": consistency.summary(),
            **consistency.report(field or None, max(0, offset), clamp_limit(limit, 2000))}


@server.tool(annotations=WRITES)
def edit_photos(photo_ids: list[int], taken: Optional[str] = None, caption: Optional[str] = None, title: Optional[str] = None,
                rating: Optional[int] = None, apply_to_database: bool = True) -> dict:
    """Change the date taken ("YYYY-MM-DDTHH:MM:SS"), caption, title or rating of these photos exactly as the web app
    does: written to each sidecar (and the file when the viewer writes into files), in the edit log with undo, and with
    apply_to_database written into digiKam's database right away (digiKam closed). Admins only."""
    need_admin(writes=True)
    import re
    ch = {}
    if taken is not None:
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", taken):
            raise ToolError("taken must be YYYY-MM-DDTHH:MM:SS")
        ch["taken"] = taken
    for k, v in (("caption", caption), ("title", title)):
        if v is not None:
            ch[k] = v.strip()
    if rating is not None:
        ch["rating"] = max(0, min(5, int(rating)))
    if not ch:
        raise ToolError("Nothing to change")
    con = db()
    try:
        photos = [dict(r) for r in in_chunks(con, "SELECT id, path FROM photos WHERE id IN ({ids})", map(int, photo_ids))]
    finally:
        con.close()
    res = edits.apply(photos, ch)
    if apply_to_database and res["edited"]:
        pend = edits.pending()
        try:
            res["database"] = dbsync.apply_pending(pending={p["id"]: pend[p["id"]] for p in photos if p["id"] in pend})
        except dbsync.SyncError as e:
            res["database"] = {"error": str(e)}
    return res


@server.tool(annotations=STARTS_JOB)
def consistency_scan(rescan_all: bool = False) -> dict:
    """Start the metadata consistency check in the background, as Maintenance > Metadata consistency's buttons do:
    every photo (rescan_all) or those changed since they were last checked. consistency_report shows the progress and
    the results. Admins only."""
    need_admin()
    con = db()
    try:
        rows = [dict(r) for r in con.execute("SELECT id, path, name, video, hash, size, modified FROM photos ORDER BY ts DESC")]
    finally:
        con.close()
    consistency.scan(rows, rescan_all=bool(rescan_all))
    return {"queued": len(rows)}


@server.tool(annotations=WRITES)
def consistency_sync(photo_ids: list[int], source: str, targets: list[str], fields: list[str]) -> dict:
    """Copy the given fields of the given photos from one source (db|sidecar|file) to the targets, exactly
    as the consistency page's Sync does: sidecars and files are backed up first; writing the database needs
    digiKam closed and is backed up too. Returns {synced, skipped}. Admins only."""
    need_admin(writes=True)
    try:
        return consistency.sync([int(i) for i in photo_ids][:MAX_IDS], source, targets, fields)
    except (ValueError, dbsync.SyncError) as e:
        raise ToolError(str(e))


@server.tool(annotations=WRITES)
def rewrite_sidecars(photo_ids: list[int]) -> dict:
    """Rewrite these photos' XMP sidecars (tag list and face regions) from digiKam's database, as the viewer does
    after a tag or face change: for when the database was changed outside the viewer. Each sidecar is backed up
    before its first change. Returns {photos, tags_failed, regions}. Admins only."""
    need_admin(writes=True)
    ids = [int(i) for i in photo_ids][:REWRITE_SIDECARS_MAX]
    paths = dict(dbsync.photo_paths(ids))
    failed = edits.rewrite_tags([{"id": i, "path": p} for i, p in paths.items()])
    regions = facemgr.write_regions_many(list(paths))
    return {"photos": len(paths), "tags_failed": failed, "regions": {str(k): v for k, v in regions.items()}}


@server.tool(annotations=STARTS_JOB)
def face_quality() -> dict:
    """Face metadata quality (Maintenance > Face quality): regions of one person stored twice a few pixels apart
    (near), the same person in two separate places (separate), empty or out-of-photo rectangles (invalid), and
    the progress of the last clean-up. Admins only."""
    need_admin()
    return facequality.summary(limit=20)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
def rename_folder(folder: str, new_name: str) -> dict:
    """Rename a folder (its last path segment) on disk and in digiKam's database, exactly as the Folders page does:
    subfolders and sidecars follow, backups move with it. folder: its id or path (e.g. "2022/20221008 Soccer").
    digiKam must be closed. Admins only."""
    need_admin(writes=True)
    con = db()
    try:
        aid = resolve_album(con, folder)
    finally:
        con.close()
    try:
        r = organize.rename_folder(aid, new_name)
    except SyncError as e:
        raise ToolError(str(e))
    events.after_write()
    return r


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
def tag_op(op: Literal["person", "notperson", "merge", "rename", "move", "add"], tag_id: int = 0,
           into: int = 0, name: str = "", parent: int = 0) -> dict:
    """Change a tag exactly as the Tags page does (digiKam's database backed up first, sidecars rewritten after, an
    Edit log entry per photo): person = mark a keyword tag as a person; notperson = undo that; merge = move everything
    on tag_id (photos, face boxes) to the person or tag `into`, then remove tag_id; rename (name); move (parent, 0 = top
    level); add (name inside parent). Ids are those of list_tags/list_people. digiKam must be closed unless the viewer
    runs without it. Admins only."""
    need_admin(writes=True)
    if not (config.DB_WRITE and edits.available()):
        raise ToolError("Tag management is turned off (needs DB_WRITE and EDITING)")
    body = {"op": op, "id": tag_id, "into": into, "name": name, "parent": parent or None}
    until = time.time() + TAG_WAIT_SECONDS  # the previous change's sidecars are still being written: wait for them, as a person would
    while tagmanager.pending_jobs() and time.time() < until:
        time.sleep(0.5)
    try:
        r = tagmanager.apply(body)
    except (SyncError, ValueError) as e:
        raise ToolError(str(e))
    events.kick()
    return r


# the date-precision run: one at a time, started by set_date_precision and read back by it with dry_run
precision_job = {"running": False, "done": 0, "total": 0, "error": None}


@server.tool(annotations=WRITES)
def set_date_precision(precision: Literal["exact", "month", "year", "unknown"], years: list[int] = None,
                       ids: list[int] = None, dry_run: bool = True) -> dict:
    """Mark how well photos' dates are known, exactly as the date editor does (Edit > Date: exact, month and year,
    year only, unknown): the tag under "Date approximate", a placeholder date where a full one is required (each
    photo's own year/month kept; an unknown date stays as it is), an Edit log entry per photo (undoable). Photos are
    chosen by `ids`, or by the year of their current date (`years`, e.g. [1970, 1974]). dry_run (default) only counts:
    how many photos match, per year, and how many already have this precision; with dry_run=False the change runs in
    the background (call again with dry_run=True to see progress). Admins only."""
    need_admin(writes=True)
    if not (ids or years):
        raise ToolError("Give ids or years")
    con = index.open_index()
    try:
        where = (f"id IN ({','.join(str(int(i)) for i in ids)})" if ids
                 else f"substr(taken, 1, 4) IN ({','.join(repr(f'{int(y):04d}') for y in years)})")
        rows = con.execute(f"SELECT id, path, substr(taken, 1, 4), dprec FROM photos WHERE {where}").fetchall()
    finally:
        con.close()
    level = dateprecision.LEVELS.get(precision)
    per_year = {}
    for _, _, y, _ in rows:
        per_year[y] = per_year.get(y, 0) + 1
    out = {"photos": len(rows), "per_year": per_year, "already": sum(1 for r in rows if r[3] == level), "job": dict(precision_job)}
    if dry_run or not rows:
        return out
    if precision_job["running"]:
        raise ToolError("A previous run is still going")
    if not edits.available():
        raise ToolError("Editing is not available")
    todo = [{"id": r[0], "path": r[1]} for r in rows if r[3] != level]
    dateprecision.ensure_tags(precision)
    precision_job.update(running=True, done=0, total=len(todo), error=None)

    def run():
        try:
            for chunk in chunked(todo, PRECISION_BATCH):
                ch, per = {}, {}
                dateprecision.prepare(chunk, precision, "", ch, per, current_dates([p["id"] for p in chunk]))
                r = edits.apply(chunk, ch, per)
                precision_job["done"] += r["edited"]
                if r["failed"]:
                    precision_job["error"] = f"{len(r['failed'])} failed, e.g. {r['failed'][0]['error']}"
            events.kick()
        except Exception as e:
            precision_job["error"] = str(e)[:jobs.ERROR_TEXT_MAX]
        finally:
            precision_job["running"] = False
    threading.Thread(target=run, name="date-precision", daemon=True).start()
    return {**out, "started": len(todo)}


@server.tool(annotations=READ_ONLY)
def date_report(detail: bool = False, limit: int = 0) -> dict:
    """For the photos whose date does not match their folder (Maintenance > Metadata quality): every date stored in
    each file and its sidecar (EXIF, XMP, IPTC, QuickTime, GPS, the file's own modification time), and which agree
    with the folder. summary counts photos with some embedded date matching the folder, none, and whose embedded
    dates disagree with each other; tags_matching_the_folder says which tags those are. detail: per photo. Read-only.
    Admins only."""
    need_admin()
    return datecheck.wrongfolder(detail=detail, limit=limit)


@server.tool(annotations=READ_ONLY)
def date_suggestions(photo_ids: list[int] = None, limit: int = 100, offset: int = 0) -> dict:
    """Year suggestions for the photos marked "Date unknown" (Explore > Date suggestions), best supported first:
    {total, items: [{id, year, lo, hi, clues: [{kind: ages|people|look|camera, text, year, sd}], people}], people, sources}.
    photo_ids limits it to those photos. Read-only: using a suggestion is edit_photos with taken "YYYY" (a year-only date)
    or the page's Use button. Admins only."""
    need_admin()
    out = dateclues.suggestions(photo_ids)
    items = out["items"]
    return {"total": len(items), "items": items[offset:offset + clamp_limit(limit, 500)], "people": out["people"],
            "sources": {"ages": ages.available(), "look": semantic_enabled()}, "faces": list(ages.coverage()), "hints": dateclues.hints()}


@server.tool(annotations=READ_ONLY)
def place_suggestions(photo_ids: list[int] = None, limit: int = 100, offset: int = 0) -> dict:
    """Where the photos without a position were probably taken (Explore > Place suggestions), surest first: {total, items:
    [{id, lat, lon, km (how far off it may be), place, region, country, basis: between|near|day|day-time|stay, text, from}]}.
    Read-only: using one is a GPS edit with that accuracy (the page's Use button). Admins only."""
    need_admin()
    items = placeguess.suggestions(photo_ids)["items"]
    return {"total": len(items), "items": items[offset:offset + clamp_limit(limit, 1000)]}


@server.tool(annotations=READ_ONLY)
def jersey_suggestions(photo_ids: list[int] = None, limit: int = 200, offset: int = 0) -> dict:
    """Jersey numbers (Explore > Jersey numbers): the players suggested for people whose shirt number was read but whose
    face is not named: {total, items: [{id, number, tag, name, basis: folder|year, n, text, box}], players: [{tag, name,
    numbers}], read, named, coverage}. Read-only: using one is edit_photos / the page's Tag button, which adds the person's
    tag to the photo. Admins only."""
    need_admin()
    out = jerseys.suggestions(photo_ids)
    items = out["items"]
    return {"total": len(items), "items": items[offset:offset + clamp_limit(limit, 2000)], "players": out["players"],
            "read": out["read"], "named": out["named"], "coverage": list(jerseys.coverage())}


@server.tool(annotations=READ_ONLY)
def clothing_suggestions(photo_ids: list[int] = None, limit: int = 200, offset: int = 0) -> dict:
    """Clothing matches (Explore > Clothing matches): people probably present in a photo, matched by what they wear to the
    photos of the same event (one folder, one day) where their face is named: {total, items: [{id, tag, name, n, score,
    lead, box, event, text}], coverage}. Read-only: using one adds the person's tag to the photo (edit_photos / the page's
    Tag button). Admins only."""
    need_admin()
    items = clothing.suggestions(photo_ids)["items"]
    return {"total": len(items), "items": items[offset:offset + clamp_limit(limit, 2000)], "coverage": list(clothing.coverage())}


@server.tool(annotations=READ_ONLY)
def rotation_findings(photo_ids: list[int] = None, limit: int = 200, offset: int = 0) -> dict:
    """Photos that read as stored on their side or upside down (Maintenance > Wrongly rotated): {total, items: [{id, turn
    (quarter turns clockwise the file is off by), op (the lossless rotate that rights it: left|180|right), text, score}],
    dismissed, coverage, phase}. Read-only: righting one is the web app's Rotate button (POST /api/photos/transform).
    Admins only."""
    need_admin()
    out = rotation.findings(photo_ids)
    items = out["items"]
    return {"total": len(items), "items": items[offset:offset + clamp_limit(limit, 2000)], "dismissed": out["dismissed"],
            "coverage": list(rotation.coverage()), "phase": rotation.progress.get("phase")}


@server.tool(annotations=WRITES)
def set_date_hint(folder: str, not_before: Optional[int] = None, not_after: Optional[int] = None) -> dict:
    """A folder hint for the date suggestions: nothing in this folder (subfolders included; name, path such as "/1974"
    or id) was taken before not_before or after not_after. Both empty removes the hint. Written to iopix's own
    database only; the suggestions then keep to the window. Admins only."""
    need_admin()
    con = db()
    try:
        album = resolve_album(con, folder)
    finally:
        con.close()
    try:
        dateclues.set_hint(album, not_before, not_after)
    except ValueError as e:
        raise ToolError(str(e))
    return {"ok": True, "album": album, "hints": dateclues.hints()}


@server.tool(annotations=WRITES)
def run_analysis(job: str = "all", reset: bool = False) -> dict:
    """Start a background analysis as Maintenance > AI analysis does: job is all, embeddings, quality, vision, objects,
    captions, looks, faces, ages, jerseys, clothing, rotation, videos or facedetect (facedetect writes faces into digiKam's database). Without reset only
    new or changed photos are looked at; with reset that analysis starts over. Follow it with job_queue. Admins only."""
    need_admin(writes=(job == "facedetect"))
    from fastapi import HTTPException
    try:
        return start_analysis(job, reset)
    except HTTPException as e:
        raise ToolError(e.detail)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
def face_cleanup(files: bool = False) -> dict:
    """Remove the near-duplicate face regions, exactly as Maintenance > Face quality > Remove duplicates: one region
    of each group is kept, digiKam's database is backed up first, those photos' sidecars are rewritten (not the
    image files) and each photo gets a change-log entry. files=True, once that has finished: the image files of
    the cleaned photos that still carry the duplicates are rewritten too (each backed up first, pixels checked
    unchanged). Runs in the background; face_quality shows progress. Admins only."""
    need_admin()
    if not config.DB_WRITE or appstate.read_only():
        raise ToolError("Writing to digiKam's database is turned off")
    try:
        return {"photos": facequality.fix_files() if files else facequality.fix_near_duplicates()}
    except dbsync.SyncError as e:
        raise ToolError(str(e))
