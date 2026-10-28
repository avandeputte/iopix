"""Face quality checks and the change log with its restores."""

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from app import access, config, events
from app.digikam import dbsync
from app.photos import filecheck, history, picklabels
from app.data import state as appstate
from app.core import DB_ERRORS
from app.faces import facequality


router = APIRouter()

@router.get("/api/quality", dependencies=[access.admin])
def api_quality():
    """Maintenance › Metadata quality: face regions stored twice, the same person in two places, broken rectangles."""
    try:
        return facequality.summary()
    except (*DB_ERRORS, dbsync.SyncError) as e:
        raise HTTPException(503, f"Could not read the face regions: {str(e)[:200]}")


@router.post("/api/quality/accept", dependencies=[access.admin])
async def api_quality_accept(request: Request):
    """Body {photos: [ids], check: "unnamed", on: true}: accept these findings as they are (on: false takes it back)."""
    body = await request.json()
    try:
        n = await run_in_threadpool(facequality.accept, body.get("photos") or [], body.get("check", "unnamed"), bool(body.get("on", True)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"accepted": n}


@router.get("/api/quality/items", dependencies=[access.admin])
def api_quality_items(check: str, offset: int = 0, limit: int = 120):
    """The next entries of one Face quality list."""
    try:
        return facequality.items(check, offset, limit)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/quality/accepted", dependencies=[access.admin])
def api_quality_accepted(check: str = "unnamed"):
    """The photos accepted as they are for a check."""
    return {"photos": sorted(facequality.accepted_ids(check))}


@router.post("/api/quality/ignore", dependencies=[access.admin])
async def api_quality_ignore(request: Request):
    """Body {photos: [ids]}: every unnamed face on them becomes "not a face to name"."""
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to digiKam's database is turned off")
    body = await request.json()
    try:
        r = await run_in_threadpool(facequality.ignore_unnamed, [int(p) for p in body.get("photos") or []])
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    events.kick()
    return r


@router.post("/api/quality/fit", dependencies=[access.admin])
async def api_quality_fit(request: Request):
    """Body {photos?: [ids]}: trim face rectangles that stick out of their photo to its edges, remove empty ones."""
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to digiKam's database is turned off")
    body = await request.json() if (await request.body()) else {}
    try:
        r = await run_in_threadpool(facequality.fit_inside, body.get("photos"))
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    events.kick()
    return r


@router.post("/api/quality/fix", dependencies=[access.admin])
async def api_quality_fix(request: Request):
    """Body {files: false}: remove the near-duplicate face regions (one kept per group) and rewrite those sidecars;
    {files: true}: then the image files that still carry the duplicates. In the background."""
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to digiKam's database is turned off")
    files = bool((await request.json() if (await request.body()) else {}).get("files"))
    try:
        n = await run_in_threadpool(facequality.fix_files if files else facequality.fix_near_duplicates, events.after_write)
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    return {"photos": n}


@router.get("/api/filecheck", dependencies=[access.admin])
def api_filecheck():
    """Maintenance › Damaged files: what the file check found so far, and its progress."""
    return filecheck.report()


@router.post("/api/filecheck/run", dependencies=[access.admin])
async def api_filecheck_run(request: Request):
    """Body {redo: false}: check the files not checked yet (or changed since); redo: every file again. In the background."""
    body = await request.json() if (await request.body()) else {}
    return {"queued": await run_in_threadpool(filecheck.start, bool(body.get("redo")))}


@router.post("/api/filecheck/repair", dependencies=[access.admin_write])
async def api_filecheck_repair(request: Request):
    """Body {ids: [...]}: add the missing end marker to those JPEGs (backed up first, undoable from the edit log)."""
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to the library is turned off")
    body = await request.json()
    try:
        r = await run_in_threadpool(filecheck.repair, [int(i) for i in body.get("ids") or []])
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    if r["repaired"]:
        await run_in_threadpool(events.after_write)
    return r


@router.post("/api/filecheck/keep", dependencies=[access.admin_write])
async def api_filecheck_keep(request: Request):
    """Body {ids: [...]}: crop those damaged JPEGs losslessly to the part of the picture that is intact (backed up
    first, undoable from the edit log)."""
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to the library is turned off")
    body = await request.json()
    try:
        r = await run_in_threadpool(filecheck.keep_intact, [int(i) for i in body.get("ids") or []])
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    if r["cropped"]:
        await run_in_threadpool(events.after_write)
    return r


@router.get("/api/picklabels", dependencies=[access.admin])
def api_picklabels():
    """Maintenance › Pick labels: how many photos carry each of digiKam's pick labels, and the clearing's progress."""
    return {"counts": picklabels.counts(), "progress": dict(picklabels.progress)}


@router.post("/api/picklabels/clear", dependencies=[access.admin_write])
async def api_picklabels_clear(request: Request):
    """Body {levels: [1, 2, 3]}: take those pick labels (Rejected, Pending, Accepted) off every photo, in digiKam's
    database now (after a full backup) and in the sidecars in the background; each photo's change is in the edit log."""
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to the library is turned off")
    body = await request.json()
    try:
        n = await run_in_threadpool(picklabels.clear, body.get("levels") or [], True, events.kick)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    await run_in_threadpool(events.after_write)
    return {"cleared": n}


@router.get("/api/history", dependencies=[access.editor])
def api_history(photo: int = None, limit: int = 100, offset: int = 0, action: str = None, q: str = None, person: int = None,
                user: str = None, since: int = None, until: int = None):
    """The edit log: every change the viewer made, newest first; for one photo with ?photo=; searchable by words (summary,
    file name, user), a person, a user and a time range."""
    r = history.entries(photo, max(1, min(limit, 500)), max(0, offset), action or None, q, person, user or None, since, until)
    if not offset:
        r["users"] = history.users()
    return r


@router.post("/api/history/{entry_id}/restore", dependencies=[access.editor_write])
async def api_history_restore(entry_id: int, request: Request):
    """Body: {kind: undo|sidecar|original|deleted}: bring back what that change log entry allows."""
    body = await request.json()
    try:
        r = await run_in_threadpool(history.restore, entry_id, str(body.get("kind", "")))
    except (LookupError, FileNotFoundError, FileExistsError, ValueError) as e:
        raise HTTPException(409, str(e))
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    except DB_ERRORS as e:
        raise HTTPException(500, f"The restore failed and was rolled back: {e}")
    await run_in_threadpool(events.after_write)
    return r
