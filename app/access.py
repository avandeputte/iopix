"""Who may call a route, declared on the route itself: `dependencies=[access.admin]` (or editor, editor_write,
signed_in, public). The server's middleware only works out who is asking and whether sign-in is required at all;
every API route carries one of these markers, and a test fails when one is missing, so a new route can never be
open to everyone by mistake.

- public: anyone the sign-in rule lets in (reads for everyone, and routes that decide finer inside)
- signed_in: an account (favorites, albums, saved searches)
- editor: an editor or admin (reads that are not for everyone: the change log, metadata, imports)
- editor_write: a change to the library (refused for everyone in read-only mode)
- admin: settings, maintenance, users, logs, backups
- admin_write: a library change only admins make (merging a library, repairing files)
"""
from fastapi import Depends, HTTPException, Request

from app import users
from app.data import state as appstate

ROLES = ("public", "signed_in", "editor", "admin")


def require(role: str, write: bool = False):
    """The dependency for a route. `write`: the route changes the photo library (not the viewer's settings), so
    read-only mode refuses it."""
    if role not in ROLES:
        raise ValueError(role)

    async def check(request: Request):
        user = request.state.user
        if write and appstate.read_only():
            raise HTTPException(403, "The viewer is in read-only mode; nothing in the library can be changed.")
        if role == "public":
            return
        reading = request.method in ("GET", "HEAD", "OPTIONS")
        if role == "admin" and not users.is_admin(user):
            raise HTTPException(403 if user else 401, "Admins only" if reading else "Only an admin can change settings or run maintenance.")
        if role == "editor" and not users.can_write(user):
            raise HTTPException(403 if user else 401, "Editors only" if reading else "Sign in as an editor to change the library.")
        if role == "signed_in" and not users.is_signed_in(user):
            raise HTTPException(401, "Sign in to use this" if reading else "Sign in to keep favorites and albums.")

    check.access_role, check.access_write = role, write
    return Depends(check)


public = require("public")
signed_in = require("signed_in")
editor = require("editor")
editor_write = require("editor", write=True)
admin = require("admin")
admin_write = require("admin", write=True)


def declared(route) -> tuple:
    """(role, write) a route declares, or None: for the test that checks every API route has one."""
    found = [(d.dependency.access_role, d.dependency.access_write) for d in route.dependencies if hasattr(d.dependency, "access_role")]
    return found[0] if len(found) == 1 else None
