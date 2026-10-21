"""One way to talk to digiKam's database, whichever backend digiKam uses.

digiKam keeps the same tables and columns on SQLite and on MySQL/MariaDB;
what differs is how you connect, the parameter marker, a few statements,
how dates come back, how you notice that digiKam changed something, and how
you take a backup. All of that lives here, so the rest of the app writes
plain SQL with `?` markers and SQLite-style `INSERT OR IGNORE`.

Settings (environment):
  DIGIKAM_DB_TYPE      sqlite (default) or mysql
  DIGIKAM_DB_DIR       folder with digikam4.db          (sqlite)
  DIGIKAM_DB_HOST/PORT/NAME/USER/PASSWORD                (mysql; NAME defaults to "digikam")
  DIGIKAM_DB_BACKUP    auto (default: a full copy before destructive operations only) or skip (never); daily, the old name, means auto
  DIGIKAM_DB_DUMP      optional: a command that writes a full dump to stdout, used instead of
                       mysqldump, e.g. "docker exec iopix-mariadb mariadb-dump -udigikam -pX digikam"
"""
import datetime as _dt
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from app import config

# The environment is the starting point; the admin can override it from Settings (the
# `db_*` settings, see settings()). Everything below reads the current values per call.
ENV = {"type": os.environ.get("DIGIKAM_DB_TYPE", "sqlite").lower(), "sqlite_dir": str(config.DIGIKAM_DB_DIR),
       "host": os.environ.get("DIGIKAM_DB_HOST", "127.0.0.1"), "port": os.environ.get("DIGIKAM_DB_PORT", "3306"),
       "database": os.environ.get("DIGIKAM_DB_NAME", "digikam"), "user": os.environ.get("DIGIKAM_DB_USER", "digikam"),
       "password": os.environ.get("DIGIKAM_DB_PASSWORD", ""), "backup": os.environ.get("DIGIKAM_DB_BACKUP", "auto")}
KEYS = tuple(ENV)
BACKUP_DIR = config.DATA_DIR / "db-backups"
KEEP_BACKUPS = 10

_INSERT_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.I)
_INSERT_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.I)


class SyncError(Exception):
    pass


def settings() -> dict:
    """The effective connection settings: a `db_<key>` setting wins over the environment."""
    from app.data import state
    out = dict(ENV)
    for k in KEYS:
        v = state.get_setting("db_" + k, "")
        if v != "":
            out[k] = v
    out["type"] = "mysql" if str(out["type"]).lower() in ("mysql", "mariadb") else "sqlite"
    try:
        out["port"] = int(out["port"])
    except (TypeError, ValueError):
        out["port"] = 3306
    return out


def sqlite_path() -> Path:
    return Path(settings()["sqlite_dir"]) / "digikam4.db"


def _mysql_kwargs() -> dict:
    c = settings()
    return {"host": c["host"], "port": c["port"], "database": c["database"], "user": c["user"], "password": c["password"]}


def test_connection(c: dict = None) -> dict:
    """Try the given (or current) settings without saving them: counts, DBVersion, album roots."""
    c = {**settings(), **{k: v for k, v in (c or {}).items() if v not in (None, "")}}
    if str(c["type"]).lower() in ("mysql", "mariadb"):
        import pymysql
        raw = pymysql.connect(host=c["host"], port=int(c["port"]), database=c["database"], user=c["user"], password=c["password"], connect_timeout=5)
        con = Conn(raw, True, False)
    else:
        path = Path(c["sqlite_dir"]) / "digikam4.db"
        if not path.exists():
            raise SyncError(f"No digikam4.db in {c['sqlite_dir']}")
        con = Conn(sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True), False, False)
    try:
        images = con.execute("SELECT count(*) FROM Images WHERE status = 1").fetchone()[0]
        tags = con.execute("SELECT count(*) FROM Tags").fetchone()[0]
        ver = con.execute("SELECT value FROM Settings WHERE keyword = 'DBVersion'").fetchone()
        roots = [r[0] for r in con.execute("SELECT specificPath FROM AlbumRoots")]
        return {"ok": True, "images": images, "tags": tags, "dbVersion": ver[0] if ver else None, "roots": roots}
    finally:
        con.close()


def is_mysql() -> bool:
    return settings()["type"] == "mysql"


def describe() -> str:
    c = settings()
    return f"MySQL {c['user']}@{c['host']}:{c['port']}/{c['database']}" if is_mysql() else str(sqlite_path())


# --------------------------------------------------------------------------
# dates: SQLite rows carry 'YYYY-MM-DDTHH:MM:SS.mmm' strings; MySQL DATETIME
# columns come back as datetime objects. Everything above this layer sees
# the SQLite form, and may write either form.
# --------------------------------------------------------------------------

# How digiKam's MySQL/MariaDB database holds date-times. digiKam's source converts them to UTC for QMYSQL
# connections (BdEngineBackend::asDBDateTime), but real libraries are found holding local time (the owner's
# library: all 30,267 Images.modificationDate values equal the files' local modification times). So it is
# measured, once, by comparing Images.modificationDate with the files' own times; DIGIKAM_MYSQL_DATES=local|utc
# settles it by hand. Getting it wrong shifts every date by the server's UTC offset, both ways.
_dates = {"utc": None}


def mysql_dates_utc() -> bool:
    forced = os.environ.get("DIGIKAM_MYSQL_DATES", "").strip().lower()
    if forced in ("local", "utc"):
        return forced == "utc"
    if not is_mysql():
        return False
    if _dates["utc"] is None:
        _dates["utc"] = _detect_mysql_dates()
    return _dates["utc"]


def _detect_mysql_dates() -> bool:
    """True when Images.modificationDate (the later of the file's and its sidecar's modification time, as
    digiKam's scanner records it) matches the files in UTC rather than in local time; local when unsure."""
    try:
        import pymysql
        raw = pymysql.connect(**_mysql_kwargs(), charset="utf8mb4", connect_timeout=10, read_timeout=60)
    except Exception:
        return False
    try:
        cur = raw.cursor()
        cur.execute("SELECT id, specificPath FROM AlbumRoots")
        roots = resolve_root_paths(cur.fetchall())
        cur.execute("SELECT a.albumRoot, a.relativePath, i.name, i.modificationDate FROM Images i JOIN Albums a ON a.id = i.album "
                    "WHERE i.status = 1 AND i.modificationDate IS NOT NULL ORDER BY i.id DESC LIMIT 200")
        local = utc = 0
        for root, rel, name, md in cur.fetchall():
            p = os.path.join(roots.get(root, ""), (rel or "/").strip("/"), name)
            try:
                m = os.stat(p).st_mtime
            except OSError:
                continue
            for side in (p + ".xmp", os.path.splitext(p)[0] + ".xmp"):
                try:
                    m = max(m, os.stat(side).st_mtime)
                except OSError:
                    pass
            if abs((md - _dt.datetime.fromtimestamp(m)).total_seconds()) <= 2:
                local += 1
            elif abs((md - _dt.datetime.fromtimestamp(m, _dt.timezone.utc).replace(tzinfo=None)).total_seconds()) <= 2:
                utc += 1
        import logging
        logging.getLogger("app.digikam.dkdb").info("digiKam's MySQL dates are %s time (%d files matched in local time, %d in UTC)",
                                           "UTC" if utc > local and utc >= 5 else "local", local, utc)
        return utc > local and utc >= 5
    except Exception:
        return False
    finally:
        raw.close()


def _norm_plain(v):
    """Dates as ISO text without any time-zone conversion (the viewer's own database)."""
    if isinstance(v, _dt.datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S.") + f"{v.microsecond // 1000:03d}"
    if isinstance(v, _dt.date):
        return v.strftime("%Y-%m-%dT00:00:00.000")
    return v


def _norm_value(v):
    if isinstance(v, _dt.datetime):
        # the app works in local time, digiKam's SQLite convention; a MySQL library holding UTC is converted
        if mysql_dates_utc():
            v = v.replace(tzinfo=_dt.timezone.utc).astimezone().replace(tzinfo=None)
        return v.strftime("%Y-%m-%dT%H:%M:%S.") + f"{v.microsecond // 1000:03d}"
    if isinstance(v, _dt.date):
        return v.strftime("%Y-%m-%dT00:00:00.000")
    return v


_ISO_DT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?")


def looks_like_datetime(s: str) -> bool:
    """Whether a string parameter is a bare ISO timestamp (what the viewer writes into DATETIME columns), and not
    text that merely starts with one (a caption, a title, a tag name)."""
    return bool(_ISO_DT.fullmatch(s))


def db_datetime(s):
    """A value this backend accepts in a DATETIME/TEXT column, from an ISO string (local time)."""
    if s is None or not is_mysql():
        return s
    try:
        local = _dt.datetime.fromisoformat(str(s)[:23])
        return local.astimezone().astimezone(_dt.timezone.utc).replace(tzinfo=None) if mysql_dates_utc() else local
    except ValueError:
        return s


class _Cursor:
    """Cursor-like result: fetchone / fetchall / iteration / lastrowid / rowcount."""

    def __init__(self, cur, mysql: bool):
        self._cur, self._mysql = cur, mysql

    def _fix(self, row):
        if row is None or not self._mysql:
            return row
        return tuple(_norm_value(v) for v in row)

    def fetchone(self):
        return self._fix(self._cur.fetchone())

    def fetchall(self):
        return [self._fix(r) for r in self._cur.fetchall()]

    def __iter__(self):
        for r in self._cur:
            yield self._fix(r)

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount


class Conn:
    def __init__(self, raw, mysql: bool, writable: bool):
        self.raw, self.mysql, self.writable = raw, mysql, writable

    def _sql(self, sql: str) -> str:
        if not self.mysql:
            return sql
        sql = _INSERT_IGNORE.sub("INSERT IGNORE", sql)
        sql = _INSERT_REPLACE.sub("REPLACE", sql)
        return sql.replace("%", "%%").replace("?", "%s")

    def _params(self, params):
        return tuple(db_datetime(p) if isinstance(p, str) and looks_like_datetime(p) else p for p in params) if self.mysql else params

    def execute(self, sql: str, params=()):
        if self.mysql:
            cur = self.raw.cursor()
            cur.execute(self._sql(sql), self._params(tuple(params)))
            return _Cursor(cur, True)
        return _Cursor(self.raw.execute(sql, params), False)

    def executemany(self, sql: str, rows):
        if self.mysql:
            cur = self.raw.cursor()
            cur.executemany(self._sql(sql), [self._params(tuple(r)) for r in rows])
            return _Cursor(cur, True)
        return _Cursor(self.raw.executemany(sql, rows), False)

    def begin_write(self):
        self.execute("START TRANSACTION" if self.mysql else "BEGIN IMMEDIATE")

    def begin_read(self):
        self.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT" if self.mysql else "BEGIN")

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    def integrity_ok(self) -> bool:
        """SQLite's quick_check; MySQL keeps its own consistency, so nothing to run."""
        if self.mysql:
            return True
        return self.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def sync_enabled() -> bool:
    """Settings > Sync with digiKam. Off: the viewer never opens digiKam's database; the library lives in
    the viewer's own tables (library.py) and every change is made there."""
    from app.data import state
    return state.get_setting("digikam_sync", "1") != "0"


def standalone() -> bool:
    return not sync_enabled()


def connect(write: bool = False, db_path=None, real: bool = False) -> Conn:
    """A connection to digiKam's database (read connections on SQLite are opened read-only at the file
    level, so a bug can never write through them). With Sync with digiKam off, the library tables in the
    viewer's own database instead, through digiKam's table names, so every reader and writer works as is.
    real: digiKam's database whatever the setting (copying it into the library)."""
    if not real and not sync_enabled():
        from app.digikam import library
        return library.connect(write)
    if is_mysql():
        try:
            import pymysql
        except ImportError:
            raise SyncError("MySQL support needs the PyMySQL package (pip install pymysql)")
        raw = pymysql.connect(**_mysql_kwargs(), charset="utf8mb4", autocommit=False, read_timeout=120, write_timeout=120)
        if not write:
            raw.cursor().execute("SET SESSION TRANSACTION READ ONLY")
        return Conn(raw, True, write)
    path = Path(db_path or sqlite_path())
    uri = f"file:{quote(str(path))}?mode={'rw' if write else 'ro'}"
    raw = sqlite3.connect(uri, uri=True, timeout=30 if not write else 5, isolation_level=None if write else "")
    if not write:
        raw.execute("PRAGMA query_only = ON")
    return Conn(raw, False, write)


# --------------------------------------------------------------------------
# small reads several modules need in the same shape
# --------------------------------------------------------------------------

def tag_tree(con) -> dict:
    """{tag id: (parent id, name)} for every tag. MySQL seeds an id-0 root row
    ("_Digikam_root_tag_") that is skipped; a pid of 0 means "no parent" on both backends."""
    return {tid: (pid, name) for tid, pid, name in con.execute("SELECT id, pid, name FROM Tags") if tid != 0}


def tag_path(tags: dict, tid) -> str:
    """'Grandparent/Parent/Tag' for a tag in a tag_tree(); a cycle in the data cannot loop."""
    parts, seen = [], set()
    while tid in tags and tid not in seen:
        seen.add(tid)
        parts.append(tags[tid][1])
        tid = tags[tid][0]
    return "/".join(reversed(parts))


def root_overrides() -> dict:
    """{path digiKam recorded: local path} from the `album_roots` setting (Settings > Album roots)."""
    import json
    from app.data import state
    try:
        d = json.loads(state.get_setting("album_roots", "") or "{}")
        return {str(k).rstrip("/"): str(v).rstrip("/") for k, v in d.items() if v}
    except (ValueError, AttributeError):
        return {}


def root_paths(con) -> dict:
    """{album root id: local folder}. digiKam records where each album root was on its own
    machine; here it may be mounted elsewhere (Docker, another host). A saved override per
    root wins, else a single-root library sits at PHOTOS_ROOT, else digiKam's path."""
    return resolve_root_paths(con.execute("SELECT id, specificPath FROM AlbumRoots").fetchall())


def resolve_root_paths(rows) -> dict:
    """root_paths() for rows of (root id, digiKam's specificPath), from wherever they were read."""
    over = root_overrides()
    out = {}
    for rid, sp in rows:
        key = (sp or "").rstrip("/")
        out[rid] = over.get(key) or (str(config.PHOTOS_ROOT) if len(rows) == 1 else sp)
    return out


def album_roots() -> list:
    """For Settings: each album root digiKam knows, with the local path in use and whether it is there."""
    with connect() as con:
        rows = con.execute("SELECT id, label, specificPath FROM AlbumRoots").fetchall()
        paths = root_paths(con)
    over = root_overrides()
    out = []
    for rid, label, sp in rows:
        local = paths[rid]
        out.append({"id": rid, "label": label, "digikamPath": sp, "localPath": local, "overridden": (sp or "").rstrip("/") in over,
                    "exists": os.path.isdir(local), "sample": _sample_count(local)})
    return out


def _sample_count(folder: str, limit: int = 2000) -> int:
    """How many files a quick walk finds (capped), to tell an empty mount from the real library."""
    n = 0
    try:
        for root, _dirs, files in os.walk(folder):
            n += len(files)
            if n >= limit:
                return limit
    except OSError:
        pass
    return n


# --------------------------------------------------------------------------
# did digiKam change something?
# --------------------------------------------------------------------------

def source_version() -> str:
    """Changes whenever digiKam writes to its database."""
    if not is_mysql():
        st = os.stat(sqlite_path())
        return f"{st.st_mtime_ns}-{st.st_size}"
    with connect(real=True) as con:
        scanned = con.execute("SELECT value FROM Settings WHERE keyword = 'Scanned'").fetchone()
        parts = [str(scanned[0]) if scanned else "-"]
        # counts catch additions and removals; the CRC sums catch edits in place (a caption, a rating, a
        # date, a position, a swapped tag, a tag's badge picture, face properties), whether digiKam or the
        # viewer made them: a count alone misses an edited caption, and the edit then never shows as applied
        for sql in ("SELECT count(*), max(id), SUM(CRC32(CONCAT(id,'|',COALESCE(album,''),'|',name,'|',status,'|',COALESCE(modificationDate,''),'|',COALESCE(uniqueHash,'')))) FROM Images",
                    # the path catches a folder renamed or moved (its modificationDate does not change), the rest its properties
                    "SELECT count(*), max(modificationDate), SUM(CRC32(CONCAT(id,'|',albumRoot,'|',relativePath,'|',COALESCE(caption,''),'|',COALESCE(collection,''),'|',COALESCE(icon,0)))) FROM Albums",
                    "SELECT count(*), SUM(CRC32(CONCAT(imageid,'|',tagid))) FROM ImageTags",
                    "SELECT count(*), max(id), SUM(CRC32(CONCAT(id,'|',COALESCE(pid,0),'|',name,'|',COALESCE(icon,0),'|',COALESCE(iconkde,'')))) FROM Tags",
                    "SELECT count(*), SUM(CRC32(CONCAT(tagid,'|',property,'|',COALESCE(value,'')))) FROM TagProperties",
                    "SELECT count(*), SUM(CRC32(CONCAT(imageid,'|',tagid,'|',property,'|',COALESCE(value,'')))) FROM ImageTagProperties",
                    "SELECT count(*), SUM(CRC32(CONCAT(id,'|',imageid,'|',type,'|',COALESCE(language,''),'|',COALESCE(comment,'')))) FROM ImageComments",
                    "SELECT count(*), SUM(CRC32(CONCAT(imageid,'|',COALESCE(latitudeNumber,''),'|',COALESCE(longitudeNumber,''),'|',COALESCE(altitude,'')))) FROM ImagePositions",
                    "SELECT count(*), SUM(CRC32(CONCAT(imageid,'|',COALESCE(rating,''),'|',COALESCE(creationDate,''),'|',COALESCE(orientation,'')))) FROM ImageInformation",
                    "SELECT count(*), SUM(CRC32(CONCAT(imageid,'|',property,'|',COALESCE(value,'')))) FROM ImageCopyright"):
            parts.append(",".join(str(_norm_value(v)) for v in con.execute(sql).fetchone()))
        return "|".join(parts)


def file_stamp(path: str):
    """(modificationDate, fileSize) as digiKam's scanner records them for a file: the later of the file's and its
    sidecar's modification times, local ISO text, and the file's size (ItemScanner::prepareAddImage,
    CollectionScanner::scanFileNormal). None when the file is not there."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    m = st.st_mtime
    for side in (path + ".xmp", os.path.splitext(path)[0] + ".xmp"):
        try:
            m = max(m, os.stat(side).st_mtime)
        except OSError:
            pass
    t = _dt.datetime.fromtimestamp(m)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}", st.st_size


def stamp_files(items: list):
    """items: [(digiKam image id, path)] whose sidecar the viewer just rewrote with what digiKam's database already
    holds. digiKam rescans a file when its time or size no longer matches Images (modificationDate, fileSize),
    and the rescan re-reads face regions, tags and captions into the database; a region read back one pixel off
    used to be added next to the existing one. So, as digiKam does after writing metadata itself, the row takes
    the new time (the later of file and sidecar).
    Only when the image file itself is unchanged: digiKam's uniqueHash covers the file's Exif, first 8 KB and
    size (DImg::createUniqueHash), so a file rewritten in place needs digiKam's own rescan, which recomputes the
    hash and reads back what the viewer wrote; that photo is left to it. Albums.modificationDate is left alone:
    digiKam still looks into the folder, and finds these files unchanged and any other file that is."""
    if not items or standalone():
        return 0
    rows = []
    try:
        con = connect(write=True)
        try:
            ids = ",".join(str(int(i)) for i, _p in items)
            known = {r[0]: (r[1], r[2]) for r in con.execute(f"SELECT id, modificationDate, fileSize FROM Images WHERE id IN ({ids})").fetchall()}
            for iid, path in items:
                st, was = file_stamp(path), known.get(int(iid))
                if not st or not was:
                    continue
                try:
                    recorded = _dt.datetime.fromisoformat(str(was[0])[:23]).timestamp() if was[0] else 0
                    own = os.stat(path).st_mtime
                except (OSError, ValueError):
                    continue
                if st[1] != was[1] or own > recorded + 2:
                    continue  # the image file itself was rewritten: digiKam's rescan takes care of it (new hash)
                rows.append((st[0], int(iid)))
            if rows:
                with write_lock:
                    con.begin_write()
                    con.executemany("UPDATE Images SET modificationDate = ? WHERE id = ?", rows)
                    con.commit()
                    note_own_write()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
    except Exception as e:  # digiKam then rescans these files once; nothing is lost
        import logging
        logging.getLogger("app.digikam.dkdb").warning("file dates were not updated in digiKam's database: %s", e)
        return 0
    return len(rows)


_own_write = {"at": 0.0}  # when this viewer last committed a write to digiKam's database
import threading as _threading
write_lock = _threading.RLock()  # the viewer's own writes to digiKam's database, one at a time (SQLite journal checks)


def note_own_write():
    _own_write["at"] = time.time()


def busy_recently(seconds: float = 3) -> bool:
    """SQLite: digiKam wrote the file within the last few seconds, so wait a bit. A write this viewer
    just made is not digiKam busy: the index is rebuilt at once, so the change shows without the wait."""
    if is_mysql() or standalone():
        return False
    try:
        mtime = os.stat(sqlite_path()).st_mtime
    except OSError:
        return False
    if abs(mtime - _own_write["at"]) < 2:
        return False
    return time.time() - mtime < seconds


def journal_pending(db_path=None) -> bool:
    """An unfinished SQLite write by digiKam (journal/WAL file left behind)."""
    if is_mysql() or standalone():
        return False
    p = str(db_path or sqlite_path())
    return Path(p + "-journal").exists() or Path(p + "-wal").exists()


def last_change_time():
    """When digiKam last changed its database (with sync off: the library), as a unix time, if known."""
    if standalone():
        from app.digikam import library
        d = library.info()
        return int(d.get("changed") or d.get("taken") or 0) or None
    if not is_mysql():
        try:
            return int(os.stat(sqlite_path()).st_mtime)
        except OSError:
            return None
    try:
        with connect() as con:
            row = con.execute("SELECT value FROM Settings WHERE keyword = 'Scanned'").fetchone()
        return int(_dt.datetime.fromisoformat(str(row[0])[:19]).timestamp()) if row else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# backups before writing
# --------------------------------------------------------------------------



def backup(db_path=None, force: bool = False):
    """A full copy of digiKam's database, only before a destructive operation (force=True: deleting photos,
    deleting a tag, merging changes made with sync off, or the backup download). Ordinary changes (a face, a
    tag, a person, an edit, a folder, an upload) take none: a full copy on every change made them slow, and
    each of those is recorded in the change log with what it changed. SQLite: the file. MySQL: a mysqldump
    when the tool is installed, else the operation is refused. Backup mode `skip`: never (the owner backs
    the server up another way). With Sync with digiKam off the change goes to the library tables in the
    viewer's own database, which the app backup covers: nothing to copy here."""
    if not force or standalone():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp, n = time.strftime("%Y%m%d-%H%M%S"), 0
    if not is_mysql():
        src = Path(db_path or sqlite_path())
        dst = BACKUP_DIR / f"digikam4-{stamp}.db"
        while dst.exists():
            n += 1
            dst = BACKUP_DIR / f"digikam4-{stamp}-{n}.db"
        shutil.copy2(src, dst)
        _prune("digikam4-*.db")
        return dst
    if settings()["backup"] == "skip":
        return None
    custom = os.environ.get("DIGIKAM_DB_DUMP", "").strip()
    tool = shutil.which("mysqldump") or shutil.which("mariadb-dump")
    if not tool and not custom:
        raise SyncError("No mysqldump/mariadb-dump on this machine to back the database up first. Install one, set "
                        "DIGIKAM_DB_DUMP to a command that dumps it (e.g. through docker exec), or set DIGIKAM_DB_BACKUP=skip "
                        "if the server is backed up another way.")
    dst = BACKUP_DIR / f"digikam-mysql-{stamp}.sql"
    while dst.exists():
        n += 1
        dst = BACKUP_DIR / f"digikam-mysql-{stamp}-{n}.sql"
    c = settings()
    env = dict(os.environ, MYSQL_PWD=c["password"])
    cmd = shlex.split(custom) if custom else [tool, "--single-transaction", "--routines", "--triggers", f"--host={c['host']}",
                                               f"--port={c['port']}", f"--user={c['user']}", c["database"]]
    with open(dst, "wb") as f:
        res = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, env=env, timeout=1800)
    if res.returncode != 0 or dst.stat().st_size < 1024:
        dst.unlink(missing_ok=True)
        raise SyncError("Database dump failed: " + res.stderr.decode("utf-8", "replace")[:300])
    _prune("digikam-mysql-*.sql")
    return dst


def _prune(pattern: str):
    for old in sorted(BACKUP_DIR.glob(pattern))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
