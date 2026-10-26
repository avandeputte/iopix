"""The viewer's own database: SQLite (data/viewer.db, the default) or MySQL/MariaDB.

Every module that keeps viewer data (state, users, edits, tagmanager, analysis, consistency)
gets its connection here. The wrapper translates the SQLite dialect the code is written in
(? placeholders, INSERT OR REPLACE / OR IGNORE, COLLATE NOCASE, the `key` column) and hands
back rows that answer to both an index and a column name, so the same code runs on both.

Where the connection goes is kept outside the database itself, in data/viewer-db.json
(or VIEWER_DB_* in the environment), since the settings table lives inside. Switching is
done from Settings: the target is tested, the tables are (re)created there, every row is
copied, the file is written, and the viewer restarts on the new database.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
from urllib.parse import quote
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path

from app import config
from app.digikam import library as _library
from app.digikam.dkdb import Conn, _Cursor, _norm_plain

log = logging.getLogger("app.data.vdb")
CONF = config.DATA_DIR / "viewer-db.json"
SQLITE_FILE = config.DATA_DIR / "viewer.db"
KEYS = ("type", "host", "port", "database", "user", "password")
_cfg = None
_lock = threading.Lock()
error = None  # why the configured database could not be used (then SQLite is used instead)
_KEY_COL = re.compile(r"(?<![\w`.])key(?![\w`(])")  # the settings column, a reserved word on MySQL

try:
    import pymysql
    Error = (sqlite3.Error, pymysql.err.Error)
except ImportError:  # pragma: no cover
    pymysql = None
    Error = (sqlite3.Error,)

# ---- schema: one definition per table, the SQLite and the MySQL spelling; columns in copy order
TABLES = {
    "app_settings": (["key", "value"],
                 "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)",
                 "CREATE TABLE IF NOT EXISTS app_settings (`key` VARCHAR(191) PRIMARY KEY, value MEDIUMTEXT)"),
    "user_accounts": (["id", "name", "hash", "role", "settings", "created", "last_login", "person", "email", "session_gen", "listed"],
              "CREATE TABLE IF NOT EXISTS user_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL COLLATE NOCASE, hash TEXT NOT NULL, "
              "role TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}', created INTEGER, last_login INTEGER, person INTEGER, email TEXT, session_gen INTEGER DEFAULT 0, listed INTEGER NOT NULL DEFAULT 1)",
              "CREATE TABLE IF NOT EXISTS user_accounts (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(191) NOT NULL UNIQUE, hash TEXT NOT NULL, "
              "role VARCHAR(32) NOT NULL, settings MEDIUMTEXT NOT NULL DEFAULT ('{}'), created BIGINT, last_login BIGINT, person INT, email VARCHAR(191), session_gen INT DEFAULT 0, listed INT NOT NULL DEFAULT 1)"),
    "favorite_photos": (["photo_id", "hash", "added"],
                  "CREATE TABLE IF NOT EXISTS favorite_photos (photo_id INTEGER PRIMARY KEY, hash TEXT, added INTEGER)",
                  "CREATE TABLE IF NOT EXISTS favorite_photos (photo_id BIGINT PRIMARY KEY, hash VARCHAR(64), added BIGINT)"),
    # is_shared 1: everyone sees it, editors and admins change it; 0: only owner_id (a user_accounts id) sees and changes it
    "viewer_albums": (["id", "name", "created", "owner_id", "is_shared"],
                "CREATE TABLE IF NOT EXISTS viewer_albums (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created INTEGER, owner_id INTEGER, is_shared INTEGER NOT NULL DEFAULT 1)",
                "CREATE TABLE IF NOT EXISTS viewer_albums (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255) NOT NULL, created BIGINT, owner_id BIGINT NULL, is_shared INT NOT NULL DEFAULT 1)"),
    "viewer_album_photos": (["album_id", "photo_id", "hash", "added"],
                     "CREATE TABLE IF NOT EXISTS viewer_album_photos (album_id INTEGER, photo_id INTEGER, hash TEXT, added INTEGER, PRIMARY KEY (album_id, photo_id))",
                     "CREATE TABLE IF NOT EXISTS viewer_album_photos (album_id INT, photo_id BIGINT, hash VARCHAR(64), added BIGINT, PRIMARY KEY (album_id, photo_id))"),
    "saved_searches": (["id", "name", "query", "created"],
                 "CREATE TABLE IF NOT EXISTS saved_searches (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, query TEXT NOT NULL, created INTEGER)",
                 "CREATE TABLE IF NOT EXISTS saved_searches (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255) NOT NULL, query TEXT NOT NULL, created BIGINT)"),
    "pending_edits": (["photo_id", "data", "ts"],
              "CREATE TABLE IF NOT EXISTS pending_edits (photo_id INTEGER PRIMARY KEY, data TEXT, ts INTEGER)",
              "CREATE TABLE IF NOT EXISTS pending_edits (photo_id BIGINT PRIMARY KEY, data MEDIUMTEXT, ts BIGINT)"),
    "tag_rewrite_queue": (["id", "op", "path", "image_id"],
                "CREATE TABLE IF NOT EXISTS tag_rewrite_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT, path TEXT, image_id INTEGER)",
                "CREATE TABLE IF NOT EXISTS tag_rewrite_queue (id INT AUTO_INCREMENT PRIMARY KEY, op VARCHAR(64), path TEXT, image_id BIGINT)"),
    # ---- AI results: one row per photo and job in ai_scans says which version of the file (stamp) each job
    # last looked at (detail: a small JSON note, e.g. face detection's counts); the result tables hold data only
    "ai_scans": (["photo_id", "job", "stamp", "detail"],
                 "CREATE TABLE IF NOT EXISTS ai_scans (photo_id INTEGER, job TEXT, stamp TEXT, detail TEXT, PRIMARY KEY (photo_id, job))",
                 "CREATE TABLE IF NOT EXISTS ai_scans (photo_id BIGINT, job VARCHAR(32), stamp VARCHAR(191), detail TEXT, PRIMARY KEY (photo_id, job), INDEX ai_scans_job (job))"),
    "ai_quality": (["photo_id", "sharp", "bright", "clip_lo", "clip_hi"],
                   "CREATE TABLE IF NOT EXISTS ai_quality (photo_id INTEGER PRIMARY KEY, sharp REAL, bright REAL, clip_lo REAL, clip_hi REAL)",
                   "CREATE TABLE IF NOT EXISTS ai_quality (photo_id BIGINT PRIMARY KEY, sharp DOUBLE, bright DOUBLE, clip_lo DOUBLE, clip_hi DOUBLE)"),
    "ai_text": (["photo_id", "text"],
                "CREATE TABLE IF NOT EXISTS ai_text (photo_id INTEGER PRIMARY KEY, text TEXT)",
                "CREATE TABLE IF NOT EXISTS ai_text (photo_id BIGINT PRIMARY KEY, text MEDIUMTEXT)"),
    "ai_face_count": (["photo_id", "nfaces", "faceq"],
                      "CREATE TABLE IF NOT EXISTS ai_face_count (photo_id INTEGER PRIMARY KEY, nfaces INTEGER, faceq REAL)",
                      "CREATE TABLE IF NOT EXISTS ai_face_count (photo_id BIGINT PRIMARY KEY, nfaces INT, faceq DOUBLE)"),
    "ai_objects": (["photo_id", "cls", "score", "x", "y", "w", "h"],
                "CREATE TABLE IF NOT EXISTS ai_objects (photo_id INTEGER, cls TEXT, score REAL, x REAL, y REAL, w REAL, h REAL)",
                "CREATE TABLE IF NOT EXISTS ai_objects (photo_id BIGINT, cls VARCHAR(64), score DOUBLE, x DOUBLE, y DOUBLE, w DOUBLE, h DOUBLE, INDEX ai_objects_photo (photo_id))"),
    "ai_descriptions": (["photo_id", "text"],
                        "CREATE TABLE IF NOT EXISTS ai_descriptions (photo_id INTEGER PRIMARY KEY, text TEXT)",
                        "CREATE TABLE IF NOT EXISTS ai_descriptions (photo_id BIGINT PRIMARY KEY, text TEXT)"),
    "ai_looks": (["photo_id", "aesthetic", "focus", "eyes_checked", "eyes_closed"],
                 "CREATE TABLE IF NOT EXISTS ai_looks (photo_id INTEGER PRIMARY KEY, aesthetic REAL, focus REAL, eyes_checked INTEGER, eyes_closed INTEGER)",
                 "CREATE TABLE IF NOT EXISTS ai_looks (photo_id BIGINT PRIMARY KEY, aesthetic DOUBLE, focus DOUBLE, eyes_checked INT, eyes_closed INT)"),
    "ai_face_embeddings": (["photo_id", "x", "y", "w", "h", "vec"],
                  "CREATE TABLE IF NOT EXISTS ai_face_embeddings (photo_id INTEGER, x INTEGER, y INTEGER, w INTEGER, h INTEGER, vec BLOB, PRIMARY KEY (photo_id, x, y, w, h))",
                  "CREATE TABLE IF NOT EXISTS ai_face_embeddings (photo_id BIGINT, x INT, y INT, w INT, h INT, vec BLOB, PRIMARY KEY (photo_id, x, y, w, h))"),
    "ai_person_references": (["tag_id", "photo_id", "x", "y", "w", "h", "vec"],
                    "CREATE TABLE IF NOT EXISTS ai_person_references (tag_id INTEGER, photo_id INTEGER, x INTEGER, y INTEGER, w INTEGER, h INTEGER, vec BLOB, PRIMARY KEY (photo_id, x, y, w, h))",
                    "CREATE TABLE IF NOT EXISTS ai_person_references (tag_id BIGINT, photo_id BIGINT, x INT, y INT, w INT, h INT, vec BLOB, PRIMARY KEY (photo_id, x, y, w, h))"),
    # undo_values, not "undo": UNDO is a reserved word on MariaDB and MySQL
    # file_copy: the backed-up image file (data/file-backups, the original from before the viewer first wrote into it)
    "photo_history": (["id", "photo_id", "ts", "user_name", "action", "summary", "details", "undo_values", "sidecar_copy", "path", "file_copy"],
                      "CREATE TABLE IF NOT EXISTS photo_history (id INTEGER PRIMARY KEY AUTOINCREMENT, photo_id INTEGER, ts INTEGER, user_name TEXT, action TEXT, summary TEXT, details TEXT, undo_values TEXT, sidecar_copy TEXT, path TEXT, file_copy TEXT)",
                      "CREATE TABLE IF NOT EXISTS photo_history (id BIGINT AUTO_INCREMENT PRIMARY KEY, photo_id BIGINT, ts BIGINT, user_name VARCHAR(191), action VARCHAR(32), summary TEXT, details MEDIUMTEXT, undo_values MEDIUMTEXT, sidecar_copy TEXT, path TEXT, file_copy TEXT, INDEX photo_history_photo (photo_id))"),
    "image_data_checksums": (["photo_id", "hash", "algorithm", "verified"],
                             "CREATE TABLE IF NOT EXISTS image_data_checksums (photo_id INTEGER PRIMARY KEY, hash TEXT, algorithm TEXT, verified INTEGER)",
                             "CREATE TABLE IF NOT EXISTS image_data_checksums (photo_id BIGINT PRIMARY KEY, hash VARCHAR(128), algorithm VARCHAR(16), verified BIGINT)"),
    "ai_similar_photos": (["a", "b", "score"],
                  "CREATE TABLE IF NOT EXISTS ai_similar_photos (a INTEGER, b INTEGER, score REAL)",
                  "CREATE TABLE IF NOT EXISTS ai_similar_photos (a BIGINT, b BIGINT, score DOUBLE, INDEX ai_similar_photos_a (a))"),
    # ---- the library itself (library.py): digiKam's columns under names that say what they hold
    "library_info": (["key", "value"],
                     "CREATE TABLE IF NOT EXISTS library_info (key TEXT PRIMARY KEY, value TEXT)",
                     "CREATE TABLE IF NOT EXISTS library_info (`key` VARCHAR(191) PRIMARY KEY, value MEDIUMTEXT)"),
    **_library.tables(),
    # the viewer's own identity for a photo: digiKam's id is the working key everywhere, this row remembers
    # which file it stood for (digiKam's uniqueHash + size), so viewer data can follow a photo if digiKam renumbers
    "photo_identity": (["vid", "dk_id", "hash", "size", "path", "name", "first_seen", "last_seen"],
                  "CREATE TABLE IF NOT EXISTS photo_identity (vid INTEGER PRIMARY KEY AUTOINCREMENT, dk_id INTEGER, hash TEXT, size INTEGER, path TEXT, name TEXT, first_seen INTEGER, last_seen INTEGER)",
                  "CREATE TABLE IF NOT EXISTS photo_identity (vid INT AUTO_INCREMENT PRIMARY KEY, dk_id BIGINT, hash VARCHAR(64), size BIGINT, path TEXT, name VARCHAR(255), first_seen BIGINT, last_seen BIGINT, "
                  "INDEX photo_identity_dk (dk_id), INDEX photo_identity_hash (hash, size))"),
    # a Face quality finding the owner accepted as it is (check_name, not "check": a reserved word on MariaDB);
    # signature: what the photo's faces were then, so a photo whose faces change is looked at again
    "quality_accepted": (["photo_id", "check_name", "signature", "user_name", "ts"],
                         "CREATE TABLE IF NOT EXISTS quality_accepted (photo_id INTEGER, check_name TEXT, signature TEXT, user_name TEXT, ts INTEGER, PRIMARY KEY (photo_id, check_name))",
                         "CREATE TABLE IF NOT EXISTS quality_accepted (photo_id BIGINT, check_name VARCHAR(32), signature VARCHAR(191), user_name VARCHAR(191), ts BIGINT, PRIMARY KEY (photo_id, check_name))"),
    # the People page's own groups (Family, Friends, Classmates...): digiKam's person tags (tag ids), in the order
    # chosen; a person may be in several groups. Nothing here is written to digiKam.
    "people_groups": (["id", "name", "position", "created"],
                      "CREATE TABLE IF NOT EXISTS people_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0, created INTEGER)",
                      "CREATE TABLE IF NOT EXISTS people_groups (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(191) NOT NULL, position INT NOT NULL DEFAULT 0, created BIGINT)"),
    "people_group_members": (["group_id", "tag_id", "position"],
                             "CREATE TABLE IF NOT EXISTS people_group_members (group_id INTEGER, tag_id INTEGER, position INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, tag_id))",
                             "CREATE TABLE IF NOT EXISTS people_group_members (group_id INT, tag_id BIGINT, position INT NOT NULL DEFAULT 0, PRIMARY KEY (group_id, tag_id))"),
    "metadata_check": (["photo_id", "stamp", "data", "mismatch", "has_sidecar", "file_xmp", "checked"],
                    "CREATE TABLE IF NOT EXISTS metadata_check (photo_id INTEGER PRIMARY KEY, stamp TEXT, data TEXT, mismatch INTEGER, has_sidecar INTEGER, file_xmp INTEGER, checked INTEGER)",
                    "CREATE TABLE IF NOT EXISTS metadata_check (photo_id BIGINT PRIMARY KEY, stamp VARCHAR(191), data MEDIUMTEXT, mismatch INT, has_sidecar INT, file_xmp INT, checked BIGINT)"),
}
SQLITE_INDEXES = ["CREATE INDEX IF NOT EXISTS photo_history_photo ON photo_history (photo_id)", "CREATE INDEX IF NOT EXISTS ai_objects_photo ON ai_objects (photo_id)",
                  "CREATE INDEX IF NOT EXISTS ai_similar_photos_a ON ai_similar_photos (a)",
                  "CREATE INDEX IF NOT EXISTS photo_identity_dk ON photo_identity (dk_id)",
                  "CREATE INDEX IF NOT EXISTS photo_identity_hash ON photo_identity (hash, size)"] + _library.SQLITE_INDEXES


# ---- configuration
def settings() -> dict:
    """{type, host, port, database, user, password}: the file, else the environment, else SQLite."""
    global _cfg
    if _cfg is None:
        c = {"type": "sqlite", "host": "", "port": 3306, "database": "digikam_viewer", "user": "", "password": ""}
        try:
            c.update({k: v for k, v in json.loads(CONF.read_text()).items() if k in KEYS})
        except (OSError, ValueError):
            pass
        for k in KEYS:
            v = os.environ.get("VIEWER_DB_" + k.upper())
            if v:
                c[k] = v
        c["port"] = int(c.get("port") or 3306)
        c["type"] = "mysql" if str(c.get("type")).lower() in ("mysql", "mariadb") else "sqlite"
        _cfg = c
    return dict(_cfg)


def public_settings() -> dict:
    c = settings()
    return {**{k: v for k, v in c.items() if k != "password"}, "password_set": bool(c["password"]), "error": error, "sqlite_file": str(SQLITE_FILE)}


def save_settings(c: dict):
    """Write the file and use it from now on (callers switch the data first)."""
    global _cfg
    keep = {k: c.get(k, "") for k in KEYS}
    keep["port"] = int(keep.get("port") or 3306)
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(keep, indent=1))
    try:
        os.chmod(CONF, 0o600)
    except OSError:
        pass
    _cfg = None
    settings()


def is_mysql() -> bool:
    return settings()["type"] == "mysql" and error is None


def describe() -> str:
    c = settings()
    if is_mysql():
        return f"MySQL {c['user']}@{c['host']}:{c['port']}/{c['database']}"
    return f"SQLite {SQLITE_FILE.name}" + (f" (fallback: {error})" if error else "")


def exists() -> bool:
    """Whether there is a database to read at all (SQLite: the file; MySQL: assumed, connect decides)."""
    return is_mysql() or SQLITE_FILE.exists()


# ---- connections
class _Row(tuple):
    """A tuple that also answers to column names (what sqlite3.Row gives us on SQLite)."""
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


class _VCursor(_Cursor):
    def __init__(self, cur, mysql: bool):
        super().__init__(cur, mysql)
        desc = getattr(cur, "description", None)
        self._cls = _row_class(tuple(d[0] for d in desc)) if mysql and desc else tuple

    def _fix(self, row):
        if row is None or not self._mysql:
            return row
        return self._cls(_norm_plain(v) for v in row)


class VConn(Conn):
    """The dialect bridge for the viewer's own SQL (written for SQLite)."""

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
        raise RuntimeError("MySQL support needs the PyMySQL package (pip install pymysql)")
    return pymysql.connect(host=c["host"], port=int(c["port"]), database=c["database"], user=c["user"], password=c["password"],
                           charset="utf8mb4", autocommit=False, connect_timeout=8, read_timeout=120, write_timeout=120)


def _sqlite_raw(path: Path, timeout: float):
    raw = sqlite3.connect(path, timeout=timeout)
    raw.row_factory = sqlite3.Row
    return raw


def connect(timeout: float = 10, cfg: dict = None) -> VConn:
    """The configured viewer database (or a given configuration, for tests and switching)."""
    c = cfg or settings()
    if c["type"] == "mysql" and (cfg is not None or error is None):
        return VConn(_mysql_raw(c), True, True)
    return VConn(_sqlite_raw(SQLITE_FILE, timeout), False, True)


def columns(con: VConn, table: str) -> list:
    if con.mysql:  # a table that is not there has no columns (as PRAGMA says on SQLite): a fresh, empty database
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
    added. Both engines: a column that only one branch of a migration knew about left the other failing at
    its first query (v1.63.0, session_gen on MySQL)."""
    added = []
    for name, (cols, sq, my) in TABLES.items():
        have = set(columns(con, name))
        for col, definition in declared_columns(my if con.mysql else sq).items():
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
    "settings": "app_settings", "users": "user_accounts", "favorites": "favorite_photos",
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
OLD_SQLITE_INDEXES = ["objects_photo", "near_dups_a", "lib_imagetags_image", "lib_regions_image", "lib_comments_image",
                      "lib_copyright_image", "lib_props_image", "lib_tagprops_tag", "photo_ids_dk", "photo_ids_hash",
                      "digikam_copy_photo_tags_image", "digikam_copy_face_regions_image", "digikam_copy_comments_image",
                      "digikam_copy_copyright_image", "digikam_copy_photo_properties_image", "digikam_copy_tag_properties_tag"]


def table_names(con: VConn) -> set:
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
        con.execute(f"RENAME TABLE `{a}` TO `{b}`" if con.mysql else f'ALTER TABLE "{a}" RENAME TO "{b}"')
    if todo and not con.mysql:
        for ix in OLD_SQLITE_INDEXES:  # recreated under the new names by ensure_schema
            con.execute(f'DROP INDEX IF EXISTS "{ix}"')
    if todo:
        log.info("viewer database: renamed %d tables to their new names", len(todo))


def _backup_before_migration(tag: str):
    """A copy of the viewer database before its tables change shape: the SQLite file through the backup API, or a
    mysqldump of the MySQL database when a dump tool is installed. A copy that cannot be made is logged, not fatal:
    the migration itself carries every row over."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = config.DATA_DIR / "db-backups"
    try:
        d.mkdir(parents=True, exist_ok=True)
        if not is_mysql():
            dst = d / f"viewer-before-{tag}-{stamp}.db"
            src = sqlite3.connect(f"file:{quote(str(SQLITE_FILE))}?mode=ro", uri=True)
            try:
                out = sqlite3.connect(str(dst))
                try:
                    src.backup(out)
                finally:
                    out.close()
            finally:
                src.close()
        else:
            tool = shutil.which("mysqldump") or shutil.which("mariadb-dump") or next((c for c in ("/opt/homebrew/opt/mysql-client/bin/mysqldump", "/usr/local/opt/mysql-client/bin/mysqldump") if os.access(c, os.X_OK)), None)
            if not tool:
                log.warning("viewer database: no mysqldump/mariadb-dump here, so no copy before the %s migration", tag)
                return
            c = settings()
            dst = d / f"viewer-before-{tag}-{stamp}.sql"
            with open(dst, "wb") as f:
                res = subprocess.run([tool, "-h", str(c["host"]), "-P", str(c["port"]), "-u", c["user"], "--single-transaction", c["database"]],
                                     stdout=f, stderr=subprocess.PIPE, timeout=1800, env={**os.environ, "MYSQL_PWD": c["password"] or ""})  # never on the command line
            if res.returncode != 0:
                raise RuntimeError(res.stderr.decode(errors="replace")[-300:])
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
    """v1.43: one ai_scans table says which version of each photo every job looked at (it was a stamp column in
    four result tables plus two marker tables); ai_text_and_faces is split into ai_text and ai_face_count;
    image_data_checksums is keyed by photo rather than file path. Every row is carried over; a SQLite snapshot
    of the whole database is written to db-backups first."""
    _backup_before_migration("1.43")
    have = table_names(con)
    q = (lambda t: f"`{t}`") if con.mysql else (lambda t: f'"{t}"')

    def rename(a, b):
        con.execute(f"RENAME TABLE {q(a)} TO {q(b)}" if con.mysql else f"ALTER TABLE {q(a)} RENAME TO {q(b)}")

    old = {}
    for t in ("ai_quality", "ai_descriptions", "image_data_checksums"):
        if t + "_old" in have:  # renamed by a run that did not finish: carried on from here
            old[t] = t + "_old"
        elif t in have and (("stamp" in columns(con, t)) or (t == "image_data_checksums" and "path" in columns(con, t))):
            rename(t, t + "_old"); old[t] = t + "_old"
    for name, (cols, sq, my) in TABLES.items():
        if name.startswith("ai_") or name == "image_data_checksums":
            con.execute(my if con.mysql else sq)
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
    """Every table, idempotently; SQLite gets WAL; both engines get the columns older databases lack."""
    own = con is None
    con = con or connect()
    try:
        if not con.mysql:
            con.execute("PRAGMA journal_mode = WAL")
        rename_old_tables(con)
        _drop_old_library_shape(con)
        if _old_ai_shape(con):
            _migrate_ai_tables(con)
        if not con.mysql:
            cols = columns(con, "tag_rewrite_queue")
            if cols and "image_id" not in cols:  # an early schema whose rows cannot be replayed
                con.execute("DROP TABLE tag_rewrite_queue")
        for name, (cols, sq, my) in TABLES.items():
            con.execute(my if con.mysql else sq)
        if not con.mysql:  # MySQL declares its index in the CREATE TABLE and never had the older schema
            for sql in SQLITE_INDEXES:
                con.execute(sql)
            have = columns(con, "user_accounts")
            if "person" not in have:
                con.execute("ALTER TABLE user_accounts ADD COLUMN person INTEGER")
            if "email" not in have:
                con.execute("ALTER TABLE user_accounts ADD COLUMN email TEXT")
            if "undo" in columns(con, "photo_history"):  # v1.24 named the column after a reserved word
                con.execute("ALTER TABLE photo_history RENAME COLUMN undo TO undo_values")
        if "file_copy" not in columns(con, "photo_history"):  # v1.32: the backed-up image file of each change
            con.execute("ALTER TABLE photo_history ADD COLUMN file_copy TEXT")
        have = columns(con, "viewer_albums")  # v1.27: shared and private albums; albums from before are shared
        if "owner_id" not in have:
            con.execute("ALTER TABLE viewer_albums ADD COLUMN owner_id " + ("BIGINT NULL" if con.mysql else "INTEGER"))
        if "is_shared" not in have:
            con.execute("ALTER TABLE viewer_albums ADD COLUMN is_shared " + ("INT NOT NULL DEFAULT 1" if con.mysql else "INTEGER NOT NULL DEFAULT 1"))
        add_missing_columns(con)  # v1.63.1: every other column added since a database was created (session_gen, and whatever comes next), both engines
        # v1.43: the full comparison is kept only for photos whose copies disagree or that lack a sidecar; rows from
        # before carried it for every photo (tens of MB), and nothing reads it for the others
        con.execute("UPDATE metadata_check SET data = NULL WHERE data IS NOT NULL AND mismatch = 0 AND has_sidecar = 1")
        con.commit()
    finally:
        if own:
            con.close()


def init():
    """Called once at start: use the configured database, or fall back to SQLite with a recorded reason."""
    global error
    error = None
    c = settings()
    if c["type"] == "mysql":
        try:
            con = connect(cfg=c)
            try:
                ensure_schema(con)
            finally:
                con.close()
            return
        except Exception as e:  # the server is down, the password changed...: the viewer must still start
            error = f"{type(e).__name__}: {str(e)[:160]}"
            log.error("viewer database on MySQL unavailable (%s); using the SQLite file instead", error)
    ensure_schema()


# ---- testing, copying, switching
def test_connection(c: dict) -> dict:
    """Connect with the given settings (an empty password keeps the stored one) and count what is there."""
    c = {**settings(), **{k: v for k, v in c.items() if k in KEYS and v not in (None, "")}}
    c["type"] = "mysql" if str(c["type"]).lower() in ("mysql", "mariadb") else "sqlite"
    con = connect(cfg=c)
    try:
        if con.mysql:
            ver = con.execute("SELECT VERSION()").fetchone()[0]
            have = {r[0] for r in con.execute("SHOW TABLES")}
        else:
            ver = "SQLite " + sqlite3.sqlite_version
            have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        counts = {}
        for t in TABLES:
            if t in have:
                counts[t] = con.execute(f"SELECT count(*) FROM `{t}`" if con.mysql else f"SELECT count(*) FROM {t}").fetchone()[0]
        return {"ok": True, "version": ver, "tables": counts, "existing": sorted({OLD_NAMES.get(t, t) for t in have} & set(TABLES))}
    finally:
        con.close()


def copy_all(src: VConn, dst: VConn, drop: bool = True, progress=None) -> dict:
    """Every table, row by row, ids kept. drop: DROP the viewer tables at the target first (so a
    re-run or a stale copy never leaves rows behind); else existing rows are replaced by id."""
    counts = {}
    q = (lambda t: f"`{t}`") if dst.mysql else (lambda t: t)
    qs = (lambda t: f"`{t}`") if src.mysql else (lambda t: t)
    rename_old_tables(dst)  # a target last written by an older viewer
    if dst.mysql:  # digiKam's root tag has id 0, which MySQL would otherwise replace with the next AUTO_INCREMENT value
        dst.execute("SET SESSION sql_mode = CONCAT(@@sql_mode, ',NO_AUTO_VALUE_ON_ZERO')")
    for name, (cols, sq, my) in TABLES.items():  # the whole schema first, then the rows
        if drop:
            dst.execute(f"DROP TABLE IF EXISTS {q(name)}")
        dst.execute(my if dst.mysql else sq)
    if not dst.mysql:
        for sql in SQLITE_INDEXES:
            dst.execute(sql)
    dst.commit()
    for name, (cols, sq, my) in TABLES.items():
        present = ({r[0] for r in src.execute("SHOW TABLES")} if src.mysql
                   else {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")})
        have = columns(src, name) if name in present else []  # a table the source never had (hand-made schema) copies nothing
        if not have:
            counts[name] = 0
            dst.commit()
            continue
        use = [c for c in cols if c in have]
        collist = ", ".join(("`key`" if c == "key" and dst.mysql else c) for c in use)
        sel = ", ".join(("`key`" if c == "key" and src.mysql else c) for c in use)
        cur, n = src.execute(f"SELECT {sel} FROM {qs(name)}"), 0
        while True:  # in slices: the face embeddings and tag rows of a large library do not all sit in memory at once
            rows = cur.fetchmany(2000)
            if not rows:
                break
            dst.executemany(f"INSERT OR REPLACE INTO {q(name)} ({collist}) VALUES ({','.join('?' * len(use))})", [tuple(r) for r in rows])
            n += len(rows)
        dst.commit()
        counts[name] = n
        if progress:
            progress(name, len(rows))
    return counts


def switch(c: dict, copy: bool = True, drop: bool = True) -> dict:
    """Move the viewer to the given database: test it, (re)create the tables there, copy the data from
    the current one, save the configuration. The caller restarts the viewer afterwards."""
    cur = settings()
    c = {**cur, **{k: v for k, v in c.items() if k in KEYS and v not in (None, "")}}
    c["type"] = "mysql" if str(c["type"]).lower() in ("mysql", "mariadb") else "sqlite"
    if c["type"] == "sqlite":
        if cur["type"] == "sqlite":
            raise ValueError("The viewer already uses the SQLite file")
        if error is not None:  # the configured MySQL is unreachable: the viewer already runs on the
            with _lock:        # SQLite file, so there is nothing to copy; only the configuration changes
                save_settings(c)
            return {"ok": True, "copied": {}, "database": describe()}
    elif cur["type"] == "mysql" and all(str(c[k]) == str(cur[k]) for k in ("host", "port", "database")):
        # the configured database again: copying it onto itself would drop every table and then copy from the
        # empty result; while the viewer runs on the SQLite fallback (that server was unreachable at start) the
        # copy would come from the fallback file and overwrite what the server holds. Nothing is copied: the
        # connection is tested, the settings saved, and the restart brings the viewer back onto it.
        if error is None:
            raise ValueError("The viewer already uses that MySQL database")
        test_connection(c)
        with _lock:
            save_settings(c)
        return {"ok": True, "copied": {}, "database": describe()}
    counts = {}
    with _lock:
        dst = connect(cfg=c)
        try:
            if copy:
                src = connect()
                try:
                    counts = copy_all(src, dst, drop=drop)
                finally:
                    src.close()
            else:
                ensure_schema(dst)
        finally:
            dst.close()
        save_settings(c)
    return {"ok": True, "copied": counts, "database": describe()}


def snapshot_sqlite(dst_path: Path):
    """A SQLite copy of the current viewer database, whatever it runs on (the portable backup form)."""
    dst_path.unlink(missing_ok=True)
    if not is_mysql():
        src = sqlite3.connect(f"file:{quote(str(SQLITE_FILE))}?mode=ro", uri=True)
        try:
            out = sqlite3.connect(str(dst_path))
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        return
    src = connect()
    try:
        dst = VConn(_sqlite_raw(dst_path, 10), False, True)
        try:
            copy_all(src, dst, drop=True)
        finally:
            dst.close()
    finally:
        src.close()
