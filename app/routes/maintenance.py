"""Maintenance: background analysis and models, updates, restart, the server log, the file scanner and the library merge."""
import json
import os
import shutil
import threading
import time

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.ai import analysis, jobs_meta, models as dlmodels
from app import access, config, events, jobs as bgjobs, logs, updates, users
from app.digikam import dbsync, dkdb, merge
from app.faces import ages, facedetect, facevecs
from app.photos import edits, paths, scanner
from app.data import index, state as appstate, vdb
from app.geo import maps
from app.ai.semantic import _ready as semantic_ready, enabled as semantic_enabled, ENGINES, semantic
from app.core import _compute_tag_covers, _face_detect_allowed, _start_prewarm, app_name, db, DB_ERRORS, state

from app.routes.settings import db_settings_public

router = APIRouter()


def _dir_size(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


_sizes = {"at": 0.0, "values": None, "computing": False}  # the storage figures: a walk of the cache folders (100k+ files) is not done per page open


_sizes_lock = threading.Lock()


SIZES_TTL = 300


STORAGE_DIRS = (("thumbnails", "cache/t"), ("previews", "cache/p"), ("faces", "cache/f"), ("models", "models"),
                ("backups", "sidecar-backups"), ("fileBackups", "file-backups"), ("dbBackups", "db-backups"), ("deletedPhotos", "deleted-photos"))


def _storage_sizes(refresh: bool = False) -> dict:
    """The size of each part of the data folder, remembered for a few minutes. The first call measures; later ones
    answer at once and start a fresh measurement in the background when the figures are older than SIZES_TTL
    (or when asked to refresh), so the page shows the last known figures while it runs."""
    def measure():
        values = {k: _dir_size(config.DATA_DIR / v) for k, v in STORAGE_DIRS}
        with _sizes_lock:
            _sizes.update(at=time.time(), values=values, computing=False)
        return values
    with _sizes_lock:
        values, stale = _sizes["values"], time.time() - _sizes["at"] > SIZES_TTL
        if values is not None and (stale or refresh) and not _sizes["computing"]:
            _sizes["computing"] = True
            threading.Thread(target=measure, name="storage-sizes", daemon=True).start()
    if values is None:
        values = measure()
    return {**values, "measuredAt": int(_sizes["at"])}


@router.get("/api/analysis", dependencies=[access.public])
def api_analysis(request: Request):
    """Status of every background analysis, for the AI analysis page."""
    con = db()
    try:
        total, stills = con.execute("SELECT count(*), sum(video = 0) FROM photos").fetchone()
    finally:
        con.close()
    act = semantic.active if semantic_enabled() else None
    if act is not None:
        act.load()
    names = {k: dlmodels.REGISTRY[k]["label"] for k in ENGINES}
    models = [{"key": k, "label": names[k], "installed": semantic_ready(k), "chosen": semantic.chosen() == k,
               "active": act is not None and act.name == k} for k in ENGINES]
    jobs = jobs_meta.rows(total, stills, signed_in=users.is_signed_in(request.state.user))
    return {"jobs": jobs, "photos": total, "models": models if act is not None else [], "queue": bgjobs.status()}


@router.get("/api/models", dependencies=[access.public])
def api_models():
    """The downloadable models and data: what is installed, and the last update check."""
    return dlmodels.listing()


@router.post("/api/models/check", dependencies=[access.admin])
async def api_models_check():
    """Ask each source whether the installed files have changed since they were downloaded (network)."""
    await run_in_threadpool(dlmodels.check_all)
    return dlmodels.listing()


@router.post("/api/models/download", dependencies=[access.admin])
async def api_models_download(request: Request):
    """Body: {key}. Downloads (again) in the background; the server itself stays offline."""
    key = (await request.json()).get("key")
    if key not in dlmodels.REGISTRY:
        raise HTTPException(400, "Unknown model")
    try:
        dlmodels.download(key)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@router.get("/api/update", dependencies=[access.admin])
def api_update(request: Request):
    """Installed commit, the last GitHub check, and whether update.sh can run here (admins: the log names server paths)."""
    return updates.status()


@router.post("/api/update", dependencies=[access.admin])
async def api_update_run(request: Request):
    """Body: {action: check | run, ref?}. check asks GitHub (network); run starts update.sh
    (admin), optionally for a given commit or branch."""
    body = await request.json()
    action = body.get("action")
    if action == "check":
        return await run_in_threadpool(updates.check)
    if action == "run":
        try:
            updates.run(str(body.get("ref") or ""))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}
    if action == "token":  # admin: the MCP update token (shown once here, never in /api/update)
        from app.main import mcp_enabled
        return {"token": updates.admin_token(), "fromEnv": bool(os.environ.get("MCP_ADMIN_TOKEN")), "mcpEnabled": mcp_enabled()}
    if action == "regenerate_token":
        if os.environ.get("MCP_ADMIN_TOKEN"):
            raise HTTPException(400, "The token comes from MCP_ADMIN_TOKEN in the environment; change it there")
        return {"token": updates.regenerate_token()}
    raise HTTPException(400, "action must be check, run, token or regenerate_token")


@router.post("/api/restart", dependencies=[access.admin])
def api_restart():
    """Exit; launchd (KeepAlive) or Docker (restart: unless-stopped) starts the viewer again."""
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True}


@router.get("/api/log", dependencies=[access.admin])
def api_log(lines: int = 300, level: str = "any", contains: str = "", requests: int = 0):
    """Maintenance › Server log: the end of data/viewer.log, filtered (admins)."""
    return logs.tail(lines, contains[:200], level if level in ("any", "warning", "error", "auth") else "any", bool(requests))


@router.get("/api/log/download", dependencies=[access.admin])
def api_log_download():
    if not config.LOG_FILE.exists():
        raise HTTPException(404, "No log file yet")
    return FileResponse(config.LOG_FILE, media_type="text/plain", filename="iopix-viewer.log", content_disposition_type="attachment")


@router.get("/api/maintenance", dependencies=[access.public])
def api_maintenance(request: Request):
    """Library and storage facts for the Settings page (paths, databases and storage for admins only)."""
    con = db()
    try:
        built = con.execute("SELECT value FROM meta WHERE key = 'built'").fetchone()
        n = con.execute("SELECT count(*) FROM photos").fetchone()[0]
    finally:
        con.close()
    dk_mtime = dkdb.last_change_time()
    free = shutil.disk_usage(str(config.DATA_DIR)).free
    out = {
        "photos": n, "indexBuilt": int(built[0]) if built else None, "digikamSaved": dk_mtime,
        "digikamRunning": dbsync.digikam_running(), "pendingEdits": edits.pending_count(),
        "paths": {"library": str(config.PHOTOS_ROOT), "digikamDb": dkdb.describe(), "data": str(config.DATA_DIR)},
        "storage": _storage_sizes(request.query_params.get("refresh") == "1") | {
            "index": config.INDEX_DB.stat().st_size if config.INDEX_DB.exists() else 0,
            "viewerDb": vdb.SQLITE_FILE.stat().st_size if not vdb.is_mysql() and vdb.SQLITE_FILE.exists() else 0,
            "viewerDbError": vdb.error,  # MySQL configured but unusable at start: the SQLite file is in use instead
            "embeddings": sum(e.file.stat().st_size for e in semantic.engines.values() if e.file.exists()),
            "free": free},
        "writeFiles": edits.writes_files(), "readOnly": appstate.read_only(), "readOnlyLocked": config.READ_ONLY, "digikam": index.status(), "appName": app_name(), "appNameSet": appstate.get_setting("app_name", ""),
        "mcpEnabled": appstate.get_setting("mcp_enabled", "0") == "1", "mcpLocked": os.environ.get("MCP", "").strip() == "1",
        "updateCheck": updates.auto_check_enabled(), "updateCheckLocked": os.environ.get("UPDATE_CHECK", "").strip() == "0", "database": db_settings_public(), "mapProvider": appstate.get_setting("map_provider", "osm"), "googleMapsKeySet": bool(maps.key()), "googleServerKeySet": bool(appstate.get_setting("google_server_key", "") or os.environ.get("GOOGLE_SERVER_KEY", "")), "sidecarCompat": paths.compat_naming(), "searchModel": semantic.chosen(),
        "templates": json.loads(appstate.get_setting("templates", "[]")), "templateFields": list(edits.TEMPLATE_FIELDS),
        "recentTags": json.loads(appstate.get_setting("recent_tags", "[]")),
        "scanner": _scanner_state(),
    }
    if not users.is_admin(request.state.user):
        for k in ("paths", "database", "storage", "albumRoots", "album_roots"):
            out.pop(k, None)
    return out


def _scanner_state() -> dict:
    return {"enabled": scanner.enabled(), "interval": scanner.interval(), "running": scanner.progress["running"], "last": scanner.progress["last"]}


@router.get("/api/scan", dependencies=[access.public])
def api_scan_status():
    return _scanner_state()


@router.post("/api/scan", dependencies=[access.admin])
async def api_scan_run():
    """Scan the album folders now (also when the setting is off): new, changed and vanished files and folders."""
    if not config.DB_WRITE:
        raise HTTPException(403, "Writing to digiKam's database is turned off (DB_WRITE=0)")
    if scanner.progress["running"]:
        raise HTTPException(409, "A scan is already running")
    scanner.run_soon()
    return {"started": True}


@router.post("/api/maintenance", dependencies=[access.admin])
async def api_maintenance_run(request: Request):
    """Body: {action}. reindex: rebuild the search index from digiKam's database now.
    thumbs: throw away all grid thumbnails and rebuild. previews / faces: clear those caches.
    covers: pick tag cover photos again."""
    body = await request.json()
    action = body.get("action")
    if action == "reindex":
        await run_in_threadpool(index.build)
        state["version"] = None  # so _refresh treats the new index as a change
        events.kick()
    elif action in ("thumbs", "previews", "faces"):
        await run_in_threadpool(shutil.rmtree, config.CACHE_DIR / {"thumbs": "t", "previews": "p", "faces": "f"}[action], ignore_errors=True)
        if action == "thumbs":
            await run_in_threadpool(_start_prewarm)
    elif action == "covers":
        threading.Thread(target=_compute_tag_covers, args=(state.get("source"),), daemon=True).start()
    else:
        raise HTTPException(400, "Unknown action")
    return {"ok": True}


@router.get("/api/library/merge", dependencies=[access.admin])
def api_library_merge_plan():
    """What merging the changes made with sync off into digiKam's database would do (admins)."""
    try:
        return merge.plan()
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))


@router.post("/api/library/merge", dependencies=[access.admin_write])
async def api_library_merge():
    """Merge the changes made with sync off into digiKam's database (backup first), then copy the library from digiKam again."""
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to digiKam's database is turned off")
    try:
        r = await run_in_threadpool(merge.apply)
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    except DB_ERRORS as e:
        raise HTTPException(500, f"The merge failed and was rolled back: {e}")
    await run_in_threadpool(events.after_write)
    return r


@router.post("/api/analysis/run", dependencies=[access.admin])
async def api_analysis_run(request: Request):
    """Body: {job: all|embeddings|quality|vision|objects, reset: bool}. Without reset it only
    picks up photos that are new or changed; with reset that analysis starts over."""
    body = await request.json()
    job, reset = body.get("job", "all"), bool(body.get("reset"))
    if job == "facedetect":  # writes faces into digiKam's database, so only ever on request
        _face_detect_allowed()
        if reset:
            facedetect.reset()
        n = facedetect.start(after=events.after_write)
        return {"ok": True, "queued": n}
    if reset:
        if job == "embeddings" and semantic_enabled():
            (semantic.upgrade or semantic.active).reset()
        elif job in ("quality", "vision", "objects", "captions", "looks"):
            analysis.reset(job)
        elif job == "faces":
            facevecs.reset()
        elif job == "ages":
            ages.reset()
        elif job == "facedetect":
            facedetect.reset()
        else:
            raise HTTPException(400, "Choose one analysis to run again from scratch")
    await run_in_threadpool(_start_prewarm)
    events.kick()
    return {"ok": True}
