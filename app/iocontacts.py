"""ioContacts: birthdays of the people named here, from the suite's address book (found in the suite by itself; nothing to set up).

The mirror of ioContacts' own ioPix link, with nothing to set up: ioContacts is found in the suite's list of apps, and
the server (never the browser) reads from it as iopix's own service account with a read-only app password, both made
by itself the first time (iosuite_shared.links).

Which contact is which person is ioContacts' own link (a contact's "ioPix person", made there); iopix keeps no
mapping of its own. Only linked contacts with a birthday count.
The list is kept in memory and fetched again in the background every ten minutes; pages only ever read what is kept,
so ioContacts being slow or down never holds up a page: its birthdays are then those of the last good fetch (none
after a restart), and Settings shows the error.
"""
import datetime
import json
import logging
import threading
import time
import urllib.error

from iosuite_shared import links

from app.data import index, state as appstate

log = logging.getLogger(__name__)
APP, PROVIDER = "iopix", "iocontacts"
TTL = 600              # seconds a fetch is used before the next one is started
# what Settings › ioContacts used to keep before the suite found ioContacts by itself (removed at start)
OLD_SETTINGS = ("iocontacts_url", "iocontacts_user", "iocontacts_password", "iocontacts_cf_id", "iocontacts_cf_secret")
_lock = threading.Lock()
_state = {"at": 0.0, "people": None, "error": None, "busy": False, "contacts": 0}


def forget_old_settings() -> None:
    try:
        appstate.write(lambda c: c.execute("DELETE FROM pix_settings WHERE name IN (?, ?, ?, ?, ?)", OLD_SETTINGS))
    except Exception as e:  # noqa: BLE001 - tried again next start
        log.debug("old ioContacts settings: %s", e)


def settings() -> dict:
    try:
        return {"url": links.url(PROVIDER) or ""}
    except Exception:  # noqa: BLE001 - the shared database is away
        return {"url": ""}


def enabled() -> bool:
    return bool(settings()["url"])


def public_settings() -> dict:
    s = settings()
    return {"url": s["url"], "enabled": bool(s["url"]), "matched": len(_state["people"] or {}), "contacts": _state["contacts"],
            "error": _state["error"], "fetched": int(_state["at"]) or None}


def _get(path: str, timeout: float = 15):
    if not settings()["url"]:
        raise RuntimeError("ioContacts is not part of the suite (it has not started on this Shared ioSuite database)")
    try:
        with links.get(APP, PROVIDER, path, timeout=timeout, user_agent="iopix") as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(f"ioContacts refused iopix's app password ({e.code})") from None
        raise RuntimeError(f"ioContacts answered {e.code}") from None
    except ValueError:
        raise RuntimeError("ioContacts answered something that is not ioContacts") from None


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
