"""The read-only tools any client may call without a token: the library overview, search and
counts over the same filters the web app has, everything about one photo, the people / tag /
album / place lists, duplicate sets, and looking at a picture. File paths are left out of
answers unless the client signed in as an admin (base.anon_paths_ok)."""
import io
import json
from typing import Literal, Optional

from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError

from app import thumbs
from app.data import index, state as appstate
from app.data.query import SORTS
from app.mcp.base import (FILTER_DOC, PICKS, READ_ONLY, VIEWER, anon_paths_ok, clamp_limit, db, list_rows,
                          resolve_tag, server, where_for)

LARGE_EDGE = 1568  # view_photo "large": the long edge a vision model reads detail at without a larger upload


def _summary(con, r) -> dict:
    tags = con.execute("SELECT t.name, t.person FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.photo_id = ?",
                       (r["id"],)).fetchall()
    out = {
        "id": r["id"], "file": r["name"], "album": r["album_path"], "taken": r["taken"],
        "type": "video" if r["video"] else "photo", "title": r["title"], "caption": r["caption"],
        "people": [t["name"] for t in tags if t["person"]], "tags": [t["name"] for t in tags if not t["person"]],
        "camera": r["camera"], "rating": r["rating"] if r["rating"] and r["rating"] > 0 else None,
        "pick_label": PICKS[r["pick"]] if r["pick"] else None,
        "location": [round(r["lat"], 5), round(r["lon"], 5)] if r["lat"] is not None else None,
        "viewer_url": f"{VIEWER}/#/photos?p={r['id']}",
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


@server.tool(annotations=READ_ONLY)
def library_overview() -> dict:
    """Totals, date range, and the most common cameras and years in the library."""
    con = db()
    try:
        q = con.execute
        t = q("SELECT count(*), sum(video), sum(lat IS NOT NULL), min(taken), max(taken), sum(size), "
              "sum(rating > 0), sum(title IS NOT NULL OR caption IS NOT NULL) FROM photos").fetchone()
        return {
            "items": t[0], "videos": t[1], "with_location": t[2], "earliest": t[3], "latest": t[4],
            "total_gb": round((t[5] or 0) / 1e9, 1), "rated": t[6], "with_title_or_caption": t[7],
            "people": q("SELECT count(*) FROM tags WHERE person = 1 AND n > 0").fetchone()[0],
            "tags": q("SELECT count(*) FROM tags WHERE person = 0").fetchone()[0],
            "albums": q("SELECT count(*) FROM albums").fetchone()[0],
            "top_level_tags": [r[0] for r in q("SELECT name FROM tags WHERE pid IS NULL ORDER BY n_all DESC")],
            "items_per_year": {str(r[0]): r[1] for r in q("SELECT year, count(*) FROM photos GROUP BY 1 ORDER BY 1")},
            "cameras": {r[0]: r[1] for r in q("SELECT camera, count(*) FROM photos WHERE camera IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 15")},
            "pick_labels": {PICKS[r[0]]: r[1] for r in q("SELECT pick, count(*) FROM photos GROUP BY 1")},
            "web_viewer": VIEWER,
        }
    finally:
        con.close()


def search_photos(
    text: Optional[str] = None, people: Optional[list[str]] = None, tags: Optional[list[str]] = None,
    exclude_tags: Optional[list[str]] = None, album: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None, year: Optional[int] = None,
    month: Optional[int] = None, month_day: Optional[str] = None, camera: Optional[str] = None,
    lens: Optional[str] = None, photographer: Optional[str] = None, file_format: Optional[str] = None,
    min_rating: Optional[int] = None, pick_label: Optional[Literal["none", "pending", "accepted"]] = None,
    media_type: Optional[Literal["photo", "video"]] = None,
    has_location: Optional[bool] = None, untagged: bool = False, bbox: Optional[list[float]] = None,
    iso_min: Optional[float] = None, iso_max: Optional[float] = None, aperture_min: Optional[float] = None,
    aperture_max: Optional[float] = None, focal_min: Optional[float] = None, focal_max: Optional[float] = None,
    place: Optional[str] = None, region: Optional[str] = None, country: Optional[str] = None,
    favorites_only: bool = False, app_album: Optional[str] = None,
    looks_like: Optional[str] = None, similar_to: Optional[int] = None,
    sort: Optional[Literal["new", "old", "added", "name", "size", "rating"]] = None, limit: int = 30, offset: int = 0,
) -> dict:
    # the signature is the tool's schema, which the clients see; the filters go on as one dict
    filters = {
        "text": text, "people": people, "tags": tags, "exclude_tags": exclude_tags, "album": album,
        "date_from": date_from, "date_to": date_to, "year": year, "month": month, "month_day": month_day,
        "camera": camera, "lens": lens, "photographer": photographer, "file_format": file_format,
        "min_rating": min_rating, "pick_label": pick_label, "media_type": media_type,
        "has_location": has_location, "untagged": untagged, "bbox": bbox,
        "iso_min": iso_min, "iso_max": iso_max, "aperture_min": aperture_min, "aperture_max": aperture_max,
        "focal_min": focal_min, "focal_max": focal_max, "place": place, "region": region, "country": country,
        "favorites_only": favorites_only, "app_album": app_album, "looks_like": looks_like, "similar_to": similar_to,
    }
    con = db()
    try:
        where, params, rank = where_for(con, filters)
        total = con.execute(f"SELECT count(*) FROM photos WHERE {where}", params).fetchone()[0]
        limit, offset = clamp_limit(limit, 200), max(0, int(offset))
        sql = (f"SELECT p.*, a.path AS album_path FROM photos p JOIN albums a ON a.id = p.album_id "
               f"WHERE p.id IN (SELECT id FROM photos WHERE {where})")
        if rank and not sort:  # best visual match first
            rows = sorted(con.execute(sql, params).fetchall(), key=lambda r: rank.get(r["id"], 1 << 30))[offset:offset + limit]
        else:
            rows = con.execute(sql + f" ORDER BY {SORTS[sort or 'new']} LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        return {"total_matches": total, "offset": offset, "returned": len(rows),
                "photos": [_summary(con, r) for r in rows]}
    finally:
        con.close()


search_photos.__doc__ = ("Search the library. Returns the total number of matches and one page of results "
                         "(limit up to 200; page with offset). sort: new (default), old, added, name, size, rating; content searches default to best match.\n" + FILTER_DOC)
server.tool(annotations=READ_ONLY)(search_photos)


def count_by(
    field: Literal["year", "month", "day", "person", "tag", "album", "camera", "lens", "rating", "pick_label",
                   "file_format", "photographer", "media_type", "place", "region", "country"],
    text: Optional[str] = None, people: Optional[list[str]] = None, tags: Optional[list[str]] = None,
    exclude_tags: Optional[list[str]] = None, album: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None, year: Optional[int] = None,
    month: Optional[int] = None, camera: Optional[str] = None, lens: Optional[str] = None,
    min_rating: Optional[int] = None,
    media_type: Optional[Literal["photo", "video"]] = None, has_location: Optional[bool] = None,
    place: Optional[str] = None, region: Optional[str] = None, country: Optional[str] = None,
    favorites_only: bool = False, limit: int = 50,
) -> dict:
    filters = {
        "text": text, "people": people, "tags": tags, "exclude_tags": exclude_tags, "album": album,
        "date_from": date_from, "date_to": date_to, "year": year, "month": month, "camera": camera, "lens": lens,
        "min_rating": min_rating, "media_type": media_type, "has_location": has_location,
        "place": place, "region": region, "country": country, "favorites_only": favorites_only,
    }
    con = db()
    try:
        where, params, _ = where_for(con, filters)
        sel = f"(SELECT id FROM photos WHERE {where})"
        limit = clamp_limit(limit, 500)
        if field in ("person", "tag"):
            sql = (f"SELECT t.full, count(*) FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id "
                   f"WHERE t.person = {1 if field == 'person' else 0} AND pt.photo_id IN {sel} GROUP BY t.id ORDER BY 2 DESC LIMIT ?")
        elif field == "album":
            sql = f"SELECT a.path, count(*) FROM photos p JOIN albums a ON a.id = p.album_id WHERE p.id IN {sel} GROUP BY a.id ORDER BY 2 DESC LIMIT ?"
        else:
            col = {"year": "year", "month": "substr(day, 1, 7)", "day": "day", "camera": "camera", "lens": "lens",
                   "rating": "max(rating, 0)", "pick_label": "pick", "place": "place", "region": "region", "country": "country", "file_format": "fmt", "photographer": "creator",
                   "media_type": "CASE video WHEN 1 THEN 'video' ELSE 'photo' END"}[field]
            order = "1 DESC" if field in ("year", "month", "day") else "2 DESC"
            sql = f"SELECT {col}, count(*) FROM photos WHERE id IN {sel} GROUP BY 1 ORDER BY {order} LIMIT ?"
        rows = con.execute(sql, params + [limit]).fetchall()
        label = (lambda v: PICKS[v]) if field == "pick_label" else (lambda v: "unknown" if v is None else str(v))
        return {"field": field, "counts": {label(r[0]): r[1] for r in rows}}
    finally:
        con.close()


count_by.__doc__ = ("Count matching items grouped by one metadata field, e.g. photos per year for a person, "
                    "who appears most with someone, or which cameras were used on a trip. Accepts the main "
                    "search_photos filters.\n" + FILTER_DOC)
server.tool(annotations=READ_ONLY)(count_by)


@server.tool(annotations=READ_ONLY)
def get_photo(photo_id: int) -> dict:
    """Everything known about one item: file path, dates, dimensions, camera and exposure,
    GPS, title, caption, rating and labels, tags, people with their face rectangles
    (pixels in the upright image), copyright fields and video details."""
    con = db()
    try:
        r = con.execute("SELECT p.*, a.path AS album_path FROM photos p JOIN albums a ON a.id = p.album_id WHERE p.id = ?",
                        (photo_id,)).fetchone()
        if not r:
            raise ToolError(f"No item with id {photo_id}")
        d = dict(r)
        if not anon_paths_ok():
            d.pop("path", None)  # the file's location is for admins (the web API hides it from visitors too)
        extra = json.loads(d.pop("extra") or "{}")
        d["favorite"] = appstate.is_favorite(photo_id)
        for k in ("hash", "ts", "mts", "md", "day", "ntags", "album_id"):
            d.pop(k, None)
        d["width"], d["height"] = d.pop("w"), d.pop("h")
        d["pick_label"], d["color_label"] = PICKS[d.pop("pick")], index.COLORS[d.pop("color")].lower()
        d["rating"] = max(d["rating"], 0)
        d["tags"] = [x[0] for x in con.execute(
            "SELECT t.full FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.photo_id = ? AND t.person = 0", (photo_id,))]
        faces = {x["tag_id"]: [x["x"], x["y"], x["w"], x["h"]] for x in con.execute("SELECT * FROM faces WHERE photo_id = ?", (photo_id,))}
        d["people"] = [{"name": x["name"], **({"face_rect_xywh": faces[x["id"]]} if x["id"] in faces else {})} for x in con.execute(
            "SELECT t.id, t.name FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.photo_id = ? AND t.person = 1", (photo_id,))]
        d.update({k: v for k, v in extra.items() if k in ("copyright", "properties", "video")})
        d["viewer_url"] = f"{VIEWER}/#/photos?p={photo_id}"
        return {k: v for k, v in d.items() if v not in (None, "", [], {})}
    finally:
        con.close()


@server.tool(annotations=READ_ONLY)
def list_people(query: str = "", limit: int = 100) -> list[dict]:
    """People in the library with how many items each appears in, most frequent first.
    `query` filters by part of the name."""
    return list_rows("SELECT id, name, full AS path, n_all AS items FROM tags WHERE person = 1 AND n > 0 AND name LIKE ? "
                     "ORDER BY n_all DESC LIMIT ?", (f"%{query}%", clamp_limit(limit, 1000)))


@server.tool(annotations=READ_ONLY)
def list_tags(query: str = "", parent: Optional[str] = None, limit: int = 200) -> list[dict]:
    """Keyword tags (not people) as full paths like "Activity/Soccer", with item counts
    that include nested tags. `query` filters by part of the path; `parent` (a tag name,
    path or id) lists only that tag's direct children instead."""
    if parent:
        con = db()
        try:
            pid = resolve_tag(con, parent, None)
        finally:
            con.close()
        return list_rows("SELECT id, name, full AS path, n_all AS items FROM tags WHERE pid = ? ORDER BY n_all DESC LIMIT ?",
                         (pid, clamp_limit(limit, 1000)))
    return list_rows("SELECT id, name, full AS path, n_all AS items FROM tags WHERE person = 0 AND full LIKE ? "
                     "ORDER BY n_all DESC LIMIT ?", (f"%{query}%", clamp_limit(limit, 1000)))


@server.tool(annotations=READ_ONLY)
def list_albums(query: str = "", limit: int = 400) -> list[dict]:
    """digiKam's folders ("albums" in digiKam's vocabulary: the directories on disk) with item counts including sub-folders and the date range they cover."""
    return list_rows("SELECT a.id, a.path, a.n_all AS items, "
                     "(SELECT min(day) FROM photos p JOIN album_closure c ON c.album = p.album_id WHERE c.anc = a.id) AS first_day, "
                     "(SELECT max(day) FROM photos p JOIN album_closure c ON c.album = p.album_id WHERE c.anc = a.id) AS last_day "
                     "FROM albums a WHERE a.path LIKE ? ORDER BY a.path LIMIT ?", (f"%{query}%", clamp_limit(limit, 2000)))


@server.tool(annotations=READ_ONLY)
def list_places(query: str = "", limit: int = 200) -> list[dict]:
    """Towns where photos were taken (derived offline from GPS), with region, country and item counts."""
    return list_rows("SELECT place, region, country, count(*) AS items FROM photos WHERE place IS NOT NULL "
                     "AND (place LIKE ? OR region LIKE ? OR country LIKE ?) GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT ?",
                     (f"%{query}%",) * 3 + (clamp_limit(limit, 2000),))


@server.tool(annotations=READ_ONLY)
def list_app_albums() -> list[dict]:
    """Albums made in the web viewer: groupings that exist only there, separate from digiKam's folders; plus the favorites count."""
    s = appstate.summary()
    return [{"id": a["id"], "name": a["name"], "items": a["n"]} for a in s["albums"]] + [{"name": "favorites", "items": len(s["favorites"])}]


@server.tool(annotations=READ_ONLY)
def find_duplicates(limit: int = 100) -> dict:
    """Sets of byte-identical files (same digiKam content hash and size), largest first."""
    rows = list_rows("SELECT p.id, p.hash, p.size, p.name, a.path AS album FROM photos p JOIN albums a ON a.id = p.album_id "
                     "WHERE (p.hash, p.size) IN (SELECT hash, size FROM photos WHERE hash IS NOT NULL AND hash != '' "
                     "GROUP BY hash, size HAVING count(*) > 1) ORDER BY p.size DESC, p.hash, a.path, p.name")
    groups = {}
    for r in rows:
        groups.setdefault((r["hash"], r["size"]), []).append({"id": r["id"], "file": f"{r['album']}/{r['name']}"})
    out = [{"size_bytes": k[1], "copies": v} for k, v in groups.items()]
    return {"duplicate_sets": len(out), "wasted_bytes": sum(g["size_bytes"] * (len(g["copies"]) - 1) for g in out),
            "sets": out[:max(1, limit)]}


@server.tool(annotations=READ_ONLY)
def view_photo(photo_id: int, size: Literal["small", "large"] = "small") -> Image:
    """Look at a photo (or a video's poster frame). "small" is about 360 px tall and
    cheap; "large" is up to 1568 px on the long edge for reading detail."""
    con = db()
    try:
        r = con.execute("SELECT id, path, video, duration, w, h, hash, size, modified FROM photos WHERE id = ?", (photo_id,)).fetchone()
    finally:
        con.close()
    if not r:
        raise ToolError(f"No item with id {photo_id}")
    if size == "small":
        return Image(path=thumbs.make("t", dict(r)))
    from PIL import Image as PILImage
    with PILImage.open(thumbs.make("p", dict(r))) as im:
        im.thumbnail((LARGE_EDGE, LARGE_EDGE), PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=82)
    return Image(data=buf.getvalue(), format="jpeg")
