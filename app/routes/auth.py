"""Sign-in: login, logout, the signed-in account's own settings and password, first-run setup."""

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse

from app import access, proxyauth, users
from app.core import db



router = APIRouter()


@router.post("/api/auth/login", dependencies=[access.public])
async def api_login(request: Request):
    body = await request.json()
    name, src = str(body.get("name", ""))[:64], proxyauth.source(request)
    key = f"{proxyauth.visitor_of(request)}|{name.strip().lower()}"  # the visitor's address (a header only from a trusted proxy)
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


@router.get("/api/auth/accounts", dependencies=[access.public])
def api_accounts(request: Request):
    """The account picker (public): names, and whether a password is needed. From outside (through Cloudflare) only
    accounts with a password."""
    out = users.accounts() if users.any_users() else []
    if proxyauth.external(request.headers, proxyauth.peer_of(request)):
        out = [a for a in out if not a["passwordless"]]
    return {"accounts": out}


@router.post("/api/auth/logout", dependencies=[access.public])
def api_logout(request: Request):
    u = request.state.user
    if u:
        proxyauth.authlog.info("signed out: %s from %s", u["name"], proxyauth.source(request))
    # through Cloudflare the page then goes to Cloudflare's own sign-out (/cdn-cgi/access/logout) to end that session too
    outside = proxyauth.external(request.headers, proxyauth.peer_of(request))
    resp = JSONResponse({"ok": True, "cloudflareLogout": "/cdn-cgi/access/logout" if outside else None})
    users.clear_session(resp, cloudflare_cookie=not outside)
    return resp


@router.post("/api/auth/logout_all", dependencies=[access.signed_in])
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


@router.get("/api/auth/me", dependencies=[access.public])
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


@router.post("/api/auth/settings", dependencies=[access.signed_in])
async def api_my_settings(request: Request):
    """Per-user display settings: {key: value, …}; null removes a key."""
    u = request.state.user
    if not u:
        raise HTTPException(401, "Sign in first")
    return users.set_settings(u["id"], await request.json())


@router.post("/api/auth/password", dependencies=[access.signed_in])
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


@router.post("/api/auth/setup", dependencies=[access.public])
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


def person_tag(tag_id) -> bool:
    """Is this a person tag in the index (a tag under People with digiKam's person property)?"""
    if not tag_id:
        return True
    con = db()
    try:
        r = con.execute("SELECT person FROM tags WHERE id = ?", (int(tag_id),)).fetchone()
        return bool(r and r[0])
    finally:
        con.close()


@router.post("/api/auth/person", dependencies=[access.public])
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
    if not person_tag(body.get("person")):
        raise HTTPException(400, "That tag is not a person")
    users.set_person(u["id"], body.get("person"))
    return users.get(u["id"])
