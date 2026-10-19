"""MCP server for the digiKam library.

Lets an MCP client such as Claude search the library, read any photo's
metadata, browse people / tags / albums, count things, and look at photos
(all read-only), plus a few operator tools behind the admin token: diagnostics,
the server log, the job queue, a photo's files on disk, and running update.sh.
It is served at /mcp by the web app (main.py) and can also run on its own with
`./mcp.sh` (stdio transport), sharing the web app's index and thumbnail cache.
"""
import io
import json
import os
import re
import sqlite3
from typing import Literal, Optional
from urllib.parse import quote

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app import config, thumbs, updates
from app.photos import edits
from app.data import index
from app.data import state as appstate
from app.data.query import DATE_RE, SORTS, build_where
from app.ai.semantic import semantic, enabled as semantic_enabled

VIEWER = os.environ.get("VIEWER_URL", "http://localhost:8600").rstrip("/")  # for the links in results
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PICKS = ["none", "rejected", "pending", "accepted"]
MONTH_DAY_RE = re.compile(r"^\d\d-\d\d$")
_state_ready = False  # the viewer database's tables are checked on the first call, not on every one

server = MCPServer(
    "digikam-library",
    instructions=(
        "Read-only access to a personal digiKam photo library. Start with library_overview, "
        "use list_people / list_tags / list_albums to find exact names, then search_photos. "
        "People are tags too. Selecting a tag or album also matches everything nested under it. "
        "Dates are local camera time. Use view_photo to actually look at a picture."
    ),
)


def db() -> sqlite3.Connection:
    """A read-only connection to the index (the viewer's own data is read through appstate / vdb)."""
    try:
        index.ensure_fresh()  # cheap stat; rebuilds only if digiKam changed its database
    except Exception:  # a busy or unreachable digiKam database (SQLite or MySQL): answer from the index we have
        if not config.INDEX_DB.exists():
            raise
    global _state_ready
    if not _state_ready:  # idempotent CREATE IF NOT EXISTS, so a race between two first calls is harmless
        appstate.init()
        edits.init()
        _state_ready = True
    con = sqlite3.connect(f"file:{quote(str(config.INDEX_DB))}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _resolve_tag(con, name: str, person: Optional[bool]) -> int:
    """A tag id from an id, an exact name or path, or a unique partial match. person: True for
    people only, None for any tag."""
    if str(name).isdigit():
        return int(name)
    kind = "person" if person else "tag"
    cond = "" if person is None else f" AND person = {1 if person else 0}"
    rows = con.execute(f"SELECT id, full FROM tags WHERE (name = ? COLLATE NOCASE OR full = ? COLLATE NOCASE){cond}",
                       (name, name)).fetchall()
    if not rows:
        rows = con.execute(f"SELECT id, full FROM tags WHERE full LIKE ? AND n_all > 0{cond} ORDER BY n_all DESC LIMIT 8",
                           (f"%{name}%",)).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if not rows:
        raise ToolError(f"No {kind} matches '{name}'. Use list_{'people' if person else 'tags'} to find the exact name.")
    raise ToolError(f"'{name}' is ambiguous; did you mean one of: " + "; ".join(f"{r['full']} (id {r['id']})" for r in rows))


def _resolve_album(con, name: str) -> int:
    if str(name).isdigit():
        return int(name)
    rows = con.execute("SELECT id, path FROM albums WHERE path = ? COLLATE NOCASE OR name = ? COLLATE NOCASE",
                       (name if name.startswith("/") else "/" + name, name)).fetchall()
    if not rows:
        rows = con.execute("SELECT id, path FROM albums WHERE path LIKE ? ORDER BY n_all DESC LIMIT 8", (f"%{name}%",)).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if not rows:
        raise ToolError(f"No album matches '{name}'. Use list_albums to find it.")
    raise ToolError(f"'{name}' is ambiguous; did you mean one of: " + "; ".join(f"{r['path']} (id {r['id']})" for r in rows))


def _items(con, f: dict) -> list:
    """Translate tool arguments into the web app's filter parameters (query.build_where
    ignores malformed values silently, so the shapes it is picky about are checked here)."""
    for key in ("date_from", "date_to"):
        if f.get(key) and not DATE_RE.match(str(f[key])):
            raise ToolError(f"{key} must be YYYY-MM-DD, got '{f[key]}'")
    if f.get("month_day") and not MONTH_DAY_RE.match(str(f["month_day"])):
        raise ToolError(f"month_day must be MM-DD, got '{f['month_day']}'")
    if f.get("month") is not None and not 1 <= int(f["month"]) <= 12:
        raise ToolError("month must be 1-12")
    if f.get("bbox") is not None and len(f["bbox"]) != 4:
        raise ToolError("bbox must be [south, west, north, east]")
    items = []
    add = lambda k, v: items.append((k, str(v)))
    if f.get("text"):
        add("q", f["text"])
    for name in f.get("people") or []:
        add("tag", _resolve_tag(con, name, True))
    for name in f.get("tags") or []:
        add("tag", _resolve_tag(con, name, None))
    for name in f.get("exclude_tags") or []:
        add("nottag", _resolve_tag(con, name, None))
    if f.get("album"):
        add("album", _resolve_album(con, f["album"]))
    for key, param in (("date_from", "from"), ("date_to", "to"), ("year", "year"), ("month", "month"),
                       ("camera", "camera"), ("lens", "lens"), ("min_rating", "rating"), ("file_format", "fmt"),
                       ("photographer", "creator"), ("month_day", "md"),
                       ("place", "place"), ("region", "region"), ("country", "country"),
                       ("iso_min", "iso_min"), ("iso_max", "iso_max"), ("aperture_min", "ap_min"),
                       ("aperture_max", "ap_max"), ("focal_min", "fl_min"), ("focal_max", "fl_max")):
        if f.get(key) not in (None, ""):
            add(param, f[key])
    if f.get("pick_label"):
        add("pick", PICKS.index(f["pick_label"]))
    if f.get("media_type") in ("photo", "video"):
        add("type", f["media_type"])
    if f.get("has_location") is not None:
        add("geo", 1 if f["has_location"] else 0)
    if f.get("untagged"):
        add("untagged", 1)
    if f.get("bbox"):
        add("bbox", ",".join(str(x) for x in f["bbox"]))
    if f.get("favorites_only"):
        add("fav", 1)
    if f.get("app_album"):
        aid = appstate.album_id_by_name(f["app_album"])
        if aid is None:
            raise ToolError(f"No app album named '{f['app_album']}'. Use list_app_albums.")
        add("ualbum", aid)
    return items


def _where(con, f: dict):
    """SQL for the filters, plus a ranking when searching by content or similarity."""
    items, rank = _items(con, f), None
    if f.get("looks_like") or f.get("similar_to"):
        if not semantic_enabled():
            raise ToolError("Content search is not enabled for this library (run enable-search.sh in the app folder).")
        ids = (semantic.search_text(f["looks_like"], 200) if f.get("looks_like")
               else semantic.similar(int(f["similar_to"]), 200))
        items.append(("ids", ",".join(map(str, ids)) or "0"))
        rank = {pid: i for i, pid in enumerate(ids)}
    where, params = build_where(items)
    return where, params, rank


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


FILTER_DOC = """
    Filters (all optional, combined with AND):
      text: free-text search over file names, albums, titles, captions, tags and people
      people / tags / exclude_tags: lists of names (or ids); child tags are included
      album: album name, path such as "/2022/202205 Prom", or id; sub-albums are included
      date_from / date_to: "YYYY-MM-DD" inclusive; year, month (1-12); month_day "MM-DD" for "on this day"
      camera, lens, photographer, file_format (JPG, HEIF, MOV, ...): exact values as shown by count_by
      min_rating: 1-5; pick_label: none/pending/accepted
      media_type: "photo" or "video"; has_location: true/false; untagged: true
      bbox: [south, west, north, east] in degrees
      iso_min/iso_max, aperture_min/aperture_max, focal_min/focal_max (35mm equivalent when known)
      place (town), region (state/province), country: names as shown by list_places (from GPS)
      favorites_only: items the owner marked as favorite in the viewer; app_album: an album made in the viewer
      looks_like: describe the picture's content ("dog on a beach"); ranked by visual match
      similar_to: a photo id; finds visually similar photos
"""


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
            "photos_root": str(config.PHOTOS_ROOT), "web_viewer": VIEWER,
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
    f = {k: v for k, v in locals().items() if k not in ("sort", "limit", "offset")}
    con = db()
    try:
        where, params, rank = _where(con, f)
        total = con.execute(f"SELECT count(*) FROM photos WHERE {where}", params).fetchone()[0]
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
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
    f = {k: v for k, v in locals().items() if k not in ("field", "limit")}
    con = db()
    try:
        where, params, _ = _where(con, f)
        sel = f"(SELECT id FROM photos WHERE {where})"
        limit = max(1, min(int(limit), 500))
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


def _list(sql: str, params=()) -> list:
    con = db()
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


@server.tool(annotations=READ_ONLY)
def list_people(query: str = "", limit: int = 100) -> list[dict]:
    """People in the library with how many items each appears in, most frequent first.
    `query` filters by part of the name."""
    return _list("SELECT id, name, full AS path, n_all AS items FROM tags WHERE person = 1 AND n > 0 AND name LIKE ? "
                 "ORDER BY n_all DESC LIMIT ?", (f"%{query}%", max(1, min(limit, 1000))))


@server.tool(annotations=READ_ONLY)
def list_tags(query: str = "", parent: Optional[str] = None, limit: int = 200) -> list[dict]:
    """Keyword tags (not people) as full paths like "Activity/Soccer", with item counts
    that include nested tags. `query` filters by part of the path; `parent` (a tag name,
    path or id) lists only that tag's direct children instead."""
    if parent:
        con = db()
        try:
            pid = _resolve_tag(con, parent, None)
        finally:
            con.close()
        return _list("SELECT id, name, full AS path, n_all AS items FROM tags WHERE pid = ? ORDER BY n_all DESC LIMIT ?",
                     (pid, max(1, min(limit, 1000))))
    return _list("SELECT id, name, full AS path, n_all AS items FROM tags WHERE person = 0 AND full LIKE ? "
                 "ORDER BY n_all DESC LIMIT ?", (f"%{query}%", max(1, min(limit, 1000))))


@server.tool(annotations=READ_ONLY)
def list_albums(query: str = "", limit: int = 400) -> list[dict]:
    """digiKam's folders ("albums" in digiKam's vocabulary: the directories on disk) with item counts including sub-folders and the date range they cover."""
    return _list("SELECT a.id, a.path, a.n_all AS items, "
                 "(SELECT min(day) FROM photos p JOIN album_closure c ON c.album = p.album_id WHERE c.anc = a.id) AS first_day, "
                 "(SELECT max(day) FROM photos p JOIN album_closure c ON c.album = p.album_id WHERE c.anc = a.id) AS last_day "
                 "FROM albums a WHERE a.path LIKE ? ORDER BY a.path LIMIT ?", (f"%{query}%", max(1, min(limit, 2000))))


@server.tool(annotations=READ_ONLY)
def list_places(query: str = "", limit: int = 200) -> list[dict]:
    """Towns where photos were taken (derived offline from GPS), with region, country and item counts."""
    return _list("SELECT place, region, country, count(*) AS items FROM photos WHERE place IS NOT NULL "
                 "AND (place LIKE ? OR region LIKE ? OR country LIKE ?) GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT ?",
                 (f"%{query}%",) * 3 + (max(1, min(limit, 2000)),))


@server.tool(annotations=READ_ONLY)
def list_app_albums() -> list[dict]:
    """Albums made in the web viewer: groupings that exist only there, separate from digiKam's folders; plus the favorites count."""
    s = appstate.summary()
    return [{"id": a["id"], "name": a["name"], "items": a["n"]} for a in s["albums"]] + [{"name": "favorites", "items": len(s["favorites"])}]


@server.tool(annotations=READ_ONLY)
def find_duplicates(limit: int = 100) -> dict:
    """Sets of byte-identical files (same digiKam content hash and size), largest first."""
    rows = _list("SELECT p.id, p.hash, p.size, p.name, a.path AS album FROM photos p JOIN albums a ON a.id = p.album_id "
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
        im.thumbnail((1568, 1568), PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=82)
    return Image(data=buf.getvalue(), format="jpeg")



# ---- diagnostics: what an operator would check in a shell, read-only
def _need_token(token: str):
    """The diagnostics tools reveal paths, log lines and settings; on an endpoint without sign-in
    they take the same admin token as update_viewer (Maintenance > Viewer version)."""
    if not updates.token_ok(token):
        raise ToolError("This tool needs the admin token (Maintenance > Viewer version > MCP update token)")


@server.tool(annotations=READ_ONLY)
def diagnostics(token: str) -> dict:
    """The viewer's own health: version and commit, platform, database backend, which external tools
    exist (exiftool, ffmpeg, mysqldump), disk space, the background job queue with timing, installed
    models and features, user and permission mode, settings without secrets. Start here for any issue."""
    _need_token(token)
    import platform
    import shutil as sh
    import sys
    import time
    from app import __version__, jobs, users
    from app.digikam import dkdb
    from app.ai import models
    from app.ai.analysis import progress as analysis_progress
    try:
        free = sh.disk_usage(str(config.DATA_DIR)).free
    except OSError:
        free = None
    q = jobs.status()
    dbc = dkdb.settings()
    out = {
        "version": __version__, "commit": updates.local_commit()[:7], "docker": updates.in_docker(),
        "platform": platform.platform(), "python": sys.version.split()[0], "app_dir": str(config.APP_DIR), "data_dir": str(config.DATA_DIR),
        "database": {"type": dbc["type"], "location": dkdb.describe(), "backup_mode": dbc["backup"], "digikam_check": config.DIGIKAM_CHECK},
        "tools": {"exiftool": bool(edits.EXIFTOOL), "ffmpeg": bool(thumbs.FFMPEG), "mysqldump": bool(sh.which("mysqldump") or sh.which("mariadb-dump"))},
        "disk_free_gb": round(free / 1e9, 1) if free is not None else None,
        "jobs": {**q, "running_for_min": round((time.time() - q["since"]) / 60, 1) if q.get("since") else None},
        "analysis_progress": {k: {"running": v["running"], "done": v["done"], "total": v["total"]} for k, v in analysis_progress.items()},
        "consistency": {"progress": dict(__import__("app.photos.consistency", fromlist=["progress"]).progress), "summary": __import__("app.photos.consistency", fromlist=["summary"]).summary()},
        "models": [{"key": m["key"], "installed": m["installed"], "revision": m.get("revision")} for m in models.listing()["models"]],
        "search": {"enabled": semantic_enabled(), "running": semantic.running, "done": semantic.done, "total": semantic.total, "model": semantic.chosen()},
        "users": {"exist": users.any_users(), "count": len(users.list_users())},
        "proxySignIn": __import__("app.proxyauth", fromlist=["status"]).status(),
        "read_only": appstate.read_only(),
        "settings": _public_settings(),
        "log_file": str(config.LOG_FILE), "log_file_exists": config.LOG_FILE.exists(),
    }
    try:
        con = db()
        try:
            out["index"] = {"photos": con.execute("SELECT count(*) FROM photos").fetchone()[0], "built": con.execute("SELECT value FROM meta WHERE key = 'built'").fetchone()[0]}
        finally:
            con.close()
    except Exception as e:
        out["index"] = {"error": str(e)[:200]}
    return out


def _public_settings() -> dict:
    """Every stored setting except secrets (passwords, keys, session vouches)."""
    con = appstate.connect()
    try:
        rows = con.execute("SELECT key, value FROM app_settings").fetchall()
    finally:
        con.close()
    hide = ("password", "key", "secret", "token")
    return {k: ("***" if any(h in k for h in hide) else v) for k, v in rows}


@server.tool(annotations=READ_ONLY)
def server_log(token: str, lines: int = 200, contains: str = "", level: Literal["any", "warning", "error"] = "any") -> dict:
    """The last lines of the viewer's own log (data/viewer.log, rotating). `contains` keeps only lines
    with that text; `level` keeps only WARNING+ or ERROR+ lines (tracebacks count as errors)."""
    _need_token(token)
    from app import logs
    try:
        return logs.tail(min(int(lines), 2000), contains, level)
    except OSError as e:
        raise ToolError(f"cannot read the log: {e}")


@server.tool(annotations=READ_ONLY)
def job_queue(token: str) -> dict:
    """What the background worker is doing right now: the running job, its start time and elapsed
    minutes, what is queued, the core budget, and per-job progress with the time of the last
    completed item (a stall shows as a large 'idle_seconds')."""
    _need_token(token)
    import time
    from app import jobs
    from app.ai.analysis import progress as analysis_progress
    q = jobs.status()
    now = time.time()
    out = {"running": q["running"], "queued": q["queued"], "threads": q["threads"], "cores": q["cores"],
           "running_for_min": round((now - q["since"]) / 60, 1) if q.get("since") else None,
           "analysis": {k: {"running": v["running"], "done": v["done"], "total": v["total"]} for k, v in analysis_progress.items()},
           "search": {"running": semantic.running, "done": semantic.done, "total": semantic.total}}
    try:  # the thumbnail prewarmer lives in the web app; present when this runs inside it
        from app.main import prewarmer
        out["thumbnails"] = {"running": prewarmer.running, "done": prewarmer.done, "total": prewarmer.total,
                             "idle_seconds": round(now - prewarmer.last) if prewarmer.last else None, "current": list(prewarmer.current.values())[:3]}
    except Exception:
        pass
    return out


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def repair_video(token: str, photo_id: int) -> dict:
    """Repair a video that does not play because its wrapper is damaged (inspect_file shows it): the same streams
    rewrapped without re-encoding, checked to play in full before it replaces the file. The original is kept
    (the change log can put it back). Needs the admin token."""
    _need_token(token)
    from app.photos import repair
    from app.digikam import dbsync
    try:
        return repair.video(int(photo_id))
    except dbsync.SyncError as e:
        raise ToolError(str(e))


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def proxy_signin(token: str, mode: Optional[str] = None, cf_access_team: Optional[str] = None, cf_access_aud: Optional[str] = None,
                 trusted: Optional[str] = None, create_role: Optional[str] = None) -> dict:
    """Sign-in at a reverse proxy (Cloudflare Access, Authelia behind HAProxy/nginx/Caddy/Traefik): with no settings
    given, how it is set up and the Cloudflare Access team and application last seen in requests; with settings, saves
    them (mode: off|cloudflare|haproxy|nginx|caddy|traefik|header; an empty string clears one; iopix.env still wins).
    Needs the admin token."""
    _need_token(token)
    from app import proxyauth
    changes = {k: v for k, v in (("AUTH_PROXY", mode), ("CF_ACCESS_TEAM", cf_access_team), ("CF_ACCESS_AUD", cf_access_aud),
                                 ("AUTH_PROXY_TRUSTED", trusted), ("AUTH_PROXY_CREATE", create_role)) if v is not None}
    if "AUTH_PROXY" in changes and changes["AUTH_PROXY"] not in ("", "off", *proxyauth.PRESETS):
        raise ToolError("mode: off, cloudflare, haproxy, nginx, caddy, traefik or header")
    if changes:
        proxyauth.save(changes)
    return proxyauth.status()


@server.tool(annotations=READ_ONLY)
def inspect_file(token: str, photo_id: int) -> dict:
    """What is inside one photo's or video's file, for "it does not show" questions: exiftool's reading of its
    container and streams (warnings included), ffmpeg's decode check of the first seconds, and the file's first and
    last bytes (a QuickTime file needs its moov box; a file cut short has none). Read-only. Needs the admin token."""
    _need_token(token)
    import shutil
    import subprocess
    con = db()
    try:
        r = con.execute("SELECT path FROM photos WHERE id = ?", (int(photo_id),)).fetchone()
    finally:
        con.close()
    if not r:
        raise ToolError("No such photo")
    path = r[0]
    out = {"path": path}
    try:
        res = subprocess.run([*edits.exiftool_cmd(), "-j", "-G1", "-q", "-m", "-api", "LargeFileSupport=1", "-FileType", "-MIMEType", "-Warning", "-Error",
                              "-MajorBrand", "-CompatibleBrands", "-Duration", "-CompressorID", "-CompressorName", "-ImageWidth", "-ImageHeight",
                              "-AvgBitrate", "-VideoFrameRate", "-AudioFormat", "-HandlerType", "-ContentIdentifier", "-MediaDataSize", "-MovieDataSize", path],
                             capture_output=True, text=True, timeout=120)
        out["exiftool"] = json.loads(res.stdout or "[{}]")[0]
    except Exception as e:  # noqa: BLE001 - a diagnostic: report what failed
        out["exiftool"] = {"error": str(e)[:300]}
    ff = shutil.which("ffmpeg")
    if ff:
        res = subprocess.run([ff, "-v", "error", "-nostdin", "-t", "3", "-i", path, "-f", "null", "-"], capture_output=True, text=True, timeout=120)
        out["ffmpeg_decode"] = {"ok": res.returncode == 0 and not res.stderr.strip(), "errors": res.stderr.strip()[:1500]}
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64))
            tail = f.read(64)
        out["size"], out["head"], out["tail"] = size, head.hex(), tail.hex()
        boxes, pos = [], 0
        with open(path, "rb") as f:  # the top-level boxes of an MP4 / QuickTime file
            while pos < size and len(boxes) < 30:
                f.seek(pos)
                hd = f.read(16)
                if len(hd) < 8:
                    break
                n, kind = int.from_bytes(hd[:4], "big"), hd[4:8].decode("latin-1")
                if n == 1 and len(hd) >= 16:
                    n = int.from_bytes(hd[8:16], "big")
                elif n == 0:
                    n = size - pos
                boxes.append({"box": kind, "at": pos, "size": n})
                if n < 8:
                    break
                pos += n
        out["boxes"] = boxes
        out["complete"] = bool(boxes) and pos == size
    except OSError as e:
        out["read_error"] = str(e)
    return out


@server.tool(annotations=READ_ONLY)
def photo_file(token: str, photo_id: int) -> dict:
    """Checks one photo's files on disk: the image (exists, size, readable), its XMP sidecar, the
    thumbnail cache entry, and any backups the viewer made. For 'this photo does not show' issues."""
    _need_token(token)
    con = db()
    try:
        r = con.execute("SELECT id, path, name, size, hash, modified, video FROM photos WHERE id = ?", (photo_id,)).fetchone()
    finally:
        con.close()
    if not r:
        raise ToolError(f"No item with id {photo_id}")
    path = r["path"]
    out = {"id": r["id"], "path": path, "indexed_size": r["size"], "video": bool(r["video"])}
    try:
        st = os.stat(path)
        out.update(exists=True, size=st.st_size, readable=os.access(path, os.R_OK), writable=os.access(path, os.W_OK), size_matches=st.st_size == r["size"])
    except OSError as e:
        out.update(exists=False, error=str(e))
    side = path + ".xmp"
    out["sidecar"] = {"exists": os.path.exists(side), "writable_dir": os.access(os.path.dirname(path), os.W_OK)}
    try:
        tp = thumbs.cache_path("t", r["id"], thumbs.stamp_for(r["hash"], r["size"], r["modified"]))
        out["thumbnail_cached"] = tp.exists()
    except Exception:
        pass
    try:
        out["sidecar_backup"] = edits._backup_path(path).exists()
        out["file_backup"] = edits.file_backup_path(path).exists()
    except Exception:
        pass
    return out


@server.tool(annotations=READ_ONLY)
def check_for_update() -> dict:
    """Installed version and commit versus GitHub's main branch (network), and the outcome of the
    last update run (lastUpdate: ok, text, at)."""
    from app import __version__
    return {"version": __version__, **updates.check(), "lastUpdate": updates.last_result()}


@server.tool(annotations=READ_ONLY)
def consistency_report(token: str, field: str = "", offset: int = 0, limit: int = 500) -> dict:
    """The metadata consistency report (Maintenance › Metadata consistency): photos whose rating, title,
    caption or tags differ between digiKam's database, the XMP sidecar and the image file, with each
    source's value. field narrows it to one of rating|title|caption|tags. Needs the admin token."""
    _need_token(token)
    from app.photos import consistency
    return {"progress": dict(consistency.progress), "summary": consistency.summary(),
            **consistency.report(field or None, max(0, offset), max(1, min(limit, 2000)))}


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def edit_photos(token: str, photo_ids: list[int], taken: Optional[str] = None, caption: Optional[str] = None, title: Optional[str] = None,
                rating: Optional[int] = None, apply_to_database: bool = True) -> dict:
    """Change the date taken ("YYYY-MM-DDTHH:MM:SS"), caption, title or rating of these photos exactly as the web app
    does: written to each sidecar (and the file when the viewer writes into files), in the edit log with undo, and with
    apply_to_database written into digiKam's database right away (digiKam closed). Needs the admin token."""
    _need_token(token)
    import re
    from app.digikam import dbsync
    ch = {}
    if taken is not None:
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", taken):
            raise ToolError("taken must be YYYY-MM-DDTHH:MM:SS")
        ch["taken"] = taken
    for k, v in (("caption", caption), ("title", title)):
        if v is not None:
            ch[k] = v.strip()
    if rating is not None:
        ch["rating"] = max(0, min(5, int(rating)))
    if not ch:
        raise ToolError("Nothing to change")
    con = db()
    try:
        photos = [dict(r) for r in con.execute(f"SELECT id, path FROM photos WHERE id IN ({','.join(str(int(i)) for i in photo_ids) or 'NULL'})")]
    finally:
        con.close()
    res = edits.apply(photos, ch)
    if apply_to_database and res["edited"]:
        pend = edits.pending()
        try:
            res["database"] = dbsync.apply_pending(pending={p["id"]: pend[p["id"]] for p in photos if p["id"] in pend})
        except dbsync.SyncError as e:
            res["database"] = {"error": str(e)}
    return res


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def consistency_scan(token: str, rescan_all: bool = False) -> dict:
    """Start the metadata consistency check in the background, as Maintenance > Metadata consistency's buttons do:
    every photo (rescan_all) or those changed since they were last checked. consistency_report shows the progress and
    the results. Needs the admin token."""
    _need_token(token)
    from app.photos import consistency
    con = db()
    try:
        rows = [dict(r) for r in con.execute("SELECT id, path, name, video, hash, size, modified FROM photos ORDER BY ts DESC")]
    finally:
        con.close()
    consistency.scan(rows, rescan_all=bool(rescan_all))
    return {"queued": len(rows)}


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def consistency_sync(token: str, photo_ids: list[int], source: str, targets: list[str], fields: list[str]) -> dict:
    """Copy the given fields of the given photos from one source (db|sidecar|file) to the targets, exactly
    as the consistency page's Sync does: sidecars and files are backed up first; writing the database needs
    digiKam closed and is backed up too. Returns {synced, skipped}. Needs the admin token."""
    _need_token(token)
    from app.photos import consistency
    from app.digikam import dbsync
    try:
        return consistency.sync([int(i) for i in photo_ids][:20000], source, targets, fields)
    except (ValueError, dbsync.SyncError) as e:
        raise ToolError(str(e))


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def rewrite_sidecars(token: str, photo_ids: list[int]) -> dict:
    """Rewrite these photos' XMP sidecars (tag list and face regions) from digiKam's database, as the viewer does
    after a tag or face change: for when the database was changed outside the viewer. Each sidecar is backed up
    before its first change. Returns {photos, tags_failed, regions}. Needs the admin token."""
    _need_token(token)
    from app.digikam import dbsync
    from app.faces import facemgr
    from app.photos import edits
    ids = [int(i) for i in photo_ids][:5000]
    paths = dict(dbsync._photo_paths(ids))
    failed = edits.rewrite_tags([{"id": i, "path": p} for i, p in paths.items()])
    regions = facemgr.write_regions_many(list(paths))
    return {"photos": len(paths), "tags_failed": failed, "regions": {str(k): v for k, v in regions.items()}}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def face_quality(token: str) -> dict:
    """Face metadata quality (Maintenance > Face quality): regions of one person stored twice a few pixels apart
    (near), the same person in two separate places (separate), empty or out-of-photo rectangles (invalid), and
    the progress of the last clean-up. Needs the admin token."""
    _need_token(token)
    from app.faces import facequality
    return facequality.summary(limit=20)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
def face_cleanup(token: str, files: bool = False) -> dict:
    """Remove the near-duplicate face regions, exactly as Maintenance > Face quality > Remove duplicates: one region
    of each group is kept, digiKam's database is backed up first, those photos' sidecars are rewritten (not the
    image files) and each photo gets a change-log entry. files=True, once that has finished: the image files of
    the cleaned photos that still carry the duplicates are rewritten too (each backed up first, pixels checked
    unchanged). Runs in the background; face_quality shows progress. Needs the admin token."""
    _need_token(token)
    from app import config
    from app.digikam import dbsync
    from app.faces import facequality
    from app.data import state as appstate
    if not config.DB_WRITE or appstate.read_only():
        raise ToolError("Writing to digiKam's database is turned off")
    try:
        return {"photos": facequality.fix_files() if files else facequality.fix_near_duplicates()}
    except dbsync.SyncError as e:
        raise ToolError(str(e))


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def update_viewer(token: str, ref: str = "") -> dict:
    """Update the viewer to the latest commit on GitHub and restart it (update.sh: code, packages,
    service; settings and data are kept). Needs the admin token from Maintenance > Viewer version,
    because this endpoint has no sign-in. ref: the commit sha (or branch) to install; pass the sha
    you just pushed, since GitHub can report the previous commit for a short while after a push.
    Nothing happens when that commit is already installed. The connection drops when the viewer
    restarts; call check_for_update afterwards: lastUpdate carries the outcome and installed the
    commit. Not available in Docker (pull the image)."""
    if not updates.token_ok(token):
        raise ToolError("Wrong or missing admin token (Maintenance > Viewer version shows it to an admin)")
    try:
        updates.run(ref)
    except RuntimeError as e:
        raise ToolError(str(e))
    return {"started": True, "installed": updates.local_commit()[:7], "ref": ref or "main",
            "note": "update.sh is running; the viewer restarts when it finishes (about a minute). Then call check_for_update: lastUpdate says what happened."}


if __name__ == "__main__":
    server.run()
