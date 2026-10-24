"""Favorites, app albums and saved searches (app-side state; digiKam is not touched)."""

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from app.data import state as appstate
from app import users
from app.core import db
from app.core import jresp


router = APIRouter()

# --------------------------------------------------------------------------
# Favorites and app albums (app-side state; digiKam is not touched)
# --------------------------------------------------------------------------

def _with_hashes(ids) -> list:
    ids = [int(i) for i in ids][:50000]
    if not ids:
        return []
    con = db()
    try:
        return [(r[0], r[1]) for r in con.execute(f"SELECT id, hash FROM photos WHERE id IN ({','.join(map(str, ids))})")]
    finally:
        con.close()


def _album_rights(user) -> dict:
    """What this user may do with albums: shared ones are created and changed by editors and admins; private
    ones by their owner, any signed-in user (with no accounts yet, every album is shared)."""
    return {"canShareAlbums": bool(users.can_write(user)), "canPrivateAlbums": bool(user and user.get("id") is not None)}


def _can_change_album(a: dict, user) -> bool:
    if a["is_shared"]:
        return bool(users.can_write(user))
    return bool(user) and a["owner_id"] == user.get("id")


@router.get("/api/state")
def api_state(request: Request):
    user = request.state.user
    s = appstate.summary(user)
    for a in s["albums"]:
        a["shared"] = bool(a.pop("is_shared"))
        a["mine"] = bool(user) and a["owner_id"] == user.get("id")
        a["canEdit"] = _can_change_album({"is_shared": a["shared"], "owner_id": a.pop("owner_id")}, user)
    return jresp(request, {"rev": appstate.rev, **s, **_album_rights(user)})


@router.post("/api/favorites")
async def api_favorites(request: Request):
    body = await request.json()
    await run_in_threadpool(lambda: appstate.set_favorites(_with_hashes(body.get("ids", [])), bool(body.get("on", True))))  # the viewer database may be MySQL
    return {"ok": True}


@router.post("/api/ualbums")
async def api_ualbum_create(request: Request):
    """Body: {name, shared: bool (default: shared when allowed), ids?}. Shared albums: editors and admins;
    private albums: any signed-in user, seen only by them."""
    body = await request.json()
    user = request.state.user
    name = str(body.get("name", "")).strip()[:200]
    if not name:
        raise HTTPException(400, "Album name is required")
    rights = _album_rights(user)
    shared = bool(body["shared"]) if "shared" in body else rights["canShareAlbums"]
    if shared and not rights["canShareAlbums"]:
        raise HTTPException(403, "Only editors and admins can create shared albums; make it private instead")
    if not shared and not rights["canPrivateAlbums"]:
        raise HTTPException(403, "Sign in to keep private albums")
    album_id = appstate.create_album(name, user.get("id") if user else None, shared)
    if body.get("ids"):
        appstate.set_album_items(album_id, _with_hashes(body["ids"]), True)
    return {"id": album_id}


@router.post("/api/ualbums/{album_id}")
async def api_ualbum_update(album_id: int, request: Request):
    """Body: {name} to rename, {ids, on} to add or remove photos, {shared: bool} to share it or make it
    private, {delete: true} to remove the album. A shared album is changed by editors and admins, a private
    one by its owner; sharing needs both (the owner, and editor or admin)."""
    body = await request.json()
    user = request.state.user
    a = appstate.album(album_id)
    if not a or not (a["is_shared"] or (user and a["owner_id"] == user.get("id"))):
        raise HTTPException(404, "No such album")
    if not _can_change_album(a, user):
        raise HTTPException(403, "Only editors and admins can change a shared album")
    if "shared" in body and bool(body["shared"]) != a["is_shared"]:
        if bool(body["shared"]) and not users.can_write(user):
            raise HTTPException(403, "Only editors and admins can share an album")
        if not bool(body["shared"]) and not (user and user.get("id") is not None):
            raise HTTPException(403, "Sign in to keep private albums")
        appstate.set_album_sharing(album_id, bool(body["shared"]), None if body["shared"] else user["id"])
    if body.get("delete"):
        appstate.delete_album(album_id)
        return {"ok": True}
    if body.get("name"):
        appstate.rename_album(album_id, str(body["name"]).strip()[:200])
    if "ids" in body:
        appstate.set_album_items(album_id, _with_hashes(body["ids"]), bool(body.get("on", True)))
    return {"ok": True}


@router.post("/api/searches")
async def api_search_create(request: Request):
    """A smart album: {name, query} where query is the photo filter query string (as in #/photos?...)."""
    if not users.can_write(request.state.user):  # smart albums are shared by everyone: editors and admins keep them, as with shared albums
        raise HTTPException(403, "Only editors and admins can create smart albums")
    body = await request.json()
    name = str(body.get("name", "")).strip()[:200]
    query = str(body.get("query", "")).strip().lstrip("?")[:4000]
    if not name or not query:
        raise HTTPException(400, "A name and a search are required")
    return {"id": appstate.create_search(name, query)}


@router.post("/api/searches/{search_id}")
async def api_search_update(search_id: int, request: Request):
    """Body: {name} to rename, {delete: true} to remove."""
    if not users.can_write(request.state.user):
        raise HTTPException(403, "Only editors and admins can change smart albums")
    body = await request.json()
    if body.get("delete"):
        appstate.delete_search(search_id)
    elif body.get("name"):
        appstate.rename_search(search_id, str(body["name"]).strip()[:200])
    return {"ok": True}
