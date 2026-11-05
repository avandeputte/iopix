"""Sign-in: login, logout, the signed-in account's own settings and password, first-run setup (iosuite-shared's
routes, shared with the other ioThing apps), plus iopix's own: linking an account to a person."""

from fastapi import HTTPException
from fastapi import Request

from app import access, users
from app.auth import auth, person_tag
from app.http import json_body

router = auth.router()  # the shared routes, plus iopix's own below


@router.post("/api/auth/person", dependencies=[access.admin])
async def api_my_person(request: Request):
    """Link the signed-in admin's own account to a person from People: {person: tag id or null}. Admins only:
    a linked person's face is shown on the sign-in screen, so who is who is the admin's call (other accounts
    are linked from Settings › Users)."""
    u = request.state.user
    if not u:
        raise HTTPException(401, "Sign in first")
    if not users.is_admin(u):
        raise HTTPException(403, "Only an admin can link an account to a person")
    body = await json_body(request)
    if not person_tag(body.get("person")):
        raise HTTPException(400, "That tag is not a person")
    users.set_person(u["id"], body.get("person"))
    return users.get(u["id"])
