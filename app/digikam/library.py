"""The library itself, in the viewer's own database: photos, folders, tags, faces, captions...

These tables have exactly the columns of the digiKam tables they correspond to (Images -> photos,
Albums -> folders, ImageTagProperties -> face_regions, ...), only with names that say what they hold.
That is what lets the viewer work without digiKam:

- While Sync with digiKam is on, these tables follow digiKam's database (copy_from_digikam, in the
  background after the index is built): the rows on both sides are compared and only the rows that differ
  are deleted, inserted or replaced, so a change, made here or in digiKam, costs a few row writes.
- When Sync with digiKam is off, dkdb.connect() hands out a LibraryConn instead of a connection to
  digiKam's database. It speaks digiKam's table names and rewrites them to these, so every module that
  writes the library (tags, faces, folders, uploads, place tags, tag pictures, applying edits) works
  unchanged, on the viewer's own copy. Each change made that way is counted (library_info
  local_changes), so turning sync back on never silently replaces it with digiKam's data.
"""
import re
import time

# viewer table: (digiKam table, [(column, SQLite type, MySQL type)], SQLite constraints, MySQL constraints)
SPEC = {
    "album_roots": ("AlbumRoots", [("id", "INTEGER PRIMARY KEY", "INT AUTO_INCREMENT PRIMARY KEY"), ("label", "TEXT", "TEXT"), ("status", "INTEGER", "INT"),
                                    ("type", "INTEGER", "INT"), ("identifier", "TEXT", "TEXT"), ("specificPath", "TEXT", "TEXT"), ("caseSensitivity", "INTEGER", "INT")], "", ""),
    "folders": ("Albums", [("id", "INTEGER PRIMARY KEY", "INT AUTO_INCREMENT PRIMARY KEY"), ("albumRoot", "INTEGER", "INT"), ("relativePath", "TEXT", "VARCHAR(700)"),
                           ("date", "TEXT", "VARCHAR(32)"), ("caption", "TEXT", "TEXT"), ("collection", "TEXT", "TEXT"), ("icon", "INTEGER", "BIGINT"),
                           ("modificationDate", "TEXT", "VARCHAR(32)")],
                ", UNIQUE (albumRoot, relativePath)", ", UNIQUE KEY folders_path (albumRoot, relativePath)"),
    "photos": ("Images", [("id", "INTEGER PRIMARY KEY", "BIGINT AUTO_INCREMENT PRIMARY KEY"), ("album", "INTEGER", "INT"), ("name", "TEXT", "VARCHAR(500)"),
                          ("status", "INTEGER", "INT"), ("category", "INTEGER", "INT"), ("modificationDate", "TEXT", "VARCHAR(32)"), ("fileSize", "INTEGER", "BIGINT"),
                          ("uniqueHash", "TEXT", "VARCHAR(128)"), ("manualOrder", "INTEGER", "BIGINT")],
               ", UNIQUE (album, name)", ", UNIQUE KEY photos_name (album, name), INDEX photos_hash (uniqueHash)"),
    "photo_information": ("ImageInformation", [("imageid", "INTEGER PRIMARY KEY", "BIGINT PRIMARY KEY"), ("rating", "INTEGER", "INT"), ("creationDate", "TEXT", "VARCHAR(32)"),
                                               ("digitizationDate", "TEXT", "VARCHAR(32)"), ("orientation", "INTEGER", "INT"), ("width", "INTEGER", "INT"), ("height", "INTEGER", "INT"),
                                               ("format", "TEXT", "VARCHAR(64)"), ("colorDepth", "INTEGER", "INT"), ("colorModel", "INTEGER", "INT"), ("timeZone", "INTEGER", "INT")], "", ""),
    "photo_metadata": ("ImageMetadata", [("imageid", "INTEGER PRIMARY KEY", "BIGINT PRIMARY KEY"), ("make", "TEXT", "TEXT"), ("model", "TEXT", "TEXT"), ("lens", "TEXT", "TEXT"),
                                         ("aperture", "REAL", "DOUBLE"), ("focalLength", "REAL", "DOUBLE"), ("focalLength35", "REAL", "DOUBLE"), ("exposureTime", "REAL", "DOUBLE"),
                                         ("exposureProgram", "INTEGER", "INT"), ("exposureMode", "INTEGER", "INT"), ("sensitivity", "INTEGER", "INT"), ("flash", "INTEGER", "INT"),
                                         ("whiteBalance", "INTEGER", "INT"), ("whiteBalanceColorTemperature", "INTEGER", "INT"), ("meteringMode", "INTEGER", "INT"),
                                         ("subjectDistance", "REAL", "DOUBLE"), ("subjectDistanceCategory", "INTEGER", "INT")], "", ""),
    "photo_positions": ("ImagePositions", [("imageid", "INTEGER PRIMARY KEY", "BIGINT PRIMARY KEY"), ("latitude", "TEXT", "TEXT"), ("latitudeNumber", "REAL", "DOUBLE"),
                                           ("longitude", "TEXT", "TEXT"), ("longitudeNumber", "REAL", "DOUBLE"), ("altitude", "REAL", "DOUBLE"), ("orientation", "REAL", "DOUBLE"),
                                           ("tilt", "REAL", "DOUBLE"), ("roll", "REAL", "DOUBLE"), ("accuracy", "REAL", "DOUBLE"), ("description", "TEXT", "TEXT")], "", ""),
    "video_metadata": ("VideoMetadata", [("imageid", "INTEGER PRIMARY KEY", "BIGINT PRIMARY KEY"), ("aspectRatio", "TEXT", "TEXT"), ("audioBitRate", "TEXT", "TEXT"),
                                         ("audioChannelType", "TEXT", "TEXT"), ("audioCompressor", "TEXT", "TEXT"), ("duration", "TEXT", "TEXT"), ("frameRate", "TEXT", "TEXT"),
                                         ("exposureProgram", "INTEGER", "INT"), ("videoCodec", "TEXT", "TEXT")], "", ""),
    "comments": ("ImageComments", [("id", "INTEGER PRIMARY KEY", "BIGINT AUTO_INCREMENT PRIMARY KEY"), ("imageid", "INTEGER", "BIGINT"), ("type", "INTEGER", "INT"),
                                   ("language", "TEXT", "VARCHAR(128)"), ("author", "TEXT", "TEXT"), ("date", "TEXT", "VARCHAR(32)"), ("comment", "TEXT", "MEDIUMTEXT")],
                 "", ", INDEX comments_image (imageid)"),
    "copyright": ("ImageCopyright", [("id", "INTEGER PRIMARY KEY", "BIGINT AUTO_INCREMENT PRIMARY KEY"), ("imageid", "INTEGER", "BIGINT"), ("property", "TEXT", "TEXT"),
                                     ("value", "TEXT", "TEXT"), ("extraValue", "TEXT", "TEXT")], "", ", INDEX copyright_image (imageid)"),
    "photo_properties": ("ImageProperties", [("imageid", "INTEGER", "BIGINT"), ("property", "TEXT", "VARCHAR(255)"), ("value", "TEXT", "TEXT")],
                         ", UNIQUE (imageid, property)", ", UNIQUE KEY photo_properties_key (imageid, property)"),
    "photo_tags": ("ImageTags", [("imageid", "INTEGER", "BIGINT"), ("tagid", "INTEGER", "INT")], ", UNIQUE (imageid, tagid)", ", PRIMARY KEY (imageid, tagid), INDEX photo_tags_tag (tagid)"),
    "face_regions": ("ImageTagProperties", [("imageid", "INTEGER", "BIGINT"), ("tagid", "INTEGER", "INT"), ("property", "TEXT", "VARCHAR(255)"), ("value", "TEXT", "TEXT")],
                     "", ", INDEX face_regions_image (imageid), INDEX face_regions_tag (tagid)"),
    "tags": ("Tags", [("id", "INTEGER PRIMARY KEY", "INT AUTO_INCREMENT PRIMARY KEY"), ("pid", "INTEGER", "INT"), ("name", "TEXT", "VARCHAR(500)"), ("icon", "INTEGER", "BIGINT"),
                      ("iconkde", "TEXT", "TEXT")], ", UNIQUE (name, pid)", ", UNIQUE KEY tags_name (name, pid)"),
    "tag_properties": ("TagProperties", [("tagid", "INTEGER", "INT"), ("property", "TEXT", "VARCHAR(255)"), ("value", "TEXT", "TEXT")], "", ", INDEX tag_properties_tag (tagid)"),
    "library_settings": ("Settings", [("keyword", "TEXT", "VARCHAR(191)"), ("value", "TEXT", "TEXT")], ", UNIQUE (keyword)", ", UNIQUE KEY library_settings_key (keyword)"),
}
SQLITE_INDEXES = ["CREATE INDEX IF NOT EXISTS comments_image ON comments (imageid)", "CREATE INDEX IF NOT EXISTS copyright_image ON copyright (imageid)",
                  "CREATE INDEX IF NOT EXISTS face_regions_image ON face_regions (imageid)", "CREATE INDEX IF NOT EXISTS face_regions_tag ON face_regions (tagid)",
                  "CREATE INDEX IF NOT EXISTS photo_tags_tag ON photo_tags (tagid)", "CREATE INDEX IF NOT EXISTS tag_properties_tag ON tag_properties (tagid)",
                  "CREATE INDEX IF NOT EXISTS photos_hash ON photos (uniqueHash)"]
DIGIKAM = {dk: ours for ours, (dk, *_rest) in SPEC.items()}
_NAMES = re.compile(r"\b(" + "|".join(sorted(DIGIKAM, key=len, reverse=True)) + r")\b")
_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


def tables() -> dict:
    """vdb.TABLES entries for the library tables: name -> (columns, SQLite DDL, MySQL DDL)."""
    out = {}
    for name, (_dk, cols, sq_extra, my_extra) in SPEC.items():
        out[name] = ([c for c, _s, _m in cols],
                     f"CREATE TABLE IF NOT EXISTS {name} (" + ", ".join(f"{c} {s}" for c, s, _m in cols) + sq_extra + ")",
                     f"CREATE TABLE IF NOT EXISTS {name} (" + ", ".join(f"`{c}` {m}" for c, _s, m in cols) + my_extra + ")")
    return out


def rename(sql: str) -> str:
    """digiKam's table names in a statement -> the viewer's."""
    return _NAMES.sub(lambda m: DIGIKAM[m.group(1)], sql)


def _vconn_class():
    from app.data.vdb import VConn

    class LibraryConn(VConn):
        """A connection to the library tables that speaks digiKam's table names (see the module docstring)."""

        def __init__(self, raw, mysql, writable):
            super().__init__(raw, mysql, writable)
            self.changed = False
            self.standalone = True
            if mysql:  # keep digiKam's root tag (id 0) as it is when rows are written back
                raw.cursor().execute("SET SESSION sql_mode = CONCAT(@@sql_mode, ',NO_AUTO_VALUE_ON_ZERO')")

        def execute(self, sql: str, params=()):
            if _WRITE.match(sql):
                self.changed = True
            return super().execute(rename(sql), params)

        def executemany(self, sql: str, rows):
            if _WRITE.match(sql):
                self.changed = True
            return super().executemany(rename(sql), rows)

        def begin_write(self):
            ensure_base()  # before the first change made here: what digiKam held, for the merge back
            self.raw.execute("BEGIN IMMEDIATE") if not self.mysql else self.raw.cursor().execute("START TRANSACTION")

        def begin_read(self):
            if self.mysql:
                self.raw.cursor().execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")

        def commit(self):
            if self.changed:  # a change made here, not copied from digiKam: count it (see note_local_change)
                _bump(self)
                self.changed = False
            self.raw.commit()

        def integrity_ok(self) -> bool:
            return True

    return LibraryConn


_CLASS = None


def connect(write: bool = False):
    """The library tables, through digiKam's table names."""
    global _CLASS
    from app.data import vdb
    if _CLASS is None:
        _CLASS = _vconn_class()
    v = vdb.connect()
    return _CLASS(v.raw, v.mysql, write)


def _info(con, key: str, default=None):
    row = con.execute("SELECT value FROM library_info WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _set_info(con, pairs: dict):
    for k, v in pairs.items():
        con.execute("DELETE FROM library_info WHERE key = ?", (k,))
        con.execute("INSERT INTO library_info (key, value) VALUES (?,?)", (k, str(v)))


def _bump(con):
    """Inside the writer's transaction: one more change that digiKam does not have."""
    n = int(_info(con, "local_changes", "0") or 0) + 1
    v = int(_info(con, "version", "0") or 0) + 1
    _set_info(con, {"local_changes": n, "version": v, "changed": int(time.time())})


def info() -> dict:
    """{source, taken, photos, local_changes, version, changed} or {} when no copy exists yet."""
    from app.data import vdb
    try:
        con = vdb.connect()
        try:
            d = {r[0]: r[1] for r in con.execute("SELECT key, value FROM library_info").fetchall()}
        finally:
            con.close()
    except Exception:
        return {}
    return d


def version() -> str:
    """Changes whenever the library tables change (a copy from digiKam, or a change made here)."""
    d = info()
    if not int(d.get("local_changes") or 0) and d.get("source") not in (None, "-"):
        return d["source"]  # an untouched copy of digiKam: the same version as digiKam itself, so no rebuild
    return f"local:{d.get('source', '-')}:{d.get('version', '0')}"


def _canon(v):
    """One form for a value read from either database, so equal values compare equal (5 and 5.0, bytes and text)."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, int):
        return v
    try:
        f = float(v)  # float, Decimal
    except (TypeError, ValueError):
        return str(v)
    return int(f) if f.is_integer() else f


def _key_cols(cols: list) -> list:
    """The primary key of a library table: its id or imageid column; None for the tables without one,
    whose rows are compared whole."""
    first = cols[0]
    return [first[0]] if "PRIMARY KEY" in first[1] else None


def copy_from_digikam(src) -> dict:
    """Bring the library tables to digiKam's current rows, in one transaction, writing only what differs:
    rows digiKam no longer has are deleted, new and changed ones are written, the rest is left alone.
    src: a read connection to digiKam's database (dkdb.Conn) inside a snapshot. Returns {table: rows
    written}; the very first copy writes everything."""
    from app.digikam import dkdb
    from app.data import vdb
    t0 = time.time()
    theirs = {}
    for name, (dk, cols, *_rest) in SPEC.items():
        theirs[name] = [tuple(r) for r in src.execute(f"SELECT {', '.join(c for c, _s, _m in cols)} FROM {dk}").fetchall()]
    source = dkdb.source_version()
    ours = current()
    con = vdb.connect()
    written, photos = {}, len(theirs["photos"])
    try:
        if con.mysql:  # digiKam's MySQL root tag has id 0, which AUTO_INCREMENT would otherwise renumber
            con.execute("SET SESSION sql_mode = CONCAT(@@sql_mode, ',NO_AUTO_VALUE_ON_ZERO')")
        else:
            con.raw.execute("BEGIN IMMEDIATE")
        for name, (dk, cols, *_rest) in SPEC.items():
            names = [c for c, _s, _m in cols]
            q = (lambda c: f"`{c}`") if con.mysql else (lambda c: c)
            ql = ", ".join(q(c) for c in names)
            insert = f"INSERT INTO {name} ({ql}) VALUES ({', '.join('?' * len(names))})"
            keys = _key_cols(cols)
            gone, add = [], []
            if keys:  # one row per id: compare by id
                mine = {r[0]: tuple(_canon(v) for v in r) for r in ours[name]}
                for r in theirs[name]:
                    c = tuple(_canon(v) for v in r)
                    was = mine.pop(c[0], None)
                    if was != c:
                        if was is not None:
                            gone.append((c[0],))
                        add.append(r)
                gone += [(k,) for k in mine]
                for i in range(0, len(gone), 2000):
                    con.executemany(f"DELETE FROM {name} WHERE {q(keys[0])} = ?", gone[i:i + 2000])
            else:  # no key: rows are compared whole, duplicates counted
                from collections import Counter
                mine, want, raw_of = Counter(), Counter(), {}
                for r in ours[name]:
                    mine[tuple(_canon(v) for v in r)] += 1
                for r in theirs[name]:
                    c = tuple(_canon(v) for v in r)
                    want[c] += 1
                    raw_of.setdefault(c, r)
                same = "<=>" if con.mysql else "IS"
                where = " AND ".join(f"{q(c)} {same} ?" for c in names)
                for c in set(mine) | set(want):
                    if mine[c] != want[c]:
                        if mine[c]:
                            gone.append(c)
                        if want[c]:
                            add += [raw_of[c]] * want[c]
                for i in range(0, len(gone), 2000):
                    con.executemany(f"DELETE FROM {name} WHERE {where}", gone[i:i + 2000])
            for i in range(0, len(add), 2000):
                con.executemany(insert, add[i:i + 2000])
            if gone or add:
                written[name] = len(gone) + len(add)
        _set_info(con, {"source": source, "taken": int(time.time()), "photos": photos, "local_changes": 0,
                        "version": int(time.time()), "base_source": "-"})
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    import logging
    logging.getLogger("app.digikam.library").info("library brought up to date with digiKam in %.1fs: %s", time.time() - t0,
                                          ", ".join(f"{n} {k}" for k, n in written.items()) or "nothing changed")
    return written


BASE_FILE = None  # set from config below


def _base_path():
    from app import config
    return config.DATA_DIR / "library-base.sqlite"


def ensure_base():
    """The merge base: the library as it was while it still matched digiKam, saved once before the first change
    made here (sync off, or digiKam unreachable), rather than after every copy."""
    d = info()
    if not d.get("taken") or int(d.get("local_changes") or 0) or d.get("base_source") == d.get("source"):
        return
    save_base(current())
    from app.data import vdb
    con = vdb.connect()
    try:
        _set_info(con, {"base_source": d.get("source")})
        con.commit()
    finally:
        con.close()


def save_base(data: dict):
    """The rows as they came from digiKam, in a SQLite file: the base of the three-way merge that brings
    changes made with sync off back into digiKam (merge.py)."""
    import os
    import sqlite3
    path = _base_path()
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;")
        for name, (dk, cols, sq_extra, _my) in SPEC.items():
            names = [c for c, _s, _m in cols]
            con.execute(f"CREATE TABLE {name} (" + ", ".join(f"{c} {s}" for c, s, _m in cols) + ")")
            con.executemany(f"INSERT INTO {name} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", data[name])
        con.commit()
    finally:
        con.close()
    os.replace(tmp, path)


def load_base() -> dict:
    """{table: rows} from the base file, or None when there is none."""
    import sqlite3
    path = _base_path()
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for name, (dk, cols, *_rest) in SPEC.items():
            names = [c for c, _s, _m in cols]
            try:
                out[name] = [tuple(r) for r in con.execute(f"SELECT {', '.join(names)} FROM {name}").fetchall()]
            except sqlite3.OperationalError:
                out[name] = []
        return out
    finally:
        con.close()


def current() -> dict:
    """{table: rows} of the library tables as they are now."""
    from app.data import vdb
    con = vdb.connect()
    try:
        out = {}
        for name, (dk, cols, *_rest) in SPEC.items():
            names = [c for c, _s, _m in cols]
            out[name] = [tuple(r) for r in con.execute(f"SELECT {', '.join(names)} FROM {name}").fetchall()]
        return out
    finally:
        con.close()


def discard_local_changes():
    """Forget that changes were made here, so the next read of digiKam replaces the library with its data."""
    from app.data import vdb
    con = vdb.connect()
    try:
        _set_info(con, {"local_changes": 0, "source": "-"})
        con.commit()
    finally:
        con.close()
