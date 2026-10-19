"""Sign-in done by a reverse proxy in front of the viewer: Cloudflare Access (a Cloudflare Tunnel with an Access
policy), or HAProxy, nginx, Caddy or Traefik with an authentication step (Authelia, Authentik, oauth2-proxy, basic
auth...). The proxy passes who signed in in a request header; the viewer takes that person as signed in when the
account exists (matched on the account's email address, then its name).

The header is believed only from the proxy itself, never from anyone who can reach the viewer directly:
- Cloudflare Access: with CF_ACCESS_TEAM and CF_ACCESS_AUD set, the signed token Cloudflare adds to every request
  (Cf-Access-Jwt-Assertion) is checked against the team's public keys, and the email comes from it.
- otherwise: the request must come straight from one of AUTH_PROXY_TRUSTED (addresses or networks; by default this
  machine only, 127.0.0.1 and ::1), the address the proxy connects from.

Settings (environment, iopix.env):
  AUTH_PROXY          off (default) | cloudflare | haproxy | nginx | caddy | traefik | header
  AUTH_PROXY_HEADER   the header(s) holding the user, comma-separated; defaults: Cf-Access-Authenticated-User-Email
                      for cloudflare, otherwise Remote-User, X-Forwarded-User, X-Auth-Request-User,
                      X-Auth-Request-Email, X-Forwarded-Email (the names Authelia, Authentik and oauth2-proxy use)
  AUTH_PROXY_TRUSTED  addresses or networks the proxy connects from (default 127.0.0.1, ::1)
  AUTH_PROXY_CREATE   a role (viewer or editor) to create an account for someone the proxy lets in who has none
                      yet; empty (default): such a person is treated as not signed in
  CF_ACCESS_TEAM      the Cloudflare Access team domain, e.g. myteam.cloudflareaccess.com
  CF_ACCESS_AUD       the Access application's audience (AUD) tag
"""
import ipaddress
import logging
import os
import threading
import time

log = logging.getLogger("app.proxyauth")

PRESETS = ("cloudflare", "haproxy", "nginx", "caddy", "traefik", "header")
CF_HEADER = "Cf-Access-Authenticated-User-Email"
CF_JWT_HEADER = "Cf-Access-Jwt-Assertion"
GENERIC_HEADERS = ("Remote-User", "X-Forwarded-User", "X-Auth-Request-User", "X-Auth-Request-Email", "X-Forwarded-Email")
_jwks = {"client": None, "team": None}
_lock = threading.Lock()


KEYS = ("AUTH_PROXY", "AUTH_PROXY_HEADER", "AUTH_PROXY_TRUSTED", "AUTH_PROXY_CREATE", "CF_ACCESS_TEAM", "CF_ACCESS_AUD")
_opts = {"at": 0.0, "values": {}}
seen = {}  # the last Cloudflare Access token seen (unverified claims), to set this up from: {iss, aud, email, peer, at}


def _opt(name: str):
    """A setting: the environment (iopix.env) wins, else what an admin saved (Settings, or the MCP tool), re-read every
    few seconds."""
    if os.environ.get(name) not in (None, ""):
        return os.environ.get(name)
    now = time.time()
    if now - _opts["at"] > 5:
        try:
            from app.data import state
            _opts["values"] = {k: state.get_setting(k.lower(), "") for k in KEYS}
        except Exception:
            _opts["values"] = {}
        _opts["at"] = now
    return _opts["values"].get(name) or None


def save(values: dict):
    """Store proxy sign-in settings (keys of KEYS; empty removes one). Values in the environment still win."""
    from app.data import state
    for k, v in values.items():
        if k in KEYS:
            state.set_setting(k.lower(), str(v or "").strip())
    _opts["at"] = 0.0
    _cache.clear()


def note(headers_in, peer: str):
    """Remember the claims of a Cloudflare Access token (not verified: only to show which team and application the
    requests come through, so they can be set up)."""
    tok = headers_in.get(CF_JWT_HEADER)
    if not tok:
        return
    try:
        import jwt
        c = jwt.decode(tok, options={"verify_signature": False})
        seen.update(iss=c.get("iss"), aud=c.get("aud"), email=c.get("email"), peer=peer, at=int(time.time()))
    except Exception:
        pass


EXTERNAL_HEADERS = ("cf-ray", "cf-connecting-ip", "cf-access-jwt-assertion")


def external(headers_in) -> bool:
    """Did the request come in from outside, through Cloudflare? (Requests on the LAN reach the viewer directly.)
    Outside, one-click sign-in to a passwordless account is not offered: a password is required. A forged header can
    only take that option away from the one who forges it, never grant anything."""
    return any(headers_in.get(h) for h in EXTERNAL_HEADERS)


def mode() -> str:
    m = (_opt("AUTH_PROXY") or "off").strip().lower()
    return m if m in PRESETS else "off"


def headers() -> list:
    raw = (_opt("AUTH_PROXY_HEADER") or "").strip()
    if raw:
        return [h.strip() for h in raw.split(",") if h.strip()]
    return [CF_HEADER] if mode() == "cloudflare" else list(GENERIC_HEADERS)


def trusted() -> list:
    raw = (_opt("AUTH_PROXY_TRUSTED") or "").strip() or "127.0.0.1,::1"
    out = []
    for part in raw.replace(" ", ",").split(","):
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                log.warning("AUTH_PROXY_TRUSTED: %r is not an address or network", part)
    return out


def cf_settings():
    team = (_opt("CF_ACCESS_TEAM") or "").strip().removeprefix("https://").rstrip("/")
    aud = (_opt("CF_ACCESS_AUD") or "").strip()
    return (team, aud) if team and aud else (None, None)


def _from_trusted(peer: str) -> bool:
    try:
        ip = ipaddress.ip_address((peer or "").split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in trusted())


def _cf_email(token: str, team: str, aud: str):
    """The email in a Cloudflare Access token, once its signature, audience, issuer and expiry check out."""
    import jwt
    with _lock:
        if _jwks["team"] != team:
            _jwks.update(team=team, client=jwt.PyJWKClient(f"https://{team}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600))
        client = _jwks["client"]
    try:
        key = client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=aud, issuer=f"https://{team}", leeway=30)
    except Exception as e:  # a forged, expired or foreign token: nobody
        log.warning("Cloudflare Access token refused: %s", str(e)[:200])
        return None
    return (claims.get("email") or claims.get("common_name") or "").strip() or None


def identity(headers_in, peer: str):
    """Who the proxy says is asking, or None (not configured, not from the proxy, or no user in the request)."""
    m = mode()
    if m == "off":
        return None
    if m == "cloudflare":
        team, aud = cf_settings()
        if team:
            token = headers_in.get(CF_JWT_HEADER) or ""
            if not token:
                return None
            return _cf_email(token, team, aud)
    if not _from_trusted(peer):
        return None
    for name in headers():
        value = (headers_in.get(name) or "").strip()
        if value:
            return value
    return None


def user_for(ident: str):
    """The account for this identity (email first, then name, case-insensitively), created with AUTH_PROXY_CREATE's
    role when there is none and that is allowed; None otherwise."""
    from app import users
    from app.data import state
    v = ident.strip().lower()
    con = state.connect()
    try:
        r = con.execute(f"SELECT {users.COLS} FROM user_accounts WHERE lower(email) = ?", (v,)).fetchone() \
            or con.execute(f"SELECT {users.COLS} FROM user_accounts WHERE lower(name) = ?", (v,)).fetchone()
    finally:
        con.close()
    if r:
        return users._row(r)
    role = (_opt("AUTH_PROXY_CREATE") or "").strip().lower()
    if role not in ("viewer", "editor"):
        return None
    name = ident.strip()[:64]
    try:
        import secrets
        # a random password nobody knows: this person signs in at the proxy (an admin can set a real one later)
        u = users.create(name, secrets.token_urlsafe(24), role, email=ident if "@" in ident else "")
        log.info("account %s created for a sign-in through the proxy (%s)", name, role)
        return u
    except ValueError as e:
        log.warning("could not create an account for %s: %s", name, e)
        return None


_cache = {}  # identity -> (user, until): a page load makes dozens of requests


def user(request):
    """The signed-in user the proxy vouches for, or None."""
    ident = identity(request.headers, request.scope.get("iopix.peer") or (request.client.host if request.client else ""))
    if not ident:
        return None
    hit = _cache.get(ident)
    now = time.time()
    if hit and hit[1] > now:
        return hit[0]
    u = user_for(ident)
    _cache[ident] = (u, now + 30)
    return u


def status() -> dict:
    """For Settings and diagnostics: how proxy sign-in is set up (no secrets)."""
    team, aud = cf_settings()
    return {"mode": mode(), "headers": headers(), "trusted": [str(n) for n in trusted()], "seen": dict(seen),
            "team": team, "aud": aud,
            "cloudflareVerified": bool(team), "create": (_opt("AUTH_PROXY_CREATE") or "").strip().lower() or None}


def remember_peer():
    """uvicorn rewrites the client address from X-Forwarded-For for a trusted proxy (its --forwarded-allow-ips) before
    the app sees the request; keep the address the connection really came from, which is what proxy sign-in trusts."""
    try:
        from uvicorn.middleware import proxy_headers
    except ImportError:
        return
    cls = proxy_headers.ProxyHeadersMiddleware
    if getattr(cls, "_iopix_peer", False):
        return
    original = cls.__call__

    async def call(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket") and scope.get("client"):
            scope["iopix.peer"] = scope["client"][0]
        return await original(self, scope, receive, send)
    cls.__call__ = call
    cls._iopix_peer = True
