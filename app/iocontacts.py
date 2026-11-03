"""ioContacts: birthdays of the people named here, from the suite's address book (optional, Settings › ioContacts).

The mirror of ioContacts' own ioPix link. The server talks to ioContacts, never the browser: an ioContacts account
with an app password (HTTP Basic, which ioContacts takes for any account of the shared ioThing sign-in) and,
behind Cloudflare Access, a service token. Settings › ioContacts stores them; IOCONTACTS_* in the environment wins.

Which contact is which person is ioContacts' own link (a contact's "ioPix person", made there); iopix keeps no
mapping of its own. Only linked contacts with a birthday count.
The list is kept in memory and fetched again in the background every ten minutes; pages only ever read what is kept,
so ioContacts being slow or down never holds up a page: its birthdays are then those of the last good fetch (none
after a restart), and Settings shows the error.
"""
import base64
import datetime
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from app.data import index, state as appstate

log = logging.getLogger(__name__)
TTL = 600              # seconds a fetch is used before the next one is started
ENV = {k: os.environ.get(f"IOCONTACTS_{k.upper()}", "").strip() for k in ("url", "user", "password", "cf_client_id", "cf_client_secret")}
KEYS = {"url": "iocontacts_url", "user": "iocontacts_user", "password": "iocontacts_password",
        "cf_client_id": "iocontacts_cf_id", "cf_client_secret": "iocontacts_cf_secret"}
_lock = threading.Lock()
_state = {"at": 0.0, "people": None, "error": None, "busy": False, "contacts": 0}


def settings() -> dict:
    s = {k: ENV[k] or appstate.get_setting(KEYS[k]) or "" for k in KEYS}
    s["url"] = s["url"].rstrip("/")
    return s


def enabled() -> bool:
    return bool(settings()["url"])


def public_settings() -> dict:
    s = settings()
    return {"url": s["url"], "user": s["user"], "hasPassword": bool(s["password"]), "cfId": s["cf_client_id"],
            "hasCfSecret": bool(s["cf_client_secret"]), "enabled": bool(s["url"]), "fromEnv": sorted(k for k in KEYS if ENV[k]),
            "matched": len(_state["people"] or {}), "contacts": _state["contacts"], "error": _state["error"],
            "fetched": int(_state["at"]) or None}


def save_settings(body: dict) -> None:
    """Any of url, user, password, cf_client_id, cf_client_secret; an empty string clears one, a missing key keeps it."""
    for k, key in KEYS.items():
        if k in body and body[k] is not None and not ENV[k]:
            appstate.set_setting(key, str(body[k]).strip())
    with _lock:
        _state.update(at=0.0, people=None, error=None, contacts=0)


def _get(path: str, timeout: float = 15):
    s = settings()
    if not s["url"]:
        raise RuntimeError("ioContacts is not set up (Settings › ioContacts)")
    headers = {"Accept": "application/json", "User-Agent": "iopix"}
    if s["user"] and s["password"]:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{s['user']}:{s['password']}".encode()).decode()
    if s["cf_client_id"] and s["cf_client_secret"]:
        headers.update({"CF-Access-Client-Id": s["cf_client_id"], "CF-Access-Client-Secret": s["cf_client_secret"]})
    try:
        with urllib.request.urlopen(urllib.request.Request(s["url"] + path, headers=headers), timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("ioContacts refused the account: check the user and the app password") from None
        raise RuntimeError(f"ioContacts answered {e.code}") from None
    except ValueError:
        raise RuntimeError("ioContacts answered something that is not ioContacts (check the address)") from None


def match(contacts: list, persons: dict, base: str = "") -> dict:
    """{tag id: {contactId, name, year, month, day, url}} for the contacts with a birthday that ioContacts links to a
    person here (its "ioPix person"); a link to a person no longer here is left out. `persons` is {tag id: name}."""
    out = {}
    for c in contacts:
        b, tag = c.get("birthday") or {}, c.get("iopixPersonId")
        if tag not in persons or not (b.get("month") and b.get("day")) or tag in out:
            continue
        out[tag] = {"contactId": c["id"], "name": c.get("fn"), "year": b.get("year"), "month": int(b["month"]), "day": int(b["day"]),
                    "url": f"{base}/#/c/{c['id']}" if base else None}
    return out


def _persons() -> dict:
    con = index.open_index()
    try:
        return {int(r[0]): r[1] for r in con.execute("SELECT id, name FROM tags WHERE person = 1")}
    finally:
        con.close()


def refresh() -> dict:
    """Fetch the contacts now and match them; returns {ok, contacts, matched} or {ok: False, error}."""
    try:
        contacts = _get("/api/contacts?iopix_linked=true&limit=20000", timeout=30).get("items", [])
        people = match(contacts, _persons(), settings()["url"])
    except Exception as e:  # noqa: BLE001 - kept for Settings; the last good list stays in use
        msg = str(e) if isinstance(e, RuntimeError) else f"ioContacts did not answer: {type(e).__name__}: {e}"
        with _lock:
            _state.update(error=msg, at=time.time(), busy=False)
        log.info("ioContacts: %s", msg)
        return {"ok": False, "error": msg}
    with _lock:
        _state.update(people=people, contacts=len(contacts), error=None, at=time.time(), busy=False)
    return {"ok": True, "contacts": len(contacts), "matched": len(people)}


def people() -> dict:
    """The matched birthdays as last fetched ({} when ioContacts is off or not reached yet); a stale list starts a
    fetch in the background and is returned as it is."""
    if not enabled():
        return {}
    with _lock:
        stale = not _state["busy"] and time.time() - _state["at"] > TTL
        if stale:
            _state["busy"] = True
    if stale:
        threading.Thread(target=refresh, name="iocontacts", daemon=True).start()
    return _state["people"] or {}


def birth_years() -> dict:
    """{tag id: year} for the matched people whose birthday has a year."""
    return {t: p["year"] for t, p in people().items() if p.get("year")}


def age_text(p: dict, taken: str, dprec) -> str | None:
    """How old someone was in a photo taken on `taken` (YYYY-MM-DD...), as text: "34", "33 or 34" when the photo's
    date is only known to the month (in their birthday month) or year, "under 1" for a baby; None when it cannot be
    said (no birth year, no date, a date before they were born)."""
    if not p.get("year") or not taken or dprec == 3:
        return None
    try:
        y, m, d = int(taken[:4]), int(taken[5:7]), int(taken[8:10])
    except ValueError:
        return None
    if dprec == 2:
        lo, hi = y - p["year"] - 1, y - p["year"]
    elif dprec == 1 and m == p["month"]:
        lo, hi = y - p["year"] - 1, y - p["year"]
    else:
        lo = hi = y - p["year"] - ((m, d) < (p["month"], p["day"]))
    if hi < 0:
        return None
    lo = max(lo, 0)
    one = lambda a: "under 1" if a == 0 else str(a)
    return one(hi) if lo == hi else f"{one(lo)} or {hi}"


def upcoming(today: datetime.date = None, days: int = 31) -> list:
    """The matched birthdays in the next `days` days, today first: [{tag, name, month, day, year, turns, in, url}]."""
    today = today or datetime.date.today()
    out = []
    for tag, p in people().items():
        for y in (today.year, today.year + 1):
            try:
                when = datetime.date(y, p["month"], p["day"])
            except ValueError:  # 29 February outside a leap year: the 28th
                when = datetime.date(y, p["month"], p["day"] - 1)
            if when >= today:
                break
        n = (when - today).days
        if n < days:
            out.append({"tag": tag, "month": p["month"], "day": p["day"], "year": p.get("year"),
                        "turns": when.year - p["year"] if p.get("year") else None, "in": n, "date": when.isoformat(), "url": p.get("url")})
    out.sort(key=lambda x: (x["in"], x["tag"]))
    return out
