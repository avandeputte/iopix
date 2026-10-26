"""Settings, accounts, background analysis, models, maintenance, updates, logs and backups."""
import json
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from urllib.parse import quote

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.ai import analysis
from app.ai import caption
from app import proxyauth
from app import config
from app.digikam import dbsync
from app.ai import detector
from app.ai import looks
from app.faces import ages
from app.digikam import dkdb
from app.photos import edits
from app.digikam import merge
from app.faces import facedetect
from app.faces import facevecs
from app.digikam import library
from app.data import index
from app.data import vdb
from app import thumbs
from app.data import state as appstate
from app import jobs as bgjobs
from app.geo import geocode
from app.geo import maps
from app import users
from app import updates
from app.ai import models as dlmodels
from app.ai.semantic import ENGINES
from app.ai.semantic import semantic
from app.ai.semantic import enabled as semantic_enabled
from app.ai.semantic import _ready as semantic_ready
from app.core import DB_ERRORS
from app.core import state
from app.core import prewarmer
from app.core import db
from app.core import _start_prewarm
from app.core import _kick
from app.core import app_name
from app.core import _face_detect_allowed
from app.core import _rebuild_after_write

from app.core import _compute_tag_covers

from app.routes.browse import _photos_cached

router = APIRouter()

# --------------------------------------------------------------------------
# Settings, background analysis, models, maintenance
# --------------------------------------------------------------------------

def _settings_public() -> dict:
    return {"write_files": edits.writes_files(), "search_model": semantic.chosen(), **{g: dlmodels.chosen(g) for g in ("face_detector", "object_model", "caption_model", "face_vectors")}, "bg_threads": bgjobs.threads(), "read_only": appstate.read_only(), "require_login": users.require_login(), "update_check": updates.auto_check_enabled(), "update_check_locked": os.environ.get("UPDATE_CHECK", "").strip() == "0", "digikam_sync": index.sync_enabled(), "app_name": appstate.get_setting("app_name", ""), "database": _db_settings_public(), "album_roots": dkdb.root_overrides(), "map_provider": appstate.get_setting("map_provider", "osm"), "google_maps_key_set": bool(maps.key()), "templates": json.loads(appstate.get_setting("templates", "[]"))}


@router.get("/api/settings")
def api_settings_get(request: Request):
    """The settings the Settings page shows (admin values are read-only here; secrets are never included;
    where the databases are is for admins)."""
    out = _settings_public()
    if not users.is_admin(request.state.user):
        out.pop("database", None); out.pop("album_roots", None)
    if not users.can_write(request.state.user):  # the owner's name and contact (the edit templates) and recent tags are the editors' business
        out.pop("templates", None); out.pop("recent_tags", None); out.pop("google_maps_key_set", None)
    return out


@router.get("/api/settings/proxy")
def api_proxy_get(request: Request):
    """Sign-in at a reverse proxy (Cloudflare Access...): its settings, which of them iopix.env fixes, and the
    Cloudflare Access team and application last seen in requests. Admins only."""
    if not users.is_admin(request.state.user):
        raise HTTPException(403, "Admins only")
    return proxyauth.status()


@router.post("/api/settings/proxy")
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
        team = changes.get("CF_ACCESS_TEAM", proxyauth._opt("CF_ACCESS_TEAM") or "")
        aud = changes.get("CF_ACCESS_AUD", proxyauth._opt("CF_ACCESS_AUD") or "")
        if bool(team) != bool(aud):
            raise HTTPException(400, "Cloudflare needs both the team domain and the AUD tag (or neither)")
    proxyauth.save(changes)
    return proxyauth.status()


@router.post("/api/settings")
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
            updates._checked.clear()
    if "file_scanner" in body:  # watch the album folders for files added, changed or removed outside the viewer
        appstate.set_setting("file_scanner", "1" if body["file_scanner"] else "0")
        if body["file_scanner"]:
            from app.photos import scanner
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
        _kick.set()
    if "digikam_sync" in body:  # off: the viewer works from its own copy of the library and never opens digiKam's database
        appstate.set_setting("digikam_sync", "1" if body["digikam_sync"] else "0")
        if body["digikam_sync"]:
            index.offline.update(since=None, error=None)
        _kick.set()
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
        _kick.set()
    if "album_roots" in body:  # admin: {digiKam path: local path}; empty value removes an override
        d = body["album_roots"] or {}
        if not isinstance(d, dict):
            raise HTTPException(400, "album_roots must be an object")
        appstate.set_setting("album_roots", json.dumps({str(k): str(v).strip() for k, v in d.items() if str(v).strip()}))
        _kick.set()
    if "map_provider" in body:  # osm or google; google needs a key (setting or GOOGLE_MAPS_API_KEY)
        if body["map_provider"] not in ("osm", "google"):
            raise HTTPException(400, "Unknown map provider")
        appstate.set_setting("map_provider", body["map_provider"])
        maps.reset_session()
        geocode._cache.clear()
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
            await run_in_threadpool(dlmodels._reset, key)  # the next use loads the new model (its locks may be held by a loading job)
            if group == "face_vectors":
                await run_in_threadpool(facevecs.start)  # every face is embedded again with the new model
            elif group in ("object_model", "caption_model"):
                await run_in_threadpool(_start_prewarm)  # new photos use the new model; "Run again" redoes the rest
    return _settings_public()


def nf(n) -> str:
    return f"{n:,}"


def _dir_size(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


@router.get("/api/analysis")
def api_analysis(request: Request):
    """Status of every background analysis, for the AI analysis page."""
    con = db()
    try:
        total, stills = con.execute("SELECT count(*), sum(video = 0) FROM photos").fetchone()
    finally:
        con.close()
    cov = analysis.coverage()
    up = semantic.upgrade
    act = semantic.active if semantic_enabled() else None
    if act is not None:
        act.load()
    names = {k: dlmodels.REGISTRY[k]["label"] for k in ENGINES}
    mname = lambda group: dlmodels.REGISTRY[dlmodels.chosen(group)]["label"]  # noqa: E731
    models = [{"key": k, "label": names[k], "installed": semantic_ready(k), "chosen": semantic.chosen() == k,
               "active": act is not None and act.name == k} for k in ENGINES]
    jobs = [
        {"key": "thumbs", "label": "Thumbnails", "detail": "Small previews for the grid.",
         "done": total - (prewarmer.total - prewarmer.done if prewarmer.running else 0), "total": total,
         "running": prewarmer.running, "enabled": True, "rerun": False, "since": prewarmer.started, "last": prewarmer.last, "current": (list(prewarmer.current.values())[:3] if users.is_signed_in(request.state.user) else [])},
        {"key": "embeddings", "label": "Content search", "enabled": act is not None, "rerun": True,
         "detail": (f"Model: {names[act.name]}." + (("" if act is up else f" Switching to {names[up.name]}: ") + f"{nf(len(up.ids) if up.ids is not None else 0)} of {nf(total)} indexed" + ("; results come from the photos indexed so far." if act is up else f"; search stays on {names[act.name]} until it finishes.") if up else "")
                    + (f" {nf(len(act.failed))} could not be embedded (no thumbnail; see the server log)." if act is not None and act.failed else "")) if act else "Not installed (see README).",
         "done": int(len(act.ids)) if act is not None and act.ids is not None else 0, "total": total, "running": semantic.running},
        {"key": "quality", "label": "Sharpness and exposure", "detail": "Used for the Blurry / Sharpest filters and for picking the best frame of a burst. Computed in the same pass as object detection (one decode per photo), so the two rows advance together.",
         "done": cov["quality"], "total": stills, "running": analysis.progress["quality"]["running"], "enabled": config.ANALYSIS, "rerun": True},
        {"key": "vision", "label": "Text in photos and face quality", "detail": "Text recognition (RapidOCR) makes signs, jerseys, documents and screenshots searchable; face quality is measured on each face region.",
         "done": cov["vision"], "total": stills, "running": analysis.progress["vision"]["running"],
         "enabled": config.ANALYSIS and analysis.vision_available(), "rerun": True},
        {"key": "objects", "label": "Object detection", "detail": f"{mname('object_model')}: people, balls, animals, vehicles and other everyday objects, with boxes. Computed in the same pass as sharpness and exposure, so the two rows advance together. After switching to another size, “Run again” redoes every photo with it; until then only new photos use it.",
         "done": cov["objects"], "total": stills, "running": analysis.progress["objects"]["running"],
         "enabled": config.ANALYSIS and detector.available(), "rerun": True},
        {"key": "faces", "label": "Face grouping and suggestions", "detail": f"An embedding ({mname('face_vectors')}) for every face that has no name yet, whoever marked it, and for the most recent faces of each named person, so the Faces page shows look-alikes together with a “looks like” suggestion and a one-click name. A suggestion is written nowhere until you accept it.",
         "done": facevecs.coverage()[0], "total": facevecs.coverage()[1], "running": facevecs.progress["running"], "enabled": config.ANALYSIS and facevecs.available(), "rerun": True},
        {"key": "facedetect", "label": "Face detection", "detail": f"Finds faces nothing has marked yet ({mname('face_detector')}) and adds them as unnamed faces, ready for grouping and naming. Existing face regions, from digiKam or any other tool, are kept: a face that overlaps one is not added again, even when the two rectangles differ. Runs only when you start it (here for the library, or from a folder or a photo); writes to digiKam's database, backed up first."
         + (f" Last run: {nf(facedetect.progress['added'])} new faces in {nf(facedetect.progress['photos_with_new'])} photos." if facedetect.progress.get("started") else ""),
         "done": facedetect.coverage(), "total": stills, "running": facedetect.progress["running"], "enabled": facedetect.available() and config.DB_WRITE, "rerun": True},
        {"key": "looks", "label": "Photo quality", "detail": "How sharp the sharpest part of each photo is (so a soft background is fine), "
         + ("an aesthetic score (" + dlmodels.REGISTRY["aesthetic"]["label"] + ")" if looks.aesthetic_available() else "an aesthetic score once its model is installed")
         + (" and closed eyes on faces seen from the front" if looks.eyes_available() else ", and closed eyes once the eye model and a face detection model are installed")
         + ". Picks the best frame of each burst and suggests likely rejects (Filters › Quality); nothing is marked until you do it. A model installed later fills in its part without redoing the rest.",
         "done": cov["looks"], "total": stills, "running": analysis.progress["looks"]["running"], "enabled": config.ANALYSIS, "rerun": True},
        {"key": "ages", "label": "Ages for date suggestions", "detail": "How old the named people look (" + dlmodels.REGISTRY["mivolo"]["label"] + ") in photos marked “Date unknown”, and in a sample of the same people’s photos with a known date, which calibrate it for each of them. Explore › Date suggestions then proposes a year for each undated photo; nothing is written until you use one.",
         "done": ages.coverage()[0], "total": ages.coverage()[1], "running": ages.progress["running"], "enabled": config.ANALYSIS and ages.available(), "rerun": True},
        {"key": "captions", "label": "Photo descriptions", "detail": f"A description of each photo ({mname('caption_model')}: one sentence with BLIP, a short paragraph with Florence-2), searchable from the search box and shown in the info panel. After switching to another model, “Run again” redoes every photo with it; until then only new photos use it.",
         "done": cov["captions"], "total": stills, "running": analysis.progress["captions"]["running"],
         "enabled": config.ANALYSIS and caption.available(), "rerun": True},
    ]
    queue_key = {"thumbs": "thumbs", "embeddings": "embeddings", "quality": "pixels", "objects": "pixels", "vision": "vision", "captions": "captions", "faces": "faces", "facedetect": "facedetect", "looks": "looks", "ages": "ages"}
    for j in jobs:
        j["queued"] = bgjobs.is_queued(queue_key[j["key"]])
    return {"jobs": jobs, "photos": total, "models": models if act is not None else [], "queue": bgjobs.status()}


@router.post("/api/auth/login")
async def api_login(request: Request):
    body = await request.json()
    name, src = str(body.get("name", ""))[:64], proxyauth.source(request)
    key = f"{proxyauth.peer_of(request)}|{name.strip().lower()}"  # the real connection address, not a header anyone can set
    nkey = f"name|{name.strip().lower()}"
    wait = max(users.login_wait(key), users.login_wait(nkey))
    if wait:
        proxyauth.authlog.warning("sign-in as %r from %s refused: %d s to wait after repeated failures", name, src, wait)
        raise HTTPException(429, f"Too many attempts; try again in {wait} seconds", headers={"Retry-After": str(wait)})
    u = users.authenticate(name, str(body.get("password", ""))[:1024])
    if not u:
        wait = max(users.login_failed(key), users.login_failed(nkey, users.LOCK_AFTER_NAME))
        proxyauth.authlog.warning("failed sign-in as %r from %s (wrong user name or password%s)", name, src, f"; {wait} s wait" if wait else "")
        raise HTTPException(401, "Wrong user name or password")
    users.login_succeeded(key); users.login_succeeded(nkey)
    if u.get("passwordless") and proxyauth.external(request.headers, proxyauth.peer_of(request)):
        proxyauth.authlog.warning("one-click sign-in as %s refused from %s (outside: a password is required)", u["name"], src)
        raise HTTPException(403, "From outside your network, sign in with an account that has a password")
    proxyauth.authlog.info("signed in: %s (%s, %s) from %s", u["name"], u["role"], "one click" if u.get("passwordless") else "password", src)
    resp = JSONResponse({"user": u, "settings": users.get_settings(u["id"])})
    users.set_session(resp, u["id"], "oneclick" if u.get("passwordless") else "password", users.over_https(request))
    return resp


@router.get("/api/auth/accounts")
def api_accounts(request: Request):
    """The account picker (public): names, and whether a password is needed. From outside (through Cloudflare) only
    accounts with a password."""
    out = users.accounts() if users.any_users() else []
    if proxyauth.external(request.headers, proxyauth.peer_of(request)):
        out = [a for a in out if not a["passwordless"]]
    return {"accounts": out}


@router.post("/api/auth/logout")
def api_logout(request: Request):
    u = request.state.user
    if u:
        proxyauth.authlog.info("signed out: %s from %s", u["name"], proxyauth.source(request))
    # through Cloudflare the page then goes to Cloudflare's own sign-out (/cdn-cgi/access/logout) to end that session too
    outside = proxyauth.external(request.headers, proxyauth.peer_of(request))
    resp = JSONResponse({"ok": True, "cloudflareLogout": "/cdn-cgi/access/logout" if outside else None})
    users.clear_session(resp, cloudflare_cookie=not outside)
    return resp


@router.post("/api/auth/logout_all")
def api_logout_all(request: Request):
    """Every session of this account ends (other browsers, phones); this one is renewed."""
    u = request.state.user
    if not u:
        raise HTTPException(401, "Sign in first")
    users.end_all_sessions(u["id"])
    proxyauth.authlog.info("signed out everywhere: %s from %s", u["name"], proxyauth.source(request))
    resp = JSONResponse({"ok": True})
    users.set_session(resp, u["id"], request.cookies.get(users.VIA_COOKIE) or "password")
    return resp


@router.get("/api/auth/me")
def api_me(request: Request):
    u = request.state.user
    return {"user": u, "settings": users.get_settings(u["id"]) if u else {}, "usersExist": users.any_users(),
            "isAdmin": users.is_admin(u), "canWrite": users.can_write(u), "signedIn": users.is_signed_in(u), "requireLogin": users.require_login(),
            "viaProxy": bool(getattr(request.state, "via_proxy", False)), "signedInBy": _signed_in_by(request, u),
            "throughCloudflare": proxyauth.external(request.headers, proxyauth.peer_of(request))}


def _signed_in_by(request: Request, u):
    """How this browser is signed in: cloudflare, proxy (another proxy's sign-in), password, oneclick; None if not."""
    if not u:
        return None
    if getattr(request.state, "via_proxy", False):
        return "cloudflare" if proxyauth.mode() == "cloudflare" else "proxy"
    via = request.cookies.get(users.VIA_COOKIE)
    return via if via in ("cloudflare", "proxy", "password", "oneclick") else ("oneclick" if u.get("passwordless") else "password")


@router.post("/api/auth/settings")
async def api_my_settings(request: Request):
    """Per-user display settings: {key: value, …}; null removes a key."""
    u = request.state.user
    if not u:
        raise HTTPException(401, "Sign in first")
    return users.set_settings(u["id"], await request.json())


@router.post("/api/auth/password")
async def api_my_password(request: Request):
    u = request.state.user
    if not u:
        raise HTTPException(401, "Sign in first")
    body = await request.json()
    if u.get("passwordless"):  # shared by whoever clicks it: a password set from one browser would lock the others out
        raise HTTPException(403, "This account signs in with one click; an admin can give it a password under Settings › Users")
    if not users.authenticate(u["name"], str(body.get("current", ""))):
        proxyauth.authlog.warning("password change for %s refused from %s (wrong current password)", u["name"], proxyauth.source(request))
        raise HTTPException(403, "The current password is wrong")
    try:
        users.update(u["id"], password=str(body.get("password", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    proxyauth.authlog.info("password changed: %s from %s", u["name"], proxyauth.source(request))
    resp = JSONResponse({"ok": True})
    users.set_session(resp, u["id"], request.cookies.get(users.VIA_COOKIE) or "password", users.over_https(request))  # the old sessions are dead now; this one goes on
    return resp


@router.post("/api/auth/setup")
async def api_setup(request: Request):
    """First admin, allowed only while no user exists."""
    if users.any_users():
        raise HTTPException(409, "Users already exist; sign in as an admin")
    if proxyauth.external(request.headers, proxyauth.peer_of(request)):  # a fresh install is open: the first account is made from inside the network
        proxyauth.authlog.warning("first-account setup refused from outside (%s)", proxyauth.source(request))
        raise HTTPException(403, "The first account is created from inside your network (or on the server itself)")
    body = await request.json()
    try:
        u = users.create(str(body.get("name", ""))[:64], str(body.get("password", ""))[:1024], "admin")
    except ValueError as e:
        raise HTTPException(400, str(e))
    proxyauth.authlog.info("first account created and signed in: %s (admin) from %s", u["name"], proxyauth.source(request))
    resp = JSONResponse({"user": u})
    users.set_session(resp, u["id"], "password", users.over_https(request))
    return resp


def _person_tag(tag_id) -> bool:
    """Is this a person tag in the index (a tag under People with digiKam's person property)?"""
    if not tag_id:
        return True
    con = db()
    try:
        r = con.execute("SELECT person FROM tags WHERE id = ?", (int(tag_id),)).fetchone()
        return bool(r and r[0])
    finally:
        con.close()


@router.post("/api/auth/person")
async def api_my_person(request: Request):
    """Link the signed-in admin's own account to a person from People: {person: tag id or null}. Admins only:
    a linked person's face is shown on the sign-in screen, so who is who is the admin's call (other accounts
    are linked from Settings › Users)."""
    u = request.state.user
    if not u:
        raise HTTPException(401, "Sign in first")
    if not users.is_admin(u):
        raise HTTPException(403, "Only an admin can link an account to a person")
    body = await request.json()
    if not _person_tag(body.get("person")):
        raise HTTPException(400, "That tag is not a person")
    users.set_person(u["id"], body.get("person"))
    return users.get(u["id"])


@router.get("/api/users")
def api_users(request: Request):
    if not users.is_admin(request.state.user):
        raise HTTPException(403, "Admins only")
    return {"users": users.list_users(), "roles": list(users.ROLES)}


@router.post("/api/users")
async def api_users_create(request: Request):
    body = await request.json()
    try:
        return users.create(str(body.get("name", "")), str(body.get("password", "")), str(body.get("role", "viewer")), str(body.get("email") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/users/{user_id}")
async def api_users_update(user_id: int, request: Request):
    """{role} and/or {password} and/or {name} and/or {email} (optional; "" clears it) and/or {listed} (on the sign-in
    screen's account list); {delete: true} removes the user."""
    body = await request.json()
    if not users.get(user_id):
        raise HTTPException(404, "No such user")
    try:
        if body.get("delete"):
            if request.state.user and request.state.user["id"] == user_id:
                raise ValueError("You cannot delete your own account while signed in")
            users.delete(user_id)
        else:
            users.update(user_id, role=body.get("role"), password=body.get("password"), name=body.get("name"), email=body.get("email"),
                         listed=None if body.get("listed") is None else bool(body.get("listed")))
            if "person" in body:
                if not _person_tag(body["person"]):
                    raise ValueError("That tag is not a person")
                users.set_person(user_id, body["person"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


def _db_settings_public() -> dict:
    c = dkdb.settings()
    return {k: c[k] for k in dkdb.KEYS if k != "password"} | {"password_set": bool(c["password"]), "env_type": dkdb.ENV["type"]}


@router.get("/api/roots")
async def api_roots(request: Request):
    """Album roots from digiKam with the local folder each one maps to (admin)."""
    if not users.is_admin(request.state.user):
        raise HTTPException(403, "Admins only")
    return {"roots": await run_in_threadpool(dkdb.album_roots), "photosRoot": str(config.PHOTOS_ROOT)}


@router.post("/api/db/test")
async def api_db_test(request: Request):
    """Try connection settings without saving them (admin). Body: the database fields; an empty password keeps the stored one."""
    if not users.is_admin(request.state.user):
        raise HTTPException(403, "Admins only")
    d = await request.json()
    try:
        return await run_in_threadpool(dkdb.test_connection, d)
    except Exception as e:
        raise HTTPException(400, f"Connection failed: {str(e)[:200]}")


@router.get("/api/viewerdb")
def api_viewerdb(request: Request):
    """Where the viewer keeps its own data: SQLite (the default) or MySQL/MariaDB."""
    if not users.is_admin(request.state.user):
        raise HTTPException(403, "Admins only")
    return vdb.public_settings()


@router.post("/api/viewerdb/test")
async def api_viewerdb_test(request: Request):
    """Try the given viewer-database settings without saving (admin); an empty password keeps the stored one."""
    if not users.is_admin(request.state.user):
        raise HTTPException(403, "Admins only")
    d = await request.json()
    try:
        return await run_in_threadpool(vdb.test_connection, d)
    except Exception as e:
        raise HTTPException(400, f"Connection failed: {str(e)[:200]}")


@router.post("/api/viewerdb/switch")
async def api_viewerdb_switch(request: Request):
    """Body: the settings plus copy (default true: carry every row over) and drop (default true: drop
    and recreate the viewer's tables at the target first). Saves the configuration and restarts the viewer."""
    if not users.is_admin(request.state.user):
        raise HTTPException(403, "Admins only")
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


@router.get("/api/maps")
def api_maps(request: Request):
    """Tile provider for the browser: OpenStreetMap, or Google Maps with the owner's key (session created here)."""
    if users.is_signed_in(request.state.user):
        maps.note_referer(request.headers)  # a visitor's Referer must not steer the key's referrer restriction
    return maps.config()


@router.get("/api/geocode")
async def api_geocode(request: Request, q: str = ""):
    """Address or landmark search for the location dialog: OpenStreetMap's Nominatim (the same
    project the map tiles come from), with the offline GeoNames towns as a fallback."""
    if users.is_signed_in(request.state.user):
        maps.note_referer(request.headers)  # a visitor's Referer must not steer the key's referrer restriction
    q = q.strip()
    if len(q) < 2:
        return {"results": [], "source": None}
    return await run_in_threadpool(geocode.search, q)


@router.get("/api/location/suggest")
def api_location_suggest(id: int, hours: float = 6):
    """Where a photo was probably taken, from the photos nearest in time that have a position: the closest
    one before and after, and others the same day, nearest first (with how far apart in time)."""
    con = db()
    try:
        me = con.execute("SELECT id, ts, camera FROM photos WHERE id = ?", (id,)).fetchone()
        if not me:
            raise HTTPException(404)
        span = int(max(0.25, min(hours, 72)) * 3600)
        rows = con.execute("SELECT id, name, ts, lat, lon, alt, place, region, country, camera FROM photos "
                           "WHERE lat IS NOT NULL AND id != ? AND ts BETWEEN ? AND ? ORDER BY abs(ts - ?) LIMIT 400",
                           (id, me["ts"] - span, me["ts"] + span, me["ts"])).fetchall()
    finally:
        con.close()
    out, seen = [], []
    for r in rows:  # distinct places only: the nearest photo in time stands for each spot within ~150 m
        if any(abs(r["lat"] - s[0]) < 0.0015 and abs(r["lon"] - s[1]) < 0.002 for s in seen):
            continue
        seen.append((r["lat"], r["lon"]))
        out.append({"id": r["id"], "name": r["name"], "minutes": round((r["ts"] - me["ts"]) / 60), "lat": r["lat"], "lon": r["lon"], "alt": r["alt"],
                    "place": ", ".join(x for x in (r["place"], r["region"], r["country"]) if x), "same_camera": r["camera"] == me["camera"]})
        if len(out) >= 6:
            break
    return {"suggestions": out, "landmarks": maps.provider() == "google"}


@router.post("/api/location/landmark")
async def api_location_landmark(request: Request):
    """Body: {id}. Google Cloud Vision looks for a famous landmark in the photo (a preview is sent, not the original)."""
    body = await request.json()
    pid = int(body.get("id") or 0)
    if not maps.key():
        raise HTTPException(409, "Landmark recognition uses Google Cloud Vision: add a Google API key in Settings › Map provider")
    con = db()
    try:
        row = con.execute("SELECT path, w, h, video FROM photos WHERE id = ?", (pid,)).fetchone()
    finally:
        con.close()
    if not row or row["video"]:
        raise HTTPException(404, "No such photo")

    def work():
        import io
        im, _ = thumbs._open_oriented(row["path"], 1024, 1024)
        im = im.convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return maps.landmarks(buf.getvalue())
    if users.is_signed_in(request.state.user):
        maps.note_referer(request.headers)  # a visitor's Referer must not steer the key's referrer restriction
    try:
        found = await run_in_threadpool(work)
    except RuntimeError as e:
        raise HTTPException(502, f"Google Cloud Vision: {e}. It needs the Cloud Vision API enabled for the key, and a key not restricted to website addresses (Google refuses those from a server).")
    return {"landmarks": found}


@router.get("/api/elevation")
async def api_elevation(request: Request, lat: float, lon: float):
    """Ground height at a point for the location dialog's altitude field (Google Elevation, else Open-Meteo)."""
    if users.is_signed_in(request.state.user):
        maps.note_referer(request.headers)  # a visitor's Referer must not steer the key's referrer restriction
    if abs(lat) > 90 or abs(lon) > 180:
        raise HTTPException(400, "Not a position")
    try:
        return await run_in_threadpool(maps.elevation, lat, lon)
    except Exception as e:
        raise HTTPException(502, f"No elevation service could be reached: {str(e)[:160]}")


@router.get("/api/models")
def api_models():
    """The downloadable models and data: what is installed, and the last update check."""
    return dlmodels.listing()


@router.post("/api/models/check")
async def api_models_check():
    """Ask each source whether the installed files have changed since they were downloaded (network)."""
    await run_in_threadpool(dlmodels.check_all)
    return dlmodels.listing()


@router.post("/api/models/download")
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


@router.get("/api/update")
def api_update(request: Request):
    """Installed commit, the last GitHub check, and whether update.sh can run here (admins: the log names server paths)."""
    if not users.is_admin(request.state.user):
        raise HTTPException(403 if request.state.user else 401, "Admins only")
    return updates.status()


@router.post("/api/update")
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
        return {"token": updates.admin_token(), "fromEnv": bool(os.environ.get("MCP_ADMIN_TOKEN"))}
    if action == "regenerate_token":
        if os.environ.get("MCP_ADMIN_TOKEN"):
            raise HTTPException(400, "The token comes from MCP_ADMIN_TOKEN in the environment; change it there")
        return {"token": updates.regenerate_token()}
    raise HTTPException(400, "action must be check, run, token or regenerate_token")


@router.post("/api/restart")
def api_restart():
    """Exit; launchd (KeepAlive) or Docker (restart: unless-stopped) starts the viewer again."""
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True}


@router.get("/api/log")
def api_log(lines: int = 300, level: str = "any", contains: str = "", requests: int = 0):
    """Maintenance › Server log: the end of data/viewer.log, filtered (admins)."""
    from app import logs
    return logs.tail(lines, contains[:200], level if level in ("any", "warning", "error", "auth") else "any", bool(requests))


@router.get("/api/log/download")
def api_log_download():
    if not config.LOG_FILE.exists():
        raise HTTPException(404, "No log file yet")
    return FileResponse(config.LOG_FILE, media_type="text/plain", filename="iopix-viewer.log", content_disposition_type="attachment")


@router.get("/api/maintenance")
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
        "storage": {k: _dir_size(config.DATA_DIR / v) for k, v in (
            ("thumbnails", "cache/t"), ("previews", "cache/p"), ("faces", "cache/f"), ("models", "models"),
            ("backups", "sidecar-backups"), ("fileBackups", "file-backups"), ("dbBackups", "db-backups"), ("deletedPhotos", "deleted-photos"))} | {
            "index": config.INDEX_DB.stat().st_size if config.INDEX_DB.exists() else 0,
            "viewerDb": vdb.SQLITE_FILE.stat().st_size if not vdb.is_mysql() and vdb.SQLITE_FILE.exists() else 0,
            "viewerDbError": vdb.error,  # MySQL configured but unusable at start: the SQLite file is in use instead
            "embeddings": sum(e.file.stat().st_size for e in semantic.engines.values() if e.file.exists()),
            "free": free},
        "writeFiles": edits.writes_files(), "readOnly": appstate.read_only(), "readOnlyLocked": config.READ_ONLY, "digikam": index.status(), "appName": app_name(), "appNameSet": appstate.get_setting("app_name", ""),
        "updateCheck": updates.auto_check_enabled(), "updateCheckLocked": os.environ.get("UPDATE_CHECK", "").strip() == "0", "database": _db_settings_public(), "mapProvider": appstate.get_setting("map_provider", "osm"), "googleMapsKeySet": bool(maps.key()), "googleServerKeySet": bool(appstate.get_setting("google_server_key", "") or os.environ.get("GOOGLE_SERVER_KEY", "")), "sidecarCompat": edits.compat_naming(), "searchModel": semantic.chosen(),
        "templates": json.loads(appstate.get_setting("templates", "[]")), "templateFields": list(edits.TEMPLATE_FIELDS),
        "recentTags": json.loads(appstate.get_setting("recent_tags", "[]")),
        "scanner": _scanner_state(),
    }
    if not users.is_admin(request.state.user):
        for k in ("paths", "database", "storage", "albumRoots", "album_roots"):
            out.pop(k, None)
    return out


def _scanner_state() -> dict:
    from app.photos import scanner
    return {"enabled": scanner.enabled(), "interval": scanner.interval(), "running": scanner.progress["running"], "last": scanner.progress["last"]}


@router.get("/api/scan")
def api_scan_status():
    return _scanner_state()


@router.post("/api/scan")
async def api_scan_run():
    """Scan the album folders now (also when the setting is off): new, changed and vanished files and folders."""
    from app.photos import scanner
    if not config.DB_WRITE:
        raise HTTPException(403, "Writing to digiKam's database is turned off (DB_WRITE=0)")
    if scanner.progress["running"]:
        raise HTTPException(409, "A scan is already running")
    scanner.run_soon()
    return {"started": True}


@router.post("/api/maintenance")
async def api_maintenance_run(request: Request):
    """Body: {action}. reindex: rebuild the search index from digiKam's database now.
    thumbs: throw away all grid thumbnails and rebuild. previews / faces: clear those caches.
    covers: pick tag cover photos again."""
    body = await request.json()
    action = body.get("action")
    if action == "reindex":
        await run_in_threadpool(index.build)
        state["version"] = None  # so _refresh treats the new index as a change
        _kick.set()
    elif action in ("thumbs", "previews", "faces"):
        await run_in_threadpool(shutil.rmtree, config.CACHE_DIR / {"thumbs": "t", "previews": "p", "faces": "f"}[action], ignore_errors=True)
        if action == "thumbs":
            await run_in_threadpool(_start_prewarm)
    elif action == "covers":
        threading.Thread(target=_compute_tag_covers, args=(state.get("source"),), daemon=True).start()
    else:
        raise HTTPException(400, "Unknown action")
    return {"ok": True}



@router.get("/api/library/merge")
def api_library_merge_plan():
    """What merging the changes made with sync off into digiKam's database would do (admins)."""
    try:
        return merge.plan()
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))


@router.post("/api/library/merge")
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
    await run_in_threadpool(_rebuild_after_write)
    return r



@router.post("/api/analysis/run")
async def api_analysis_run(request: Request):
    """Body: {job: all|embeddings|quality|vision|objects, reset: bool}. Without reset it only
    picks up photos that are new or changed; with reset that analysis starts over."""
    body = await request.json()
    job, reset = body.get("job", "all"), bool(body.get("reset"))
    if job == "facedetect":  # writes faces into digiKam's database, so only ever on request
        _face_detect_allowed()
        if reset:
            facedetect.reset()
        n = facedetect.start(after=_rebuild_after_write)
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
    _kick.set()
    return {"ok": True}



@router.get("/api/backup/{which}")
def api_backup(which: str, request: Request):
    if request.headers.get("sec-fetch-site") == "cross-site":  # a link on another site must not start a multi-hundred-MB dump
        raise HTTPException(403, "Open this from the viewer itself")
    """A download of digiKam's database (a consistent copy of the SQLite file, or a fresh mysqldump)
    or of the viewer's own state database. Admins only; the copy is also kept in db-backups."""
    if not users.is_admin(request.state.user):
        raise HTTPException(403 if request.state.user else 401, "Admins only")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dkdb.BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    def snapshot(db_file, dst):
        """A consistent copy of a SQLite database that may be in use."""
        src = sqlite3.connect(f"file:{quote(str(db_file))}?mode=ro", uri=True)
        try:
            out = sqlite3.connect(str(dst))
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()

    if which == "app":  # everything the viewer keeps: its database (state and AI results) plus the content-search index
        dst = dkdb.BACKUP_DIR / f"viewer-backup-{stamp}.zip"
        tmp = dkdb.BACKUP_DIR / f".snap-{stamp}"
        tmp.mkdir(exist_ok=True)
        try:
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
                vdb.snapshot_sqlite(tmp / "viewer.db")  # a SQLite copy even when the viewer runs on MySQL
                z.write(tmp / "viewer.db", "viewer.db")
                for f in sorted(config.DATA_DIR.glob("embeddings*.npz")) + sorted(config.DATA_DIR.glob("embeddings*.npz.complete")):
                    z.write(f, f.name)
                z.writestr("README.txt", "iopix backup. Restore by stopping the viewer and copying these files into its data folder "
                           "(viewer.db: users, albums, favorites, settings, pending edits, and the analysis results: sharpness, text, objects, "
                           "captions, consistency report; embeddings*.npz: the content-search index). digiKam's own database is a separate download.\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        dkdb._prune("viewer-backup-*.zip")
        return FileResponse(dst, media_type="application/zip", filename=f"iopix-backup-{stamp}.zip", content_disposition_type="attachment")
    if which != "digikam":
        raise HTTPException(404, "backup is 'digikam' or 'app'")
    if dkdb.no_digikam():
        raise HTTPException(404, "There is no digiKam database; the library is in the app backup")
    if dkdb.is_mysql():
        try:
            dst = dkdb.backup(force=True)
        except dbsync.SyncError as e:
            raise HTTPException(409, str(e))
        if dst is None:
            raise HTTPException(400, "Database backups are switched off (DIGIKAM_DB_BACKUP=skip)")
        return FileResponse(dst, media_type="application/sql", filename=dst.name, content_disposition_type="attachment")
    dst = dkdb.BACKUP_DIR / f"digikam4-download-{stamp}.db"
    snapshot(dkdb.sqlite_path(), dst)  # a consistent copy even while digiKam writes
    dkdb._prune("digikam4-download-*.db")
    return FileResponse(dst, media_type="application/vnd.sqlite3", filename=f"digikam4-{stamp}.db", content_disposition_type="attachment")
