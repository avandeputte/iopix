"""Face quality checks and the change log with its restores."""

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from app import config
from app.digikam import dbsync
from app.photos import history
from app.data import state as appstate
from app.core import DB_ERRORS
from app.core import _kick
from app.core import _rebuild_after_write


router = APIRouter()

@router.get("/api/quality")
def api_quality():
    """Maintenance › Metadata quality: face regions stored twice, the same person in two places, broken rectangles."""
    from app.faces import facequality
    try:
        return facequality.summary()
    except (*DB_ERRORS, dbsync.SyncError) as e:
        raise HTTPException(503, f"Could not read the face regions: {str(e)[:200]}")


@router.post("/api/quality/accept")
async def api_quality_accept(request: Request):
    """Body {photos: [ids], check: "unnamed", on: true}: accept these findings as they are (on: false takes it back)."""
    from app.faces import facequality
    body = await request.json()
    try:
        n = await run_in_threadpool(facequality.accept, body.get("photos") or [], body.get("check", "unnamed"), bool(body.get("on", True)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"accepted": n}


@router.get("/api/quality/items")
def api_quality_items(check: str, offset: int = 0, limit: int = 120):
    """The next entries of one Face quality list."""
    from app.faces import facequality
    try:
        return facequality.items(check, offset, limit)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/quality/accepted")
def api_quality_accepted(check: str = "unnamed"):
    """The photos accepted as they are for a check."""
    from app.faces import facequality
    return {"photos": sorted(facequality._accepted(check))}


@router.post("/api/quality/ignore")
async def api_quality_ignore(request: Request):
    """Body {photos: [ids]}: every unnamed face on them becomes "not a face to name"."""
    from app.faces import facequality
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to digiKam's database is turned off")
    body = await request.json()
    try:
        r = await run_in_threadpool(facequality.ignore_unnamed, [int(p) for p in body.get("photos") or []])
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    _kick.set()
    return r


@router.post("/api/quality/fit")
async def api_quality_fit(request: Request):
    """Body {photos?: [ids]}: trim face rectangles that stick out of their photo to its edges, remove empty ones."""
    from app.faces import facequality
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to digiKam's database is turned off")
    body = await request.json() if (await request.body()) else {}
    try:
        r = await run_in_threadpool(facequality.fit_inside, body.get("photos"))
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    _kick.set()
    return r


@router.post("/api/quality/fix")
async def api_quality_fix(request: Request):
    """Body {files: false}: remove the near-duplicate face regions (one kept per group) and rewrite those sidecars;
    {files: true}: then the image files that still carry the duplicates. In the background."""
    from app.faces import facequality
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to digiKam's database is turned off")
    files = bool((await request.json() if (await request.body()) else {}).get("files"))
    try:
        n = await run_in_threadpool(facequality.fix_files if files else facequality.fix_near_duplicates, _rebuild_after_write)
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    return {"photos": n}


@router.get("/api/history")
def api_history(photo: int = None, limit: int = 100, offset: int = 0, action: str = None, q: str = None, person: int = None,
                user: str = None, since: int = None, until: int = None):
    """The edit log: every change the viewer made, newest first; for one photo with ?photo=; searchable by words (summary,
    file name, user), a person, a user and a time range."""
    r = history.entries(photo, max(1, min(limit, 500)), max(0, offset), action or None, q, person, user or None, since, until)
    if not offset:
        r["users"] = history.users()
    return r


@router.post("/api/history/{entry_id}/restore")
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
    await run_in_threadpool(_rebuild_after_write)
    return r
