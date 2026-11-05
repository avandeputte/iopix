"""The remembered edits: what the viewer wrote to sidecars (and files) that digiKam's database does
not have yet, kept in the viewer's own database (vdb's pix_pending_edits table) so the viewer shows the
new values right away and the index can lay them over digiKam's rows.

One row per photo, the whole edit as JSON, replaced on every change, so an edit is read as one
dict and merged by edits.apply. Rows are dropped when digiKam's database catches up (resolved:
the index compared them), when the edit is undone (revert), or when a field is given up
(drop_pending_fields). Every row carries a stamp that only moves forward (`_next_ts`), because
signature() is part of the index version: two edits within one second must still rebuild it.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from app.data import index, state as appstate, vdb
from app.digikam.dkdb import in_chunks
from app.photos.paths import backup_path

# Re-entrant: edits.apply() and revert() hold it while they read and update the remembered edits,
# and edits.run_exiftool() takes it too so two exiftool processes never rewrite the same file at once
lock = threading.RLock()


def signature() -> str:
    """Changes whenever the set of remembered edits changes (part of the index version)."""
    if not vdb.ready():
        return "e0"
    con = appstate.connect()
    try:
        n, ts = con.execute("SELECT count(*), coalesce(max(updated_at), 0) FROM pix_pending_edits").fetchone()
        return f"e{n}.{ts}"
    except Exception:
        return "e0"
    finally:
        con.close()


def _next_ts(c) -> int:
    """The stamp for a pending edit written now: never equal to the newest one, so two edits within the same second
    still change signature() (and the index shows the second)."""
    newest = c.execute("SELECT coalesce(max(updated_at), 0) FROM pix_pending_edits").fetchone()[0] or 0
    return max(int(time.time()), int(newest) + 1)


def save_pending(rows: list):
    """rows: [(photo id, edit dict)]: remembered (replacing what was there), with a fresh stamp."""
    if rows:
        def go(c):
            ts = _next_ts(c)
            c.executemany("INSERT OR REPLACE INTO pix_pending_edits VALUES (?,?,?)", [(int(pid), json.dumps(e), ts) for pid, e in rows])
        appstate.write(go)


def pending() -> dict:
    """photo_id -> remembered edit."""
    if not vdb.ready():
        return {}
    con = appstate.connect()
    try:
        return {r[0]: json.loads(r[1]) for r in con.execute("SELECT photo_id, data FROM pix_pending_edits")}
    except Exception:
        return {}
    finally:
        con.close()


def pending_one(photo_id: int):
    """The remembered edit of one photo, or None."""
    if not vdb.ready():
        return None
    con = appstate.connect()
    try:
        r = con.execute("SELECT data FROM pix_pending_edits WHERE photo_id = ?", (photo_id,)).fetchone()
        return json.loads(r[0]) if r else None
    except Exception:
        return None
    finally:
        con.close()


def drop_pending_tag(photo_id: int, path: str):
    """A tag the user just set or cleared directly in digiKam's rows (a person without a face box) leaves the photo's
    remembered edit, so the next apply does not add or remove it again."""
    with lock:
        e = pending_one(photo_id)
        if not e:
            return
        changed = False
        for k in ("add_tags", "remove_tags"):
            if path in e.get(k, []):
                e[k] = [t for t in e[k] if t != path]
                changed = True
        if changed:
            save_pending([(photo_id, e)])


def pending_count() -> int:
    """How many edits are remembered (what status polls show; cheaper than loading them all)."""
    if not vdb.ready():
        return 0
    con = appstate.connect()
    try:
        return con.execute("SELECT count(*) FROM pix_pending_edits").fetchone()[0]
    except Exception:
        return 0
    finally:
        con.close()


def newest_pending() -> int:
    """When the most recent remembered edit was made (unix time), 0 when there is none."""
    con = appstate.connect()
    try:
        row = con.execute("SELECT max(updated_at) FROM pix_pending_edits").fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        con.close()


def forget(photo_ids):
    """Drop remembered edits (digiKam has caught up, or they were undone)."""
    ids = [(int(i),) for i in photo_ids]
    if ids:
        appstate.write(lambda c: c.executemany("DELETE FROM pix_pending_edits WHERE photo_id = ?", ids))


def remap_pending_tags(mapping: dict) -> list:
    """After tags were renamed, moved, merged or deleted in digiKam's database: the pending edits that name them by
    path follow ({old path: new path, or None for a deleted tag}). Returns the photos whose edits changed, whose
    sidecars should then be rewritten."""
    if not mapping:
        return []
    def follow(t):
        return mapping[t] if t in mapping else t
    with lock:
        cur, rows, touched = pending(), [], []
        for pid, e in cur.items():
            if not (set(e.get("add_tags", [])) | set(e.get("remove_tags", []))) & set(mapping):
                continue
            e = dict(e)
            for k in ("add_tags", "remove_tags"):
                if k in e:
                    e[k] = list(dict.fromkeys(n for n in (follow(t) for t in e[k]) if n))
                    if not e[k]:
                        e.pop(k)
            rows.append((pid, json.dumps(e), int(time.time())))
            touched.append(pid)
        if rows:
            save_pending([(r[0], json.loads(r[1])) for r in rows])
    return touched


def drop_pending_fields(photo_ids, fields):
    """Give up these fields of the photos' pending edits (tags: the tag additions and removals)."""
    keys = set(fields) | ({"add_tags", "remove_tags"} if "tags" in fields else set()) | ({"caption_date"} if "caption" in fields else set())
    with lock:
        cur = pending()
        rows, gone = [], []
        for pid in photo_ids:
            e = cur.get(int(pid))
            if not e:
                continue
            e = {k: v for k, v in e.items() if k not in keys}
            (rows.append((int(pid), json.dumps(e), int(time.time()))) if e else gone.append(int(pid)))
        if rows:
            save_pending([(r[0], json.loads(r[1])) for r in rows])
        forget(gone)


def resolved(photo_ids, snapshot: dict = None):
    """digiKam's database now matches these edits: forget them and drop their
    backups, so a later edit starts from a fresh backup. With `snapshot` ({id: the edit as it was when compared}),
    an edit that changed since (a new edit made while the index was being built) is kept."""
    ids = [int(i) for i in photo_ids]
    if snapshot is not None:
        with lock:
            now = pending()
            ids = [i for i in ids if i in snapshot and now.get(i) == snapshot[i]]
            return _resolved(ids)
    return _resolved(ids)


def _resolved(ids):
    if not ids:
        return
    try:
        con = index.open_index()
        try:
            rows = list(in_chunks(con, "SELECT path FROM photos WHERE id IN ({ids})", ids))
        finally:
            con.close()
        for (path,) in rows:
            b = backup_path(path)
            b.unlink(missing_ok=True)
            Path(str(b) + ".created").unlink(missing_ok=True)
    except (sqlite3.Error, OSError):
        pass  # a backup left behind is harmless; the edit is forgotten either way
    forget(ids)
