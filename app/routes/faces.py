"""Faces: detection, naming and confirming, suggestions, grouping, near-duplicate regions."""

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app import config
from app.digikam import dbsync
from app.photos import edits
from app.faces import facedetect
from app.faces import facemgr
from app.faces import facevecs
from app.ai import neardups
from app import thumbs
from app.core import IMG_CACHE
from app.core import DB_ERRORS
from app.core import db
from app.core import _kick
from app.core import jresp
from app.core import _photo_row
from app.core import _face_detect_allowed
from app.core import _rebuild_after_write


router = APIRouter()

@router.post("/api/faces/detect")
async def api_faces_detect(request: Request):
    """Body: {ids: [...]} | {album: id} | {all: true}, optional redo. Up to five photos are looked at now and
    the answer says what was found; a folder, the library or a larger selection runs in the background."""
    body = await request.json()
    _face_detect_allowed()
    ids = [int(i) for i in body.get("ids") or []][:50000]
    if ids and len(ids) <= 5:
        photos = await run_in_threadpool(facedetect._photos, ids)
        try:
            r = await run_in_threadpool(facedetect.detect, photos)
        except dbsync.SyncError as e:
            raise HTTPException(409, str(e))
        if r["added"]:
            await run_in_threadpool(_rebuild_after_write)
            facevecs.start()  # embed the new faces for grouping and "looks like" suggestions
        return {"done": True, **r}
    album = body.get("album")
    if not ids and album is None and not body.get("all"):
        raise HTTPException(400, "Choose photos, a folder or the whole library")

    def after():
        _rebuild_after_write()
        facevecs.start()
    n = facedetect.start(ids or None, int(album) if album is not None else None, bool(body.get("redo")), after=after)
    return {"done": False, "queued": n}



# --------------------------------------------------------------------------
# Faces (digiKam's database; digiKam must be closed)
# --------------------------------------------------------------------------

@router.post("/api/faces/op")
async def api_face_op(request: Request):
    """One face change (see facemgr.apply). Writes digiKam's database; digiKam must be closed."""
    if not (config.DB_WRITE and edits.available()):
        raise HTTPException(403, "Face management needs editing and database writes enabled")
    op = await request.json()
    try:
        res = await run_in_threadpool(facemgr.apply, op, None, True, True)  # sidecar written after the answer
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    except (*DB_ERRORS, ValueError, KeyError, TypeError) as e:
        raise HTTPException(500, f"The change failed and was rolled back: {e}")
    _kick.set()
    return res


@router.get("/api/faces/nearby")
def api_faces_nearby(photo: int, limit: int = 12):
    """The people in the photos around this one in the same folder (named faces or person tags), the nearest in
    time and the most frequent first: [{tag, n}]. For the "Who is this?" list."""
    con = db()
    try:
        row = con.execute("SELECT album_id, ts FROM photos WHERE id = ?", (photo,)).fetchone()
        if not row:
            return {"people": []}
        rows = con.execute(
            "SELECT t.id, count(*) AS n, min(abs(p.ts - ?)) AS gap FROM photos p "
            "JOIN (SELECT photo_id, tag_id FROM faces WHERE kind = 'confirmed' UNION SELECT pt.photo_id, pt.tag_id FROM photo_tags pt "
            "      JOIN tags tt ON tt.id = pt.tag_id AND tt.person = 1) x ON x.photo_id = p.id "
            "JOIN tags t ON t.id = x.tag_id AND t.person = 1 AND t.full != 'People' "
            "WHERE p.album_id = ? AND p.id != ? GROUP BY t.id ORDER BY gap / 3600 ASC, n DESC LIMIT ?",
            (row["ts"] or 0, row["album_id"], photo, max(1, min(limit, 40)))).fetchall()
    finally:
        con.close()
    return {"people": [{"tag": r[0], "n": r[1]} for r in rows]}


@router.post("/api/faces/batch")
async def api_face_batch(request: Request):
    """Many face changes in one transaction with one backup: {ops: [...]} (see facemgr.apply_many)."""
    if not (config.DB_WRITE and edits.available()):
        raise HTTPException(403, "Face management needs editing and database writes enabled")
    body = await request.json()
    ops = [o for o in (body.get("ops") or []) if isinstance(o, dict)][:2000]
    if not ops:
        raise HTTPException(400, "No operations")
    try:
        res = await run_in_threadpool(facemgr.apply_many, ops, None, True, True)
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    except (*DB_ERRORS, ValueError, KeyError, TypeError) as e:
        raise HTTPException(500, f"The change failed and was rolled back: {e}")
    _kick.set()
    return {"results": res, "done": sum(1 for r in res if "error" not in r), "failed": [r for r in res if "error" in r]}


@router.get("/api/faces/queue")
def api_faces_queue(kind: str = "suggested", tag: int = None, offset: int = 0, limit: int = 200):
    """Faces waiting for a decision: suggestions grouped by the suggested person, or unnamed faces."""
    if kind not in ("suggested", "unknown", "ignored"):
        raise HTTPException(400, "kind must be suggested, unknown or ignored")
    con = db()
    try:
        where, params = "f.kind = ?", [kind]
        if tag:
            where += " AND f.tag_id = ?"
            params.append(tag)
        total = con.execute(f"SELECT count(*) FROM faces f WHERE {where}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT f.photo_id, f.tag_id, f.x, f.y, f.w, f.h, p.ts, p.name AS photo, p.w AS pw, p.h AS ph, "
            f"(SELECT name FROM tags t WHERE t.id = f.tag_id) AS name FROM faces f JOIN photos p ON p.id = f.photo_id "
            f"WHERE {where} ORDER BY f.tag_id, p.ts DESC, f.x LIMIT ? OFFSET ?", params + [max(1, min(limit, 500)), max(0, offset)]).fetchall()
        groups = [[r["tag_id"], r["name"], r["c"]] for r in con.execute(
            "SELECT f.tag_id, (SELECT name FROM tags t WHERE t.id = f.tag_id) AS name, count(*) AS c FROM faces f "
            "WHERE f.kind = ? GROUP BY f.tag_id ORDER BY c DESC", (kind,))] if kind == "suggested" else []
        return {"total": total, "offset": offset, "groups": groups,
                "faces": [{"photo": r["photo_id"], "tag": r["tag_id"], "name": r["name"], "x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"],
                           "ts": r["ts"], "file": r["photo"], "pw": r["pw"], "ph": r["ph"]} for r in rows]}
    finally:
        con.close()


@router.get("/facecrop/{photo_id}/{x}/{y}/{w}/{h}.jpg")
def facecrop(photo_id: int, x: int, y: int, w: int, h: int):
    """A square crop around any face rectangle (upright pixels), for the face queues."""
    row = _photo_row(photo_id)
    try:
        path = thumbs.make_face(0, row, (x, y, w, h))
    except Exception:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers=IMG_CACHE)


@router.post("/api/faces/person")
async def api_face_person(request: Request):
    """Create a new person tag: {name, parent} (parent = a People family tag id, or 0)."""
    if not (config.DB_WRITE and edits.available()):
        raise HTTPException(403, "Needs editing and database writes enabled")
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "A name is needed")
    try:
        tid = await run_in_threadpool(facemgr.create_person, name, int(body.get("parent") or 0))
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    _kick.set()
    return {"id": tid}


@router.get("/api/faces/groups")
def api_faces_groups(kind: str = "unknown", tag: int = None, threshold: float = None):
    """Undecided faces with look-alikes grouped (SFace embeddings); never a name proposal."""
    if kind not in ("unknown", "suggested"):
        raise HTTPException(400, "kind must be unknown or suggested")
    t = max(0.2, min(0.9, threshold)) if threshold else facevecs.group_threshold()
    try:
        out = facevecs.groups(kind, t, tag)
    except Exception as e:
        raise HTTPException(500, f"Could not group faces: {str(e)[:200]}")
    out["available"] = facevecs.available()
    out["progress"] = dict(facevecs.progress)
    return out


@router.get("/api/neardups")
def api_neardups(request: Request):
    """Near-duplicate groups from the content-search embeddings, with whether they are current."""
    st = neardups.status()
    st["current"] = bool(st["gen"]) and st["gen"] == neardups.expected_gen(st["level"])
    st["levels"] = neardups.LEVELS
    st["groups"] = neardups.groups() if st["gen"] else []
    return jresp(request, st)


@router.post("/api/neardups")
async def api_neardups_run(request: Request):
    """Body: {level: strict|normal|loose}. Computes the pairs in the background (admin)."""
    body = await request.json()
    try:
        neardups.start(str(body.get("level") or "normal"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@router.get("/api/faces/summary")
def api_faces_summary():
    con = db()
    try:
        return {k: list(con.execute("SELECT count(DISTINCT photo_id), count(*) FROM faces WHERE kind = ?", (k,)).fetchone())
                for k in ("unknown", "suggested", "ignored")}  # [photos, faces]
    finally:
        con.close()
