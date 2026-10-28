"""Settings: the Settings page, the databases, album roots, the map provider and sign-in at a proxy."""
import json
import os
import threading
import time

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from app.ai import models as dlmodels
from app import access, config, events, jobs as bgjobs, proxyauth, updates, users
from app.digikam import dkdb, library
from app.faces import facevecs
from app.photos import edits, scanner
from app.data import index, state as appstate, vdb
from app.geo import geocode, maps
from app.ai.semantic import _ready as semantic_ready, ENGINES, semantic
from app.core import _start_prewarm


from app.routes.browse import _photos_cached

router = APIRouter()


# --------------------------------------------------------------------------
# Settings, background analysis, models, maintenance
# --------------------------------------------------------------------------
def _settings_public() -> dict:
    return {"write_files": edits.writes_files(), "search_model": semantic.chosen(), **{g: dlmodels.chosen(g) for g in ("face_detector", "object_model", "caption_model", "face_vectors")}, "bg_threads": bgjobs.threads(), "read_only": appstate.read_only(), "require_login": users.require_login(), "mcp_enabled": appstate.get_setting("mcp_enabled", "0") == "1", "update_check": updates.auto_check_enabled(), "update_check_locked": os.environ.get("UPDATE_CHECK", "").strip() == "0", "digikam_sync": index.sync_enabled(), "app_name": appstate.get_setting("app_name", ""), "database": db_settings_public(), "album_roots": dkdb.root_overrides(), "map_provider": appstate.get_setting("map_provider", "osm"), "google_maps_key_set": bool(maps.key()), "templates": json.loads(appstate.get_setting("templates", "[]"))}


@router.get("/api/settings", dependencies=[access.public])
def api_settings_get(request: Request):
    """The settings the Settings page shows (admin values are read-only here; secrets are never included;
    where the databases are is for admins)."""
    out = _settings_public()
    if not users.is_admin(request.state.user):
        out.pop("database", None); out.pop("album_roots", None)
    if not users.can_write(request.state.user):  # the owner's name and contact (the edit templates) and recent tags are the editors' business
        out.pop("templates", None); out.pop("recent_tags", None); out.pop("google_maps_key_set", None)
    return out


@router.get("/api/settings/proxy", dependencies=[access.admin])
def api_proxy_get(request: Request):
    """Sign-in at a reverse proxy (Cloudflare Access...): its settings, which of them iopix.env fixes, and the
    Cloudflare Access team and application last seen in requests. Admins only."""
    return proxyauth.status()


@router.post("/api/settings/proxy", dependencies=[access.admin])
async def api_proxy_set(request: Request):
    """Body: any of proxyauth.KEYS (an empty string clears one). Settings in iopix.env cannot be changed here."""
    body = await request.json()
    changes = {k: str(v or "").strip() for k, v in body.items() if k in proxyauth.KEYS}
    fixed = [k for k in changes if os.environ.get(k) not in (None, "")]
    if fixed:
        raise HTTPException(400, f"Set in iopix.env, change it there: {', '.join(fixed)}")
    m = changes.get("AUTH_PROXY")
    if m is not None and m.lower() not in ("", "off", *proxyauth.PRESETS):
        raise HTTPException(400, "Unknown proxy")
    if changes.get("AUTH_PROXY_CREATE", "").lower() not in ("", "viewer", "editor", "deny"):
        raise HTTPException(400, "Someone with no account: the sign-in screen, deny, or a viewer or editor account")
    if (m or proxyauth.mode()).lower() == "cloudflare" and ("CF_ACCESS_TEAM" in changes or "CF_ACCESS_AUD" in changes or m):
        team = changes.get("CF_ACCESS_TEAM", proxyauth.opt("CF_ACCESS_TEAM") or "")
        aud = changes.get("CF_ACCESS_AUD", proxyauth.opt("CF_ACCESS_AUD") or "")
        if bool(team) != bool(aud):
            raise HTTPException(400, "Cloudflare needs both the team domain and the AUD tag (or neither)")
    proxyauth.save(changes)
    return proxyauth.status()


@router.post("/api/settings", dependencies=[access.admin])
async def api_settings(request: Request):
    """Body: {write_files: bool} - also write metadata into image files (JPEG, PNG, TIFF, WebP), not just sidecars."""
    body = await request.json()
    if "write_files" in body:
        appstate.set_setting("write_files", "1" if body["write_files"] else "0")
    if "update_check" in body:  # the daily look at GitHub for a newer version (off: the viewer never contacts GitHub on its own)
        if body["update_check"] and os.environ.get("UPDATE_CHECK", "").strip() == "0":
            raise HTTPException(400, "Switched off by UPDATE_CHECK=0 in the environment")
        appstate.set_setting("update_check", "1" if body["update_check"] else "0")
        if body["update_check"]:
            threading.Thread(target=updates.check, daemon=True).start()
        else:
            updates.forget_check()
    if "file_scanner" in body:  # watch the album folders for files added, changed or removed outside the viewer
        appstate.set_setting("file_scanner", "1" if body["file_scanner"] else "0")
        if body["file_scanner"]:
            scanner.run_soon()
    if "scan_interval" in body:
        try:
            appstate.set_setting("scan_interval", str(max(1, min(1440, int(body["scan_interval"])))))
        except (TypeError, ValueError):
            raise HTTPException(400, "scan_interval: minutes between passes")
    if "app_name" in body:  # how the viewer brands itself; empty puts the default back
        appstate.set_setting("app_name", str(body["app_name"] or "").strip()[:40])
    if body.get("library_replace"):  # changes made with sync off are dropped: the next read of digiKam replaces the library
        library.discard_local_changes()
        events.kick()
    if "digikam_sync" in body:  # off: the viewer works from its own copy of the library and never opens digiKam's database
        appstate.set_setting("digikam_sync", "1" if body["digikam_sync"] else "0")
        if body["digikam_sync"]:
            index.offline.update(since=None, error=None)
        events.kick()
    if "mcp_enabled" in body:  # the MCP endpoint at /mcp (off by default)
        appstate.set_setting("mcp_enabled", "1" if body["mcp_enabled"] else "0")
    if "require_login" in body:  # visitors get the sign-in screen and nothing else
        if body["require_login"] and not users.any_users():
            raise HTTPException(400, "Create an account first, or nobody could sign in")
        appstate.set_setting("require_login", "1" if body["require_login"] else "0")
    if "read_only" in body:  # browse only: every library-changing endpoint is refused while it is on
        if config.READ_ONLY and not body["read_only"]:
            raise HTTPException(400, "Read-only mode is locked on by READ_ONLY=1 in the environment")
        appstate.set_setting("read_only", "1" if body["read_only"] else "0")
    if body.get("digikam_closed"):  # Docker: the owner vouches that digiKam is closed, for 10 minutes
        appstate.set_setting("digikam_closed_until", str(time.time() + 600))
    if "templates" in body:  # metadata templates: [{name, fields}]
        tpls = [{"name": str(t.get("name", ""))[:80], "fields": {k: str(v or "")[:1000] for k, v in (t.get("fields") or {}).items() if k in edits.TEMPLATE_FIELDS}}
                for t in body["templates"] if isinstance(t, dict) and t.get("name")]
        appstate.set_setting("templates", json.dumps(tpls))
    if "database" in body:  # admin: the digiKam database connection; applies at once (the index rebuilds)
        d = body["database"] or {}
        for k in dkdb.KEYS:
            if k == "password" and not d.get("password"):
                continue  # an empty password field means "keep the stored one"
            if k in d:
                appstate.set_setting("db_" + k, str(d[k]).strip() if d[k] is not None else "")
        events.kick()
    if "album_roots" in body:  # admin: {digiKam path: local path}; empty value removes an override
        d = body["album_roots"] or {}
        if not isinstance(d, dict):
            raise HTTPException(400, "album_roots must be an object")
        appstate.set_setting("album_roots", json.dumps({str(k): str(v).strip() for k, v in d.items() if str(v).strip()}))
        events.kick()
    if "map_provider" in body:  # osm or google; google needs a key (setting or GOOGLE_MAPS_API_KEY)
        if body["map_provider"] not in ("osm", "google"):
            raise HTTPException(400, "Unknown map provider")
        appstate.set_setting("map_provider", body["map_provider"])
        maps.reset_session()
        geocode.clear_cache()
    if "google_maps_key" in body:  # stored, never shown back
        appstate.set_setting("google_maps_key", str(body["google_maps_key"]).strip())
    if "google_server_key" in body:  # a second key, restricted to this server's address, for the APIs called from here
        appstate.set_setting("google_server_key", str(body["google_server_key"]).strip())
    if "sidecar_compat" in body:  # new sidecars as photo.xmp (digiKam's "compatible file name" option) instead of photo.jpg.xmp
        appstate.set_setting("sidecar_compat", "1" if body["sidecar_compat"] else "0")
        maps.reset_session()
    if "bg_threads" in body:  # cores one background job may use; takes effect when the next job starts
        n = int(body["bg_threads"])
        if not 1 <= n < (os.cpu_count() or 4):
            raise HTTPException(400, "Choose between 1 and the number of cores minus one")
        appstate.set_setting("bg_threads", str(n))
    if "search_model" in body:
        name = str(body["search_model"])
        if name not in ENGINES or not semantic_ready(name):
            raise HTTPException(400, "That search model is not installed")
        appstate.set_setting("search_model", name)
        _photos_cached.cache_clear()
        await run_in_threadpool(_start_prewarm)  # starts indexing for the newly chosen model if needed
    for group in ("face_detector", "object_model", "caption_model", "face_vectors"):  # which model a feature runs on
        if group in body:
            key = str(body[group])
            if key not in dlmodels.options(group):
                raise HTTPException(400, f"Unknown model for {dlmodels.CHOICES[group]['label'].lower()}")
            if not dlmodels.local(key)["installed"]:
                raise HTTPException(400, f"{dlmodels.REGISTRY[key]['label']} is not installed; download it first")
            appstate.set_setting(group, key)
            await run_in_threadpool(dlmodels.reset, key)  # the next use loads the new model (its locks may be held by a loading job)
            if group == "face_vectors":
                await run_in_threadpool(facevecs.start)  # every face is embedded again with the new model
            elif group in ("object_model", "caption_model"):
                await run_in_threadpool(_start_prewarm)  # new photos use the new model; "Run again" redoes the rest
    return _settings_public()


def db_settings_public() -> dict:
    c = dkdb.settings()
    return {k: c[k] for k in dkdb.KEYS if k != "password"} | {"password_set": bool(c["password"]), "env_type": dkdb.ENV["type"]}


@router.get("/api/roots", dependencies=[access.admin])
async def api_roots(request: Request):
    """Album roots from digiKam with the local folder each one maps to (admin)."""
    return {"roots": await run_in_threadpool(dkdb.album_roots), "photosRoot": str(config.PHOTOS_ROOT)}


@router.post("/api/db/test", dependencies=[access.admin])
async def api_db_test(request: Request):
    """Try connection settings without saving them (admin). Body: the database fields; an empty password keeps the stored one."""
    d = await request.json()
    try:
        return await run_in_threadpool(dkdb.test_connection, d)
    except Exception as e:
        raise HTTPException(400, f"Connection failed: {str(e)[:200]}")


@router.get("/api/viewerdb", dependencies=[access.admin])
def api_viewerdb(request: Request):
    """Where the viewer keeps its own data: SQLite (the default) or MySQL/MariaDB."""
    return vdb.public_settings()


@router.post("/api/viewerdb/test", dependencies=[access.admin])
async def api_viewerdb_test(request: Request):
    """Try the given viewer-database settings without saving (admin); an empty password keeps the stored one."""
    d = await request.json()
    try:
        return await run_in_threadpool(vdb.test_connection, d)
    except Exception as e:
        raise HTTPException(400, f"Connection failed: {str(e)[:200]}")


@router.post("/api/viewerdb/switch", dependencies=[access.admin])
async def api_viewerdb_switch(request: Request):
    """Body: the settings plus copy (default true: carry every row over) and drop (default true: drop
    and recreate the viewer's tables at the target first). Saves the configuration and restarts the viewer."""
    d = await request.json()
    try:
        res = await run_in_threadpool(vdb.switch, d, bool(d.get("copy", True)), bool(d.get("drop", True)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Switch failed, nothing was saved: {str(e)[:300]}")
    if not d.get("no_restart"):
        threading.Timer(1.0, lambda: os._exit(0)).start()  # the service manager starts the viewer again on the new database
    return {**res, "restarting": not d.get("no_restart")}


@router.get("/api/maps", dependencies=[access.public])
def api_maps(request: Request):
    """Tile provider for the browser: OpenStreetMap, or Google Maps with the owner's key (session created here)."""
    if users.is_signed_in(request.state.user):
        maps.note_referer(request.headers)  # a visitor's Referer must not steer the key's referrer restriction
    return maps.config()
