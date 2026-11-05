"""User accounts (admins): list, create, change, delete; iosuite-shared's routes. The role shown and set is the
account's role in iopix; {person} in a change links the account's face badge (app/auth.py)."""

from app.auth import auth

router = auth.users_router()
