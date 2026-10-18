"""Images: thumbnails, previews, face avatars, originals and zip downloads."""
import mimetypes
import os
import re
import time
import sqlite3
import zipfile

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse

from app import thumbs
from app.core import THUMB_COLS
from app.core import IMG_CACHE
from app.core import db
from app.core import resolve
from app.core import _form_items
from app.core import _photo_row
from app.core import _cached_image


router = APIRouter()

# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


@router.get("/thumb/{photo_id}")
def thumb(photo_id: int):
    return _cached_image("t", photo_id)


@router.get("/preview/{photo_id}")
def preview(photo_id: int, dl: int = 0):
    """The screen-size JPEG; dl=1 downloads it under the photo's name (Share > Download preview)."""
    return _cached_image("p", photo_id, dl=bool(dl))


@router.get("/face/{tag_id}")
def face(tag_id: int):
    """A face crop to use as a person's avatar."""
    con = db()
    try:
        row = con.execute(
            f"SELECT f.x, f.y, f.w AS fw, f.h AS fh, {', '.join('p.' + c.strip() for c in THUMB_COLS.split(','))} "
            "FROM faces f JOIN photos p ON p.id = f.photo_id JOIN tags t ON t.id = f.tag_id "
            "WHERE f.tag_id = ? AND f.kind = 'confirmed' AND p.video = 0 "
            # the photo chosen in digiKam (or here) wins; otherwise a good, large, recent face
            "ORDER BY (p.id = t.icon) DESC, (p.pick = 1), (min(f.w, f.h) >= 160) DESC, p.rating DESC, p.ts DESC LIMIT 1",
            (tag_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404)
    d = dict(row)
    try:
        path = thumbs.make_face(tag_id, d, (d["x"], d["y"], d["fw"], d["fh"]))
    except Exception:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers=IMG_CACHE)


@router.get("/orig/{photo_id}")
def original(photo_id: int, dl: int = 0):
    """The untouched original file (supports range requests for video)."""
    row = _photo_row(photo_id, "path, name")
    if not os.path.isfile(row["path"]):
        raise HTTPException(404, "Original file is not available")
    mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
    return FileResponse(row["path"], media_type=mime, filename=row["name"],
                        content_disposition_type="attachment" if dl else "inline",
                        headers={"Cache-Control": "private, max-age=3600"})


class _Sink:
    """Write-only buffer so zipfile can stream straight into the HTTP response."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf += data
        return len(data)

    def flush(self):
        pass

    def take(self) -> bytes:
        data = bytes(self.buf)
        self.buf.clear()
        return data


def _zip_stream(entries):
    """Yield a zip of (absolute path, name in archive) pairs without ever holding
    more than a chunk in memory. Stored, not deflated: photos don't compress.
    An entry may instead be (bytes, name), for a converted copy."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path, arc in entries:
            if isinstance(path, (bytes, bytearray)):
                info = zipfile.ZipInfo(arc, time.localtime()[:6])
                info.compress_type = zipfile.ZIP_STORED
                with zf.open(info, "w", force_zip64=True) as dst:
                    for i in range(0, len(path), 1 << 20):
                        dst.write(path[i:i + (1 << 20)])
                        yield sink.take()
                yield sink.take()
                continue
            try:
                info = zipfile.ZipInfo.from_file(path, arc)
                src = open(path, "rb")
            except OSError:
                continue  # file missing on disk; skip it
            info.compress_type = zipfile.ZIP_STORED
            with src, zf.open(info, "w", force_zip64=True) as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
                    yield sink.take()
            yield sink.take()
    yield sink.take()


@router.post("/api/zip")
async def api_zip_post(request: Request):
    """Same as GET /api/zip, for selections too long for a URL (form-encoded body)."""
    items = await _form_items(request)
    d = dict(items)
    return _zip_response(items, 1 if d.get("sidecars") == "1" else 0, d.get("name") or "photos")


@router.get("/api/zip")
def api_zip(request: Request, sidecars: int = 0, name: str = "photos"):
    return _zip_response(request.query_params.multi_items(), sidecars, name)


FILTER_KEYS_IGNORED = {"sort", "name", "sidecars", "part", "part_mb", "sem_k", "xsize", "xfmt", "xquality", "nogps", "xname", "flat"}


def _zip_parts(items, part_mb: int):
    """The files for a download, split into parts of at most part_mb megabytes each.
    The split is by album order, so the parts are stable between calls."""
    if not any(k not in FILTER_KEYS_IGNORED and v != "" for k, v in items):
        raise HTTPException(400, "Choose an album, a search or a selection first; the whole library is not offered as one download.")
    where, params, _ = resolve(items)
    con = db()
    try:
        rows = con.execute(
            f"SELECT p.path, a.path, p.name, p.size, p.ts FROM photos p JOIN albums a ON a.id = p.album_id "
            f"WHERE p.id IN (SELECT id FROM photos WHERE {where}) ORDER BY a.path, p.name", params).fetchall()
    except sqlite3.OperationalError as e:
        raise HTTPException(400, f"Bad query: {e}")
    finally:
        con.close()
    limit = max(100, min(int(part_mb or 2000), 16000)) * 1024 * 1024
    parts, cur, cur_bytes = [], [], 0
    for r in rows:
        size = r[3] or 0
        if cur and cur_bytes + size > limit:
            parts.append(cur)
            cur, cur_bytes = [], 0
        cur.append(r)
        cur_bytes += size
    if cur:
        parts.append(cur)
    return parts


@router.post("/api/zip/plan")
async def api_zip_plan(request: Request):
    """How a download would be split. Body: the filters, form-encoded, plus part_mb."""
    items = await _form_items(request)
    parts = _zip_parts(items, int(dict(items).get("part_mb") or 2000))
    return {"files": sum(len(p) for p in parts), "bytes": sum(r[3] or 0 for p in parts for r in p),
            "parts": [{"files": len(p), "bytes": sum(r[3] or 0 for r in p),
                       "first": p[0][1].strip("/"), "last": p[-1][1].strip("/")} for p in parts]}


def _zip_response(items, sidecars: int, name: str):
    """Download the originals matching the filters as a zip (or one part of a split
    download), keeping the album folder structure. Optionally includes XMP sidecars."""
    d = dict(items)
    parts = _zip_parts(items, int(d.get("part_mb") or 2000))
    if not parts:
        raise HTTPException(404, "Nothing matches")
    part = int(d.get("part") or 0)
    if part < 0 or part >= len(parts):
        raise HTTPException(404, "No such part")
    rows = parts[part]
    from app.photos import export
    # export options (x-prefixed: "size" and "fmt" are search filters)
    opts = {"size": int(d.get("xsize") or 0), "fmt": d.get("xfmt") or "keep", "quality": int(d.get("xquality") or 90),
            "nogps": d.get("nogps") == "1"}
    pattern, flat = d.get("xname") or "keep", d.get("flat") == "1"
    used = {}

    def entries():
        for path, album, fname, _size, ts in rows:
            folder = "" if flat else album.strip("/")
            ext = export.out_ext(path, opts)
            fname2 = export.name_for(pattern, ts, fname, used.setdefault(folder, set()), ext)
            arc = f"{folder}/{fname2}".lstrip("/")
            try:
                data = export.prepare(path, opts)
            except Exception:  # a file Pillow cannot read: the original goes in instead
                data, arc = None, f"{folder}/{export.name_for(pattern, ts, fname, used[folder])}".lstrip("/")
            yield (data if data is not None else path), arc
            if sidecars and os.path.isfile(path + ".xmp") and not opts["nogps"]:
                yield path + ".xmp", arc + ".xmp"

    safe = re.sub(r"[^\w .()+-]+", "_", name).strip(" ._")[:80] or "photos"
    if len(parts) > 1:
        safe += f" (part {part + 1} of {len(parts)})"
    return StreamingResponse(_zip_stream(entries()), media_type="application/zip", headers={
        "Content-Disposition": f"attachment; filename=\"{safe}.zip\"", "Cache-Control": "no-store"})
