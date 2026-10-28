"""Clearing digiKam's pick labels (Rejected, Pending, Accepted) from the whole library.

iopix does not use pick labels and never sets them. Some libraries carry thousands from an older tool or import;
this removes the chosen ones, on request (Maintenance › Library), in one go:

- digiKam's database: the internal tags "Pick Label Rejected/Pending/Accepted" are taken off those photos (no
  label is digiKam's own "no pick label"), in one guarded transaction after a full database backup;
- sidecars that still carry the label (XMP-digiKam:PickLabel, xmpDM:Pick/Good) get it removed too, in the
  background, each sidecar backed up first (edits.run_exiftool), so a later "read metadata from file" in digiKam
  cannot bring it back;
- every photo gets an edit-log entry whose Undo puts its label back.
"""
import logging
import re
import threading

from app import jobs
from app.digikam import dbsync, dkdb
from app.data import index
from app.photos import edits, history

log = logging.getLogger("app.photos.picklabels")
LEVELS = {1: "Rejected", 2: "Pending", 3: "Accepted"}
progress = jobs.Progress("picklabels", cleared=0, sidecars=0, finished=None)
_SIDECAR_PICK = re.compile(r'(?:digiKam:PickLabel|xmpDM:Pick|xmpDM:Good)\s*=\s*"[^"]+"|<(?:digiKam:PickLabel|xmpDM:Pick|xmpDM:Good)>')


def counts() -> dict:
    """{1: photos labelled Rejected, 2: Pending, 3: Accepted} from the index."""
    con = index.open_index()
    try:
        have = dict(con.execute("SELECT pick, count(*) FROM photos WHERE pick IN (1, 2, 3) GROUP BY 1").fetchall())
    finally:
        con.close()
    return {k: int(have.get(k, 0)) for k in LEVELS}


def clear(levels: list, check_running: bool = True, after=None) -> int:
    """Take the chosen pick labels (1 Rejected, 2 Pending, 3 Accepted) off every photo. The database part is done
    before this returns; the sidecars follow in the background. Returns how many photos lost a label."""
    levels = sorted({int(x) for x in levels if int(x) in LEVELS})
    if not levels:
        raise ValueError("Choose Rejected, Pending or Accepted")
    if progress["running"]:
        raise dbsync.SyncError("Pick labels are being cleared already")
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the photos")
    dkdb.backup(db_path, force=True)  # thousands of rows at once: a full copy first
    internal = "_Digikam_Internal_Tags_/"
    photos = []
    with dbsync.write_transaction(db_path, check_running) as con:
        tags = dkdb.tag_tree(con)
        by_full = {dkdb.tag_path(tags, tid): tid for tid in tags}
        for lv in levels:
            tid = by_full.get(f"{internal}Pick Label {LEVELS[lv]}")
            if tid is None:
                continue
            ids = [r[0] for r in con.execute("SELECT imageid FROM ImageTags WHERE tagid = ?", (tid,))]
            con.execute("DELETE FROM ImageTags WHERE tagid = ?", (tid,))
            photos += [(i, lv) for i in ids]
    paths = dict(dbsync.photo_paths([i for i, _lv in photos]))
    history.record([{"photo_id": i, "action": "edit", "summary": f"Pick label {LEVELS[lv]} cleared (Maintenance › Clear pick labels)",
                     "path": paths.get(i), "undo": {"pick": lv}} for i, lv in photos])
    progress.start(len(photos), cleared=len(photos), sidecars=0)
    threading.Thread(target=_sidecars, args=([paths[i] for i, _lv in photos if i in paths], after), name="picklabels", daemon=True).start()
    return len(photos)


def _sidecars(paths: list, after):
    """Remove the label from the sidecars that carry it (most do not: digiKam writes it there only when told to)."""
    try:
        todo = []
        for path in paths:
            progress.step()
            side = edits.sidecar_path(path)
            try:
                with open(side, encoding="utf-8", errors="replace") as f:
                    if not _SIDECAR_PICK.search(f.read()):
                        continue
            except OSError:
                continue
            todo.append((side, edits.write_args({"pick": 0}, None, False)))
            if len(todo) >= 200:
                edits.run_exiftool(todo)
                progress["sidecars"] += len(todo)
                todo = []
        if todo:
            edits.run_exiftool(todo)
            progress["sidecars"] += len(todo)
    except Exception as e:
        log.warning("pick labels: sidecars not all cleared: %s", e)
        progress.fail(e)
    finally:
        progress.finish()
        if after:
            try:
                after()
            except Exception:
                pass
