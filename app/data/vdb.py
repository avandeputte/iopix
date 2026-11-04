"""The viewer's own database: MariaDB (or MySQL), and nothing else since v1.113.

Every module that keeps viewer data (state, users, edits, tagmanager, analysis, consistency, the library
copy) gets its connection here. The code is written in the SQLite dialect it grew up in (? placeholders,
INSERT OR REPLACE / OR IGNORE, COLLATE NOCASE, the `key` column); the wrapper translates it for MySQL and
hands back rows that answer to both an index and a column name. Keeping the translation rather than
rewriting a thousand statements is deliberate: the digiKam side (dkdb.py) still speaks both dialects, and
the statements stay readable.

Where the connection goes is kept outside the database itself, in data/viewer-db.json (or VIEWER_DB_* in
the environment), since the settings table lives inside. Without a configuration, or with one the server
refuses, the viewer starts in its "database needed" state (main.py): the Settings page asks for the
details, tests them, and the viewer restarts on them. An install from before v1.113 still has its SQLite
file (data/viewer.db): the first switch copies every row out of it, once, and the file is renamed. That
read-only import is the only SQLite code left here; the search index (index.db) is a rebuilt cache and
stays a file.
"""
import json
import logging
import os
import re
import shutil
import sqlite3  # only to read a viewer.db from before v1.112 once (import_sqlite)
import subprocess
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from app import config
from app.digikam import library as _library
from app.digikam.dkdb import Conn, Cursor, norm_plain

log = logging.getLogger("app.data.vdb")
CONF = config.DATA_DIR / "viewer-db.json"
LEGACY_SQLITE = config.DATA_DIR / "viewer.db"  # the file of an install from before v1.112: imported at the first switch
KEYS = ("type", "host", "port", "database", "user", "password")
_cfg = None
_lock = threading.Lock()
error = None  # why the configured database cannot be used right now (None when it answers)
_KEY_COL = re.compile(r"(?<![\w`.])key(?![\w`(])")  # the settings column, a reserved word on MySQL

try:
    import pymysql
    Error = (pymysql.err.Error, sqlite3.Error)
except ImportError:  # pragma: no cover
    pymysql = None
    Error = (sqlite3.Error,)

# ---- schema: one definition per table, the MySQL spelling; columns in copy order
TABLES = {
    "app_settings": (["key", "value"],
                 "CREATE TABLE IF NOT EXISTS app_settings (`key` VARCHAR(191) PRIMARY KEY, value MEDIUMTEXT)"),
    "favorite_photos": (["photo_id", "hash", "added"],
                  "CREATE TABLE IF NOT EXISTS favorite_photos (photo_id BIGINT PRIMARY KEY, hash VARCHAR(64), added BIGINT)"),
    # is_shared 1: everyone sees it, editors and admins change it; 0: only owner_id (a user_accounts id) sees and changes it
    "viewer_albums": (["id", "name", "created", "owner_id", "is_shared"],
                "CREATE TABLE IF NOT EXISTS viewer_albums (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255) NOT NULL, created BIGINT, owner_id BIGINT NULL, is_shared INT NOT NULL DEFAULT 1)"),
    "viewer_album_photos": (["album_id", "photo_id", "hash", "added"],
                     "CREATE TABLE IF NOT EXISTS viewer_album_photos (album_id INT, photo_id BIGINT, hash VARCHAR(64), added BIGINT, PRIMARY KEY (album_id, photo_id))"),
    "saved_searches": (["id", "name", "query", "created"],
                 "CREATE TABLE IF NOT EXISTS saved_searches (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255) NOT NULL, query TEXT NOT NULL, created BIGINT)"),
    "pending_edits": (["photo_id", "data", "ts"],
              "CREATE TABLE IF NOT EXISTS pending_edits (photo_id BIGINT PRIMARY KEY, data MEDIUMTEXT, ts BIGINT)"),
    "tag_rewrite_queue": (["id", "op", "path", "image_id"],
                "CREATE TABLE IF NOT EXISTS tag_rewrite_queue (id INT AUTO_INCREMENT PRIMARY KEY, op VARCHAR(64), path TEXT, image_id BIGINT)"),
    # ---- AI results: one row per photo and job in ai_scans says which version of the file (stamp) each job
    # last looked at (detail: a small JSON note, e.g. face detection's counts); the result tables hold data only
    "ai_scans": (["photo_id", "job", "stamp", "detail"],
                 "CREATE TABLE IF NOT EXISTS ai_scans (photo_id BIGINT, job VARCHAR(32), stamp VARCHAR(191), detail TEXT, PRIMARY KEY (photo_id, job), INDEX ai_scans_job (job))"),
    "ai_quality": (["photo_id", "sharp", "bright", "clip_lo", "clip_hi"],
                   "CREATE TABLE IF NOT EXISTS ai_quality (photo_id BIGINT PRIMARY KEY, sharp DOUBLE, bright DOUBLE, clip_lo DOUBLE, clip_hi DOUBLE)"),
    "ai_text": (["photo_id", "text"],
                "CREATE TABLE IF NOT EXISTS ai_text (photo_id BIGINT PRIMARY KEY, text MEDIUMTEXT)"),
    "ai_face_count": (["photo_id", "nfaces", "faceq"],
                      "CREATE TABLE IF NOT EXISTS ai_face_count (photo_id BIGINT PRIMARY KEY, nfaces INT, faceq DOUBLE)"),
    "ai_objects": (["photo_id", "cls", "score", "x", "y", "w", "h"],
                "CREATE TABLE IF NOT EXISTS ai_objects (photo_id BIGINT, cls VARCHAR(64), score DOUBLE, x DOUBLE, y DOUBLE, w DOUBLE, h DOUBLE, INDEX ai_objects_photo (photo_id))"),
    "ai_descriptions": (["photo_id", "text"],
                        "CREATE TABLE IF NOT EXISTS ai_descriptions (photo_id BIGINT PRIMARY KEY, text TEXT)"),
    "ai_looks": (["photo_id", "aesthetic", "focus", "eyes_checked", "eyes_closed"],
                 "CREATE TABLE IF NOT EXISTS ai_looks (photo_id BIGINT PRIMARY KEY, aesthetic DOUBLE, focus DOUBLE, eyes_checked INT, eyes_closed INT)"),
    "ai_face_ages": (["photo_id", "x", "y", "w", "h", "age"],
                     "CREATE TABLE IF NOT EXISTS ai_face_ages (photo_id BIGINT, x INT, y INT, w INT, h INT, age DOUBLE, PRIMARY KEY (photo_id, x, y, w, h))"),
    "person_births": (["tag_id", "birth_year", "ts"],
                      "CREATE TABLE IF NOT EXISTS person_births (tag_id BIGINT PRIMARY KEY, birth_year INT, ts BIGINT)"),
    "ai_face_embeddings": (["photo_id", "x", "y", "w", "h", "vec"],
                  "CREATE TABLE IF NOT EXISTS ai_face_embeddings (photo_id BIGINT, x INT, y INT, w INT, h INT, vec BLOB, PRIMARY KEY (photo_id, x, y, w, h))"),
    "ai_person_references": (["tag_id", "photo_id", "x", "y", "w", "h", "vec"],
                    "CREATE TABLE IF NOT EXISTS ai_person_references (tag_id BIGINT, photo_id BIGINT, x INT, y INT, w INT, h INT, vec BLOB, PRIMARY KEY (photo_id, x, y, w, h))"),
    # undo_values, not "undo": UNDO is a reserved word on MariaDB and MySQL
    # file_copy: the backed-up image file (data/file-backups, the original from before the viewer first wrote into it)
    "photo_history": (["id", "photo_id", "ts", "user_name", "action", "summary", "details", "undo_values", "sidecar_copy", "path", "file_copy"],
                      "CREATE TABLE IF NOT EXISTS photo_history (id BIGINT AUTO_INCREMENT PRIMARY KEY, photo_id BIGINT, ts BIGINT, user_name VARCHAR(191), action VARCHAR(32), summary TEXT, details MEDIUMTEXT, undo_values MEDIUMTEXT, sidecar_copy TEXT, path TEXT, file_copy TEXT, INDEX photo_history_photo (photo_id))"),
    "image_data_checksums": (["photo_id", "hash", "algorithm", "verified"],
                             "CREATE TABLE IF NOT EXISTS image_data_checksums (photo_id BIGINT PRIMARY KEY, hash VARCHAR(128), algorithm VARCHAR(16), verified BIGINT)"),
    "ai_jerseys": (["photo_id", "px", "py", "pw", "ph", "number", "score", "hue", "sat", "val"],
                   "CREATE TABLE IF NOT EXISTS ai_jerseys (photo_id BIGINT, px INT, py INT, pw INT, ph INT, number VARCHAR(8), score DOUBLE, hue INT, sat INT, val INT, PRIMARY KEY (photo_id, px, py, pw, ph))"),
    "ai_person_looks": (["photo_id", "px", "py", "pw", "ph", "vec"],
                        "CREATE TABLE IF NOT EXISTS ai_person_looks (photo_id BIGINT, px INT, py INT, pw INT, ph INT, vec BLOB, PRIMARY KEY (photo_id, px, py, pw, ph))"),
    "ai_rotation": (["photo_id", "turn", "score", "stamp"],
                    "CREATE TABLE IF NOT EXISTS ai_rotation (photo_id BIGINT PRIMARY KEY, turn INT, score DOUBLE, stamp VARCHAR(191))"),
    "video_info": (["photo_id", "stamp", "cover_t", "chapters", "transcript", "segments", "lang", "speech_model", "ts"],
                   "CREATE TABLE IF NOT EXISTS video_info (photo_id BIGINT PRIMARY KEY, stamp VARCHAR(191), cover_t DOUBLE, chapters TEXT, transcript MEDIUMTEXT, segments MEDIUMTEXT, lang VARCHAR(16), speech_model VARCHAR(64), ts BIGINT)"),
    "suggestion_dismissed": (["kind", "photo_id", "tag_id", "user_name", "ts"],
                             "CREATE TABLE IF NOT EXISTS suggestion_dismissed (kind VARCHAR(16), photo_id BIGINT, tag_id BIGINT, user_name VARCHAR(191), ts BIGINT, PRIMARY KEY (kind, photo_id, tag_id))"),
    "date_hints": (["album_id", "not_before", "not_after", "user_name", "ts"],
                   "CREATE TABLE IF NOT EXISTS date_hints (album_id BIGINT PRIMARY KEY, not_before INT, not_after INT, user_name VARCHAR(191), ts BIGINT)"),
    "face_sidecar_queue": (["image_id", "ts"],
                           "CREATE TABLE IF NOT EXISTS face_sidecar_queue (image_id BIGINT PRIMARY KEY, ts BIGINT)"),
    "ai_similar_photos": (["a", "b", "score"],
                  "CREATE TABLE IF NOT EXISTS ai_similar_photos (a BIGINT, b BIGINT, score DOUBLE, INDEX ai_similar_photos_a (a))"),
    # ---- the library itself (library.py): digiKam's columns under names that say what they hold
    "library_info": (["key", "value"],
                     "CREATE TABLE IF NOT EXISTS library_info (`key` VARCHAR(191) PRIMARY KEY, value MEDIUMTEXT)"),
    **_library.tables(),
    # the viewer's own identity for a photo: digiKam's id is the working key everywhere, this row remembers
    # which file it stood for (digiKam's uniqueHash + size), so viewer data can follow a photo if digiKam renumbers
    "photo_identity": (["vid", "dk_id", "hash", "size", "path", "name", "first_seen", "last_seen"],
                  "CREATE TABLE IF NOT EXISTS photo_identity (vid INT AUTO_INCREMENT PRIMARY KEY, dk_id BIGINT, hash VARCHAR(64), size BIGINT, path TEXT, name VARCHAR(255), first_seen BIGINT, last_seen BIGINT, "
                  "INDEX photo_identity_dk (dk_id), INDEX photo_identity_hash (hash, size))"),
    # a Face quality finding the owner accepted as it is (check_name, not "check": a reserved word on MariaDB);
    # signature: what the photo's faces were then, so a photo whose faces change is looked at again
    "quality_accepted": (["photo_id", "check_name", "signature", "user_name", "ts"],
                         "CREATE TABLE IF NOT EXISTS quality_accepted (photo_id BIGINT, check_name VARCHAR(32), signature VARCHAR(191), user_name VARCHAR(191), ts BIGINT, PRIMARY KEY (photo_id, check_name))"),
    # the People page's own groups (Family, Friends, Classmates...): digiKam's person tags (tag ids), in the order
    # chosen; a person may be in several groups. Nothing here is written to digiKam.
    "people_groups": (["id", "name", "position", "created"],
                      "CREATE TABLE IF NOT EXISTS people_groups (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(191) NOT NULL, position INT NOT NULL DEFAULT 0, created BIGINT)"),
    "people_group_members": (["group_id", "tag_id", "position"],
                             "CREATE TABLE IF NOT EXISTS people_group_members (group_id INT, tag_id BIGINT, position INT NOT NULL DEFAULT 0, PRIMARY KEY (group_id, tag_id))"),
    "metadata_check": (["photo_id", "stamp", "data", "mismatch", "has_sidecar", "file_xmp", "checked"],
                    "CREATE TABLE IF NOT EXISTS metadata_check (photo_id BIGINT PRIMARY KEY, stamp VARCHAR(191), data MEDIUMTEXT, mismatch INT, has_sidecar INT, file_xmp INT, checked BIGINT)"),
}


# ---- configuration
def settings() -> dict:
    """{type, host, port, database, user, password}: the file, then the environment on top. `type` is
    always mysql (MariaDB and MySQL are the same to PyMySQL); it stays in the file for older readers."""
    global _cfg
    if _cfg is None:
        c = {"type": "mysql", "host": "", "port": 3306, "database": "", "user": "", "password": ""}
        try:
            c.update({k: v for k, v in json.loads(CONF.read_text()).items() if k in KEYS})
        except (OSError, ValueError):
            pass
        for k in KEYS:
            v = config.viewer_db_env(k)
            if v:
                c[k] = v
        c["port"] = int(c.get("port") or 3306)
        c["type"] = "mysql"
        _cfg = c
    return dict(_cfg)


def configured() -> bool:
    """Whether a server, database and user are named at all (the file or the environment)."""
    c = settings()
    return bool(c["host"] and c["database"] and c["user"])


def ready() -> bool:
    """Configured and answering: the state the viewer needs for everything but its database set-up page."""
    return configured() and error is None


def public_settings() -> dict:
    """The database settings for the settings page: the password replaced by whether one is set, plus the
    current error and whether a viewer.db from before v1.113 waits to be imported."""
    c = settings()
    return {**{k: v for k, v in c.items() if k != "password"}, "password_set": bool(c["password"]), "configured": configured(),
            "error": error, "legacy_sqlite": str(LEGACY_SQLITE) if LEGACY_SQLITE.exists() else None}


def save_settings(c: dict):
    """Write the file and use it from now on (callers switch the data first)."""
    global _cfg
    keep = {k: c.get(k, "") for k in KEYS}
    keep["type"], keep["port"] = "mysql", int(keep.get("port") or 3306)
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(keep, indent=1))
    try:
        os.chmod(CONF, 0o600)
    except OSError:
        pass
    _cfg = None
    settings()


def is_mysql() -> bool:
    """Always true since v1.113; kept for the modules that asked (remove after v1.120)."""
    return True


def describe() -> str:
    """Where the viewer's own data lives, for status pages (with the error when the server is not answering)."""
    c = settings()
    if not configured():
        return "no database configured"
    return f"MySQL {c['user']}@{c['host']}:{c['port']}/{c['database']}" + (f" (unavailable: {error})" if error else "")


def exists() -> bool:
    """Whether there is a database to read at all. Kept for its callers (remove after v1.120): same as ready()."""
    return ready()


# ---- connections
class _Row(tuple):
    """A tuple that also answers to column names (what sqlite3.Row gave the code when it ran on SQLite)."""
    __slots__ = ()
    _keys = ()

    def __getitem__(self, k):
        if isinstance(k, str):
            return tuple.__getitem__(self, self._keys.index(k))
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._keys)


@lru_cache(maxsize=256)
def _row_class(keys: tuple):
    """One row class per column list (the same few queries run again and again)."""
    return type("Row", (_Row,), {"__slots__": (), "_keys": keys})


class _VCursor(Cursor):
    def __init__(self, cur, mysql: bool):
        super().__init__(cur, mysql)
        desc = getattr(cur, "description", None)
        self._cls = _row_class(tuple(d[0] for d in desc)) if mysql and desc else tuple

    def _fix(self, row):
        if row is None or not self._mysql:
            return row
        return self._cls(norm_plain(v) for v in row)


class VConn(Conn):
    """The dialect bridge for the viewer's own SQL (written for SQLite). `mysql` is False only for the
    read-only import of a viewer.db from before v1.113."""

    def _sql(self, sql: str) -> str:
        if not self.mysql:
            return sql
        sql = super()._sql(sql).replace(" COLLATE NOCASE", "")
        return _KEY_COL.sub("`key`", sql)

    def _params(self, params):  # text stamps stay text: no datetime coercion here
        return tuple(params)

    def execute(self, sql: str, params=()):
        if self.mysql:
            cur = self.raw.cursor()
            cur.execute(self._sql(sql), self._params(params))
            return _VCursor(cur, True)
        return _VCursor(self.raw.execute(sql, params), False)

    def executemany(self, sql: str, rows):
        if self.mysql:
            cur = self.raw.cursor()
            rows = [self._params(r) for r in rows]
            if rows:
                cur.executemany(self._sql(sql), rows)
            return _VCursor(cur, True)
        return _VCursor(self.raw.executemany(sql, rows), False)

    def executescript(self, script: str):
        if self.mysql:
            for stmt in script.split(";"):
                if stmt.strip():
                    self.execute(stmt)
        else:
            self.raw.executescript(script)


def _mysql_raw(c: dict):
    if pymysql is None:
        raise RuntimeError("the viewer database needs the PyMySQL package (pip install pymysql)")
    if not (c.get("host") and c.get("database") and c.get("user")):
        raise RuntimeError("no viewer database configured (Settings › digiKam and server › iopix database, or VIEWER_DB_*)")
    return pymysql.connect(host=c["host"], port=int(c["port"]), database=c["database"], user=c["user"], password=c["password"],
                           charset="utf8mb4", autocommit=False, connect_timeout=8, read_timeout=120, write_timeout=120)


_tl = threading.local()  # one MySQL connection per thread, kept open: a fresh TCP+auth handshake per query was most of a request


class _Kept:
    """A pooled PyMySQL connection: close() rolls back whatever was not committed and keeps the socket for the
    next use on this thread; a broken one is dropped so the next connect() opens a new one."""

    def __init__(self, raw):
        self.raw = raw

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def close(self):
        try:
            self.raw.rollback()
        except Exception:
            try:
                self.raw.close()
            except Exception:
                pass
            if getattr(_tl, "raw", None) is self.raw:
                _tl.raw = None


def _mysql_kept(c: dict):
    key = (c["host"], c["port"], c["database"], c["user"])
    raw = getattr(_tl, "raw", None)
    if raw is not None and getattr(_tl, "key", None) == key:
        try:
            raw.ping()  # a dead socket raises: dropped below, and a new connection is opened
            return _Kept(raw)
        except Exception:
            try:
                raw.close()
            except Exception:
                pass
    _tl.raw, _tl.key = _mysql_raw(c), key
    return _Kept(_tl.raw)


def connect(timeout: float = 10, cfg: dict = None) -> VConn:
    """The configured viewer database (pooled per thread), or a given configuration (its own connection, for
    tests and switching). `timeout` is kept for the callers that pass one (the server's own timeouts apply)."""
    c = cfg or settings()
    return VConn(_mysql_raw(c) if cfg is not None else _mysql_kept(c), True, True)


def _sqlite_source(path: Path) -> VConn:
    """A viewer.db from before v1.113, read-only: the source of the one-time import."""
    raw = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    raw.row_factory = sqlite3.Row
    return VConn(raw, False, False)


write_lock = threading.Lock()  # one writer at a time in this process: the jobs and the requests take turns, never wait on row locks


@contextmanager
def reader(timeout: float = 10):
    """A read connection to the viewer database, closed when the block ends."""
    con = connect(timeout=timeout)
    try:
        yield con
    finally:
        con.close()


@contextmanager
def writer(timeout: float = 30):
    """One serialised write transaction on the viewer database: committed on success, always closed.
    Every module that writes AI results, checks or bookkeeping goes through this."""
    with write_lock:
        con = connect(timeout=timeout)
        try:
            yield con
            con.commit()
        finally:
            con.close()


def columns(con: VConn, table: str) -> list:
    """The table's column names in order; empty for a table that does not exist (a fresh, empty database)."""
    if con.mysql:
        return [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = ? "
                                          "ORDER BY ordinal_position", (table,))]
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def declared_columns(create_sql: str) -> dict:
    """name -> definition of every column in a CREATE TABLE statement (table constraints left out), so a
    column added to TABLES reaches databases created before it: ensure_schema adds what a table lacks."""
    body = create_sql[create_sql.index("(") + 1:create_sql.rindex(")")]
    defs, depth, cur = [], 0, []
    for ch in body:  # split on the commas between definitions, not those inside VARCHAR(191) or DEFAULT ('{}')
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            defs.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    defs.append("".join(cur))
    out = {}
    for d in (d.strip() for d in defs):
        # a table constraint (PRIMARY KEY (a, b), INDEX name (col), UNIQUE (col)...) rather than a column; a column may
        # be named key, so the constraint forms are matched whole
        if not d or _CONSTRAINT.match(d):
            continue
        out[d.split()[0].strip('`"')] = d
    return out


_CONSTRAINT = re.compile(r"^(PRIMARY\s+KEY|FOREIGN\s+KEY|CONSTRAINT\b|CHECK\s*\(|(UNIQUE|INDEX|KEY)(\s+KEY)?(\s+[`\"]?\w+[`\"]?)?\s*\()", re.I)


def add_missing_columns(con: VConn) -> list:
    """Every column TABLES declares that a table lacks, added with its declared definition; returns what was
    added (a column added to the schema after a database was created, v1.63.0's session_gen for one)."""
    added = []
    for name, (cols, ddl) in TABLES.items():
        have = set(columns(con, name))
        for col, definition in declared_columns(ddl).items():
            if col not in have:
                try:
                    con.execute(f"ALTER TABLE {name} ADD COLUMN {definition}")
                except Exception as e:  # another process (the MCP server, a second worker) added it a moment ago
                    if "duplicate column" not in str(e).lower() and "1060" not in str(e):
                        raise
                    continue
                added.append(f"{name}.{col}")
    if added:
        log.info("viewer database: columns added: %s", ", ".join(added))
    return added


# the table names before v1.18, renamed in place (rows kept) the first time a newer viewer opens the database
OLD_NAMES = {
    "settings": "app_settings", "favorites": "favorite_photos",
    "ualbums": "viewer_albums", "ualbum_items": "viewer_album_photos", "searches": "saved_searches",
    "edits": "pending_edits", "tagjobs": "tag_rewrite_queue",
    "quality": "ai_quality", "vision": "ai_text_and_faces", "objects": "ai_objects", "objects_done": "ai_objects_scanned",
    "captions": "ai_descriptions", "face_vecs": "ai_face_embeddings", "face_protos": "ai_person_references",
    "near_dups": "ai_similar_photos", "consistency": "metadata_check",
    "lib_meta": "library_info", "lib_roots": "album_roots", "lib_albums": "folders",
    "lib_tags": "tags", "lib_tagprops": "tag_properties", "lib_imagetags": "photo_tags",
    "lib_regions": "face_regions", "lib_comments": "comments", "lib_copyright": "copyright",
    "lib_props": "photo_properties", "lib_images": "photos", "photo_ids": "photo_identity",
    # v1.18 called the library tables digikam_copy_*; they hold the library itself, digiKam or not
    "digikam_copy_info": "library_info",
    "digikam_copy_album_roots": "album_roots",
    "digikam_copy_folders": "folders",
    "digikam_copy_photos": "photos",
    "digikam_copy_tags": "tags",
    "digikam_copy_tag_properties": "tag_properties",
    "digikam_copy_photo_tags": "photo_tags",
    "digikam_copy_face_regions": "face_regions",
    "digikam_copy_comments": "comments",
    "digikam_copy_copyright": "copyright",
    "digikam_copy_photo_properties": "photo_properties",
}


def table_names(con: VConn) -> set:
    """Every table in the viewer database."""
    if con.mysql:
        return {r[0] for r in con.execute("SHOW TABLES")}
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _drop_old_library_shape(con: VConn):
    """v1.15-v1.20 kept a reduced copy of the library (photos with columns of its own). The library tables
    now have digiKam's columns: drop the old ones; the next read of digiKam fills the new ones."""
    have = table_names(con)
    if "photos" in have and "file_size" in columns(con, "photos"):
        for t in ("album_roots", "folders", "photos", "tags", "tag_properties", "photo_tags", "face_regions", "comments", "copyright", "photo_properties"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        if "library_info" in have:
            con.execute("DELETE FROM library_info")
        log.info("viewer database: the library tables take digiKam's full columns now; they are filled again at the next read of digiKam")


def rename_old_tables(con: VConn):
    """Give tables from before v1.18 their self-explanatory names, keeping every row."""
    have = table_names(con)
    todo = [(a, b) for a, b in OLD_NAMES.items() if a in have and b not in have]
    for a, b in todo:
        con.execute(f"RENAME TABLE `{a}` TO `{b}`")
    if todo:
        log.info("viewer database: renamed %d tables to their new names", len(todo))


def dump_tool() -> str:
    """mariadb-dump or mysqldump, wherever it is (Homebrew's mysql-client is not on PATH by default); None without one."""
    return (shutil.which("mariadb-dump") or shutil.which("mysqldump")
            or next((c for c in ("/opt/homebrew/opt/mysql-client/bin/mysqldump", "/usr/local/opt/mysql-client/bin/mysqldump") if os.access(c, os.X_OK)), None))


def _dump_options(tool: str) -> list:
    """MySQL's own mysqldump 8 reads information_schema.COLUMN_STATISTICS, which MariaDB has not got, and stops after
    the first table; --column-statistics=0 turns that off (MariaDB's dump and MySQL's before 8 do not know the option)."""
    try:
        v = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    m = re.search(r"Distrib (\d+)", v) or re.search(r"Ver (\d+)\.", v)
    major = int(m.group(1)) if m else 0
    return ["--column-statistics=0"] if "mariadb" not in v.lower() and major >= 8 else []


def dump(dst: Path, cfg: dict = None, skip_token: bool = True):
    """A mysqldump of the viewer database into `dst` (the backup form of the viewer's data: restore with
    `mariadb <database> < file.sql`). skip_token leaves the MCP admin token out: it can be generated again
    and need not travel with the data. Raises when no dump tool is installed or the dump fails."""
    tool = dump_tool()
    if not tool:
        raise RuntimeError("no mysqldump/mariadb-dump on this machine (install mariadb-client)")
    c = cfg or settings()
    cmd = [tool, *_dump_options(tool), "-h", str(c["host"]), "-P", str(c["port"]), "-u", c["user"], "--single-transaction", "--skip-extended-insert", c["database"]]
    with open(dst, "wb") as f:
        res = subprocess.run(cmd, stdout=subprocess.PIPE if skip_token else f, stderr=subprocess.PIPE, timeout=1800,
                             env={**os.environ, "MYSQL_PWD": c["password"] or ""})  # the password never on the command line
        if res.returncode != 0:
            f.close(); dst.unlink(missing_ok=True)  # a dump cut short is no backup
            raise RuntimeError(res.stderr.decode(errors="replace")[-300:])
        if skip_token:  # one INSERT per row (--skip-extended-insert), so the token's row is one line
            f.write(b"".join(line for line in res.stdout.splitlines(keepends=True) if b"'mcp_admin_token'" not in line))


def _backup_before_migration(tag: str):
    """A dump of the viewer database before its tables change shape. A copy that cannot be made is logged, not
    fatal: the migration itself carries every row over."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = config.DATA_DIR / "db-backups"
    try:
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"viewer-before-{tag}-{stamp}.sql"
        dump(dst, skip_token=False)
        log.info("viewer database: copied to %s before the %s migration", dst, tag)
    except Exception as e:
        log.warning("viewer database: no copy before the %s migration (%s)", tag, e)


def _old_ai_shape(con: VConn) -> bool:
    """Whether the AI tables are still in their pre-1.43 shape (a stamp column per table, five marker tables)."""
    have = table_names(con)
    return ("ai_text_and_faces" in have or "ai_objects_scanned" in have or "ai_faces_scanned" in have
            or "ai_quality_old" in have or "ai_descriptions_old" in have or "image_data_checksums_old" in have  # a migration that stopped halfway
            or "stamp" in columns(con, "ai_quality") or "stamp" in columns(con, "ai_descriptions")
            or "path" in columns(con, "image_data_checksums"))


def _migrate_ai_tables(con: VConn):
    """v1.43 (remove after v1.120, with _old_ai_shape and the backup it takes): one ai_scans table says which version of each photo every job looked at (it was a stamp column in
    four result tables plus two marker tables); ai_text_and_faces is split into ai_text and ai_face_count;
    image_data_checksums is keyed by photo rather than file path. Every row is carried over; a dump of the
    whole database is written to db-backups first."""
    _backup_before_migration("1.43")
    have = table_names(con)

    def rename(a, b):
        con.execute(f"RENAME TABLE `{a}` TO `{b}`")

    old = {}
    for t in ("ai_quality", "ai_descriptions", "image_data_checksums"):
        if t + "_old" in have:  # renamed by a run that did not finish: carried on from here
            old[t] = t + "_old"
        elif t in have and (("stamp" in columns(con, t)) or (t == "image_data_checksums" and "path" in columns(con, t))):
            rename(t, t + "_old"); old[t] = t + "_old"
    for name, (cols, ddl) in TABLES.items():
        if name.startswith("ai_") or name == "image_data_checksums":
            con.execute(ddl)
    if "ai_quality" in old:
        con.execute("INSERT OR IGNORE INTO ai_quality (photo_id, sharp, bright, clip_lo, clip_hi) SELECT photo_id, sharp, bright, clip_lo, clip_hi FROM ai_quality_old")
        con.execute("INSERT OR IGNORE INTO ai_scans (photo_id, job, stamp, detail) SELECT photo_id, 'quality', stamp, NULL FROM ai_quality_old")
        con.execute("DROP TABLE ai_quality_old")
    if "ai_descriptions" in old:
        con.execute("INSERT OR IGNORE INTO ai_descriptions (photo_id, text) SELECT photo_id, text FROM ai_descriptions_old")
        con.execute("INSERT OR IGNORE INTO ai_scans (photo_id, job, stamp, detail) SELECT photo_id, 'captions', stamp, NULL FROM ai_descriptions_old")
        con.execute("DROP TABLE ai_descriptions_old")
    if "ai_text_and_faces" in have:
        con.execute("INSERT OR IGNORE INTO ai_text (photo_id, text) SELECT photo_id, text FROM ai_text_and_faces")
        con.execute("INSERT OR IGNORE INTO ai_face_count (photo_id, nfaces, faceq) SELECT photo_id, nfaces, faceq FROM ai_text_and_faces")
        con.execute("INSERT OR IGNORE INTO ai_scans (photo_id, job, stamp, detail) SELECT photo_id, 'vision', stamp, NULL FROM ai_text_and_faces")
        con.execute("DROP TABLE ai_text_and_faces")
    if "ai_objects_scanned" in have:
        con.execute("INSERT OR IGNORE INTO ai_scans (photo_id, job, stamp, detail) SELECT photo_id, 'objects', stamp, NULL FROM ai_objects_scanned")
        con.execute("DROP TABLE ai_objects_scanned")
    if "ai_faces_scanned" in have:
        rows = con.execute("SELECT photo_id, stamp, found, added, checked FROM ai_faces_scanned").fetchall()
        con.executemany("INSERT OR IGNORE INTO ai_scans (photo_id, job, stamp, detail) VALUES (?, 'facedetect', ?, ?)",
                        [(r[0], r[1], json.dumps({"found": r[2], "added": r[3], "checked": r[4]})) for r in rows])
        con.execute("DROP TABLE ai_faces_scanned")
    if "image_data_checksums" in old:
        # the photo each path stood for is known from photo_identity; paths no photo has any more are dropped
        con.execute("INSERT OR IGNORE INTO image_data_checksums (photo_id, hash, algorithm, verified) "
                    "SELECT i.dk_id, c.hash, c.algorithm, c.verified FROM image_data_checksums_old c JOIN photo_identity i ON i.path = c.path WHERE i.dk_id IS NOT NULL")
        con.execute("DROP TABLE image_data_checksums_old")
    log.info("viewer database: AI tables restructured (ai_scans, ai_text, ai_face_count; checksums by photo)")


def ensure_schema(con: VConn = None):
    """Every table, idempotently, plus the columns older databases lack."""
    own = con is None
    con = con or connect()
    try:
        rename_old_tables(con)
        _drop_old_library_shape(con)
        if _old_ai_shape(con):
            _migrate_ai_tables(con)
        for name, (cols, ddl) in TABLES.items():
            con.execute(ddl)
        if "file_copy" not in columns(con, "photo_history"):  # v1.32: the backed-up image file of each change
            con.execute("ALTER TABLE photo_history ADD COLUMN file_copy TEXT")
        have = columns(con, "viewer_albums")  # v1.27: shared and private albums; albums from before are shared
        if "owner_id" not in have:
            con.execute("ALTER TABLE viewer_albums ADD COLUMN owner_id BIGINT NULL")
        if "is_shared" not in have:
            con.execute("ALTER TABLE viewer_albums ADD COLUMN is_shared INT NOT NULL DEFAULT 1")
        add_missing_columns(con)  # v1.63.1: every other column added since a database was created (session_gen, and whatever comes next)
        # v1.43: the full comparison is kept only for photos whose copies disagree or that lack a sidecar; rows from
        # before carried it for every photo (tens of MB), and nothing reads it for the others
        con.execute("UPDATE metadata_check SET data = NULL WHERE data IS NOT NULL AND mismatch = 0 AND has_sidecar = 1")
        con.commit()
    finally:
        if own:
            con.close()


def init():
    """Called once at start: open the configured database and bring its schema up to date. Never raises: a
    missing or unreachable database is recorded in `error` and the viewer starts in its "database needed" state."""
    global error
    error = None
    if not configured():
        error = "no viewer database configured"
        log.error("viewer database: %s; the viewer waits for one (Settings › digiKam and server › iopix database, or VIEWER_DB_*)", error)
        return
    try:
        con = connect(cfg=settings())
        try:
            ensure_schema(con)
        finally:
            con.close()
    except Exception as e:  # the server is down, the password changed...: the viewer must still start to say so
        error = f"{type(e).__name__}: {str(e)[:160]}"
        log.error("viewer database unavailable (%s); the viewer waits for it", error)


def retry():
    """Try the configured database again (the Settings page's Retry, or a request that finds the viewer waiting);
    True when it answers now."""
    init()
    return ready()


# ---- testing, copying, switching
def test_connection(c: dict) -> dict:
    """Connect with the given settings (an empty password keeps the stored one) and count what is there."""
    c = {**settings(), **{k: v for k, v in c.items() if k in KEYS and v not in (None, "")}}
    con = connect(cfg=c)
    try:
        ver = con.execute("SELECT VERSION()").fetchone()[0]
        have = {r[0] for r in con.execute("SHOW TABLES")}
        counts = {}
        for t in TABLES:
            if t in have:
                counts[t] = con.execute(f"SELECT count(*) FROM `{t}`").fetchone()[0]
        return {"ok": True, "version": ver, "tables": counts, "existing": sorted({OLD_NAMES.get(t, t) for t in have} & set(TABLES))}
    finally:
        con.close()


def copy_all(src: VConn, dst: VConn, drop: bool = True, progress=None) -> dict:
    """Every table, row by row, ids kept, into a MySQL target from another MySQL database or a viewer.db of
    before v1.112. drop: DROP the viewer tables at the target first (so a re-run or a stale copy never leaves
    rows behind); else existing rows are replaced by id."""
    counts = {}
    qs = (lambda t: f"`{t}`") if src.mysql else (lambda t: t)
    rename_old_tables(dst)  # a target last written by an older viewer
    dst.execute("SET SESSION sql_mode = CONCAT(@@sql_mode, ',NO_AUTO_VALUE_ON_ZERO')")  # digiKam's root tag has id 0, which MySQL would otherwise replace
    for name, (cols, ddl) in TABLES.items():  # the whole schema first, then the rows
        if drop:
            dst.execute(f"DROP TABLE IF EXISTS `{name}`")
        dst.execute(ddl)
    dst.commit()
    present = table_names(src)
    for name, (cols, ddl) in TABLES.items():
        have = columns(src, name) if name in present else []  # a table the source never had (hand-made schema) copies nothing
        if not have:
            counts[name] = 0
            dst.commit()
            continue
        use = [c for c in cols if c in have]
        collist = ", ".join(("`key`" if c == "key" else c) for c in use)
        sel = ", ".join(("`key`" if c == "key" and src.mysql else c) for c in use)
        cur, n = src.execute(f"SELECT {sel} FROM {qs(name)}"), 0
        while True:  # in slices: the face embeddings and tag rows of a large library do not all sit in memory at once
            rows = cur.fetchmany(2000)
            if not rows:
                break
            dst.executemany(f"INSERT OR REPLACE INTO `{name}` ({collist}) VALUES ({','.join('?' * len(use))})", [tuple(r) for r in rows])
            n += len(rows)
        dst.commit()
        counts[name] = n
        if progress:
            progress(name, len(rows))
    return counts


def switch(c: dict, copy: bool = True, drop: bool = True) -> dict:
    """Move the viewer to the given database: test it, (re)create the tables there, copy the data from the
    current one (or, on an install from before v1.113 that has no database yet, from its viewer.db, which is
    then renamed), save the configuration. The caller restarts the viewer afterwards."""
    cur = settings()
    c = {**cur, **{k: v for k, v in c.items() if k in KEYS and v not in (None, "")}}
    c["type"] = "mysql"
    if not (c["host"] and c["database"] and c["user"]):
        raise ValueError("A host, a database and a user are needed")
    same = configured() and all(str(c[k]) == str(cur[k]) for k in ("host", "port", "database"))
    if same and ready():
        raise ValueError("The viewer already uses that database")
    if same:  # the configured database again while it was unreachable at start: nothing to copy from; the restart brings it back
        test_connection(c)
        with _lock:
            save_settings(c)
        return {"ok": True, "copied": {}, "database": describe()}
    counts, imported = {}, None
    with _lock:
        dst = connect(cfg=c)
        try:
            if copy and ready():
                src = connect()
                try:
                    counts = copy_all(src, dst, drop=drop)
                finally:
                    src.close()
            elif copy and not configured() and LEGACY_SQLITE.exists():
                counts = import_sqlite(LEGACY_SQLITE, dst, drop=drop)
                imported = str(LEGACY_SQLITE)
            else:
                ensure_schema(dst)
        finally:
            dst.close()
        save_settings(c)
        if imported:
            LEGACY_SQLITE.rename(LEGACY_SQLITE.with_name("viewer.db.imported"))  # kept until the owner deletes it; never read again
    return {"ok": True, "copied": counts, "database": describe(), "imported": imported}


def import_sqlite(path: Path, dst: VConn, drop: bool = True) -> dict:
    """Every row of a viewer.db from before v1.113 into `dst` (the one-time import of an upgraded install). Its
    user_accounts rows are not among them: accounts live in the suite's auth database since v1.112."""
    src = _sqlite_source(path)
    try:
        if "undo" in columns(src, "photo_history"):  # v1.24 named the column after a reserved word; the file may predate the rename
            src.raw.close()
            raw = sqlite3.connect(path)
            raw.execute("ALTER TABLE photo_history RENAME COLUMN undo TO undo_values"); raw.commit(); raw.close()
            src = _sqlite_source(path)
        return copy_all(src, dst, drop=drop)
    finally:
        src.close()
