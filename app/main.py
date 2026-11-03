"""iopix: a web front end for a digiKam library.

The library (digiKam's database and the photo files) is only read here; the
few endpoints that change it (sidecars, image files, digiKam's database on
request) declare access.editor_write or access.admin_write on their route and
go through the modules that own those writes."""
import json
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import JSONResponse

from app.ai import analysis, models as dlmodels
from app.faces import facemgr
from app import config, events, mcp, proxyauth, thumbs, updates, users
from app.auth import auth as auth_shared
from app.photos import consistency, edits, history, scanner, tagmanager
from app.data import state as appstate
from app.digikam import dkdb
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
proxyauth.remember_peer()  # before uvicorn's forwarded-header rewrite runs: proxy sign-in trusts the real peer
from app.routes import auth, backup, browse, images, albums, editing, geo, maintenance, review, organize, faces, settings, users as users_routes
from app.runtime import state, kick, refresh, auto, watch, resolve, prewarmer, setup_file_log, models_arrived
__all__ = ["app", "state", "kick", "refresh", "auto", "watch", "resolve", "prewarmer"]  # tests and app.mcp reach these through app.main

@asynccontextmanager
async def lifespan(_app):
    setup_file_log()
    upgraded = config.INDEX_DB.exists()  # an install that has run before (a fresh one has no index yet)
    appstate.init()
    _mcp_first_setting(upgraded)
    users.init()
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
    updates.start_daily()  # the daily "is there a newer version" check, when the setting allows it
    if config.AUTO_MODELS:  # containers and fresh installs: fetch missing models, then start the jobs that need them
        dlmodels.auto_download(config.AUTO_MODELS, on_done=models_arrived)
    async with mcp.server.session_manager.run():  # the MCP endpoint (/mcp) lives in this process
        yield


app = FastAPI(title="iopix", lifespan=lifespan, docs_url=None, redoc_url=None)

# every endpoint that changes the library (sidecars, files, digiKam's database); refused in read-only mode
MAX_JSON_BODY = 4 << 20  # a JSON request larger than this is refused before it is read (uploads are multipart and not bound here)


# what a visitor may still fetch when sign-in is required: the app shell and the sign-in endpoints
OPEN_PATHS = ("/", "/index.html", "/app.js", "/app.css", "/sw.js", "/manifest.webmanifest", "/api/status", "/share")
OPEN_PREFIXES = ("/icons/", "/js/", "/vendor/", "/api/auth/")  # /vendor/: the map library, part of the app shell


def _open_to_visitors(path: str) -> bool:
    if path.startswith("/face/"):  # the avatars on the sign-in screen: only the faces accounts are linked to
        tail = path[6:].split("/")[0]
        return tail.isdigit() and int(tail) in users.linked_persons()
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def _mcp_first_setting(upgraded: bool):
    """The MCP switch is stored once and then kept across updates: off on a fresh install, on for an install
    from before the switch existed (v1.85), whose /mcp answered until then."""
    if appstate.get_setting("mcp_enabled", "") == "":
        appstate.set_setting("mcp_enabled", "1" if upgraded else "0")
        logging.getLogger("app").info("MCP server %s (first start with the MCP switch)", "kept on: an existing install" if upgraded else "off: a fresh install")


def mcp_enabled() -> bool:
    """Settings › Viewer › MCP server: off unless an admin turns it on (or MCP=1 in the environment)."""
    if config.MCP_FORCED:
        return True
    return appstate.get_setting("mcp_enabled", "0") == "1"


def _mcp_token_ok(request: Request) -> bool:
    """The MCP endpoint needs the admin token as a bearer header on every request (since v1.111)."""
    auth = request.headers.get("authorization", "")
    return auth.lower().startswith("bearer ") and updates.token_ok(auth[7:].strip())


def _gate(request: Request):
    """Once the user is known (app/auth.py's middleware): who the change history and private albums see, and the
    MCP endpoint's own gates (the switch, and the admin token on every request)."""
    user = request.state.user
    history.user.set(user["name"] if user else ("MCP" if request.url.path.startswith("/mcp") else "visitor"))
    appstate.viewer.set(user)  # private albums: seen only by their owner
    if request.url.path.startswith("/mcp"):
        if not mcp_enabled():  # switched off (the default): as if there were no such endpoint
            return JSONResponse({"detail": "The MCP server is switched off (Settings › Viewer › MCP server)."}, status_code=404)
        if not _mcp_token_ok(request):  # every request, the read-only tools included: the library is nobody else's business
            return JSONResponse({"detail": "The MCP server needs the admin token on every request: 'Authorization: Bearer <token>' "
                                           "(Maintenance › iopix version › MCP update token)"}, status_code=401)
        mcp.token_given.set(True)  # answers may carry file paths: the caller holds the admin token
    return None


# who is asking (the shared session cookie, the proxy's identity), whether sign-in is required at all, and the
# cross-site check on writes: iothing-auth's middleware. What each route needs beyond that (an editor, an admin,
# an account) is declared on the route (app/access.py).
app.middleware("http")(auth_shared.middleware(is_open=_open_to_visitors, open_prefixes=("/mcp",), before=_gate))


@app.middleware("http")
async def static_revalidate(request: Request, call_next):
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if request.method not in ("GET", "HEAD", "OPTIONS") and ctype in ("application/json", "application/x-www-form-urlencoded", "text/plain", ""):  # (uploads are multipart)
        try:
            if int(request.headers.get("content-length") or 0) > MAX_JSON_BODY:
                return JSONResponse({"detail": "That request is too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Bad Content-Length"}, status_code=400)
        if "content-length" not in request.headers and "chunked" in (request.headers.get("transfer-encoding") or "").lower():
            return JSONResponse({"detail": "Send the request with a Content-Length"}, status_code=411)  # a chunked body has no length to check
    response = await call_next(request)
    if "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(ValueError)
@app.exception_handler(TypeError)
@app.exception_handler(json.JSONDecodeError)
async def _bad_input(request: Request, e: Exception):
    """A body or parameter that is not what a route expects (a word where a number belongs, malformed JSON, a list
    where an id belongs) is the client's mistake: a 400 that says so, not a 500 and a traceback."""
    logging.getLogger("app").warning("bad input on %s %s: %s: %s", request.method, request.url.path, type(e).__name__, str(e)[:200])
    return JSONResponse({"detail": f"Bad request: {str(e)[:200] or type(e).__name__}"}, status_code=400)


app.include_router(browse.router)
app.include_router(images.router)
app.include_router(albums.router)
app.include_router(editing.router)
app.include_router(settings.router)
app.include_router(maintenance.router)
app.include_router(auth.router)
app.include_router(users_routes.router)
app.include_router(geo.router)
app.include_router(backup.router)
app.include_router(review.router)
app.include_router(organize.router)
app.include_router(faces.router)

# MCP over HTTP on the same port: an assistant connects to http://host:8600/mcp with the admin token as a bearer header.
# DNS-rebinding protection is off because the viewer is reached by LAN name or IP, not localhost only.
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

_mcp_asgi = mcp.server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                                   transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
# its route handles the exact path /mcp; placed first so the static root mount never sees it
app.router.routes[:0] = list(_mcp_asgi.routes)
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
