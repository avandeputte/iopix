"""Users, sessions and permissions.

Three roles: viewer (signed in; own settings kept on the server; favorites and viewer
albums), editor (viewer plus every library write: edits, tags, faces, place tags, GPX,
sync, database apply) and admin (editor plus settings, maintenance and user management).
Anyone not signed in browses read-only with display settings in the browser. A viewer may
have no password at all: such an account signs in with one click from the account picker
(like a media-server profile) and can never be more than a viewer.

Until the first user exists the viewer stays open as before, so a fresh install is not
locked out; Settings asks to create the admin, or ADMIN_USER / ADMIN_PASSWORD in the
environment create one at start (containers, installers). Passwords are scrypt-hashed;
a session is a signed, HttpOnly cookie (secret in data/secret) valid for 30 days.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from app import config, proxyauth
from app.data import state

ROLES = ("viewer", "editor", "admin")
COOKIE = "iopix_session"
SESSION_DAYS = 30
_secret = None


def init():
    # the users table (and its person / email columns) is part of the schema vdb.ensure_schema keeps; state.init ran it
    name, pw = os.environ.get("ADMIN_USER", "").strip(), os.environ.get("ADMIN_PASSWORD", "")
    if name and pw and not any_users():
        create(name, pw, "admin")


def _key() -> bytes:
    global _secret
    if _secret is None:
        p = config.DATA_DIR / "secret"
        try:
            _secret = p.read_bytes().strip()
            if len(_secret) < 32:
                raise ValueError
        except (OSError, ValueError):
            _secret = secrets.token_bytes(32)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_secret)
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
    return _secret


# ---- passwords
SCRYPT = (2 ** 16, 8, 1)  # n, r, p: 64 MiB per hash (OWASP's floor is 2^17 x 8 x 1; 2^16 keeps a small container responsive)


def hash_password(pw: str) -> str:
    """scrypt2$n$r$p$salt$hash: the parameters travel with the hash, so they can be raised later."""
    n, r, p = SCRYPT
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=256 * 1024 * 1024)
    return f"scrypt2${n}${r}${p}$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


_DUMMY_HASH = None  # a hash to check an unknown name against (made once, so a miss costs the same as a wrong password)


def check_password(pw: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if parts[0] == "scrypt2":
            n, r, p, s, h = int(parts[1]), int(parts[2]), int(parts[3]), parts[4], parts[5]
        else:  # the first format: scrypt$salt$hash with n=2^14, r=8, p=1
            _, s, h = parts
            n, r, p = 2 ** 14, 8, 1
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=n, r=r, p=p, dklen=32, maxmem=256 * 1024 * 1024)
        return hmac.compare_digest(got, base64.b64decode(h))
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    """An older or weaker hash: replaced with the current one once the password is seen again."""
    parts = (stored or "").split("$")
    return bool(stored) and (parts[0] != "scrypt2" or tuple(int(x) for x in parts[1:4]) != SCRYPT)


# ---- users
COLS = "id, name, role, created, last_login, hash, person, email, listed"


def row(r) -> dict:
    return {"id": r[0], "name": r[1], "role": r[2], "created": r[3], "last_login": r[4], "passwordless": not r[5], "person": r[6], "email": r[7] or "", "listed": r[8] != 0}


def clean_email(email) -> str:
    """An optional address: empty, or something@something.tld, at most 254 characters."""
    email = (email or "").strip().lower()
    if email and (len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        raise ValueError("That does not look like an email address")
    return email


def require_login() -> bool:
    """Settings › Users › Require sign-in: nothing but the sign-in screen for visitors. Only
    meaningful once an account exists (otherwise nobody could ever sign in)."""
    return state.get_setting("require_login", "0") == "1" and any_users()


def linked_persons() -> set:
    """The person tags listed accounts are linked to: their face badges are the account picker's avatars,
    so they stay visible on the sign-in screen even when sign-in is required."""
    con = state.connect()
    try:
        return {r[0] for r in con.execute("SELECT person FROM user_accounts WHERE person IS NOT NULL AND listed = 1")}
    finally:
        con.close()


def any_users() -> bool:
    con = state.connect()
    try:
        return con.execute("SELECT 1 FROM user_accounts LIMIT 1").fetchone() is not None
    finally:
        con.close()


def list_users() -> list:
    con = state.connect()
    try:
        return [row(r) for r in con.execute(f"SELECT {COLS} FROM user_accounts ORDER BY id")]
    finally:
        con.close()


def get(user_id: int):
    con = state.connect()
    try:
        r = con.execute(f"SELECT {COLS} FROM user_accounts WHERE id = ?", (user_id,)).fetchone()
        return row(r) if r else None
    finally:
        con.close()


def create(name: str, password: str, role: str, email: str = "") -> dict:
    name = name.strip()
    email = clean_email(email)
    if not name or len(name) > 64:
        raise ValueError("A user name is needed (up to 64 characters)")
    if role not in ROLES:
        raise ValueError("Unknown role")
    if password == "" and role != "viewer":
        raise ValueError("Only a viewer can have no password")
    if password != "" and len(password) < 8:
        raise ValueError("The password needs at least 8 characters")
    con = state.connect()
    try:
        if con.execute("SELECT 1 FROM user_accounts WHERE lower(name) = lower(?)", (name,)).fetchone():
            raise ValueError("That user name is taken")
        if email and con.execute("SELECT 1 FROM user_accounts WHERE email = ?", (email,)).fetchone():
            raise ValueError("Another account already has that email address")
        cur = con.execute("INSERT INTO user_accounts (name, hash, role, created, email) VALUES (?,?,?,?,?)", (name, hash_password(password) if password else "", role, int(time.time()), email or None))
        con.commit()
        return get(cur.lastrowid)
    finally:
        con.close()


def update(user_id: int, role: str = None, password: str = None, name: str = None, email: str = None, listed: bool = None):
    con = state.connect()
    try:
        if email is not None:
            email = clean_email(email)
            if email and con.execute("SELECT 1 FROM user_accounts WHERE email = ? AND id != ?", (email, user_id)).fetchone():
                raise ValueError("Another account already has that email address")
            con.execute("UPDATE user_accounts SET email = ? WHERE id = ?", (email or None, user_id))
        if role is not None:
            if role not in ROLES:
                raise ValueError("Unknown role")
            if role != "admin" and con.execute("SELECT count(*) FROM user_accounts WHERE role = 'admin' AND id != ?", (user_id,)).fetchone()[0] == 0 \
                    and con.execute("SELECT role FROM user_accounts WHERE id = ?", (user_id,)).fetchone()[0] == "admin":
                raise ValueError("That is the only admin; make someone else admin first")
            if role != "viewer" and not con.execute("SELECT hash FROM user_accounts WHERE id = ?", (user_id,)).fetchone()[0]:
                raise ValueError("Set a password first; an account without one can only be a viewer")
            con.execute("UPDATE user_accounts SET role = ? WHERE id = ?", (role, user_id))
        if password is not None:
            if password == "":  # remove the password: one-click sign-in, viewers only
                if con.execute("SELECT role FROM user_accounts WHERE id = ?", (user_id,)).fetchone()[0] != "viewer":
                    raise ValueError("Only a viewer can have no password")
                con.execute("UPDATE user_accounts SET hash = '' WHERE id = ?", (user_id,))
            else:
                if len(password) < 8:
                    raise ValueError("The password needs at least 8 characters")
                con.execute("UPDATE user_accounts SET hash = ? WHERE id = ?", (hash_password(password), user_id))
        if name is not None:
            name = name.strip()
            if not name or len(name) > 64:
                raise ValueError("A user name is needed (up to 64 characters)")
            if con.execute("SELECT 1 FROM user_accounts WHERE lower(name) = lower(?) AND id != ?", (name, user_id)).fetchone():
                raise ValueError("That user name is taken")
            con.execute("UPDATE user_accounts SET name = ? WHERE id = ?", (name, user_id))
        if listed is not None:  # off the sign-in screen's account list: the account signs in through "Other", by name
            con.execute("UPDATE user_accounts SET listed = ? WHERE id = ?", (1 if listed else 0, user_id))
        con.commit()
    finally:
        con.close()
    _forget_proxy_identities()


def delete(user_id: int):
    con = state.connect()
    try:
        r = con.execute("SELECT role FROM user_accounts WHERE id = ?", (user_id,)).fetchone()
        if r and r[0] == "admin" and con.execute("SELECT count(*) FROM user_accounts WHERE role = 'admin'").fetchone()[0] == 1:
            raise ValueError("That is the only admin; make someone else admin first")
        con.execute("DELETE FROM user_accounts WHERE id = ?", (user_id,))
        con.commit()
    finally:
        con.close()
    _forget_proxy_identities()


def _forget_proxy_identities():
    """A sign-in through the proxy is remembered for half a minute (proxyauth); a change here must not outlive that."""
    proxyauth.forget_identities()


def authenticate(name: str, password: str):
    con = state.connect()
    try:
        r = con.execute("SELECT id, hash FROM user_accounts WHERE name = ?", (name.strip(),)).fetchone()
        if not r:
            global _DUMMY_HASH
            if _DUMMY_HASH is None:
                _DUMMY_HASH = hash_password("not a real password")
            check_password(password, _DUMMY_HASH)  # the same work as for a wrong password: an unknown name is not told apart by its timing
            return None
        if r[1] and not check_password(password, r[1]):  # an empty hash is a passwordless viewer: one click signs in
            return None
        if r[1] and needs_rehash(r[1]):  # the password is known now: stored with the current parameters
            con.execute("UPDATE user_accounts SET hash = ? WHERE id = ?", (hash_password(password), r[0]))
        con.execute("UPDATE user_accounts SET last_login = ? WHERE id = ?", (int(time.time()), r[0]))
        con.commit()
        return get(r[0])
    finally:
        con.close()


def accounts() -> list:
    """What the sign-in picker shows to anyone: names and whether a password is asked for. An account taken off
    the list (Settings › Users › Sign-in list) signs in through "Other", by name."""
    con = state.connect()
    try:
        return [{"id": r[0], "name": r[1], "passwordless": not r[2], "person": r[3]} for r in con.execute("SELECT id, name, hash, person FROM user_accounts WHERE listed = 1 ORDER BY lower(name)")]
    finally:
        con.close()


def set_person(user_id: int, tag_id):
    """Link the account to a digiKam person tag (None unlinks). The caller checks the tag is a person."""
    con = state.connect()
    try:
        con.execute("UPDATE user_accounts SET person = ? WHERE id = ?", (int(tag_id) if tag_id else None, user_id))
        con.commit()
    finally:
        con.close()


# ---- per-user settings (display preferences the browser used to keep in localStorage)
def get_settings(user_id: int) -> dict:
    con = state.connect()
    try:
        r = con.execute("SELECT settings FROM user_accounts WHERE id = ?", (user_id,)).fetchone()
        return json.loads(r[0]) if r and r[0] else {}
    finally:
        con.close()


def set_settings(user_id: int, changes: dict) -> dict:
    cur = get_settings(user_id)
    for k, v in changes.items():
        if not isinstance(k, str) or len(k) > 64:
            continue
        if v is None:
            cur.pop(k, None)
        elif isinstance(v, (str, int, float, bool)) and len(str(v)) <= 2000:
            cur[k] = v
    con = state.connect()
    try:
        con.execute("UPDATE user_accounts SET settings = ? WHERE id = ?", (json.dumps(cur), user_id))
        con.commit()
    finally:
        con.close()
    return cur


# ---- sign-in attempts: a wrong password costs a growing wait, per address and account, so a password cannot be
# guessed by trying many (the counters live in memory: a restart clears them)
_attempts = {}  # key -> [failures, locked_until]
_attempts_lock = threading.Lock()
LOCK_AFTER = 5         # free tries before the first wait (per address and account)
LOCK_AFTER_NAME = 20   # free tries per account whatever the addresses: many addresses cannot share the guessing
LOCK_BASE = 30         # seconds; doubles with every further failure, up to LOCK_MAX
LOCK_MAX = 15 * 60


def login_wait(key: str) -> int:
    """Seconds this key still has to wait before another try (0: go ahead)."""
    with _attempts_lock:
        rec = _attempts.get(key)
        if not rec:
            return 0
        return max(0, int(-(-(rec[1] - time.time()) // 1)))  # rounded up: never tell someone 0 s when a moment remains


def login_failed(key: str, free: int = LOCK_AFTER) -> int:
    """Count a wrong password; returns the wait now imposed (0 while under the free tries)."""
    with _attempts_lock:
        if len(_attempts) > 10000:  # never let strangers fill the memory: expired entries go, the live locks stay
            now = time.time()
            for k in [k for k, (n, until) in _attempts.items() if until < now - LOCK_MAX]:
                del _attempts[k]
            if len(_attempts) > 10000:
                for k in list(_attempts)[:len(_attempts) // 2]:
                    del _attempts[k]
        rec = _attempts.setdefault(key, [0, 0.0])
        rec[0] += 1
        over = rec[0] - free
        wait = min(LOCK_MAX, LOCK_BASE * 2 ** (over - 1)) if over > 0 else 0
        rec[1] = time.time() + wait
        return wait


def login_succeeded(key: str):
    with _attempts_lock:
        _attempts.pop(key, None)


# ---- sessions
def _session_salt(user_id: int) -> str:
    """Part of the password hash plus the account's session generation: a changed password, or "sign out
    everywhere", ends every session that was signed before."""
    con = state.connect()
    try:
        r = con.execute("SELECT hash, session_gen FROM user_accounts WHERE id = ?", (user_id,)).fetchone()
    finally:
        con.close()
    return f"{(r[0] or 'none')[-16:]}:{r[1] or 0}" if r else "none"


def end_all_sessions(user_id: int):
    """Every session of this account stops working (the caller sets a fresh cookie for the current one if wanted)."""
    con = state.connect()
    try:
        con.execute("UPDATE user_accounts SET session_gen = coalesce(session_gen, 0) + 1 WHERE id = ?", (user_id,))
        con.commit()
    finally:
        con.close()


def make_token(user_id: int) -> str:
    payload = f"{user_id}.{int(time.time()) + SESSION_DAYS * 86400}"
    sig = hmac.new(_key(), (payload + "|" + _session_salt(user_id)).encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


VIA_COOKIE = "iopix_via"  # how the session began (password, oneclick, cloudflare, proxy): shown in the account dialog only


def set_session(response, user_id: int, via: str, secure: bool = False):
    """secure: the request came over HTTPS (the proxy said so): the cookie is never sent over plain HTTP then."""
    response.set_cookie(COOKIE, make_token(user_id), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/", secure=secure)
    response.set_cookie(VIA_COOKIE, via, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/", secure=secure)


def over_https(request) -> bool:
    """Whether the client reached the viewer over HTTPS: directly, or through a proxy that says so."""
    return request.url.scheme == "https" or (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https"


def clear_session(response, cloudflare_cookie: bool = False):
    """cloudflare_cookie: also drop Cloudflare Access's own session cookie for this site (on the home network, where
    Cloudflare's sign-out page cannot be reached), so the next visit from outside asks Cloudflare's sign-in again."""
    response.delete_cookie(COOKIE, path="/")
    response.delete_cookie(VIA_COOKIE, path="/")
    if cloudflare_cookie:
        response.delete_cookie("CF_Authorization", path="/")


def renew_due(token: str) -> bool:
    """A session in use is renewed (a fresh 30 days) once a day, so a device used at least once a month stays signed in."""
    try:
        return int(token.split(".")[1]) - time.time() < (SESSION_DAYS - 1) * 86400
    except (ValueError, IndexError, AttributeError):
        return False


def user_from_token(token: str):
    try:
        uid, exp, sig = token.split(".")
        if not hmac.compare_digest(hmac.new(_key(), f"{uid}.{exp}|{_session_salt(int(uid))}".encode(), hashlib.sha256).hexdigest(), sig):
            return None
        if int(exp) < time.time():
            return None
        return get(int(uid))
    except (ValueError, AttributeError):
        return None


# ---- permissions
def can_write(user) -> bool:
    """Library writes (sidecars, files, digiKam's database)."""
    return not any_users() or (user is not None and user["role"] in ("editor", "admin"))


def is_admin(user) -> bool:
    return not any_users() or (user is not None and user["role"] == "admin")


def is_signed_in(user) -> bool:
    return not any_users() or user is not None


if __name__ == "__main__":
    import sys
    state.init(); init()
    if len(sys.argv) >= 4 and sys.argv[1] == "create":
        print(create(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "admin"))
    elif sys.argv[1:] == ["list"]:
        for u in list_users():
            print(u)
    else:
        print("usage: python -m app.users create <name> <password> [viewer|editor|admin] | list")
