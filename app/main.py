"""ioPix: a web front end for a digiKam library.

Who may call a route is declared on the route (app/access.py); iosuite-shared's middleware works out who is asking
(the shared session cookie, the identity a reverse proxy such as Cloudflare Access vouches for, HTTP Basic or a bearer
app password), whether sign-in is required at all, and refuses cross-site writes. The same scheme as every app of the
suite. The library (digiKam's database and the photo files) is only read here; the few endpoints that change it
(sidecars, image files, digiKam's database on request) declare access.editor_write or access.admin_write on their route
and go through the modules that own those writes."""
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi import HTTPException
from iosuite_shared import mcp as shared_mcp
from iosuite_shared.fastapi import public_user
from iosuite_shared.updates import setup_log

from app.ai import analysis, models as dlmodels
from app.faces import facedetect, facemgr
from app import config, events, jobs as bgjobs, mcp, thumbs, users
from app.auth import auth, updater
from app.photos import consistency, edits, history, lossless, scanner, tagmanager
from app.data import index as photo_index, state as appstate, vdb
from app.digikam import dkdb
from app.geo import maps, places
from app.ai.semantic import enabled as semantic_enabled, semantic
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes import auth as auth_routes, backup, browse, images, albums, editing, geo, maintenance, review, organize, faces, settings, users as users_routes
from app.runtime import state, kick, refresh, auto, watch, resolve, prewarmer, models_arrived, APP_NAME_DEFAULT, app_name, app_stamp, public_version
from app import __version__ as APP_VERSION

setup_log(config.LOG_FILE)  # the server log that Maintenance › Updates and logs shows; the app runs without it
__all__ = ["app", "state", "kick", "refresh", "auto", "watch", "resolve", "prewarmer"]  # tests and app.mcp reach these through app.main

@asynccontextmanager
async def lifespan(_app):
    appstate.init()
    if not vdb.ready():  # no viewer database (a fresh install, or its server is down): the set-up page only
        logging.getLogger("app.main").error("starting without a viewer database (%s): only the database set-up page answers until one is configured", vdb.error)
        async with shared_mcp.running(mcp.server):
            yield
        return
    users.init()
    maps.declare_keys()  # the Google keys live with the suite's keys and tokens
    edits.init()
    tagmanager.init()
    analysis.init()
    consistency.init()
    analysis.on_progress = kick.set  # fold new analysis results into the index as they arrive
    if tagmanager.pending_jobs():  # finish sidecar updates left over from a previous run
        tagmanager.start_worker()
    facemgr.resume_queue()  # and the face sidecar rewrites a restart interrupted
    refresh(initial=True)
    threading.Thread(target=thumbs.prune_previews, daemon=True).start()
    threading.Thread(target=watch, name="reindex", daemon=True).start()
    scanner.start(events.after_write)  # the file scanner (Maintenance › File scanner): passes only while it is on
    if dkdb.no_digikam() and scanner.enabled():
        scanner.run_soon()  # no digiKam to have found what was added while iopix was stopped: a pass right after start-up
    updater.start_daily()  # the daily "is there a newer version" check, when the setting allows it
    if config.AUTO_MODELS:  # containers and fresh installs: fetch missing models, then start the jobs that need them
        dlmodels.auto_download(config.AUTO_MODELS, on_done=models_arrived)
    async with shared_mcp.running(mcp.server):  # the MCP endpoint (/mcp) lives in this process
        yield


app = FastAPI(title="ioPix", version=APP_VERSION, lifespan=lifespan, docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
              description="A web front end for a digiKam photo library, for the family. Also an MCP endpoint (/mcp) for AI assistants.")

MAX_JSON_BYTES = 4 << 20   # a JSON request larger than this is refused
MAX_UPLOAD_BYTES = 2 << 30  # a multipart upload (photos, a library, a model): what the upload routes accept at most

# what a visitor may still fetch when sign-in is required: the app shell and its files (the map library among them),
# the API docs, a share link, and the MCP endpoint (its gate below)
OPEN_PATHS = ("/", "/sw.js", "/manifest.webmanifest", "/share", "/api/docs", "/api/openapi.json")
OPEN_PREFIXES = ("/static/", "/mcp")


def _open_to_visitors(path: str) -> bool:
    if path.startswith("/face/"):  # the avatars on the sign-in screen: only the faces accounts are linked to
        tail = path[6:].split("/")[0]
        return tail.isdigit() and int(tail) in users.linked_persons()
    return False


def mcp_enabled() -> bool:
    """Settings › Viewer › MCP server: off unless an admin turns it on (or MCP=1 in the environment)."""
    return shared_mcp.enabled(appstate.get_setting, config.MCP_FORCED)


_mcp_gate = auth.mcp_gate(mcp_enabled, on_user=mcp.current_user.set, settings_page="Settings › Viewer › MCP server")


def _before(request: Request):
    """Once the user is known: who the change history and private albums see, and the MCP endpoint's gate (the switch;
    an app password or session; every tool acts as that account, as in every app of the suite)."""
    user = request.state.user
    history.user.set(user["name"] if user else ("MCP" if request.url.path.startswith("/mcp") else "visitor"))
    appstate.viewer.set(user)  # private albums: seen only by their owner
    return _mcp_gate(request)


# who is asking (the shared session cookie, the proxy's identity, Basic, a bearer app password), whether sign-in is
# required at all, and the cross-site check on writes: iosuite-shared's middleware. What each route needs beyond
# that (an editor, an admin, an account) is declared on the route (app/access.py).
app.middleware("http")(auth.middleware(open_paths=OPEN_PATHS, open_prefixes=OPEN_PREFIXES, is_open=_open_to_visitors, before=_before))


async def _without_database(request: Request, call_next):
    """The viewer has no usable database of its own (vdb.ready() is false): the app shell and the status answer,
    the database set-up endpoints are served here directly, as an admin (there is nobody else yet, as on a fresh
    install; the sign-in middleware and the routes' own checks would need the database), and everything else
    says what is missing. The viewer restarts once a database is chosen. Once a database is configured, only
    Retry stays open (see below)."""
    path, method = request.url.path, request.method
    if path == "/api/status":
        return JSONResponse({"ok": False, "app": config.APP, "name": APP_NAME_DEFAULT, "version": APP_VERSION, "appName": APP_NAME_DEFAULT,
                             "needsDatabase": True, "databaseError": vdb.error, "databaseConfigured": vdb.configured(), "index": "", "stamp": ""})
    if path.startswith("/api/viewerdb"):
        if method not in ("GET", "HEAD", "OPTIONS") and auth.cross_site(request):  # the sign-in middleware's own rule
            return JSONResponse({"detail": "Requests from another site are refused"}, status_code=403)
        # a database is configured but did not answer at start (the server is down, or started after the viewer): nobody is
        # signed in, so nothing here may test another server with the stored password or point the viewer at another
        # database; only Retry, and the error. Changing the connection then is done with DB_* in config.env.
        if vdb.configured():
            if path == "/api/viewerdb" and method == "GET":
                return JSONResponse({"configured": True, "locked": True, "error": vdb.error, "type": "mysql"})
            if path == "/api/viewerdb/retry" and method == "POST":
                try:
                    return JSONResponse(await settings.api_viewerdb_retry())
                except HTTPException as e:
                    return JSONResponse({"detail": e.detail}, status_code=e.status_code)
            if path in ("/api/viewerdb/test", "/api/viewerdb/switch"):
                return JSONResponse({"detail": f"{APP_NAME_DEFAULT}'s database is configured but not answering ({vdb.error}). "
                                               "Retry once it is up, or change the connection with the DB_* settings in config.env."}, status_code=403)
            return JSONResponse({"detail": "Not found"}, status_code=404)
        try:
            if path == "/api/viewerdb" and method == "GET":
                return JSONResponse(settings.api_viewerdb())
            if path == "/api/viewerdb/test" and method == "POST":
                return JSONResponse(await settings.api_viewerdb_test(request))
            if path == "/api/viewerdb/switch" and method == "POST":
                return JSONResponse(await settings.api_viewerdb_switch(request))
            if path == "/api/viewerdb/retry" and method == "POST":
                return JSONResponse(await settings.api_viewerdb_retry())
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if path in OPEN_PATHS or path.startswith("/static/") or path.endswith((".png", ".svg", ".ico")):  # the shell and its files, not the sign-in API
        return await call_next(request)
    return JSONResponse({"detail": f"{APP_NAME_DEFAULT} has no database yet ({vdb.error}); open the viewer to set one up", "needsDatabase": True}, status_code=503)


@app.middleware("http")
async def _database_gate(request: Request, call_next):
    """Registered last, so it runs first: while the viewer has no database of its own, nothing below it runs."""
    if not vdb.ready():
        return await _without_database(request, call_next)
    return await call_next(request)


def _limit_for(path: str, ctype: str) -> int:
    """What BodyCap lets through: JSON and forms a few MiB, multipart uploads up to MAX_UPLOAD_BYTES (the upload routes
    refuse larger files themselves too), anything else as JSON."""
    if ctype.startswith("multipart/"):
        return MAX_UPLOAD_BYTES
    return MAX_JSON_BYTES


# 400 for bad input, the headers, no-store on the API, the body cap: the same in every app of the suite
auth.install(app, limit_for=_limit_for, forwarded_allow_ips=config.FORWARDED_ALLOW_IPS)  # also X-Forwarded-* from those proxies


def _status_extra(request: Request) -> dict:
    """ioPix's own fields of GET /api/status, beside the suite's: the index version the page compares to reload, the
    front end's stamp, the jobs, what the viewer may do. On the sign-in screen (sign-in required, nobody signed in):
    only what it needs, nothing about the library or the server."""
    user = request.state.user
    out = {"index": public_version(), "stamp": app_stamp(), "appName": app_name(), "readOnly": appstate.read_only(),
           "readOnlyLocked": config.READ_ONLY, "digikamCheck": config.DIGIKAM_CHECK}
    if user is None and users.require_login():
        return {**out, "editing": False, "dbWrite": False, "pendingEdits": 0}
    admin, signed, writer = users.is_admin(user), users.is_signed_in(user), users.can_write(user)
    dk = photo_index.status()
    out.update({
        "error": state["error"] if admin or not state["error"] else "The library could not be read; an admin can see why in Maintenance.", "rev": appstate.rev,
        "thumbs": {"running": prewarmer.running, "done": prewarmer.done, "total": prewarmer.total, "since": prewarmer.started, "last": prewarmer.last,
                   "current": list(prewarmer.current.values())[:3] if signed else []},  # file paths: not for anonymous clients
        "semantic": {"enabled": semantic_enabled(), "running": semantic.running, "done": semantic.done,
                     "total": semantic.total, "indexed": 0 if semantic.ids is None else int(len(semantic.ids))},
        "places": places.available(), "editing": edits.available() and writer, "pendingEdits": edits.pending_count(),
        "dbWrite": config.DB_WRITE and not appstate.read_only() and writer, "writeFiles": edits.writes_files(),
        "user": {**public_user(user), "person": user.get("person")} if user else None,  # the suite's shape, plus the linked person
        "digikam": dk if admin else {k: v for k, v in dk.items() if k != "error"}, "faceDetect": facedetect.available(),
        "lossless": lossless.capabilities() if config.DB_WRITE and edits.available() else None,
        "tagJobs": {**tagmanager.progress, "pending": tagmanager.pending_jobs()},
        "analysis": analysis.progress, "jobs": bgjobs.status()})
    return out


app.include_router(browse.router)
app.include_router(images.router)
app.include_router(albums.router)
app.include_router(editing.router)
app.include_router(settings.router)
app.include_router(maintenance.router)
app.include_router(auth.status_router(updater, extra=_status_extra))  # /api/status: public, the same shape in every app
app.include_router(auth.server_router(updater, log_file=config.LOG_FILE, info=lambda: {"mcpEnabled": mcp_enabled(), "mcpLocked": config.MCP_FORCED}))  # /api/server, /api/log: admins
app.include_router(auth_routes.router)
app.include_router(users_routes.router)
app.include_router(auth.admin_router())  # /api/suite-admin (+ ui.js, the Settings › Suite panel): the whole suite (suite admins)
app.include_router(geo.router)
app.include_router(backup.router)
app.include_router(review.router)
app.include_router(organize.router)
app.include_router(faces.router)

shared_mcp.mount(app, mcp.server)  # MCP over streamable HTTP at /mcp, ahead of everything else


@app.get("/", include_in_schema=False)
def index():
    """The page, with the version stamped on the script and stylesheet: after an update the browser (and Cloudflare,
    which caches .js and .css at the edge) fetches the new ones instead of serving what it kept."""
    html = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for f in ("static/app.js", "static/app.css"):
        html = html.replace(f'"{f}"', f'"{f}?v={APP_VERSION}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """At the root, so the service worker's scope is the whole app."""
    return FileResponse(config.STATIC_DIR / "sw.js", media_type="text/javascript")


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
