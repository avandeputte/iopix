"""iopix: a web front end for a digiKam library.

The library (digiKam's database and the photo files) is only read here; the
few endpoints that change it (sidecars, image files, digiKam's database on
request) are listed in WRITE_PATHS and go through the modules that own those
writes."""
import json
import logging
import threading
import os
import time
from urllib.parse import urlsplit
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

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
from app.digikam import dkdb
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
    upgraded = config.INDEX_DB.exists()  # an install that has run before (a fresh one has no index yet)
    appstate.init()
    _mcp_first_setting(upgraded)
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
    if dkdb.no_digikam() and scanner.enabled():
        scanner.run_soon()  # no digiKam to have found what was added while iopix was stopped: a pass right after start-up
    updates.start_daily()  # the daily "is there a newer version" check, when the setting allows it
    if config.AUTO_MODELS:  # containers and fresh installs: fetch missing models, then start the jobs that need them
        dlmodels.auto_download(config.AUTO_MODELS, on_done=_models_arrived)
    async with mcp_server.server.session_manager.run():  # the MCP endpoint (/mcp) lives in this process
        yield


app = FastAPI(title="iopix", lifespan=lifespan, docs_url=None, redoc_url=None)

# every endpoint that changes the library (sidecars, files, digiKam's database); refused in read-only mode
WRITE_PATHS = ("/api/picklabels/clear", "/api/filecheck/keep", "/api/filecheck/repair", "/api/edit", "/api/edits/db", "/api/faces/detect", "/api/photos/delete", "/api/photos/trash", "/api/photos/move", "/api/photos/rename", "/api/photos/rename-pattern", "/api/metadata", "/api/photos/transform", "/api/photos/crop", "/api/photos/adjust", "/api/trash", "/api/import", "/api/history", "/api/library/merge", "/api/location/landmark", "/api/gpx/preview", "/api/consistency/sync", "/api/gpx/apply", "/api/faces/op", "/api/faces/batch", "/api/folders",
               "/api/faces/person", "/api/placetags", "/api/tags/op")


ADMIN_PATHS = ("/api/picklabels", "/api/filecheck", "/api/settings", "/api/maintenance", "/api/scan", "/api/quality", "/api/log", "/api/library/merge", "/api/consistency/scan", "/api/analysis/run", "/api/models/check", "/api/models/download", "/api/restart", "/api/users", "/api/update", "/api/neardups")
SIGNED_IN_PATHS = ("/api/people/birth", "/api/favorites", "/api/ualbums", "/api/searches", "/api/tags/recent", "/api/auth/settings", "/api/auth/password", "/api/auth/logout_all")
# reads that spend a shared resource (Nominatim's one request a second; the place-tag plan reads the whole index)
SIGNED_IN_READ_PATHS = ("/api/geocode", "/api/placetags")
# reads that are not for everyone, whatever the method: the server log and the merge plan (admins); the change log and
# the elevation lookup, which spends Google credits (editors)
ADMIN_READ_PATHS = ("/api/picklabels", "/api/filecheck", "/api/log", "/api/library/merge", "/api/quality")
EDITOR_READ_PATHS = ("/api/datesuggest", "/api/history", "/api/elevation", "/api/import", "/api/metadata", "/api/consistency")  # (the report lists file paths)
MAX_JSON_BODY = 4 << 20  # a JSON request larger than this is refused before it is read (uploads are multipart and not bound here)


def _hits(path: str, paths) -> bool:
    return any(path == p or path.startswith(p + "/") for p in paths)


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
    if os.environ.get("MCP", "").strip() == "1":
        return True
    return appstate.get_setting("mcp_enabled", "0") == "1"


def _mcp_token_ok(request: Request) -> bool:
    """With sign-in required, the MCP endpoint needs the admin token as a bearer header."""
    auth = request.headers.get("authorization", "")
    return auth.lower().startswith("bearer ") and updates.token_ok(auth[7:].strip())


def _no_access(request: Request, ident: str):
    """Someone the proxy let in who has no account here, with Settings › Users › Sign-in at a proxy set to deny them."""
    msg = f"{ident} has no account on this photo library. Ask its owner for access."
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": msg, "noAccess": True}, status_code=403)
    from html import escape
    out = "/cdn-cgi/access/logout" if proxyauth.external(request.headers, proxyauth.peer_of(request)) else None
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>No access</title><style>body{{font:16px system-ui,sans-serif;max-width:520px;margin:15vh auto;padding:0 16px;color:#222;background:#fff}}
@media (prefers-color-scheme:dark){{body{{color:#eee;background:#111}}a{{color:#8ab4f8}}}}</style>
<h1>No access</h1><p>{escape(msg)}</p>{f'<p><a href="{out}">Sign in with another account</a></p>' if out else ''}""", status_code=403)


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    """Who is asking (session cookie), and may they do this? Library writes need an editor
    or admin, settings and maintenance an admin, favorites and albums a signed-in user; all
    of that only once a user exists. Read-only mode refuses library writes for everyone."""
    session = request.cookies.get(users.COOKIE)
    user = users.user_from_token(session) if session else None
    cookie_uid = user["id"] if user else None
    request.state.via_proxy = False
    if "cf-access-jwt-assertion" in request.headers and time.time() - proxyauth.seen.get("at", 0) > 60:
        proxyauth.note(request.headers, request.scope.get("iopix.peer") or (request.client.host if request.client else ""))
    if proxyauth.mode() != "off":  # signed in at a reverse proxy (Cloudflare Access, Authelia...): that person, when vouched for
        pu = proxyauth.user(request)
        if pu:
            user, request.state.via_proxy = pu, True
        elif getattr(request.state, "proxy_ident", None) and proxyauth.deny_unknown() and not request.url.path.startswith("/mcp"):
            # (MCP clients sign in with the admin token, whatever Cloudflare service token got them through)
            return _no_access(request, request.state.proxy_ident)
    if user and not request.state.via_proxy and user.get("passwordless") and proxyauth.external(request.headers, proxyauth.peer_of(request)):
        user = None  # a passwordless account's session (begun on the LAN) does not work from outside
        if time.time() - proxyauth._logged.get(("lan-session", request.cookies.get(users.COOKIE)), 0) > 3600:
            proxyauth._logged[("lan-session", request.cookies.get(users.COOKIE))] = time.time()
            proxyauth.authlog.info("a one-click session is not used from outside: shown the sign-in screen (from %s)", proxyauth.source(request))
    request.state.user = user
    history.user.set(user["name"] if user else ("MCP" if request.url.path.startswith("/mcp") else "visitor"))
    appstate.viewer.set(user)  # private albums: seen only by their owner
    if request.url.path.startswith("/mcp"):
        if not mcp_enabled():  # switched off (the default): as if there were no such endpoint
            return JSONResponse({"detail": "The MCP server is switched off (Settings › Viewer › MCP server)."}, status_code=404)
        mcp_server.token_given.set(_mcp_token_ok(request))  # answers with file paths only for the token holder
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
    if _hits(path, SIGNED_IN_READ_PATHS) and not users.is_signed_in(user):
        return JSONResponse({"detail": "Sign in to use this"}, status_code=401)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # a page on another site (or another port) cannot drive this one with the browser's cookies: the request's
        # Origin, when the browser sends one, has to be this host
        origin, site = request.headers.get("origin"), request.headers.get("sec-fetch-site")
        if site == "cross-site" or (origin and (origin == "null" or urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower())):
            return JSONResponse({"detail": "Requests from another site are refused"}, status_code=403)
        if _hits(path, WRITE_PATHS) and appstate.read_only():
            return JSONResponse({"detail": "The viewer is in read-only mode; nothing in the library can be changed."}, status_code=403)
        if _hits(path, WRITE_PATHS) and not users.can_write(user):
            return JSONResponse({"detail": "Sign in as an editor to change the library."}, status_code=403 if user else 401)
        if _hits(path, ADMIN_PATHS) and not users.is_admin(user):
            return JSONResponse({"detail": "Only an admin can change settings or run maintenance."}, status_code=403 if user else 401)
        if _hits(path, SIGNED_IN_PATHS) and not users.is_signed_in(user):
            return JSONResponse({"detail": "Sign in to keep favorites and albums."}, status_code=401)
    response = await call_next(request)
    if (user and not request.state.via_proxy and cookie_uid == user["id"] and path not in ("/api/auth/logout", "/api/auth/logout_all", "/api/auth/password")
            and users.renew_due(session) and "set-cookie" not in response.headers):
        users.set_session(response, user["id"], request.cookies.get(users.VIA_COOKIE) or "password", users.over_https(request))
    if request.state.via_proxy and user and cookie_uid != user["id"] and path != "/api/auth/logout":
        # signed in at the proxy (Cloudflare Access): a session here too, so the same browser stays signed in on the
        # home network, where requests reach the viewer directly without Cloudflare
        users.set_session(response, user["id"], "cloudflare" if proxyauth.mode() == "cloudflare" else "proxy")
    return response



@app.middleware("http")
async def static_revalidate(request: Request, call_next):
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if request.method not in ("GET", "HEAD", "OPTIONS") and ctype in ("application/json", "application/x-www-form-urlencoded", "text/plain", ""):  # (uploads are multipart)
        try:
            if int(request.headers.get("content-length") or 0) > MAX_JSON_BODY:
                return JSONResponse({"detail": "That request is too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Bad Content-Length"}, status_code=400)
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
