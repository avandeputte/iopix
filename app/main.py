"""iopix: a web front end for a digiKam library.

The library (digiKam's database and the photo files) is only read here; the
few endpoints that change it (sidecars, image files, digiKam's database on
request) are listed in WRITE_PATHS and go through the modules that own those
writes."""
import threading
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import JSONResponse

from app.ai import analysis
from app import config
from app.photos import consistency
from app.photos import edits
from app.photos import history
from app.photos import tagmanager
from app import thumbs
from app.data import state as appstate
from app import users
from app import proxyauth
from app import mcp_server
from app import updates
from app.ai import models as dlmodels
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
proxyauth.remember_peer()  # before uvicorn's forwarded-header rewrite runs: proxy sign-in trusts the real peer
from app.routes import browse, images, albums, editing, admin, review, organize, faces
from app.core import state, _kick, _refresh, _auto, _watch, resolve, prewarmer, _setup_file_log, _models_arrived
__all__ = ["app", "state", "_kick", "_refresh", "_auto", "_watch", "resolve", "prewarmer"]  # tests and mcp_server reach these through app.main

@asynccontextmanager
async def lifespan(_app):
    _setup_file_log()
    appstate.init()
    users.init()
    edits.init()
    tagmanager.init()
    analysis.init()
    consistency.init()
    analysis.on_progress = _kick.set  # fold new analysis results into the index as they arrive
    if tagmanager.pending_jobs():  # finish sidecar updates left over from a previous run
        tagmanager.start_worker()
    _refresh(initial=True)
    threading.Thread(target=thumbs.prune_previews, daemon=True).start()
    threading.Thread(target=_watch, name="reindex", daemon=True).start()
    from app.photos import scanner
    from app.core import _rebuild_after_write
    scanner.start(_rebuild_after_write)  # the file scanner (Maintenance › File scanner): passes only while it is on
    updates.start_daily()  # the daily "is there a newer version" check, when the setting allows it
    if config.AUTO_MODELS:  # containers and fresh installs: fetch missing models, then start the jobs that need them
        dlmodels.auto_download(config.AUTO_MODELS, on_done=_models_arrived)
    async with mcp_server.server.session_manager.run():  # the MCP endpoint (/mcp) lives in this process
        yield


app = FastAPI(title="iopix", lifespan=lifespan, docs_url=None, redoc_url=None)

# every endpoint that changes the library (sidecars, files, digiKam's database); refused in read-only mode
WRITE_PATHS = ("/api/edit", "/api/edits/db", "/api/faces/detect", "/api/photos/delete", "/api/photos/trash", "/api/photos/move", "/api/photos/rename", "/api/photos/rename-pattern", "/api/metadata", "/api/photos/transform", "/api/photos/crop", "/api/photos/adjust", "/api/trash", "/api/import", "/api/history", "/api/library/merge", "/api/location/landmark", "/api/gpx/preview", "/api/consistency/sync", "/api/gpx/apply", "/api/faces/op", "/api/faces/batch", "/api/folders",
               "/api/faces/person", "/api/placetags", "/api/tags/op")


ADMIN_PATHS = ("/api/settings", "/api/maintenance", "/api/scan", "/api/quality", "/api/log", "/api/library/merge", "/api/consistency/scan", "/api/analysis/run", "/api/models/check", "/api/models/download", "/api/restart", "/api/users", "/api/update", "/api/neardups")
SIGNED_IN_PATHS = ("/api/favorites", "/api/ualbums", "/api/searches", "/api/tags/recent", "/api/auth/settings", "/api/auth/password")
# reads that are not for everyone, whatever the method: the server log and the merge plan (admins); the change log and
# the elevation lookup, which spends Google credits (editors)
ADMIN_READ_PATHS = ("/api/log", "/api/library/merge", "/api/quality")
EDITOR_READ_PATHS = ("/api/history", "/api/elevation", "/api/import")


def _hits(path: str, paths) -> bool:
    return any(path == p or path.startswith(p + "/") for p in paths)


# what a visitor may still fetch when sign-in is required: the app shell and the sign-in endpoints
OPEN_PATHS = ("/", "/index.html", "/app.js", "/app.css", "/sw.js", "/manifest.webmanifest", "/api/status", "/share")
OPEN_PREFIXES = ("/icons/", "/js/", "/api/auth/")


def _open_to_visitors(path: str) -> bool:
    if path.startswith("/face/"):  # the avatars on the sign-in screen: only the faces accounts are linked to
        tail = path[6:].split("/")[0]
        return tail.isdigit() and int(tail) in users.linked_persons()
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def _mcp_token_ok(request: Request) -> bool:
    """With sign-in required, the MCP endpoint needs the admin token as a bearer header."""
    auth = request.headers.get("authorization", "")
    return auth.lower().startswith("bearer ") and updates.token_ok(auth[7:].strip())


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    """Who is asking (session cookie), and may they do this? Library writes need an editor
    or admin, settings and maintenance an admin, favorites and albums a signed-in user; all
    of that only once a user exists. Read-only mode refuses library writes for everyone."""
    user = users.user_from_token(request.cookies.get(users.COOKIE, "")) if request.cookies.get(users.COOKIE) else None
    request.state.via_proxy = False
    if proxyauth.mode() != "off":  # signed in at a reverse proxy (Cloudflare Access, Authelia...): that person, when vouched for
        pu = proxyauth.user(request)
        if pu:
            user, request.state.via_proxy = pu, True
    request.state.user = user
    history.user.set(user["name"] if user else ("MCP" if request.url.path.startswith("/mcp") else "visitor"))
    appstate.viewer.set(user)  # private albums: seen only by their owner
    if user is None and users.require_login():
        path = request.url.path
        if path.startswith("/mcp"):
            if not _mcp_token_ok(request):
                return JSONResponse({"detail": "Sign-in is required; MCP clients send the admin token as 'Authorization: Bearer <token>'"}, status_code=401)
        elif not _open_to_visitors(path):
            return JSONResponse({"detail": "Sign in to use the viewer."}, status_code=401)
    path = request.url.path
    if _hits(path, ADMIN_READ_PATHS) and not users.is_admin(user):
        return JSONResponse({"detail": "Admins only"}, status_code=403 if user else 401)
    if _hits(path, EDITOR_READ_PATHS) and not users.can_write(user):
        return JSONResponse({"detail": "Editors only"}, status_code=403 if user else 401)
    if request.method == "POST":
        if _hits(path, WRITE_PATHS) and appstate.read_only():
            return JSONResponse({"detail": "The viewer is in read-only mode; nothing in the library can be changed."}, status_code=403)
        if _hits(path, WRITE_PATHS) and not users.can_write(user):
            return JSONResponse({"detail": "Sign in as an editor to change the library."}, status_code=403 if user else 401)
        if _hits(path, ADMIN_PATHS) and not users.is_admin(user):
            return JSONResponse({"detail": "Only an admin can change settings or run maintenance."}, status_code=403 if user else 401)
        if _hits(path, SIGNED_IN_PATHS) and not users.is_signed_in(user):
            return JSONResponse({"detail": "Sign in to keep favorites and albums."}, status_code=401)
    return await call_next(request)



@app.middleware("http")
async def static_revalidate(request: Request, call_next):
    response = await call_next(request)
    if "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(browse.router)
app.include_router(images.router)
app.include_router(albums.router)
app.include_router(editing.router)
app.include_router(admin.router)
app.include_router(review.router)
app.include_router(organize.router)
app.include_router(faces.router)

# MCP over HTTP on the same port: an assistant connects to http://host:8600/mcp (read-only tools).
# DNS-rebinding protection is off because the viewer is reached by LAN name or IP, not localhost only.
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

_mcp_asgi = mcp_server.server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                                   transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
# its route handles the exact path /mcp; placed first so the static root mount never sees it
app.router.routes[:0] = list(_mcp_asgi.routes)
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
