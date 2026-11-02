"""Response helpers the route modules share: JSON answers with an ETag and gzip, request bodies
(JSON objects, form-encoded filter lists), and the thumbnail and preview file responses with
their cache headers. Nothing here knows about digiKam; the index connection comes from app.runtime."""
import gzip
import hashlib
import os
from urllib.parse import parse_qsl

import orjson
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import Response

from app import thumbs
from app.runtime import THUMB_COLS, db

IMG_CACHE = {"Cache-Control": "private, max-age=86400"}


def jresp(request: Request, data) -> Response:
    """JSON, gzipped when large, with an ETag: a client that sends it back (If-None-Match) gets 304 and no body while
    the answer is unchanged (the apps' reloads of meta, photos and state)."""
    body = orjson.dumps(data)
    etag = 'W/"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag, "Vary": "Accept-Encoding, Cookie"}
    if etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    if len(body) > 2048 and "gzip" in request.headers.get("accept-encoding", ""):
        body = gzip.compress(body, compresslevel=3)
        headers["Content-Encoding"] = "gzip"
    return Response(body, media_type="application/json", headers=headers)


async def form_items(request: Request) -> list:
    """(key, value) pairs of a form-encoded body: the same filters the URL carries,
    for selections too long for a URL."""
    return parse_qsl((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)


def photo_row(photo_id: int, cols: str = THUMB_COLS) -> dict:
    con = db()
    try:
        row = con.execute(f"SELECT {cols} FROM photos WHERE id = ?", (photo_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404)
    return dict(row)


def cached_image(kind: str, photo_id: int, dl: bool = False) -> FileResponse:
    row = photo_row(photo_id)
    try:
        path = thumbs.make(kind, row)
    except FileNotFoundError:
        raise HTTPException(404, "Original file is not available")
    except OSError as e:
        raise HTTPException(503, f"The file could not be read right now ({type(e).__name__})")
    if dl:  # a download of the resized JPEG, named after the photo (sharing a smaller file)
        return FileResponse(path, media_type="image/jpeg", filename=os.path.splitext(row["name"])[0] + "-preview.jpg",
                            content_disposition_type="attachment", headers=IMG_CACHE)
    return FileResponse(path, media_type="image/jpeg", headers=IMG_CACHE)


async def json_body(request) -> dict:
    """The request's JSON body, which must be an object: anything else (a list, a string, bad JSON) is the client's
    mistake and answers 400 instead of an AttributeError further down."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "The body is not valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "The body must be a JSON object")
    return body
