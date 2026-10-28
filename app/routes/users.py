"""User accounts (admins): list, create, change, delete."""

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request

from app import access, users

from app.routes.auth import person_tag

router = APIRouter()


@router.get("/api/users", dependencies=[access.admin])
def api_users(request: Request):
    return {"users": users.list_users(), "roles": list(users.ROLES)}


@router.post("/api/users", dependencies=[access.admin])
async def api_users_create(request: Request):
    body = await request.json()
    try:
        return users.create(str(body.get("name", "")), str(body.get("password", "")), str(body.get("role", "viewer")), str(body.get("email") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/users/{user_id}", dependencies=[access.admin])
async def api_users_update(user_id: int, request: Request):
    """{role} and/or {password} and/or {name} and/or {email} (optional; "" clears it) and/or {listed} (on the sign-in
    screen's account list); {delete: true} removes the user."""
    body = await request.json()
    if not users.get(user_id):
        raise HTTPException(404, "No such user")
    try:
        if body.get("delete"):
            if request.state.user and request.state.user["id"] == user_id:
                raise ValueError("You cannot delete your own account while signed in")
            users.delete(user_id)
        else:
            users.update(user_id, role=body.get("role"), password=body.get("password"), name=body.get("name"), email=body.get("email"),
                         listed=None if body.get("listed") is None else bool(body.get("listed")))
            if "person" in body:
                if not person_tag(body["person"]):
                    raise ValueError("That tag is not a person")
                users.set_person(user_id, body["person"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
