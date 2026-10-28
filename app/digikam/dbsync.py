"""Applies the viewer's pending edits to digiKam's own database.

Like the tag manager and face manager, this writes digiKam's database (SQLite
or MySQL, see dkdb) and only runs when asked to. Safeguards, shared with those
modules through `preflight` and `write_transaction`:
  * refuses while digiKam is running (it keeps database state in memory and
    would overwrite or conflict with outside changes);
  * refuses if a leftover journal shows an interrupted digiKam write;
  * backs the database up (data/db-backups) before touching it;
  * does everything in one transaction, checks integrity before committing,
    and rolls back on any error.

Only fields the viewer edits are written: rating, pick label, color label,
title, caption, date taken, GPS position, metadata template and keyword-tag
assignments, using the same rows digiKam uses. The edits are already in the
XMP sidecars, so afterwards both agree.
"""
import math
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from app import config
from app.digikam import dkdb
from app.photos import edits

PICK_TAGS = ["Pick Label None", "Pick Label Rejected", "Pick Label Pending", "Pick Label Accepted"]
COLOR_TAGS = [f"Color Label {n}" for n in ("None", "Red", "Orange", "Yellow", "Green", "Blue", "Magenta", "Gray", "Black", "White")]
SUPPORTED_DB_VERSIONS = range(10, 18)  # schema generations with the tables used below


SyncError = dkdb.SyncError  # one error type for every refusal to write digiKam's database


def digikam_running() -> bool:
    """Whether digiKam is open, as far as this machine can tell. DIGIKAM_CHECK=none (and "ask", its name
    before v1.19): the viewer cannot see digiKam's process (a container, digiKam on another machine), so
    it does not check and does not ask; a write goes ahead."""
    if config.DIGIKAM_CHECK in ("none", "ask") or dkdb.standalone():
        return False
    try:
        if subprocess.run(["pgrep", "-x", "digikam"], capture_output=True, timeout=5).returncode == 0:
            return True
        return subprocess.run(["pgrep", "-f", "digikam.app/Contents/MacOS/digikam"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell, so assume it is


def _confirmed_closed() -> bool:
    """In a container the host's processes are invisible, so the owner confirms instead
    (a short-lived flag set from the UI)."""
    try:
        from app.data import state as appstate
        return float(appstate.get_setting("digikam_closed_until", "0")) > time.time()
    except Exception:
        return False


def preflight(db_path: Path, check_running: bool = True, holds: str = None):
    """The checks every writer runs before backing up: digiKam closed, no interrupted
    write left behind. `holds` names what lives in the database for the message."""
    if check_running and digikam_running():
        raise SyncError("digiKam is running. Quit digiKam first" + (f"; {holds} live in its database." if holds else ", then try again."))
    with dkdb.write_lock:  # a write of the viewer's own still running is not an unfinished write
        pending = dkdb.journal_pending(db_path)
    if pending:
        raise SyncError("The digiKam database has an unfinished write (journal file present). "
                        "Open and close digiKam once so it can recover, then try again.")


@contextmanager
def write_transaction(db_path: Path = None, check_running: bool = True):
    """One guarded write to digiKam's database: BEGIN, schema-version check, the caller's
    statements, integrity check, a last look for a running digiKam, COMMIT. Any error
    rolls back; the connection is always closed."""
    dkdb.write_lock.acquire()
    try:
        con = dkdb.connect(write=True, db_path=db_path)
    except BaseException:
        dkdb.write_lock.release()
        raise
    try:
        con.begin_write()
        ver = con.execute("SELECT value FROM Settings WHERE keyword = 'DBVersion'").fetchone()
        if not ver or int(ver[0]) not in SUPPORTED_DB_VERSIONS:
            raise SyncError(f"Unsupported digiKam database version {ver[0] if ver else '?'}; nothing was changed.")
        yield con
        if not con.integrity_ok():
            raise SyncError("Integrity check failed; nothing was changed.")
        if check_running and digikam_running():
            raise SyncError("digiKam was started during the update; nothing was changed.")
        con.commit()
        dkdb.note_own_write()
    except BaseException:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        con.close()
        dkdb.write_lock.release()


def gps_text(is_lat: bool, v: float) -> str:
    """Port of MetaEngine::convertToGPSCoordinateString: 'DDD,MM.mmmmmmmmN'."""
    ref = ("N" if v >= 0 else "S") if is_lat else ("E" if v >= 0 else "W")
    v = abs(v)
    deg = math.floor(v)
    return f"{deg},{(v - deg) * 60.0:.8f}{ref}"


def _set_position(con, image_id: int, g):
    if not g:
        con.execute("DELETE FROM ImagePositions WHERE imageid = ?", (image_id,))
        return
    lat, lon = float(g["lat"]), float(g["lon"])
    alt = float(g["alt"]) if g.get("alt") is not None else None
    acc = float(g["accuracy"]) if g.get("accuracy") else None  # how far off it may be, in metres (ImagePositions.accuracy)
    con.execute("REPLACE INTO ImagePositions (imageid, latitude, latitudeNumber, longitude, longitudeNumber, altitude, "
                "orientation, tilt, roll, accuracy, description) VALUES (?,?,?,?,?,?,NULL,NULL,NULL,?,NULL)",
                (image_id, gps_text(True, lat), lat, gps_text(False, lon), lon, alt, acc))


def _set_copyright(con, image_id: int, template: dict):
    """ItemCopyright::setFromTemplate: each property is replaced; copyright and usage terms
    carry the x-default language in extraValue."""
    for prop, value in template.items():
        if prop not in edits.TEMPLATE_FIELDS:
            continue
        con.execute("DELETE FROM ImageCopyright WHERE imageid = ? AND property = ?", (image_id, prop))
        value = (value or "").strip()
        if value:
            extra = "x-default" if prop in ("copyrightNotice", "rightsUsageTerms") else None
            con.execute("INSERT INTO ImageCopyright (imageid, property, value, extraValue) VALUES (?,?,?,?)",
                        (image_id, prop, value, extra))


def _set_comment(con, image_id: int, ctype: int, text: str, date: str = None):
    """type 1 = caption, 3 = title; digiKam's default entry is language x-default with no author."""
    # every x-default entry of this kind, whoever wrote it (the authorless one first): the sidecar holds one x-default
    # value with no author, so the database keeps one too; other languages stay as they are
    rows = con.execute("SELECT id FROM ImageComments WHERE imageid = ? AND type = ? AND language = 'x-default' "
                       "ORDER BY CASE WHEN author IS NULL OR author = '' THEN 0 ELSE 1 END, id", (image_id, ctype)).fetchall()
    if not text:
        con.executemany("DELETE FROM ImageComments WHERE id = ?", rows)
    elif rows:
        con.execute("UPDATE ImageComments SET comment = ?, author = NULL, date = coalesce(?, date) WHERE id = ?", (text, date, rows[0][0]))
        con.executemany("DELETE FROM ImageComments WHERE id = ?", rows[1:])
    else:
        con.execute("INSERT INTO ImageComments (imageid, type, language, author, date, comment) VALUES (?,?,?,?,?,?)",
                    (image_id, ctype, "x-default", None, date, text))


def _set_label(con, image_id: int, label_tags: list, tag_id: int):
    """Pick and color labels are internal tags; an image carries at most one of each group."""
    con.execute(f"DELETE FROM ImageTags WHERE imageid = ? AND tagid IN ({','.join(map(str, label_tags))})", (image_id,))
    con.execute("INSERT OR IGNORE INTO ImageTags (imageid, tagid) VALUES (?,?)", (image_id, tag_id))


def _photo_paths(ids: list) -> list:
    """[(id, file path)] from the search index."""
    from app.data import index
    out = []
    con = index.open_index()
    try:
        for i in range(0, len(ids), 500):
            chunk = ",".join(str(int(x)) for x in ids[i:i + 500])
            out += [tuple(r) for r in con.execute(f"SELECT id, path FROM photos WHERE id IN ({chunk})")]
    finally:
        con.close()
    return out


def apply_pending(db_path: Path = None, pending: dict = None, check_running: bool = True) -> dict:
    """Write every pending viewer edit into the digiKam database."""
    db_path = Path(db_path or dkdb.sqlite_path())
    pending = edits.pending() if pending is None else pending
    if not pending:
        return {"applied": 0, "skipped": [], "backup": None}
    preflight(db_path, check_running)
    if not dkdb.is_mysql() and not dkdb.standalone() and not os.access(db_path, os.W_OK):
        raise SyncError("The digiKam database file is not writable.")
    # ordinary edits take no full database copy: each is in the change log, and the sidecars have their own backups
    applied, skipped = [], []
    with write_transaction(db_path, check_running) as con:
        tags = dkdb.tag_tree(con)
        by_full = {dkdb.tag_path(tags, tid): tid for tid in tags}
        internal = "_Digikam_Internal_Tags_/"
        pick_ids = [by_full.get(internal + n) for n in PICK_TAGS]
        color_ids = [by_full.get(internal + n) for n in COLOR_TAGS]

        for image_id, e in pending.items():
            image_id = int(image_id)
            if not con.execute("SELECT 1 FROM Images WHERE id = ? AND status = 1", (image_id,)).fetchone():
                skipped.append({"id": image_id, "reason": "no longer in the digiKam database"})
                continue
            add = [(t, by_full.get(t)) for t in e.get("add_tags", [])]
            rem = [(t, by_full.get(t)) for t in e.get("remove_tags", []) if by_full.get(t) is not None]  # a tag digiKam lacks is off already
            missing = [t for t, tid in add if tid is None]
            if missing or ("pick" in e and None in pick_ids) or ("color" in e and None in color_ids):
                skipped.append({"id": image_id, "reason": f"tag not found in digiKam: {', '.join(missing) or 'label tags'}"})
                continue
            if "rating" in e:
                r = int(e["rating"])
                if not con.execute("SELECT 1 FROM ImageInformation WHERE imageid = ?", (image_id,)).fetchone():
                    skipped.append({"id": image_id, "reason": "digiKam has no information row for this item yet"})
                    continue
                con.execute("UPDATE ImageInformation SET rating = ? WHERE imageid = ?", (max(0, min(5, r)), image_id))  # like ItemInfo::setRating
            if "pick" in e:
                _set_label(con, image_id, pick_ids, pick_ids[int(e["pick"])])
            if "color" in e:
                _set_label(con, image_id, color_ids, color_ids[int(e["color"])])
            if "taken" in e:  # ItemInfo::setDateTime -> ImageInformation.creationDate
                con.execute("UPDATE ImageInformation SET creationDate = ? WHERE imageid = ?", (e["taken"] + ".000", image_id))
            if "gps" in e:
                _set_position(con, image_id, e["gps"])
            if "template" in e:
                _set_copyright(con, image_id, e["template"])
            if "title" in e:
                _set_comment(con, image_id, 3, e["title"])
            if "caption" in e:
                stamp = e.get("caption_date")
                _set_comment(con, image_id, 1, e["caption"], stamp + ".000" if stamp else None)
            for _, tid in add:
                con.execute("INSERT OR IGNORE INTO ImageTags (imageid, tagid) VALUES (?,?)", (image_id, tid))
            for _, tid in rem:
                # a person with a face box on the photo stays: the box says they are in it, and only a face change
                # (remove or rename the face) takes them off. A tag removal never deletes face regions.
                if con.execute("SELECT 1 FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property IN ('tagRegion', 'autodetectedFace')",
                               (image_id, tid)).fetchone():
                    continue
                con.execute("DELETE FROM ImageTags WHERE imageid = ? AND tagid = ?", (image_id, tid))
            applied.append(image_id)
    if applied:  # the database now says what the sidecars (and files) say: digiKam need not rescan them
        dkdb.stamp_files(_photo_paths(applied))
    edits._recheck(applied)
    from app.photos import history
    history.record([{"photo_id": pid, "action": "database", "summary": "Edits written into " + ("the library" if dkdb.standalone() else "digiKam's database"),
                     "details": {k: v for k, v in pending.get(pid, pending.get(str(pid), {})).items() if k != "caption_date"}} for pid in applied])
    return {"applied": len(applied), "skipped": skipped, "backup": None}  # (no full database copy for ordinary edits)
