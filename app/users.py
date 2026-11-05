"""Users, sessions and permissions: the shared ioThing accounts (iosuite-shared), seen from iopix.

Three roles here: viewer (signed in; own settings kept on the server; favorites and viewer albums), editor (viewer
plus every library write: edits, tags, faces, place tags, GPX, sync, database apply) and admin (editor plus settings,
maintenance and user management). An account holds a role per app; a suite admin is admin everywhere. Anyone not
signed in browses read-only with display settings in the browser. A viewer may have no password at all: such an
account signs in with one click from the account picker (on the home network only) and can never be more than a
viewer. Until the first account exists the viewer stays open, so a fresh install is not locked out.

Passwords are scrypt-hashed and sessions are random tokens in an HttpOnly cookie (30 days, renewed in use), both
in the Shared ioSuite database, so a session begun in another ioThing app is a session here too. This module keeps iopix's
old names for the rest of the code; the work is done by iosuite_shared.
"""
from iosuite_shared import db as authdb, sessions, users as shared
from iosuite_shared.passwords import check_password, hash_password

from app.auth import APP, decorate, person_tag, set_person

__all__ = ["check_password", "hash_password", "person_tag", "set_person", "decorate", "APP"]  # re-exported for the rest of the app

ROLES = shared.APP_ROLES
COOKIE = sessions.COOKIE
VIA_COOKIE = sessions.VIA_COOKIE
SESSION_DAYS = sessions.SESSION_DAYS
CACHE_S = shared.CACHE_S
_attempts = shared._attempts
LOCK_AFTER, LOCK_AFTER_NAME, LOCK_BASE, LOCK_MAX = shared.LOCK_AFTER, shared.LOCK_AFTER_NAME, shared.LOCK_BASE, shared.LOCK_MAX
login_wait, login_failed, login_succeeded = shared.login_wait, shared.login_failed, shared.login_succeeded
any_users, delete, is_signed_in, can_write, is_admin = shared.any_users, shared.delete, shared.is_signed_in, shared.can_write, shared.is_admin
end_all_sessions = sessions.end_all


def init():
    """The Shared ioSuite database's tables, and the admin named by ADMIN_USER / ADMIN_PASSWORD on a first start with no accounts yet."""
    shared.init()


def invalidate():
    shared.invalidate()


def list_users() -> list:
    return [decorate(u) for u in shared.list_users(APP)]


def get(user_id: int):
    return decorate(shared.get(user_id, APP))


def create(name: str, password: str, role: str, email: str = "") -> dict:
    """A new account with this role in iopix; ValueError with the message to show."""
    return decorate(shared.create(name, password, role, APP, email=email))


def update(user_id: int, role: str = None, password: str = None, name: str = None, email: str = None, listed: bool = None):
    shared.update(user_id, role=role, app=APP, password=password, name=name, email=email, listed=listed)


def authenticate(name: str, password: str):
    return decorate(shared.authenticate(name, password, APP))


def accounts() -> list:
    """What the sign-in picker shows: listed accounts with access here, whether a password is asked for, and the linked person."""
    return [decorate(a) for a in shared.accounts(APP)]


def linked_persons() -> set:
    """The person tags listed accounts are linked to: their face badges are the account picker's avatars."""
    rows = authdb.q("SELECT d.value FROM ios_user_data d JOIN ios_users u ON u.id = d.user_id WHERE d.app = %s AND d.name = 'person' AND u.listed = 1", (APP,))
    return {int(v) for v in (authdb.json_load(r["value"]) for r in rows) if v}


def require_login() -> bool:
    """Settings › Users › Require sign-in: nothing but the sign-in screen for visitors."""
    return shared.require_login(APP)


def set_require_login(on: bool):
    authdb.set_setting("require_login", "1" if on else "0", APP)


def get_settings(user_id: int) -> dict:
    return shared.get_settings(user_id, APP)


def set_settings(user_id: int, changes: dict) -> dict:
    return shared.set_settings(user_id, APP, changes)


def user_from_token(token: str):
    return decorate(sessions.user_from_token(token, APP))


if __name__ == "__main__":
    import sys
    init()
    if len(sys.argv) >= 4 and sys.argv[1] == "create":
        print(create(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "admin"))
    elif sys.argv[1:] == ["list"]:
        for u in list_users():
            print(u)
    else:
        print("usage: python -m app.users create <name> <password> [viewer|editor|admin] | list")
