"""Organizing the library: lossless edits, trash, move and rename, delete, folders, upload, import and the share target."""
import logging
import os

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app import config
from app.digikam import dbsync
from app.photos import deletion
from app.photos import edits
from app.photos import folders
from app.data import index
from app.photos import lossless
from app import thumbs
from app.data import state as appstate
from app import users
from app.core import IMG_CACHE
from app.core import DB_ERRORS
from app.core import db
from app.core import _kick
from app.core import _organize_allowed
from app.core import _run_organize
from app.core import _rebuild_after_write
from app.core import CACHES
from app.digikam import dkdb
import secrets
import threading


def _rebuild_soon():
    """The index catches up in the background: the answer goes out as soon as the files and digiKam's rows are
    done (a rebuild takes seconds on a large library), and the page shows the change at once on its own."""
    threading.Thread(target=_rebuild_after_write, name="rebuild-after-write", daemon=True).start()


router = APIRouter()

@router.post("/api/photos/transform")
async def api_photos_transform(request: Request):
    """Body {ids, op: right|left|180|fliph|flipv}: turn or flip photos without loss (JPEG pixels with jpegtran,
    otherwise the orientation flag); face regions turn with them; each is in the change log with Undo."""
    _organize_allowed()
    if not edits.available():
        raise HTTPException(403, "Editing is turned off (EDITING=0 or no exiftool)")
    body = await request.json()
    ids = [int(i) for i in (body.get("ids") or [])][:500]
    op = str(body.get("op") or "")
    if not ids or op not in lossless.OPS:
        raise HTTPException(400, "ids and op (right, left, 180, fliph, flipv) are needed")
    try:
        r = await run_in_threadpool(lossless.transform, ids, op)
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    if r["done"]:
        await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/photos/adjust")
async def api_photo_adjust(request: Request):
    """Body {id, exposure, contrast, saturation, warmth, angle, autolevel}: save an adjusted copy as a new version
    (name_v1.jpg beside it, linked as digiKam links versions); the original is not changed."""
    from app.photos import versions
    _organize_allowed()
    if not edits.available():
        raise HTTPException(403, "Editing is turned off (EDITING=0 or no exiftool)")
    body = await request.json()
    try:
        r = await run_in_threadpool(versions.adjust, int(body.get("id") or 0), body)
    except dbsync.SyncError as e:
        raise HTTPException(409 if "digiKam is running" in str(e) else 400, str(e))
    except (OSError, ValueError) as e:
        raise HTTPException(500, f"The version could not be saved: {e}")
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/photos/crop")
async def api_photos_crop(request: Request):
    """Body {id, rect: [x, y, w, h]} in upright pixels: crop the file (JPEG without loss via jpegtran; PNG, TIFF,
    WebP re-saved), faces shifted or dropped with it, a copy kept for Undo."""
    _organize_allowed()
    if not edits.available():
        raise HTTPException(403, "Editing is turned off (EDITING=0 or no exiftool)")
    body = await request.json()
    rect = body.get("rect")
    if not body.get("id") or not isinstance(rect, list) or len(rect) != 4:
        raise HTTPException(400, "id and rect [x, y, w, h] are needed")
    try:
        r = await run_in_threadpool(lossless.crop, int(body["id"]), rect)
    except dbsync.SyncError as e:
        raise HTTPException(409 if "digiKam is running" in str(e) else 400, str(e))
    except (*DB_ERRORS, ValueError) as e:
        raise HTTPException(500, f"The crop failed: {e}")
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/photos/trash")
async def api_photos_trash(request: Request):
    """Body {ids}: move the photos to the trash of their album root, digiKam's way (.dtrash); restorable."""
    from app.photos import organize
    _organize_allowed()
    body = await request.json()
    ids = [int(i) for i in body.get("ids") or []][:20000]
    if not ids:
        raise HTTPException(400, "Choose photos")
    if body.get("background"):
        return await run_in_threadpool(_trash_in_background, ids)
    r = await run_in_threadpool(_run_organize, organize.trash, ids)
    _rebuild_soon()
    return r


TRASH_JOBS: dict = {}  # job -> {"done": Event, "result": {trashed, infos, failed}}; the last few only


def _trash_in_background(ids: list) -> dict:
    """{background: true}: the photos leave every page at once (out of the index and its caches) and the answer goes
    out; moving the files and writing digiKam's rows happen after, then the index catches up. The result (for Undo,
    and any photo that could not be moved, which comes back) is GET /api/photos/trash/{job}."""
    from app.photos import organize
    _run_organize(dbsync.preflight, dkdb.sqlite_path(), True, "the photos")  # digiKam open, read-only...: refused now, not later
    index.drop_photos(ids)
    for c in CACHES:
        c.cache_clear()
    job = secrets.token_hex(6)
    TRASH_JOBS[job] = {"done": threading.Event(), "result": None}
    for old in list(TRASH_JOBS)[:-50]:
        TRASH_JOBS.pop(old, None)

    def work():
        try:
            r = _run_organize(organize.trash, ids)
        except HTTPException as e:
            r = {"trashed": [], "infos": [], "failed": [{"id": i, "error": e.detail} for i in ids]}
        except Exception as e:  # the cause goes to the log; the photos come back
            logging.getLogger("app.photos.organize").exception("moving photos to the trash failed")
            r = {"trashed": [], "infos": [], "failed": [{"id": i, "error": str(e)} for i in ids]}
        index.HIDDEN.difference_update(ids)
        TRASH_JOBS[job]["result"] = r
        TRASH_JOBS[job]["done"].set()
        _rebuild_after_write()  # the photos that could not be moved come back
    threading.Thread(target=work, name="trash", daemon=True).start()
    return {"job": job, "queued": ids}


@router.get("/api/photos/trash/{job}")
async def api_photos_trash_job(request: Request, job: str, wait: float = 0):
    """How a background move to the trash went: {done, trashed, infos, failed}; wait (seconds, at most 30) holds the
    answer until it is done."""
    if not users.can_write(request.state.user):
        raise HTTPException(403 if request.state.user else 401, "Editors only")
    j = TRASH_JOBS.get(job)
    if not j:
        raise HTTPException(404, "No such job")
    if wait > 0:
        await run_in_threadpool(j["done"].wait, min(wait, 30))
    return {"done": j["done"].is_set(), **(j["result"] or {})}


@router.get("/api/trash")
def api_trash(request: Request):
    """What is in the trash (every album root), newest first."""
    from app.photos import organize
    if not users.can_write(request.state.user):
        raise HTTPException(403 if request.state.user else 401, "Editors only")
    items = organize.listing()
    return {"items": items, "bytes": sum(i["size"] for i in items)}


@router.get("/api/trash/thumb")
def api_trash_thumb(request: Request, info: str):
    """A thumbnail of a trashed file, named by its .dtrashinfo record (which must sit in an album root's trash)."""
    from app.photos import organize
    if not users.can_write(request.state.user):
        raise HTTPException(403 if request.state.user else 401, "Editors only")
    item = next((t for t in organize.listing() if t["info"] == info), None)
    if not item or not item["exists"]:
        raise HTTPException(404)
    import hashlib
    key = hashlib.sha1(f"{item['trash']}:{os.path.getmtime(item['trash'])}".encode()).hexdigest()[:16]
    dst = config.CACHE_DIR / "t" / "trash" / f"{key}.jpg"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if item["name"].rsplit(".", 1)[-1].lower() in folders.VIDEO_EXT:
                if not thumbs._video_frame(item["trash"], dst, 360, None):
                    raise HTTPException(404)
            else:
                im, icc = thumbs._open_oriented(item["trash"], 540, 360)
                im.thumbnail((540, 360))
                thumbs._save(im, dst, 76, icc)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(404)
    return FileResponse(dst, media_type="image/jpeg", headers=IMG_CACHE)


@router.post("/api/trash/restore")
async def api_trash_restore(request: Request):
    """Body {infos: [.dtrashinfo paths]}: put those photos back where they were."""
    from app.photos import organize
    _organize_allowed()
    infos = [str(i) for i in (await request.json()).get("infos") or []][:20000]
    r = await run_in_threadpool(_run_organize, organize.restore, infos)
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/trash/empty")
async def api_trash_empty(request: Request):
    """Body {infos: [...]} or {all: true}: delete those trash entries for good."""
    from app.photos import organize
    _organize_allowed()
    body = await request.json()
    infos = None if body.get("all") is True else [str(i) for i in body.get("infos") or []][:20000]
    if infos is not None and not infos:
        raise HTTPException(400, "Choose what to delete")
    r = await run_in_threadpool(_run_organize, organize.empty, infos)
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/photos/move")
async def api_photos_move(request: Request):
    """Body {ids, album}: move the photos (and their sidecars) into that folder; ids and everything attached stay."""
    from app.photos import organize
    _organize_allowed()
    body = await request.json()
    ids = [int(i) for i in body.get("ids") or []][:20000]
    if not ids or body.get("album") is None:
        raise HTTPException(400, "Choose photos and a folder")
    r = await run_in_threadpool(_run_organize, organize.move_photos, ids, int(body["album"]))
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/photos/rename")
async def api_photos_rename(request: Request):
    """Body {id, name}: a new file name (the extension stays)."""
    from app.photos import organize
    _organize_allowed()
    body = await request.json()
    r = await run_in_threadpool(_run_organize, organize.rename_photo, int(body.get("id") or 0), str(body.get("name") or ""))
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/folders/{album_id}/properties")
async def api_folder_properties(album_id: int, request: Request):
    """Body {caption?, date?, category?, icon?}: digiKam's folder properties (Albums caption, date, collection, icon)."""
    _organize_allowed()
    body = await request.json()
    try:
        r = await run_in_threadpool(folders.set_properties, album_id, {k: body[k] for k in ("caption", "date", "category", "icon") if k in body})
    except dbsync.SyncError as e:
        raise HTTPException(409 if "digiKam is running" in str(e) else 400, str(e))
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/photos/rename-pattern")
async def api_photos_rename_pattern(request: Request):
    """Body {ids, pattern: date|date-name|date-seq}: rename the files by the date they were taken (ids kept, undo per photo)."""
    from app.photos import organize
    _organize_allowed()
    body = await request.json()
    ids = [int(i) for i in (body.get("ids") or [])][:2000]
    if not ids:
        raise HTTPException(400, "No photos")
    r = await run_in_threadpool(_run_organize, organize.rename_by_pattern, ids, str(body.get("pattern") or ""))
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/folders/{album_id}/rename")
async def api_folder_rename(album_id: int, request: Request):
    """Body {name}: rename the folder on disk and in the database (subfolders follow)."""
    from app.photos import organize
    _organize_allowed()
    body = await request.json()
    r = await run_in_threadpool(_run_organize, organize.rename_folder, album_id, str(body.get("name") or ""))
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/folders/{album_id}/move")
async def api_folder_move(album_id: int, request: Request):
    """Body {parent}: move the folder (with everything in it) under another folder of the same album root."""
    from app.photos import organize
    _organize_allowed()
    body = await request.json()
    r = await run_in_threadpool(_run_organize, organize.move_folder, album_id, int(body.get("parent") or 0))
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/photos/delete")
async def api_photos_delete(request: Request):
    """Body: {ids, confirm: true}. Deletes the photos for good: a backup of the files, sidecars and
    database first (data/deleted-photos, db-backups), then the database rows, the files and sidecars,
    and everything the viewer keeps about them. Editors and admins only; refused in read-only mode."""
    body = await request.json()
    if body.get("confirm") is not True:
        raise HTTPException(400, "Deleting needs confirmation")
    if not config.DB_WRITE:
        raise HTTPException(403, "Writing to digiKam's database is turned off (DB_WRITE=0)")
    if index.sync_enabled() and index.offline["since"]:
        raise HTTPException(409, "digiKam's database is unreachable: photos cannot be deleted from it right now")
    ids = [int(i) for i in body.get("ids") or []][:20000]
    if not ids:
        raise HTTPException(400, "Choose photos to delete")
    con = db()
    try:
        photos = []
        for i in range(0, len(ids), 500):
            chunk = ",".join(map(str, ids[i:i + 500]))
            tags = {}
            for pid, full in con.execute(f"SELECT pt.photo_id, t.full FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.photo_id IN ({chunk})"):
                tags.setdefault(pid, []).append(full)
            for r in con.execute(f"SELECT id, path, name, hash, size, taken, rating, title, caption FROM photos WHERE id IN ({chunk})"):
                photos.append({**dict(r), "tags": tags.get(r["id"], [])})
    finally:
        con.close()
    try:
        result = await run_in_threadpool(deletion.delete, photos)
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    except DB_ERRORS as e:
        raise HTTPException(500, f"The deletion failed and was rolled back: {e}")
    await run_in_threadpool(_rebuild_after_write)
    return result



@router.get("/api/folders")
def api_folders():
    """Every digiKam folder as a tree, empty ones included, with the viewer's item counts where it has them."""
    try:
        rows = folders.list_all()
    except Exception as e:
        raise HTTPException(503, f"Could not read digiKam's folder list: {e}")
    con = db()
    try:
        counts = {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, n, n_all FROM albums")}
    finally:
        con.close()
    for r in rows:
        r["n"], r["nAll"] = counts.get(r["id"], (0, 0))
    return {"folders": rows, "canManage": config.DB_WRITE and edits.available() and not appstate.read_only()}


@router.post("/api/folders")
async def api_folder_create(request: Request):
    """Body: {parent: folder id, name}. A new digiKam folder: the directory and its Albums row (digiKam closed)."""
    if not (config.DB_WRITE and edits.available()):
        raise HTTPException(403, "Creating folders is turned off (needs DB_WRITE and EDITING)")
    body = await request.json()
    try:
        result = folders.create(int(body.get("parent") or 0), str(body.get("name", "")))
    except dbsync.SyncError as e:
        raise HTTPException(409 if "digiKam is running" in str(e) else 400, str(e))
    except (*DB_ERRORS, ValueError) as e:
        raise HTTPException(500, f"The change failed and was rolled back: {e}")
    _kick.set()
    return result


@router.get("/api/import/layouts")
def api_import_layouts(request: Request):
    """The folder layouts an import can use, and the one in use."""
    from app.photos import importer
    # patterns: apps may send their own folder pattern ({yyyy}/{yyyy}{mm}, Uploads...) instead of a layout key
    return {"layouts": importer.layouts(), "layout": importer.layout(), "patterns": True}


@router.post("/api/import/stage")
async def api_import_stage(request: Request):
    """Multipart `files` (and an optional `token` to add to an import started earlier): the files are kept
    aside and looked at; nothing goes into the library yet. Returns the plan."""
    from app.photos import importer
    if not (config.DB_WRITE and edits.available()):
        raise HTTPException(403, "Imports are turned off (needs DB_WRITE and EDITING)")
    form = await request.form()
    ups = [f for f in form.getlist("files") if hasattr(f, "filename")]
    if not ups:
        raise HTTPException(400, "No files")
    if any((f.size or 0) > 2 * 1024 ** 3 for f in ups):
        raise HTTPException(413, "Files over 2 GB cannot be uploaded this way")
    files = [(f.filename, f.file) for f in ups[:500]]  # the open upload file (spooled to disk by Starlette), not its bytes
    token = str(form.get("token") or "") or None
    try:
        token = await run_in_threadpool(importer.stage, files, token)
        return await run_in_threadpool(importer.plan, token, _layout_or_pattern(str(form.get("layout") or ""), str(form.get("pattern") or "")))
    except (dbsync.SyncError, ValueError) as e:
        raise HTTPException(400, str(e))


def _layout_or_pattern(layout: str, pattern: str):
    """What an import request asks for: a layout key, or (for apps) a folder pattern of its own; ValueError when the
    pattern is not usable."""
    if pattern:
        from app.photos import importer
        return importer.check_pattern(pattern)
    from app.photos import importer
    return layout if layout in importer.LAYOUTS else None


@router.get("/api/import/{token}")
def api_import_plan(token: str, layout: str = None, pattern: str = None):
    """The plan for a layout key, or for a folder pattern of the app's own (?pattern={yyyy}/{yyyy}{mm})."""
    from app.photos import importer
    try:
        key = _layout_or_pattern(layout or "", pattern or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        return importer.plan(token, key)
    except dbsync.SyncError as e:
        raise HTTPException(404, str(e))


@router.post("/api/import/{token}/run")
async def api_import_run(token: str, request: Request):
    """Body {layout?, pattern?, include?: [names], duplicates?: 'skip'|'import', remember?: bool}: import the staged
    files. `pattern` (apps): a folder pattern of their own instead of a layout, see importer.check_pattern."""
    from app.photos import importer
    if not (config.DB_WRITE and edits.available()):
        raise HTTPException(403, "Imports are turned off (needs DB_WRITE and EDITING)")
    body = await request.json() if (await request.body()) else {}
    try:
        key = _layout_or_pattern(str(body.get("layout") or ""), str(body.get("pattern") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if key in importer.LAYOUTS and body.get("remember") and users.is_admin(request.state.user):
        importer.set_layout(key)
    try:
        r = await run_in_threadpool(importer.run, token, key, body.get("include"), body.get("duplicates") != "import")
    except dbsync.SyncError as e:
        raise HTTPException(409 if "digiKam is running" in str(e) else 400, str(e))
    except (*DB_ERRORS, ValueError) as e:
        raise HTTPException(500, f"The import failed and was rolled back: {e}")
    await run_in_threadpool(_rebuild_after_write)
    return r


@router.post("/api/import/{token}/discard")
def api_import_discard(token: str):
    from app.photos import importer
    importer.discard(token)
    return {"ok": True}


@router.post("/share")
async def share_target(request: Request):
    """The installed app as a share target (Android): files shared to it are staged, then the Import page opens."""
    from app.photos import importer
    if not users.can_write(request.state.user):  # the share sheet cannot show a JSON error: the page explains
        return RedirectResponse("/#/import?error=signin", status_code=303)
    if appstate.read_only() or not (config.DB_WRITE and edits.available()):
        return RedirectResponse("/#/import?error=off", status_code=303)
    form = await request.form()
    ups = [f for f in form.getlist("files") if hasattr(f, "filename")]
    if not ups:
        return RedirectResponse("/#/import", status_code=303)
    if any((f.size or 0) > 2 * 1024 ** 3 for f in ups):
        return RedirectResponse("/#/import?error=toolarge", status_code=303)
    files = [(f.filename, f.file) for f in ups[:200]]
    token = await run_in_threadpool(importer.stage, files)
    return RedirectResponse(f"/#/import?staged={token}", status_code=303)


@router.post("/api/folders/{album_id}/upload")
async def api_folder_upload(album_id: int, request: Request):
    """Multipart: one or more `files`. Each is stored in the folder and registered in digiKam's
    database for digiKam to scan fully on its next run."""
    if not (config.DB_WRITE and edits.available()):
        raise HTTPException(403, "Uploads are turned off (needs DB_WRITE and EDITING)")
    form = await request.form()
    ups = [f for f in form.getlist("files") if hasattr(f, "filename")]
    if not ups:
        raise HTTPException(400, "No files")
    if any((f.size or 0) > 2 * 1024 ** 3 for f in ups):
        raise HTTPException(413, "Files over 2 GB cannot be uploaded this way")
    files = [(f.filename, f.file) for f in ups[:500]]
    try:
        result = await run_in_threadpool(folders.upload, album_id, files)
    except dbsync.SyncError as e:
        raise HTTPException(409 if "digiKam is running" in str(e) else 400, str(e))
    except (*DB_ERRORS, ValueError) as e:
        raise HTTPException(500, f"The upload failed and was rolled back: {e}")
    _kick.set()
    return result
