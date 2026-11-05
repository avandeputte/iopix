"""Maintenance: background analysis and models, the file scanner and the library merge. The version, updates, restart and the
server log (Maintenance › Updates and logs) are iosuite_shared's server_router (/api/server, /api/log), mounted in app/main.py."""
import json
import os
import shutil
import threading
import time

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from app.ai import analysis, clothing, jerseys, jobs_meta, models as dlmodels, rotation, videos
from app import access, config, events, jobs as bgjobs, users
from app.digikam import dbsync, dkdb, merge
from app.faces import ages, facedetect, facevecs
from app.photos import edits, paths, scanner
from app.data import index, state as appstate, vdb
from app.geo import maps
from app.ai.semantic import ready as semantic_ready, enabled as semantic_enabled, ENGINES, semantic
from app.guards import face_detect_allowed
from app.http import json_body
from app.runtime import DB_ERRORS, app_name, compute_tag_covers, db, start_prewarm, state

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
    return {"jobs": jobs, "photos": total, "models": models if act is not None else [], "queue": bgjobs.status(), "progress": bgjobs.progress_all()}


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
    key = (await json_body(request)).get("key")
    if key not in dlmodels.REGISTRY:
        raise HTTPException(400, "Unknown model")
    try:
        dlmodels.download(key)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


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
            "viewerDb": vdb.describe(),  # where the viewer's own data lives (a server, not a file in the data folder)
            "viewerDbError": vdb.error,
            "embeddings": sum(e.file.stat().st_size for e in semantic.engines.values() if e.file.exists()),
            "free": free},
        "writeFiles": edits.writes_files(), "readOnly": appstate.read_only(), "readOnlyLocked": config.READ_ONLY, "digikam": index.status(), "appName": app_name(), "appNameSet": appstate.get_setting("app_name", ""),
        "mcpEnabled": appstate.get_setting("mcp_enabled", "0") == "1", "mcpLocked": config.MCP_FORCED,
        "database": db_settings_public(), "mapProvider": appstate.get_setting("map_provider", "osm"), "googleMapsKeySet": bool(maps.key()), "googleServerKeySet": bool(appstate.get_setting("google_server_key", "") or config.GOOGLE_SERVER_KEY), "sidecarCompat": paths.compat_naming(), "searchModel": semantic.chosen(),
        "templates": json.loads(appstate.get_setting("templates", "[]")), "templateFields": list(edits.TEMPLATE_FIELDS),
        "recentTags": json.loads(appstate.get_setting("recent_tags", "[]")),
        "scanner": _scanner_state(),
    }
    if not users.is_admin(request.state.user):
        for k in ("paths", "database", "storage", "albumRoots", "album_roots"):
            out.pop(k, None)
        out["digikam"] = {k: v for k, v in out["digikam"].items() if k != "error"}  # the error names hosts and paths
    if not users.can_write(request.state.user):
        out["templates"], out["recentTags"] = [], []
    return out


def _scanner_state() -> dict:
    return {"enabled": scanner.enabled(), "interval": scanner.interval(), "running": scanner.progress["running"], "last": scanner.progress["last"]}


@router.get("/api/scan", dependencies=[access.public])
def api_scan_status():
    return _scanner_state()


@router.post("/api/scan", dependencies=[access.admin_write])
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
    body = await json_body(request)
    action = body.get("action")
    if action == "reindex":
        await run_in_threadpool(index.build)
        state["version"] = None  # so runtime.refresh treats the new index as a change
        events.kick()
    elif action in ("thumbs", "previews", "faces"):
        await run_in_threadpool(shutil.rmtree, config.CACHE_DIR / {"thumbs": "t", "previews": "p", "faces": "f"}[action], ignore_errors=True)
        if action == "thumbs":
            await run_in_threadpool(start_prewarm)
    elif action == "covers":
        threading.Thread(target=compute_tag_covers, args=(state.get("source"),), daemon=True).start()
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
    body = await json_body(request)
    return await run_in_threadpool(start_analysis, body.get("job", "all"), bool(body.get("reset")))


def start_analysis(job: str = "all", reset: bool = False) -> dict:
    """What the Run / Run again buttons do (and the MCP tool run_analysis): queue one analysis, or all of them."""
    if job == "facedetect":  # writes faces into digiKam's database, so only ever on request
        face_detect_allowed()
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
        elif job == "jerseys":
            jerseys.reset()
        elif job == "clothing":
            clothing.reset()
        elif job == "rotation":
            rotation.reset()
        elif job == "videos":
            videos.reset()
        else:
            raise HTTPException(400, "Choose one analysis to run again from scratch")
    start_prewarm()
    events.kick()
    return {"ok": True}
