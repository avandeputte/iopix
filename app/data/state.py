"""The app's own writable state: favorites, app-side albums, settings, users, the
pending-edit and tag-manager queues, and (through analysis.py and consistency.py,
which share the database) the analysis results.

Everything lives in the viewer's own database (vdb.py: the SQLite file data/viewer.db
by default, or MySQL/MariaDB), never in digiKam's database. Photos are referenced by
digiKam image id; the file hash is stored alongside so entries could be re-matched if
digiKam ever renumbers its images. Installs from before v1.8 had two SQLite files,
state.db and analysis.db: migrate_legacy() folds them into viewer.db once, keeping
every row, and parks the old files in db-backups.
"""
import contextvars
import logging
import shutil
import sqlite3
from urllib.parse import quote
import threading
import time

from app import config
from app.data import vdb
from app.digikam.dkdb import in_chunks

STATE_DB = config.DATA_DIR / "viewer.db"  # the SQLite file (vdb.SQLITE_FILE); the legacy migration's target
LEGACY = (config.DATA_DIR / "state.db", config.DATA_DIR / "analysis.db")
rev = 0  # bumped on every write so cached query results can be invalidated
lock = threading.Lock()
_settings = (-1, {})  # (stamp it was read at, {key: value}); settings are read on every poll and every POST
log = logging.getLogger("app.data.state")

# Every key of the app_settings table: {key: (default, type, what it is)}. Values are stored as text; the type
# says how the readers interpret it (a flag is "0"/"1"). get_setting(key) with no default answers the default
# here, so a reader and the Settings page cannot disagree about it. A key that is not here is a programming
# error: it is logged once, and the read or write goes ahead (the tests use probe keys on purpose).
SETTINGS = {
    "app_name": ("", str, "what the viewer calls itself (Settings › Name); empty: the app's own name"),
    "read_only": ("0", bool, "nothing in the library may be changed (READ_ONLY=1 in the environment locks it on)"),
    "require_login": ("0", bool, "visitors must sign in before they see anything"),
    "write_files": ("0", bool, "metadata edits go into the image files too, not only the sidecars"),
    "sidecar_compat": ("0", bool, "new sidecars are named photo.xmp, digiKam's compatible file name (SIDECAR_COMPAT=1 locks it on)"),
    "digikam_sync": ("1", bool, "the viewer reads digiKam's database and writes applied edits into it; off: the library tables"),
    "digikam_closed_until": ("0", float, "epoch seconds until which the admin vouched that digiKam is closed (dbsync writes without checking)"),
    "file_scanner": ("0", bool, "watch the album folders for files added, changed or removed outside the viewer (on by default with no digiKam)"),
    "scan_interval": ("15", int, "minutes between file scanner passes"),
    "mcp_enabled": ("0", bool, "the MCP endpoint answers (MCP=1 in the environment locks it on); stored once at first start"),
    "mcp_admin_token": ("", str, "the generated token MCP clients use for admin actions, unless MCP_ADMIN_TOKEN is set"),
    "update_check": ("1", bool, "look at GitHub daily for a newer version (UPDATE_CHECK=0 locks it off)"),
    "bg_threads": ("", int, "cores the background jobs may use; empty: BG_THREADS from the environment, half the machine by default"),
    "search_model": ("clip", str, "the embedding model behind search (clip, siglip2, jinaclip); runtime reads it with default '' to see whether one was chosen"),
    "face_detector": ("", str, "model key for face detection; empty: the registry's default (app/ai/models.py CHOICES)"),
    "object_model": ("", str, "model key for object detection; empty: the registry's default"),
    "caption_model": ("", str, "model key for captions; empty: the registry's default"),
    "face_vectors": ("", str, "model key for face vectors; empty: the registry's default"),
    "speech_model": ("", str, "model key for speech in videos; empty: the registry's default"),
    "face_vectors_built": ("sface", str, "the face vector model the stored vectors were made with (a change rebuilds them)"),
    "near_dups_level": ("normal", str, "how alike photos must be to count as near duplicates (strict, normal, loose)"),
    "near_dups_gen": ("", str, "the generation the near-duplicate groups were computed for; empty: compute again"),
    "import_layout": ("", str, "the folder layout imports go into (importer.LAYOUTS); empty: the first one"),
    "map_provider": ("osm", str, "osm or google: who draws the map and the Street View links"),
    "google_maps_key": ("", str, "the browser-facing Google key (GOOGLE_MAPS_API_KEY in the environment otherwise)"),
    "google_server_key": ("", str, "the Google key for the APIs this server calls (GOOGLE_SERVER_KEY otherwise; else the browser key)"),
    "album_roots": ("", "json", "{album root id: local path}: where digiKam's album roots are mounted here, when not at their recorded paths"),
    "templates": ("[]", "json", "the edit templates (owner name and contact) the Edit panel offers"),
    "recent_tags": ("[]", "json", "the last tags used in the Edit panel, newest first (at most 12)"),
    # digiKam's database, overriding the DIGIKAM_DB_* environment (dkdb.settings()): one key per dkdb.KEYS
    "db_type": ("", str, "sqlite, mysql or none; empty: DIGIKAM_DB_TYPE"),
    "db_sqlite_dir": ("", str, "the folder with digikam4.db; empty: DIGIKAM_DB_DIR"),
    "db_host": ("", str, "MySQL host; empty: DIGIKAM_DB_HOST"),
    "db_port": ("", str, "MySQL port; empty: DIGIKAM_DB_PORT"),
    "db_database": ("", str, "MySQL database name; empty: DIGIKAM_DB_NAME"),
    "db_user": ("", str, "MySQL user; empty: DIGIKAM_DB_USER"),
    "db_password": ("", str, "MySQL password; empty: DIGIKAM_DB_PASSWORD"),
    "db_backup": ("", str, "auto or skip; empty: DIGIKAM_DB_BACKUP"),
    # sign-in at a reverse proxy (proxyauth.KEYS, lower-cased): the environment wins when set there
    "auth_proxy": ("", str, "off, or a preset (cloudflare, nginx, ...): the proxy whose identity header is trusted"),
    "auth_proxy_header": ("", str, "the header that carries the signed-in user, when not the preset's"),
    "auth_proxy_trusted": ("", str, "the proxy addresses (CIDRs) whose identity headers are believed"),
    "auth_proxy_create": ("", str, "someone with no account: the sign-in screen (empty), deny, or a viewer or editor account"),
    "cf_access_team": ("", str, "the Cloudflare Access team domain whose tokens are verified"),
    "cf_access_aud": ("", str, "the Cloudflare Access application's AUD tag"),
}
_unknown_keys = set()  # logged once each


def _known(key: str):
    if key not in SETTINGS and key not in _unknown_keys:
        _unknown_keys.add(key)
        log.warning("setting %r is not in state.SETTINGS (add it there with its default)", key)


def migrate_legacy():
    """state.db + analysis.db -> viewer.db, once. Returns where the old files were parked, or None.
    Installs from before v1.8 (2024); remove after v1.120, with LEGACY and the module docstring's last sentence."""
    if STATE_DB.exists() or not any(p.exists() for p in LEGACY):
        return None
    old_state, old_an = LEGACY
    tmp = STATE_DB.with_name(STATE_DB.name + ".migrating")
    tmp.unlink(missing_ok=True)
    if old_state.exists():  # the state file is the base: same tables, same ids
        src = sqlite3.connect(f"file:{quote(str(old_state))}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    con = sqlite3.connect(tmp)
    try:
        if old_an.exists():  # the analysis tables are copied over, schema and rows (no name overlaps)
            con.execute("ATTACH DATABASE ? AS a", (str(old_an),))
            tables = con.execute("SELECT name, sql FROM a.sqlite_master WHERE type = 'table' AND sql IS NOT NULL").fetchall()
            for name, sql in tables:
                con.execute(sql)
                con.execute(f'INSERT INTO main."{name}" SELECT * FROM a."{name}"')
            for (sql,) in con.execute("SELECT sql FROM a.sqlite_master WHERE type = 'index' AND sql IS NOT NULL").fetchall():
                con.execute(sql)
            con.commit()
            con.execute("DETACH DATABASE a")
        con.execute("PRAGMA journal_mode = WAL")
        con.commit()
    finally:
        con.close()
    tmp.replace(STATE_DB)
    park = config.DATA_DIR / "db-backups" / ("migrated-" + time.strftime("%Y%m%d-%H%M%S"))
    park.mkdir(parents=True, exist_ok=True)
    for f in LEGACY:
        for g in (f, f.with_name(f.name + "-wal"), f.with_name(f.name + "-shm"), f.with_name(f.name + "-journal")):
            if g.exists():
                shutil.move(str(g), str(park / g.name))
    return park


def connect():
    """The viewer database, SQLite or MySQL as configured (see vdb.py); rows answer to names and indexes."""
    return vdb.connect(timeout=10)


def init():
    park = migrate_legacy()
    if park:
        logging.getLogger("app.data.state").info("state.db and analysis.db were folded into viewer.db; the old files are in %s", park)
    vdb.init()  # the tables of every module, on whichever database is configured


def write(fn):
    global rev
    with lock, vdb.write_lock:  # one writer in the process, the jobs included: a request never waits out SQLite's busy timeout
        con = connect()
        try:
            out = fn(con)
            con.commit()
        finally:
            con.close()
        rev += 1
        return out


# who is asking, set per request by the server (a user_accounts row, or None for visitors, MCP and jobs): private
# albums are seen only by their owner, so every album read below goes through _visible()
viewer = contextvars.ContextVar("album_viewer", default=None)


def _uid(user=...):
    u = viewer.get() if user is ... else user
    return int(u["id"]) if u and u.get("id") is not None else None


def _visible(user=..., alias: str = "") -> tuple:
    """SQL condition (and its parameters) for the albums this user may see: shared ones, and their own private ones."""
    a, uid = (alias + ".") if alias else "", _uid(user)
    if uid is None:
        return f"{a}is_shared = 1", []
    return f"({a}is_shared = 1 OR {a}owner_id = ?)", [uid]


def summary(user=...) -> dict:
    vis, vp = _visible(user, "a")
    con = connect()
    try:
        return {
            "favorites": [r[0] for r in con.execute("SELECT photo_id FROM favorite_photos ORDER BY added DESC")],
            "albums": [dict(r) for r in con.execute(
                "SELECT a.id, a.name, a.created, a.owner_id, a.is_shared, count(i.photo_id) AS n, "
                "(SELECT photo_id FROM viewer_album_photos WHERE album_id = a.id ORDER BY added, photo_id LIMIT 1) AS cover "
                f"FROM viewer_albums a LEFT JOIN viewer_album_photos i ON i.album_id = a.id WHERE {vis} "
                "GROUP BY a.id, a.name, a.created, a.owner_id, a.is_shared ORDER BY a.created DESC", vp)],
            # smart albums: a saved filter query string, listed with the albums and evaluated live
            "searches": [dict(r) for r in con.execute("SELECT id, name, query, created FROM saved_searches ORDER BY created DESC")],
        }
    finally:
        con.close()


def set_favorites(items: list, on: bool):
    """items: [(photo_id, hash)]"""
    now = int(time.time())
    if on:
        write(lambda c: c.executemany("INSERT OR IGNORE INTO favorite_photos VALUES (?,?,?)", [(i, h, now) for i, h in items]))
    else:
        write(lambda c: c.executemany("DELETE FROM favorite_photos WHERE photo_id = ?", [(i,) for i, _ in items]))


# ---- People groups (the People page's sections; digiKam is not touched)

def people_groups() -> list:
    """[{id, name, members: [tag ids in order]}] in the order chosen."""
    con = connect()
    try:
        groups = [{"id": r[0], "name": r[1], "members": []} for r in con.execute("SELECT id, name FROM people_groups ORDER BY position, id")]
        by = {g["id"]: g for g in groups}
        for gid, tid in con.execute("SELECT group_id, tag_id FROM people_group_members ORDER BY group_id, position, tag_id"):
            if gid in by:
                by[gid]["members"].append(tid)
        return groups
    finally:
        con.close()


def create_people_group(name: str) -> int:
    def go(c):
        pos = (c.execute("SELECT max(position) FROM people_groups").fetchone()[0] or 0) + 1
        return c.execute("INSERT INTO people_groups (name, position, created) VALUES (?,?,?)", (name, pos, int(time.time()))).lastrowid
    return write(go)


def update_people_group(group_id: int, name: str = None, members: list = None) -> bool:
    """Rename a group and/or set its members (tag ids, in order; replaces the list). False if there is no such group."""
    def go(c):
        if not c.execute("SELECT 1 FROM people_groups WHERE id = ?", (group_id,)).fetchone():
            return False
        if name is not None:
            c.execute("UPDATE people_groups SET name = ? WHERE id = ?", (name, group_id))
        if members is not None:
            c.execute("DELETE FROM people_group_members WHERE group_id = ?", (group_id,))
            seen = list(dict.fromkeys(int(t) for t in members))
            c.executemany("INSERT INTO people_group_members (group_id, tag_id, position) VALUES (?,?,?)", [(group_id, t, k) for k, t in enumerate(seen)])
        return True
    return write(go)


def delete_people_group(group_id: int):
    def go(c):
        c.execute("DELETE FROM people_group_members WHERE group_id = ?", (group_id,))
        c.execute("DELETE FROM people_groups WHERE id = ?", (group_id,))
    write(go)


def order_people_groups(ids: list):
    write(lambda c: c.executemany("UPDATE people_groups SET position = ? WHERE id = ?", [(k, int(i)) for k, i in enumerate(ids)]))


def create_album(name: str, owner_id: int = None, shared: bool = True) -> int:
    return write(lambda c: c.execute("INSERT INTO viewer_albums (name, created, owner_id, is_shared) VALUES (?,?,?,?)",
                                      (name, int(time.time()), owner_id, 1 if shared else 0)).lastrowid)


def album(album_id: int) -> dict:
    """{id, name, owner_id, is_shared}, or None; no visibility check (callers decide what the user may do)."""
    con = connect()
    try:
        r = con.execute("SELECT id, name, owner_id, is_shared FROM viewer_albums WHERE id = ?", (int(album_id),)).fetchone()
        return {"id": r[0], "name": r[1], "owner_id": r[2], "is_shared": bool(r[3])} if r else None
    finally:
        con.close()


def set_album_sharing(album_id: int, shared: bool, owner_id: int = None):
    """Shared: everyone sees it. Private: only owner_id does (the user who made it private)."""
    write(lambda c: c.execute("UPDATE viewer_albums SET is_shared = ?, owner_id = COALESCE(?, owner_id) WHERE id = ?",
                               (1 if shared else 0, owner_id, int(album_id))))


def rename_album(album_id: int, name: str):
    write(lambda c: c.execute("UPDATE viewer_albums SET name = ? WHERE id = ?", (name, album_id)))


def delete_album(album_id: int):
    def run(c):
        c.execute("DELETE FROM viewer_album_photos WHERE album_id = ?", (album_id,))
        c.execute("DELETE FROM viewer_albums WHERE id = ?", (album_id,))
    write(run)


def create_search(name: str, query: str) -> int:
    return write(lambda c: c.execute("INSERT INTO saved_searches (name, query, created) VALUES (?,?,?)", (name, query, int(time.time()))).lastrowid)


def rename_search(search_id: int, name: str):
    write(lambda c: c.execute("UPDATE saved_searches SET name = ? WHERE id = ?", (name, search_id)))


def delete_search(search_id: int):
    write(lambda c: c.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,)))


def set_album_items(album_id: int, items: list, on: bool):
    now = int(time.time())
    if on:
        write(lambda c: c.executemany("INSERT OR IGNORE INTO viewer_album_photos VALUES (?,?,?,?)",
                                       [(album_id, i, h, now) for i, h in items]))
    else:
        write(lambda c: c.executemany("DELETE FROM viewer_album_photos WHERE album_id = ? AND photo_id = ?",
                                       [(album_id, i) for i, _ in items]))


def _settings_stamp() -> tuple:
    """Changes on every write: rev for this process, and for writes by another one (the stdio
    MCP server shares this database) the file's mtime on SQLite, or a few-second clock on MySQL,
    where no cheap change marker exists."""
    if vdb.is_mysql():
        return rev, int(time.time() / 5)
    try:
        stamp = STATE_DB.stat().st_mtime_ns
    except OSError:
        return rev, None
    try:  # WAL mode: a commit lands in the -wal file and moves the main file only at a checkpoint
        stamp = max(stamp, STATE_DB.with_name(STATE_DB.name + "-wal").stat().st_mtime_ns)
    except OSError:
        pass
    return rev, stamp


def get_setting(key: str, default: str = None) -> str:
    """Settings are tiny and read on every status poll and every POST, so the whole
    table is cached and re-read only after a write. No default: SETTINGS' own."""
    global _settings
    _known(key)
    if default is None:
        default = SETTINGS[key][0] if key in SETTINGS else ""
    stamp, values = _settings
    now = _settings_stamp()  # taken before the query: a write racing with it makes the next call reload
    if stamp != now:
        con = connect()
        try:
            values = dict(con.execute("SELECT key, value FROM app_settings").fetchall())
        except Exception:
            return default
        finally:
            con.close()
        _settings = (now, values)
    return values.get(key, default)


def read_only() -> bool:
    """Nothing in the library may be changed: READ_ONLY=1 in the environment (locked) or the read_only setting."""
    return config.READ_ONLY or get_setting("read_only", "0") == "1"


def set_setting(key: str, value: str):
    _known(key)
    write(lambda c: c.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)", (key, value)))


# ---- what the index queries used to read through an attached copy of this database
def favorite_ids() -> list:
    con = connect()
    try:
        return [r[0] for r in con.execute("SELECT photo_id FROM favorite_photos")]
    finally:
        con.close()


def album_item_ids(album_ids: list) -> list:
    """The photos in these albums, counting only the albums the current user may see."""
    if not album_ids:
        return []
    vis, vp = _visible(alias="a")
    con = connect()
    try:
        return [r[0] for r in in_chunks(con, "SELECT i.photo_id FROM viewer_album_photos i JOIN viewer_albums a ON a.id = i.album_id "
                                             f"WHERE i.album_id IN ({{ids}}) AND {vis}", map(int, album_ids), params_after=tuple(vp))]
    finally:
        con.close()


def albums_of(photo_id: int) -> list:
    vis, vp = _visible(alias="a")
    con = connect()
    try:
        return [r[0] for r in con.execute(
            f"SELECT i.album_id FROM viewer_album_photos i JOIN viewer_albums a ON a.id = i.album_id WHERE i.photo_id = ? AND {vis}", [photo_id] + vp)]
    finally:
        con.close()


def is_favorite(photo_id: int) -> bool:
    con = connect()
    try:
        return con.execute("SELECT 1 FROM favorite_photos WHERE photo_id = ?", (photo_id,)).fetchone() is not None
    finally:
        con.close()


def album_id_by_name(name_or_id) -> int:
    vis, vp = _visible()
    con = connect()
    try:
        r = con.execute(f"SELECT id FROM viewer_albums WHERE (lower(name) = lower(?) OR id = ?) AND {vis}", [str(name_or_id), str(name_or_id)] + vp).fetchone()
        return r[0] if r else None
    finally:
        con.close()
