"""The MCP server instance and what every tool module shares.

A tool answers from the search index (`db()`), the same SQLite file the web app queries; the
viewer's own data (favorites, albums, settings) is read through app.data.state. Tools that
reveal paths or change the library take the admin token (`need_token`, or the request's bearer header), checked here rather
than by the transport so the stdio server applies the same rule as the HTTP endpoint.
"""
import contextvars
import re
import sqlite3
from typing import Optional
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app import config, users
from app.auth import updater
from app.photos import edits
from app.data import index, state as appstate
from app.data.query import DATE_RE, build_where
from app.ai.semantic import semantic, enabled as semantic_enabled

VIEWER = config.VIEWER_URL  # for the links in results
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# a tool that changes the library but can be repeated with the same outcome (an edit, a setting, a job start)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PICKS = ["none", "rejected", "pending", "accepted"]
MONTH_DAY_RE = re.compile(r"^\d\d-\d\d$")
# how many visually ranked candidates a content search considers: enough for a page or two of
# results, few enough that the ranking dict and the IN (...) clause stay small
CONTENT_SEARCH_CANDIDATES = 200
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

token_given = contextvars.ContextVar("mcp_token_given", default=False)  # set per request by the HTTP layer


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


def list_rows(sql: str, params=()) -> list:
    """The rows of one query against the index, as dicts (what the list_* tools return)."""
    con = db()
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def clamp_limit(limit, cap: int) -> int:
    """A page size a client asked for, kept between 1 and `cap` (each tool's cap is the most its answer
    stays readable at; a client that wants more pages with offset)."""
    return max(1, min(int(limit), cap))


# ---- the admin token: paths, log lines, settings and every change need it

def request_has_token() -> bool:
    return token_given.get()


def anon_paths_ok() -> bool:
    """File paths in answers: only for a request that carried the admin token (checked by the HTTP layer when sign-in
    is required), or on an install with no accounts at all."""
    return not users.any_users() or request_has_token()


def need_token(token: str, writes: bool = False):
    """The operator and maintenance tools take the admin token (Maintenance > Updates and logs) as their `token`
    argument. Over HTTP every /mcp request also carries it as a bearer header (main.py refuses the rest), so the
    argument is checked twice there; over stdio (`python -m app.mcp`) it is the only check. writes: the tool changes
    the library, so read-only mode (READ_ONLY=1 or the setting) refuses it as it refuses the web app's edits."""
    if not updater.token_ok(token):
        raise ToolError("This tool needs the admin token (Maintenance > Updates and logs > MCP update token)")
    if writes and appstate.read_only():
        raise ToolError("The viewer is in read-only mode; nothing in the library can be changed")


# ---- names to ids

def resolve_tag(con, name: str, person: Optional[bool]) -> int:
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


def resolve_album(con, name: str) -> int:
    """An album id from an id, an exact path or name, or a unique partial match of the path."""
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


# ---- search filters: tool arguments to the web app's query parameters
# The SQL itself is app.data.query's (build_where), the same filters the web UI's URLs carry, so a
# tool and the web app agree on what "tag", "from" or "bbox" matches. What stays here is the
# translation: the tool's argument names are descriptive (date_from, min_rating, photographer)
# where the query string's are terse (from, rating, creator), names are resolved to ids first, and
# the shapes build_where ignores silently (a malformed date, a 5-number bbox) are refused aloud,
# since a client cannot see that its filter was dropped.
SCALAR_PARAMS = (("date_from", "from"), ("date_to", "to"), ("year", "year"), ("month", "month"),
                 ("camera", "camera"), ("lens", "lens"), ("min_rating", "rating"), ("file_format", "fmt"),
                 ("photographer", "creator"), ("month_day", "md"),
                 ("place", "place"), ("region", "region"), ("country", "country"),
                 ("iso_min", "iso_min"), ("iso_max", "iso_max"), ("aperture_min", "ap_min"),
                 ("aperture_max", "ap_max"), ("focal_min", "fl_min"), ("focal_max", "fl_max"))

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


def filter_items(con, f: dict) -> list:
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
        add("tag", resolve_tag(con, name, True))
    for name in f.get("tags") or []:
        add("tag", resolve_tag(con, name, None))
    for name in f.get("exclude_tags") or []:
        add("nottag", resolve_tag(con, name, None))
    if f.get("album"):
        add("album", resolve_album(con, f["album"]))
    for key, param in SCALAR_PARAMS:
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


def where_for(con, f: dict):
    """SQL for the filters, plus a ranking when searching by content or similarity."""
    items, rank = filter_items(con, f), None
    if f.get("looks_like") or f.get("similar_to"):
        if not semantic_enabled():
            raise ToolError("Content search is not enabled for this library (run enable-search.sh in the app folder).")
        ids = (semantic.search_text(f["looks_like"], CONTENT_SEARCH_CANDIDATES) if f.get("looks_like")
               else semantic.similar(int(f["similar_to"]), CONTENT_SEARCH_CANDIDATES))
        items.append(("ids", ",".join(map(str, ids)) or "0"))
        rank = {pid: i for i, pid in enumerate(ids)}
    where, params = build_where(items)
    return where, params, rank
