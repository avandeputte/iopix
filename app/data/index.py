"""Builds the app's own search index from the library tables (library.py).

The library tables in the viewer's database carry digiKam's columns. While Sync with digiKam is on
they are copied from digiKam's database whenever it changed (refresh_library); with sync off, or
when digiKam's database is unreachable, they are what the viewer has, and the index is built from
them either way, with one code path. The build also keeps photo_identity, the viewer's stable
identity for a photo (digiKam's uniqueHash and size), through which favorites, albums, edits and
analysis results follow a photo if digiKam ever renumbers it.

The result is a small denormalised SQLite file with a full-text table, stored in the app's data
folder and swapped in atomically. Bump SCHEMA_VERSION whenever the tables, indexes or the meaning
of a column change, so running viewers rebuild.
"""
import calendar
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from app.ai import analysis, moments
from app import config
from app.digikam import dkdb, library
from app.photos import dateprecision, edits
from app.geo import places
from app.data import state as appstate, vdb
from app.digikam.dkdb import chunked

log = logging.getLogger("app.data.index")
SOURCE_ERRORS = (*vdb.Error, OSError, dkdb.SyncError)  # digiKam's database cannot be read right now


class Unavailable(Exception):
    """Neither digiKam's database nor a copy of the library can be read."""

SCHEMA = """
CREATE TABLE photos (
    id INTEGER PRIMARY KEY, album_id INTEGER, name TEXT, path TEXT,
    taken TEXT, ts INTEGER, day TEXT, year INTEGER, month INTEGER, md TEXT,
    w INTEGER, h INTEGER, fmt TEXT, video INTEGER, duration REAL,
    size INTEGER, rating INTEGER, pick INTEGER, color INTEGER,
    camera TEXT, make TEXT, model TEXT, lens TEXT,
    aperture REAL, focal REAL, focal35 REAL, exposure REAL, iso INTEGER, flash INTEGER,
    lat REAL, lon REAL, alt REAL,
    title TEXT, caption TEXT, creator TEXT, copyright TEXT,
    place TEXT, region TEXT, country TEXT,
    hash TEXT, modified TEXT, mts INTEGER, ntags INTEGER, extra TEXT, edited INTEGER,
    sharp REAL, sharpn REAL, bright REAL, clip_lo REAL, clip_hi REAL, faceq REAL,
    npeople INTEGER, objects TEXT, ocr TEXT, burst INTEGER, burst_best INTEGER, caption_ai TEXT,
    dprec INTEGER, aesth REAL, aesthn REAL, focusn REAL, eyes_checked INTEGER, eyes_closed INTEGER, reject TEXT
);
CREATE INDEX photos_ts ON photos (ts);
CREATE INDEX photos_day ON photos (day);
CREATE INDEX photos_album ON photos (album_id);
CREATE TABLE tags (id INTEGER PRIMARY KEY, pid INTEGER, name TEXT, full TEXT,
                   person INTEGER, n INTEGER, n_all INTEGER, icon INTEGER);
CREATE TABLE tag_closure (anc INTEGER, tag INTEGER, PRIMARY KEY (anc, tag)) WITHOUT ROWID;
CREATE TABLE photo_tags (tag_id INTEGER, photo_id INTEGER, PRIMARY KEY (tag_id, photo_id)) WITHOUT ROWID;
CREATE INDEX photo_tags_photo ON photo_tags (photo_id);
CREATE TABLE albums (id INTEGER PRIMARY KEY, pid INTEGER, path TEXT, name TEXT, root TEXT,
                     caption TEXT, n INTEGER, n_all INTEGER, cover INTEGER, ts_min INTEGER, ts_max INTEGER,
                     adate TEXT, category TEXT, icon INTEGER);
CREATE TABLE album_closure (anc INTEGER, album INTEGER, PRIMARY KEY (anc, album)) WITHOUT ROWID;
CREATE TABLE faces (photo_id INTEGER, tag_id INTEGER, x INTEGER, y INTEGER, w INTEGER, h INTEGER, kind TEXT DEFAULT 'confirmed');
CREATE INDEX faces_kind ON faces (kind);
CREATE INDEX faces_photo ON faces (photo_id);
CREATE INDEX faces_tag ON faces (tag_id);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE fts USING fts5(
    name, album, title, caption, tags, people, camera, misc, ocr, caption_ai,
    tokenize = "unicode61 remove_diacritics 2", prefix = '1 2 3'
);
"""

PICK = {"Pick Label None": 0, "Pick Label Rejected": 1, "Pick Label Pending": 2, "Pick Label Accepted": 3}
COLORS = ["None", "Red", "Orange", "Yellow", "Green", "Blue", "Magenta", "Gray", "Black", "White"]
COLOR = {f"Color Label {n}": i for i, n in enumerate(COLORS)}
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
SPECIAL_PERSON_PROPS = ("unknownPerson", "ignoredPerson", "unconfirmedPerson")
SCHEMA_VERSION = "13"  # 10: folder date, category and chosen cover; 11: AI descriptions without filler; 12: dprec (approximate dates); 13: photo quality (aesthetic, focus, eyes, likely rejects)
RECT_RE = re.compile(r'x="(-?\d+)" y="(-?\d+)" width="(\d+)" height="(\d+)"')


def source_version() -> str:
    """Changes whenever digiKam writes to its database."""
    return dkdb.source_version()


def sync_enabled() -> bool:
    return dkdb.sync_enabled()


# whether digiKam's database answered the last time it was needed, and what the index was last built from
offline = {"since": None, "error": None, "checked": None}
last_source = {"kind": None, "version": None}
_offline_lock = threading.Lock()


def _set_online():
    with _offline_lock:
        if offline["since"]:
            log.info("digiKam's database is reachable again")
        offline.update(since=None, error=None, checked=int(time.time()))


def _set_offline(err):
    with _offline_lock:
        if not offline["since"]:
            log.warning("digiKam's database is unreachable; working from the viewer's copy of the library: %s", err)
            offline["since"] = int(time.time())
        offline.update(error=str(err)[:300], checked=int(time.time()))


def mirror_info() -> dict:
    """{source, taken, photos, local_changes} of the library tables, or None when they were never filled."""
    d = library.info()
    if not d.get("taken"):
        return None
    return {"source": d.get("source"), "taken": int(d.get("taken") or 0), "photos": int(d.get("photos") or 0),
            "local_changes": int(d.get("local_changes") or 0), "changed": int(d.get("changed") or 0) or None}


conflict = {"digikam": None}  # digiKam's version while the library holds changes made with sync off


def probe() -> tuple:
    """Where the next build reads from. ('digikam', its version): sync on, digiKam answers, and the library
    holds nothing digiKam lacks; the library tables are refreshed from it first. ('library', their version):
    sync off, digiKam unreachable, or changes made with sync off that a copy would overwrite. With no digiKam
    at all, a library never filled is started fresh first."""
    m = mirror_info()
    if not m and dkdb.no_digikam() and library.start_new():
        m = mirror_info()
    if sync_enabled():
        try:
            v = dkdb.source_version()
            _set_online()
            if m and m["local_changes"]:
                conflict["digikam"] = v
                return "library", library.version()
            conflict["digikam"] = None
            return "digikam", v
        except SOURCE_ERRORS as e:
            _set_offline(e)
    if not m:
        raise Unavailable("digiKam's database cannot be read and the viewer has no copy of the library yet"
                          + (f": {offline['error']}" if offline.get("error") and sync_enabled() else " (Sync with digiKam is off)"))
    return "library", library.version()


def status() -> dict:
    """For /api/status and the banner."""
    m = mirror_info()
    return {"sync": sync_enabled(), "none": dkdb.no_digikam(), "reachable": offline["since"] is None, "since": offline["since"], "error": offline["error"],
            "source": last_source["kind"], "mirror": m, "localChanges": (m or {}).get("local_changes", 0),
            "conflict": bool(conflict["digikam"]) and sync_enabled()}


# ---- the rows the index is built from, read with digiKam's table names (from the library tables)
IMAGE_SQL = """
    SELECT i.id, i.album, i.name, i.fileSize, i.uniqueHash, i.modificationDate, i.category,
           ii.rating, ii.creationDate, ii.orientation, ii.width, ii.height, ii.format,
           m.make, m.model, m.lens, m.aperture, m.focalLength, m.focalLength35, m.exposureTime,
           m.sensitivity, m.flash, m.whiteBalance, m.meteringMode, m.exposureProgram,
           p.latitudeNumber, p.longitudeNumber, p.altitude, p.orientation AS gpsOrientation, p.accuracy AS gpsAccuracy,
           v.duration, v.videoCodec, v.frameRate, v.audioCompressor, v.aspectRatio
    FROM Images i
    LEFT JOIN ImageInformation ii ON ii.imageid = i.id
    LEFT JOIN ImageMetadata m ON m.imageid = i.id
    LEFT JOIN ImagePositions p ON p.imageid = i.id
    LEFT JOIN VideoMetadata v ON v.imageid = i.id
    WHERE i.status = 1 AND i.album IS NOT NULL AND i.category IN (1, 2)
"""


def read_rows(src) -> dict:
    """One consistent snapshot of what the index needs, through a connection that speaks digiKam's names."""
    src.begin_read()
    try:
        q = src.execute
        return {
            "roots": [tuple(r) for r in q("SELECT id, label, specificPath FROM AlbumRoots")],
            "albums": [tuple(r) for r in q("SELECT id, albumRoot, relativePath, caption, date, collection, icon FROM Albums")],
            "tags": [tuple(r) for r in q("SELECT id, pid, name, icon FROM Tags")],
            "tagprops": [tuple(r) for r in q("SELECT tagid, property, value FROM TagProperties")],
            "imagetags": [tuple(r) for r in q("SELECT imageid, tagid FROM ImageTags")],
            "regions": [tuple(r) for r in q("SELECT imageid, tagid, property, value FROM ImageTagProperties "
                                            "WHERE property IN ('tagRegion', 'autodetectedFace', 'ignoredFace')")],
            "comments": [tuple(r) for r in q("SELECT id, imageid, type, comment, language, author FROM ImageComments ORDER BY id")],
            "copyright": [tuple(r) for r in q("SELECT imageid, property, value FROM ImageCopyright")],
            "props": [tuple(r) for r in q("SELECT imageid, property, value FROM ImageProperties")],
            "images": [tuple(r) for r in q(IMAGE_SQL).fetchall()],
        }
    finally:
        src.rollback()


_copy_lock = threading.Lock()
_copy_wanted = threading.Event()


def refresh_library_soon():
    """Bring the viewer's copy of the library up to date in the background, one copy at a time; changes that
    arrive meanwhile are folded into one more copy. Skipped when sync was turned off since, or when the copy
    holds changes made with sync off (those are merged, never overwritten)."""
    _copy_wanted.set()
    if _copy_lock.locked():
        return

    def run():
        with _copy_lock:
            while _copy_wanted.is_set():
                _copy_wanted.clear()
                try:
                    m = mirror_info()
                    if not sync_enabled() or (m and m["local_changes"]):
                        return
                    if m and m["source"] == dkdb.source_version():
                        continue
                    refresh_library()
                except SOURCE_ERRORS as e:
                    log.info("the library copy was not refreshed (digiKam unreachable): %s", e)
                except Exception as e:
                    log.warning("the library copy was not refreshed: %s", e, exc_info=True)
    threading.Thread(target=run, name="library-copy", daemon=True).start()


def refresh_library():
    """Copy digiKam's tables into the library tables (one snapshot, one transaction)."""
    src = dkdb.connect(write=False, real=True)
    try:
        src.begin_read()
        library.copy_from_digikam(src)
    finally:
        src.rollback()
        src.close()


def mirror_tag_paths(image_ids: list) -> dict:
    """{image id: [tag paths]} from the library tables (every tag but digiKam's internal ones)."""
    ids = [int(x) for x in image_ids]
    con = library.connect()
    try:
        tags = dkdb.tag_tree(con)
        internal = {r[0] for r in con.execute("SELECT tagid FROM TagProperties WHERE property = 'internalTag'").fetchall()}
        out = {}
        for part in chunked(ids, 500):
            chunk = ",".join(map(str, part))
            for iid, tid in con.execute(f"SELECT imageid, tagid FROM ImageTags WHERE imageid IN ({chunk})").fetchall():
                if tid in tags and tid not in internal:
                    out.setdefault(iid, []).append(dkdb.tag_path(tags, tid))
    finally:
        con.close()
    return {iid: out.get(iid, []) for iid in ids}


# ---- photo_ids: the viewer's identity for a photo, and viewer data following a renumbered photo
REKEY = [("favorite_photos", "photo_id"), ("viewer_album_photos", "photo_id"), ("pending_edits", "photo_id"), ("ai_scans", "photo_id"), ("ai_quality", "photo_id"),
         ("ai_text", "photo_id"), ("ai_face_count", "photo_id"), ("ai_objects", "photo_id"), ("ai_descriptions", "photo_id"), ("ai_looks", "photo_id"), ("ai_face_ages", "photo_id"), ("ai_face_embeddings", "photo_id"),
         ("ai_person_references", "photo_id"), ("metadata_check", "photo_id"), ("image_data_checksums", "photo_id"), ("photo_history", "photo_id"), ("quality_accepted", "photo_id"),
         ("ai_similar_photos", "a"), ("ai_similar_photos", "b")]


REJECT_REASONS = {"blur": "Blurry all over", "eyes": "Eyes closed", "burst": "A better frame of the same burst exists", "exposure": "Nearly black or blown out"}


def likely_rejects(con, bursts: dict) -> list:
    """[(reasons, photo id)] for the photos that look like rejects, from the measures already in the index
    (con: the index being built). Reasons, comma-separated (REJECT_REASONS): blurry all over (in the
    least sharp twentieth by focus, or by overall sharpness when focus is not measured yet, and no
    sharp face), closed eyes on most of the faces judged, a clearly worse frame of a burst, or nearly
    black or mostly blown out. Only a suggestion: nothing is marked until the user does it."""
    out = []
    for pid, focusn, sharpn, faceq, judged, closed, bright, clip_lo, clip_hi in con.execute(
            "SELECT id, focusn, sharpn, faceq, eyes_checked, eyes_closed, bright, clip_lo, clip_hi FROM photos WHERE video = 0"):
        why = []
        soft = focusn if focusn is not None else sharpn
        if soft is not None and soft < 0.05 and (faceq is None or faceq < 0.35):
            why.append("blur")
        if judged and closed and closed * 2 > judged:
            why.append("eyes")
        if pid in bursts and bursts[pid][2]:
            why.append("burst")
        if bright is not None and (bright < 0.06 or (clip_lo or 0) > 0.75 or (clip_hi or 0) > 0.5):
            why.append("exposure")
        if why:
            out.append((",".join(why), pid))
    return out


def sync_photo_ids(images: list, paths: dict) -> dict:
    """images: the raw image rows just read from digiKam; paths: {digiKam id: full path}. Gives every
    photo a viewer id once, and when a file (same uniqueHash and size) comes back under another digiKam
    id, moves the viewer's rows about it to the new id. Returns {old digiKam id: new} for what moved."""
    now = int(time.time())
    current = {}
    for r in images:
        iid, name, size, uhash = r[0], r[2], r[3], r[4]
        if uhash:
            current[iid] = (uhash, size, paths.get(iid), name)
    with appstate.lock:
        con = appstate.connect()
        try:
            rows = con.execute("SELECT vid, dk_id, hash, size, path, name, last_seen FROM photo_identity").fetchall()
            by_key, by_dk, stored = {}, {}, {}
            for vid, dk, h, sz, pth, nm, seen in rows:
                by_key.setdefault((h, sz), []).append((vid, dk))
                stored[vid] = (dk, pth, nm, seen or 0)
                if dk is not None:
                    by_dk[dk] = (vid, h, sz)
            inserts, updates, rewritten, remap = [], [], [], {}
            # only what changed is written: on MySQL every statement is a round trip, and a statement per photo
            # on every build (30,000 of them) made each rebuild seconds slower. last_seen is refreshed daily.
            for dk, (h, sz, path, name) in current.items():
                known = by_key.get((h, sz))
                match = next(((v, d) for v, d in known if d == dk), None) if known else None
                if match is None and known:
                    # the same bytes under another number: that photo's row follows it only when its old number is
                    # gone (digiKam renumbered it); while both numbers exist they are two copies, each its own photo
                    match = next(((v, d) for v, d in known if d is None or d not in current), None)
                if match:
                    vid, old = match
                    if old != dk and old is not None and old not in current:
                        remap[old] = dk  # the same file under a new number
                    was = stored.get(vid)
                    if not was or was[0] != dk or was[1] != path or was[2] != name or now - was[3] > 86400:
                        updates.append((dk, path, name, now, vid))
                elif dk in by_dk and stored[by_dk[dk][0]][1] == path:
                    # the same file under the same number, rewritten in place (metadata written into it, a lossless
                    # rotate or crop, digiKam's rescan after one): the photo keeps its viewer id
                    rewritten.append((h, sz, name, now, by_dk[dk][0]))
                else:  # new, or the number now stands for another file: that is a different photo
                    inserts.append((dk, h, sz, path, name, now, now))
            if remap:
                rekey(con, remap)
            # a number that now belongs to a different file is taken away from the row that had it
            kept = {vid for _h, _s, _n, _t, vid in rewritten}
            taken = [(vid,) for dk, (vid, h, sz) in by_dk.items() if dk in current and (h, sz) != current[dk][:2] and vid not in kept]
            if rewritten:
                con.executemany("UPDATE photo_identity SET hash = ?, size = ?, name = ?, last_seen = ? WHERE vid = ?", rewritten)
            if taken:
                con.executemany("UPDATE photo_identity SET dk_id = NULL WHERE vid = ?", taken)
            if updates:
                con.executemany("UPDATE photo_identity SET dk_id = ?, path = ?, name = ?, last_seen = ? WHERE vid = ?", updates)
            if inserts:
                con.executemany("INSERT INTO photo_identity (dk_id, hash, size, path, name, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)", inserts)
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
    if remap:
        log.warning("digiKam renumbered %d photos; the viewer's data about them follows: %s", len(remap),
                    ", ".join(f"{a}->{b}" for a, b in list(remap.items())[:10]))
        try:
            from app.ai import semantic
            semantic.remap_ids(remap)
        except Exception as e:  # the embeddings are recomputed for those photos instead
            log.warning("content-search embeddings were not renumbered (%s); they are recomputed", e)
    return remap


def rekey(con, remap: dict):
    """Move the viewer's rows about renumbered photos to their new ids, in two passes through negative
    ids so a swap of two numbers cannot collide. A row already under a new id that belongs to a photo
    which is gone for good is dropped first (its photo no longer exists under any id)."""
    targets = set(remap.values()) - set(remap)
    for table, col in REKEY:
        if targets:
            con.execute(f"DELETE FROM {table} WHERE {col} IN ({','.join(map(str, targets))})")
        con.executemany(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", [(-new, old) for old, new in remap.items()])
        con.execute(f"UPDATE {table} SET {col} = -{col} WHERE {col} < 0")


def open_index(path: Path = config.INDEX_DB) -> sqlite3.Connection:
    """A read-only connection to the index (plain tuples; callers wanting rows set row_factory)."""
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)


def _parse_dt(s):
    """digiKam stores naive local 'YYYY-MM-DDTHH:MM:SS.mmm'. The timestamp we
    keep is that wall-clock time as if it were UTC, so it sorts and displays
    as the time on the camera."""
    if not s:
        return None
    try:
        y, mo, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
        hh, mi, ss = (int(s[11:13]), int(s[14:16]), int(s[17:19])) if len(s) >= 19 else (0, 0, 0)
        return calendar.timegm((y, mo, d, hh, mi, ss)), y, mo, d
    except (ValueError, IndexError):
        return None


def _camera(make, model):
    make, model = (make or "").strip(), (model or "").strip()
    if not model:
        return make or None
    first = make.split(" ")[0].lower() if make else ""
    if not make or model.lower().startswith(first):
        return model
    short = {"OLYMPUS CORPORATION": "Olympus", "OM Digital Solutions": "OM System",
             "NIKON CORPORATION": "Nikon", "EASTMAN KODAK COMPANY": "Kodak"}.get(make, make)
    return f"{short} {model}"


_build_lock = threading.Lock()  # one build at a time in this process: the watcher, a request and the MCP server can all ask
HIDDEN: set = set()  # photos on their way to the trash: out of the index at once, and out of every build until the move is done


def _drop(con, ids):
    """Delete these photos from an index database (the photo rows, their tags, faces and text)."""
    ids = sorted(int(i) for i in ids)
    for part in chunked(ids, 900):
        q = ",".join("?" * len(part))
        for table, col in (("photos", "id"), ("photo_tags", "photo_id"), ("faces", "photo_id"), ("fts", "rowid")):
            con.execute(f"DELETE FROM {table} WHERE {col} IN ({q})", part)


def drop_photos(ids, dst_path: Path = config.INDEX_DB):
    """Take photos out of the live index now (moved to the trash in the background); builds leave them out
    too until HIDDEN lets go of them, once digiKam's rows say where they are."""
    if not dst_path.exists():
        raise RuntimeError("the index is not built yet")
    con = sqlite3.connect(str(dst_path), timeout=15)
    try:
        con.execute("PRAGMA journal_mode = MEMORY")  # no journal file beside an index that readers open read-only and a build replaces
        _drop(con, ids)
        con.commit()
    finally:
        con.close()
    HIDDEN.update(int(i) for i in ids)  # only once they are out: a failed drop hides nothing


def build(dst_path: Path = config.INDEX_DB, source: tuple = None) -> dict:
    """Read everything the index needs, from digiKam's database (and refresh the viewer's copy of it)
    or from that copy when digiKam cannot be read, then write a fresh index file and swap it in.
    Returns the stats also stored in the index's meta table."""
    with _build_lock:
        stats = _build(dst_path, source)
    if stats["resolved_edits"] and Path(dst_path) == config.INDEX_DB:  # digiKam now agrees with these edits; stop tracking them
        edits.resolved(stats["resolved_edits"], stats.get("resolved_snapshot"))
        # forgetting them changed the edits' signature, which is part of the version this index was stamped with:
        # the stamp follows, or ensure_fresh would build the same index once more
        try:
            con = sqlite3.connect(str(dst_path), timeout=15)
            try:
                con.execute("PRAGMA journal_mode = MEMORY")
                con.execute("UPDATE meta SET value = ? WHERE key = 'version'", (_version_for(last_source["version"]),))
                con.commit()
            finally:
                con.close()
        except sqlite3.Error as e:
            log.warning("the index version was not restamped after resolving edits (%s); one extra build follows", e)
    return stats


def _build(dst_path: Path, source: tuple) -> dict:
    t0 = time.time()
    geo = places.geocoder()
    kind, ver = source or probe()
    raw = None
    if kind == "digikam":
        m = mirror_info()
        try:
            if not m:  # the very first copy: taken now, and the index is read from it
                refresh_library()
            elif m["source"] != ver:
                # digiKam changed (often a change made here a moment ago): the index is read from digiKam
                # itself, and the viewer's copy of the library follows in the background. Copying first put
                # several seconds (on MariaDB) between a change and its showing up.
                src = dkdb.connect(write=False, real=True)
                try:
                    raw = read_rows(src)
                    root_path = dkdb.root_paths(src)
                finally:
                    src.close()
                refresh_library_soon()
        except SOURCE_ERRORS as e:
            _set_offline(e)
            if not m:
                raise Unavailable(f"digiKam's database cannot be read and the viewer has no copy of the library yet: {e}") from e
            kind, ver, raw = "library", library.version(), None
    direct = raw is not None  # read straight from digiKam (the copy follows), or from the copy
    if raw is None:
        con = library.connect()
        try:
            raw = read_rows(con)
            root_path = dkdb.root_paths(con)
        finally:
            con.close()
    all_images = raw["images"]  # the renumbering check below sees every photo, hidden ones included (a photo queued for the trash is not "gone")
    if HIDDEN:  # on their way to the trash: left out before anything is counted (digiKam's rows may not say so yet)
        hidden = set(HIDDEN)
        raw["images"] = [r for r in raw["images"] if r[0] not in hidden]
    version = _version_for(ver)
    last_source.update(kind=kind, version=ver)
    phases = {"read": time.time() - t0}

    # ---- album roots and albums
    roots = {r[0]: r[1] for r in raw["roots"]}
    single_root = len(roots) == 1
    albums = {}
    for aid, root, rel, caption, adate, collection, icon in raw["albums"]:
        if root in roots:
            albums[aid] = {"id": aid, "root": root, "rel": rel, "caption": caption or None, "label": roots[root],
                           "date": str(adate)[:10] if adate else None, "category": collection or None, "icon": icon}
    by_path = {(a["root"], a["rel"]): aid for aid, a in albums.items()}
    for a in albums.values():
        rel = a["rel"]
        if rel == "/":
            a["pid"], a["name"] = None, a["label"] or "Library"
        else:
            parent = rel.rsplit("/", 1)[0] or "/"
            a["pid"], a["name"] = by_path.get((a["root"], parent)), rel.rsplit("/", 1)[1]
        a["path"] = rel if single_root else f"/{a['label']}{'' if rel == '/' else rel}"
    if kind == "digikam":
        full_paths = {r[0]: root_path[albums[r[1]]["root"]] + albums[r[1]]["rel"].rstrip("/") + "/" + r[2]
                      for r in all_images if r[1] in albums and albums[r[1]]["root"] in root_path}
        try:
            sync_photo_ids(all_images, full_paths)
        except Exception as e:  # bookkeeping only; the index must still be built
            log.warning("photo_ids were not updated: %s", e)

    # ---- tags
    # icon: the photo digiKam shows for the tag ("Set as Tag Thumbnail"); for people it is the face on that photo
    tags = {tid: {"id": tid, "pid": pid, "name": name, "icon": icon} for tid, pid, name, icon in raw["tags"]
            if tid != 0}  # MySQL's "_Digikam_root_tag_" row; pid 0 means "no parent" on both backends
    props = defaultdict(set)
    for tid, prop, _ in raw["tagprops"]:
        props[tid].add(prop)

    def ancestors(tid):
        out, seen = [], set()
        cur = tags.get(tid, {}).get("pid")
        while cur and cur in tags and cur not in seen:
            seen.add(cur)
            out.append(cur)
            cur = tags[cur]["pid"]
        return out

    internal_roots = {tid for tid, t in tags.items()
                      if t["name"] == "_Digikam_Internal_Tags_" and not t["pid"]}
    internal = {tid for tid in tags if tid in internal_roots or internal_roots & set(ancestors(tid))
                or "internalTag" in props[tid]}
    special = {tid for tid in tags if props[tid] & set(SPECIAL_PERSON_PROPS)}
    hidden = internal | special
    for tid, t in tags.items():
        t["anc"] = ancestors(tid)
        t["full"] = "/".join(tags[x]["name"] for x in reversed([tid] + t["anc"]))
        t["person"] = 1 if "person" in props[tid] and tid not in hidden else 0

    # ---- per-image side tables
    img_tags, pick, color = defaultdict(list), {}, {}
    for iid, tid in raw["imagetags"]:
        t = tags.get(tid)
        if not t:
            continue
        if t["name"] in PICK and tid in internal:
            pick[iid] = PICK[t["name"]]
        elif t["name"] in COLOR and tid in internal:
            color[iid] = COLOR[t["name"]]
        elif tid not in hidden:
            img_tags[iid].append(tid)

    faces = []
    # every kind of face digiKam knows: confirmed regions, faces it found but could not name
    # (on its "Unknown" tag), suggestions it is not sure about, and faces told to ignore
    unknown_tag = next((tid for tid in special if "unknownPerson" in props[tid]), None)
    ignored_tag = next((tid for tid in special if "ignoredPerson" in props[tid]), None)
    for iid, tid, prop, val in raw["regions"]:
        m = RECT_RE.search(val or "")
        if not m or tid not in tags:
            continue
        rect = tuple(map(int, m.groups()))
        if prop == "tagRegion" and tid not in hidden:
            faces.append((iid, tid, *rect, "confirmed"))
            if tid not in img_tags[iid]:
                img_tags[iid].append(tid)
            tags[tid]["person"] = 1
        elif prop == "autodetectedFace" and tid == unknown_tag:
            faces.append((iid, tid, *rect, "unknown"))
        elif prop == "autodetectedFace" and tid not in hidden:
            faces.append((iid, tid, *rect, "suggested"))
        elif prop == "ignoredFace" and tid == ignored_tag:
            faces.append((iid, tid, *rect, "ignored"))

    # digiKam's caption is type 1 and its title type 3 (2 is a headline, not shown); the x-default entry (the one the
    # viewer edits and digiKam shows first), the authorless one if there are several; other languages are not shown
    title, caption, rank = {}, {}, {}
    for row in raw["comments"]:
        _, iid, typ, text = row[:4]
        lang, author = (row[4] if len(row) > 4 else "x-default"), (row[5] if len(row) > 5 else None)
        if typ not in (1, 3) or not (text and text.strip()) or lang != "x-default":
            continue
        r = 0 if not author else 1
        key = (iid, typ)
        if key not in rank or r < rank[key]:
            rank[key] = r
            (title if typ == 3 else caption)[iid] = text.strip()
    copyright_ = defaultdict(dict)
    for iid, prop, val in raw["copyright"]:
        if val and val.strip():
            copyright_[iid].setdefault(prop, val.strip())
    iprops = defaultdict(dict)
    for iid, prop, val in raw["props"]:
        if val and val.strip() and set(val) - set("?, "):
            iprops[iid][prop] = val.strip()

    # ---- photos
    rows = raw["images"]

    photos, fts_rows, photo_tag_rows = [], [], []
    # edits made in the viewer live in sidecars; show them until digiKam's database catches up
    t1 = time.time()
    pending, resolved, resolved_snapshot = edits.pending(), [], {}
    seen = analysis.results()  # sharpness, OCR text, objects... from the app's own analysis
    phases["analysis"] = time.time() - t1
    burst_rows = []
    tag_by_full = {t["full"]: tid for tid, t in tags.items() if tid not in hidden}
    # approximate dates (dateprecision.py): the tag that says how much of a photo's date is known
    prec_tags = {tag_by_full[full]: dateprecision.LEVELS[k] for k, full in dateprecision.TAGS.items() if full in tag_by_full}
    tag_direct, tag_all = defaultdict(set), defaultdict(set)
    album_n = defaultdict(int)
    for (iid, album_id, name, size, uhash, mod, category, rating, created, orient, w, h, fmt,
         make, model, lens, aperture, focal, focal35, exposure, iso, flash, wb, metering, program,
         lat, lon, alt, gps_orientation, gps_accuracy, duration, vcodec, fps, acodec, aspect) in rows:
        album = albums.get(album_id)
        if not album:
            continue
        created_db, lat_db, lon_db, alt_db = created, lat, lon, alt  # digiKam's own values, before any pending edit is overlaid
        e0 = pending.get(iid)
        if e0 and "taken" in e0:
            created = e0["taken"]
        if e0 and "gps" in e0:
            lat, lon, alt = (e0["gps"]["lat"], e0["gps"]["lon"], e0["gps"].get("alt")) if e0["gps"] else (None, None, None)
            gps_accuracy = (e0["gps"] or {}).get("accuracy")
        mdt = _parse_dt(mod)
        ts, y, mo, d = _parse_dt(created) or mdt or (0, 1970, 1, 1)
        if not w or not h:
            w, h = 4, 3
        if orient in (5, 6, 7, 8):  # stored size is unrotated; keep the displayed size
            w, h = h, w
        video = 1 if category == 2 else 0
        try:
            dur = float(duration) / 1000.0 if duration not in (None, "") else None
        except ValueError:
            dur = None
        if lat is not None and lon is not None and lat == 0 and lon == 0:
            lat = lon = None
        loc = geo.lookup(lat, lon) if geo and lat is not None else None
        cam = _camera(make, model)
        lens = (lens or "").strip() or None
        tids = img_tags.get(iid, [])
        rating = rating if rating is not None else -1
        p_pick, p_color, p_title, p_caption, edited = pick.get(iid, 0), color.get(iid, 0), title.get(iid), caption.get(iid), 0
        e = pending.get(iid)
        if e:
            add = [tag_by_full[t] for t in e.get("add_tags", []) if t in tag_by_full]
            rem = [tag_by_full[t] for t in e.get("remove_tags", []) if t in tag_by_full]
            # a tag path digiKam does not have (yet): the edit is not done, whatever the rest says
            unknown_tags = any(t not in tag_by_full for t in e.get("add_tags", []))  # (removing a tag digiKam lacks is done)
            dbcr = copyright_.get(iid, {})
            g = e.get("gps")
            gps_same = ("gps" not in e or (g is None and lat_db is None)
                        or (g is not None and lat_db is not None and abs(g["lat"] - lat_db) < 1e-6 and abs(g["lon"] - lon_db) < 1e-6
                            and ((g.get("alt") is None and alt_db is None)
                                 or (g.get("alt") is not None and alt_db is not None and abs(float(g["alt"]) - float(alt_db)) < 0.05))))
            same = (("rating" not in e or e["rating"] == rating)
                    and ("taken" not in e or e["taken"] == (created_db or "")[:19])
                    and gps_same
                    and ("template" not in e or all((dbcr.get(k) or "") == (v or "") for k, v in e["template"].items()))
                    and ("pick" not in e or e["pick"] == p_pick) and ("color" not in e or e["color"] == p_color)
                    and ("title" not in e or (e["title"] or None) == p_title)
                    and ("caption" not in e or (e["caption"] or None) == p_caption)
                    and all(t in tids for t in add) and not any(t in tids for t in rem) and not unknown_tags)
            if same:
                resolved.append(iid)
                resolved_snapshot[iid] = e
            else:
                edited = 1
                rating = e.get("rating", rating)
                p_pick, p_color = e.get("pick", p_pick), e.get("color", p_color)
                p_title = (e["title"] or None) if "title" in e else p_title
                p_caption = (e["caption"] or None) if "caption" in e else p_caption
                tids = [t for t in tids if t not in rem] + [t for t in add if t not in tids]
        dprec = max((prec_tags[t] for t in tids if t in prec_tags), default=None)
        cr = dict(copyright_.get(iid, {}))
        if e0 and "template" in e0:
            for k, v in e0["template"].items():
                if v:
                    cr[k] = v
                else:
                    cr.pop(k, None)
        extra = {}
        if cr:
            extra["copyright"] = cr
        if iid in iprops:
            extra["properties"] = iprops[iid]
        if video:
            extra["video"] = {k: v for k, v in (("codec", vcodec), ("fps", fps), ("audio", acodec)) if v}
        if gps_orientation is not None:
            extra["orientationGps"] = gps_orientation  # compass direction the camera faced, when the file records it
        if gps_accuracy and lat is not None:
            extra["gpsAccuracy"] = gps_accuracy  # metres: an approximate position (only the town known, say)
        for k, v in (("whiteBalance", wb), ("metering", metering), ("program", program), ("orientation", orient)):
            if v is not None:
                extra[k] = v
        rel = album["rel"].rstrip("/") + "/" + name
        an = seen.get(iid, {})
        # a placeholder date is shared by many photos: no bursts among them (left out like videos)
        burst_rows.append((iid, ts, name, f"{cam}|{w}x{h}", an.get("sharp"), an.get("faceq"), video or dprec,
                           an.get("focus"), an.get("aesth"), an.get("eyes_checked"), an.get("eyes_closed")))
        photos.append((
            iid, album_id, name, root_path[album["root"]] + rel,
            # an approximate date has no day of the year to show On this day
            (created or mod or "")[:19], ts, f"{y:04d}-{mo:02d}-{d:02d}", y, mo, None if dprec else f"{mo:02d}-{d:02d}",
            w, h, fmt, video, dur, size, rating,
            p_pick, p_color, cam, (make or "").strip() or None, (model or "").strip() or None,
            lens, aperture, focal, focal35, exposure, iso, flash, lat, lon, alt,
            p_title, p_caption, cr.get("creator"), cr.get("copyrightNotice"),
            *(loc or (None, None, None)),
            uhash, (mod or "")[:19], mdt[0] if mdt else ts, len(tids), json.dumps(extra) if extra else None, edited,
            an.get("sharp"), None, an.get("bright"), an.get("clip_lo"), an.get("clip_hi"), an.get("faceq"),
            an.get("npeople"), ("," + ",".join(sorted(an["objects"])) + ",") if an.get("objects") else None,
            an.get("ocr"), None, None, an.get("caption_ai"), dprec,
            an.get("aesth"), None, None, an.get("eyes_checked"), an.get("eyes_closed"), None,
        ))
        album_n[album_id] += 1
        people_names, tag_names = [], []
        for tid in tids:
            t = tags[tid]
            photo_tag_rows.append((tid, iid))
            tag_direct[tid].add(iid)
            tag_all[tid].add(iid)
            for a in t["anc"]:
                tag_all[a].add(iid)
            (people_names if t["person"] else tag_names).append(t["full"].replace("/", " "))
        extra_words = " ".join(an["objects"]) if an.get("objects") else ""
        misc = extra_words + " " + " ".join(str(x) for x in (
            # only the known part of the date is searchable
            y if (dprec or 0) < 3 else None, MONTHS[mo] if (dprec or 0) < 2 else None, fmt, "video" if video else "photo", lens, cr.get("creator"),
            cr.get("copyrightNotice"), " ".join(iprops.get(iid, {}).values()), *(loc or ())) if x)
        fts_rows.append((iid, name, album["path"], p_title, p_caption,
                         " ; ".join(tag_names), " ; ".join(people_names), cam, misc, an.get("ocr"), an.get("caption_ai")))

    # ---- write the new index next to the old one, then swap
    dst_path = Path(dst_path)
    tmp = dst_path.with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp")  # another process (the MCP server) may build too
    for p in (tmp, Path(str(tmp) + "-journal")):
        p.unlink(missing_ok=True)
    phases["compute"] = time.time() - t0 - sum(phases.values())
    t2 = time.time()
    dst = sqlite3.connect(tmp)
    try:
        dst.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;" + SCHEMA)
        dst.executemany(f"INSERT INTO photos VALUES ({','.join('?' * 64)})", photos)
        dst.executemany("INSERT INTO fts (rowid, name, album, title, caption, tags, people, camera, misc, ocr, caption_ai) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", fts_rows)
        # sharpness as a 0..1 rank across the library, so "blurry" means the same everywhere
        ranked = sorted((r[4], r[0]) for r in burst_rows if r[4] is not None)
        if ranked:
            dst.executemany("UPDATE photos SET sharpn = ? WHERE id = ?",
                            [(round(i / max(1, len(ranked) - 1), 4), pid) for i, (_, pid) in enumerate(ranked)])
        # the same for focus and the aesthetic score
        for col, k in (("focusn", 7), ("aesthn", 8)):
            ranked = sorted((r[k], r[0]) for r in burst_rows if r[k] is not None)
            if ranked:
                dst.executemany(f"UPDATE photos SET {col} = ? WHERE id = ?",
                                [(round(i / max(1, len(ranked) - 1), 4), pid) for i, (_, pid) in enumerate(ranked)])
        try:
            bursts = moments.find_bursts(burst_rows)
        except Exception:
            bursts = {}
        dst.executemany("UPDATE photos SET burst = ?, burst_best = ? WHERE id = ?", [(b, best, pid) for pid, (b, best, _w) in bursts.items()])
        dst.execute("CREATE INDEX photos_burst ON photos (burst)")
        dst.executemany("UPDATE photos SET reject = ? WHERE id = ?", likely_rejects(dst, bursts))
        dst.executemany("INSERT OR IGNORE INTO photo_tags VALUES (?,?)", photo_tag_rows)
        present = {r[0] for r in photos}
        dst.executemany("INSERT INTO faces VALUES (?,?,?,?,?,?,?)", [f for f in faces if f[0] in present])
        dst.executemany("INSERT INTO tags VALUES (?,?,?,?,?,?,?,?)", [
            (tid, t["pid"] if t["pid"] in tags and t["pid"] not in hidden else None, t["name"], t["full"],
             t["person"], len(tag_direct[tid]), len(tag_all[tid]), t["icon"] if t["icon"] in present else None)
            for tid, t in tags.items() if tid not in hidden])
        dst.executemany("INSERT OR IGNORE INTO tag_closure VALUES (?,?)", [
            (a, tid) for tid, t in tags.items() if tid not in hidden for a in [tid] + t["anc"]])

        # album totals including sub-albums, plus a cover photo
        closure = []
        for aid, a in albums.items():
            cur, seen = aid, set()
            while cur is not None and cur not in seen:
                seen.add(cur)
                closure.append((cur, aid))
                cur = albums[cur]["pid"]
        dst.executemany("INSERT OR IGNORE INTO album_closure VALUES (?,?)", closure)
        dst.executemany("INSERT INTO albums (id, pid, path, name, root, caption, n, adate, category, icon) VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (aid, a["pid"], a["path"], a["name"], root_path[a["root"]], a["caption"], album_n[aid], a["date"], a["category"], a["icon"])
            for aid, a in albums.items()])
        if HIDDEN:  # added to the trash while this build ran: out of the tables too (the counts follow at the next build)
            _drop(dst, set(HIDDEN))
        dst.execute("""
            UPDATE albums SET
              n_all  = (SELECT count(*) FROM photos p JOIN album_closure c ON c.album = p.album_id WHERE c.anc = albums.id),
              ts_min = (SELECT min(ts) FROM photos p JOIN album_closure c ON c.album = p.album_id WHERE c.anc = albums.id),
              ts_max = (SELECT max(ts) FROM photos p JOIN album_closure c ON c.album = p.album_id WHERE c.anc = albums.id),
              cover  = coalesce((SELECT p.id FROM photos p WHERE p.id = albums.icon),  -- the cover chosen in digiKam or here
                                (SELECT p.id FROM photos p JOIN album_closure c ON c.album = p.album_id WHERE c.anc = albums.id
                                 ORDER BY p.video, (p.pick = 1), p.rating DESC, p.ts LIMIT 1))
        """)
        dst.execute("DELETE FROM albums WHERE n_all = 0")
        stats = {"version": version, "built": int(time.time()), "photos": len(photos), "resolved_edits": resolved, "resolved_snapshot": resolved_snapshot,
                 "build_seconds": round(time.time() - t0, 2)}
        dst.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in stats.items() if k != "resolved_snapshot"])
        dst.commit()
        dst.execute("INSERT INTO fts(fts) VALUES ('optimize')")
        dst.commit()
    except BaseException:
        dst.close()
        tmp.unlink(missing_ok=True)  # never leave a half-written index behind
        raise
    dst.close()
    for tail in ("-journal", "-wal", "-shm"):  # a journal left by a crash would be applied to the new file
        try:
            os.unlink(str(dst_path) + tail)
        except OSError:
            pass
    os.replace(tmp, dst_path)
    phases["write"] = time.time() - t2
    log.info("index built from %s in %.1fs: %d photos (%s)", "digiKam's database" if direct else "the library copy", time.time() - t0, len(photos),
             ", ".join(f"{k} {v:.1f}s" for k, v in phases.items()))
    return stats


def current_version(path: Path = config.INDEX_DB):
    if not path.exists():
        return None
    try:
        con = open_index(path)
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        finally:
            con.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _embed_signature() -> str:
    """Bursts use the content embeddings, so rebuild when that file changes."""
    try:
        from app.ai.semantic import semantic
        st = os.stat(semantic.active.file)
        return f"v{semantic.active.name}{int(st.st_mtime)}"
    except OSError:
        return "v0"


def _version_for(source: str) -> str:
    return f"{SCHEMA_VERSION}:{source}:{places.signature()}:{edits.signature()}:{analysis.signature()}:{_embed_signature()}"


def expected_version() -> str:
    return _version_for(probe()[1])


def ensure_fresh() -> bool:
    """Rebuild the index when anything it is built from changed: digiKam's database (or the
    viewer's copy of it), the viewer's remembered edits, analysis results, embeddings, or this
    schema. True if rebuilt."""
    src = probe()
    if current_version() == _version_for(src[1]) and not (src[0] == "digikam" and not mirror_info()):
        # (a readable digiKam with no copy taken yet, as right after this version's first start, builds once to take it)
        last_source.update(kind=src[0], version=src[1])
        return False
    build(source=src)
    return True


if __name__ == "__main__":
    print(build())
