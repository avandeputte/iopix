"""Who may call a route, declared on the route itself: `dependencies=[access.admin]` (or editor, editor_write,
signed_in, public). The server's middleware only works out who is asking and whether sign-in is required at all;
every API route carries one of these markers, and a test fails when one is missing, so a new route can never be
open to everyone by mistake. The markers come from iosuite-shared (app/auth.py), with iopix's read-only switch.

- public: anyone the sign-in rule lets in (reads for everyone, and routes that decide finer inside)
- signed_in: an account (favorites, albums, saved searches)
- editor: an editor or admin (reads that are not for everyone: the change log, metadata, imports)
- editor_write: a change to the library (refused for everyone in read-only mode)
- admin: settings, maintenance, users, logs, backups
- admin_write: a library change only admins make (merging a library, repairing files)
"""
from app.auth import auth

ROLES = ("public", "signed_in", "editor", "admin")

public = auth.access.public
signed_in = auth.access.signed_in
editor = auth.access.editor
editor_write = auth.access.editor_write
admin = auth.access.admin
admin_write = auth.access.admin_write
require = auth.access.require
declared = auth.access.declared
