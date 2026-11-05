"""The shared ioThing accounts (iosuite-shared), as iopix uses them.

Accounts, passwords, sessions and roles live in the Shared ioSuite database (IOSUITE_DB_* in config.env), shared
with ioNotes and ioContacts: one sign-in serves every app on the domain (COOKIE_DOMAIN). What is iopix's
own stays per app: the role an account holds here, its display settings, and the digiKam person it is linked to
(the face badge on the sign-in screen), kept as the "person" value of the account's iopix data.
"""
from iosuite_shared.fastapi import Auth
from iosuite_shared import users as shared
from iosuite_shared.updates import Updater

from app import __version__, config

APP = "iopix"


def _read_only() -> bool:
    from app.data import state
    return state.read_only()


# the version, address, colour and icon register iopix in the suite's list of apps (suite_apps, at the first request),
# where the start page and the other apps find it
auth = Auth(APP, read_only=_read_only, read_only_message="The viewer is in read-only mode; nothing in the library can be changed.", app_label="ioPix",
            version=__version__, public_url=config.PUBLIC_URL or None, description="Photos", color="#3b4a8c", icon="/icons/mark.svg")
auth.deny_exempt = ("/mcp",)  # MCP clients sign in with the admin token, whatever Cloudflare service token got them through


def _get_setting(key: str, default: str = "") -> str:
    from app.data import state
    return state.get_setting(key, default)


def _set_setting(key: str, value: str) -> None:
    from app.data import state
    state.set_setting(key, value)


# updates (the daily check, update.sh, restart), the MCP admin token and the server log, as in every app of the suite
# (iosuite_shared.updates); the daily-check switch and the token are kept with iopix's own settings
updater = Updater(APP, __version__, config.APP_DIR, repo=config.IOPIX_REPO, branch=config.IOPIX_BRANCH, result_file=config.DATA_DIR / ".update-result",
                  settings=(_get_setting, _set_setting), user_agent="iopix")


def decorate(u):
    """The user dict iopix serves: the shared fields plus the linked person tag."""
    if u is None:
        return None
    u = dict(u)
    u["person"] = shared.get_value(u["id"], APP, "person")
    return u


auth.decorate = decorate


def person_tag(tag_id) -> bool:
    """Is this a person tag in the index (a tag under People with digiKam's person property)?"""
    if not tag_id:
        return True
    from app.runtime import db
    con = db()
    try:
        r = con.execute("SELECT person FROM tags WHERE id = ?", (int(tag_id),)).fetchone()
        return bool(r and r[0])
    finally:
        con.close()


def set_person(user_id: int, tag_id):
    """Link the account to a digiKam person tag (None unlinks). The caller checks the tag is a person."""
    shared.set_value(user_id, APP, "person", int(tag_id) if tag_id else None)
    shared.invalidate()


def _on_update(user_id: int, body: dict):
    """Settings › Users may also send {person}: the account's face badge."""
    if "person" in body:
        if not person_tag(body["person"]):
            raise ValueError("That tag is not a person")
        set_person(user_id, body["person"])


auth.on_update = _on_update
