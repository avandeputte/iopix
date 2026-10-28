"""Tag manager: add, rename, move and delete keyword tags, like digiKam's own.

Tag definitions live in digiKam's database, so every operation here writes it
and therefore only runs while digiKam is closed, with the same safeguards as
dbsync (backup first, one transaction, integrity check).

Renaming, moving or deleting a tag also changes the tag path stored in the XMP
sidecar of every photo that carries it (or a tag beneath it). Those sidecar
rewrites are queued in the viewer's database and done in the background, resumably,
after each sidecar is copied to data/sidecar-backups/tag-ops/<operation>/.

People tags may be renamed and moved (regions are keyed by tag id) and new
tags may be created under People; deleting a person stays with digiKam, which
turns their faces back into unknown faces, and digiKam's internal and special
face tags are never touched.
"""
import os
import shutil
import threading
import time
from pathlib import Path

from app import jobs
from app.digikam import dbsync, dkdb
from app.photos import edits
from app.data import state as appstate
from app.digikam.dbsync import SyncError
from app.faces import facemgr

OPS_BACKUP_DIR = edits.BACKUP_DIR / "tag-ops"
KEEP_OPS = 8
SPECIAL_PROPS = {"internalTag", "unknownPerson", "ignoredPerson", "unconfirmedPerson"}
progress = jobs.Progress("tagjobs")
_worker_lock = threading.Lock()


def init():
    pass  # the tagjobs table (and the drop of the early schema) is part of vdb.ensure_schema (state.init)


def pending_jobs() -> int:
    con = appstate.connect()
    try:
        return con.execute("SELECT count(*) FROM tag_rewrite_queue").fetchone()[0]
    except Exception:
        return 0
    finally:
        con.close()


class _Tree:
    def __init__(self, con):
        self.rows = {tid: {"pid": pid or 0, "name": name} for tid, (pid, name) in dkdb.tag_tree(con).items()}
        props = {}
        for tid, prop in con.execute("SELECT tagid, property FROM TagProperties"):
            props.setdefault(tid, set()).add(prop)
        with_faces = {r[0] for r in con.execute("SELECT DISTINCT tagid FROM ImageTagProperties")}
        self.internal = set()    # digiKam's bookkeeping tags (labels and the like), never user-visible
        self.protected = set()   # never touched here: digiKam's internal and special face tags, and the People root
        self.people = set()      # person tags and their family groups: may be renamed and moved, not deleted
        for tid, t in self.rows.items():
            top = self.rows[self.chain(tid)[-1]]["name"]
            p = props.get(tid, set())
            if top == "_Digikam_Internal_Tags_":
                self.internal.add(tid)
                self.protected.add(tid)
            elif p & SPECIAL_PROPS or (t["name"] == "People" and not t["pid"]):
                self.protected.add(tid)
            elif top == "People" or "person" in p or tid in with_faces:
                self.people.add(tid)

    def chain(self, tid):
        out, seen = [], set()
        while tid in self.rows and tid not in seen:
            seen.add(tid)
            out.append(tid)
            tid = self.rows[tid]["pid"]
        return out  # tag, parent, ..., root

    def full(self, tid):
        return "/".join(self.rows[t]["name"] for t in reversed(self.chain(tid)))

    def subtree(self, tid):
        """tid and all descendants, parents before children."""
        kids = {}
        for t, r in self.rows.items():
            kids.setdefault(r["pid"], []).append(t)
        out, stack = [], [tid]
        while stack:
            t = stack.pop()
            out.append(t)
            stack.extend(kids.get(t, []))
        return out


def _check_name(name: str) -> str:
    name = " ".join(str(name or "").split())
    if not name or "/" in name or "|" in name or len(name) > 120:
        raise SyncError("Tag names must be 1-120 characters and cannot contain / or |.")
    return name


def _photos_with(con, tag_ids, roots):
    """{image id: absolute path} for visible items carrying any of the tags."""
    out = {}
    ids = ",".join(str(int(t)) for t in tag_ids)
    for iid, r, rel, name in con.execute(
            "SELECT DISTINCT i.id, a.albumRoot, a.relativePath, i.name FROM ImageTags it JOIN Images i ON i.id = it.imageid "
            f"JOIN Albums a ON a.id = i.album WHERE it.tagid IN ({ids}) AND i.status = 1"):
        if r in roots:
            out[iid] = roots[r] + rel.rstrip("/") + "/" + name
    return out


class _Change:
    """One tag operation in progress: the connection and tag tree it works on, and what it found to do afterwards
    (the sidecar jobs, the pending-edit remap and the summary returned to the caller)."""

    def __init__(self, con, stamp: str):
        self.con, self.stamp = con, stamp
        self.tree, self.roots = _Tree(con), dkdb.root_paths(con)
        self.jobs, self.summary, self.merged_images, self.remap = [], {}, [], {}

    def editable(self, tid, what="tag", allow_people=False):
        tree = self.tree
        if tid not in tree.rows:
            raise SyncError(f"That {what} no longer exists in digiKam.")
        if tid in tree.protected:
            raise SyncError(f"“{tree.full(tid)}” is one of digiKam's own tags; manage it in digiKam.")
        if tid in tree.people and not allow_people:
            raise SyncError(f"“{tree.full(tid)}” is a person. People can be renamed and moved here, but deleting a person "
                            f"(and turning their faces back into unknown faces) is done in digiKam.")

    def free_name(self, pid, name, ignore=None):
        if any(t != ignore and r["pid"] == pid and r["name"].lower() == name.lower() for t, r in self.tree.rows.items()):
            raise SyncError(f"A tag named “{name}” already exists there.")

    def changeable(self, op) -> int:
        """The tag of an icon/person/notperson operation: any tag but digiKam's own."""
        tid = int(op.get("id") or 0)
        if tid not in self.tree.rows or tid in self.tree.internal:
            raise SyncError("That tag cannot be changed here.")
        return tid

    def is_person(self, tid) -> bool:
        return bool(self.con.execute("SELECT 1 FROM TagProperties WHERE tagid = ? AND property = 'person'", (tid,)).fetchone())

    def photos_with(self, tag_ids) -> dict:
        """{image id: path} of the visible photos carrying any of the tags (taken before the rows change)."""
        return _photos_with(self.con, tag_ids, self.roots)

    def rewrite(self, photos: dict, what: str):
        """These photos' sidecars are rewritten afterwards, and the edit log says why."""
        from app.photos import history
        self.jobs = [(self.stamp, path, iid) for iid, path in photos.items()]
        history.record([{"photo_id": iid, "action": "tag", "summary": what, "path": path} for iid, path in photos.items()])


def _op_icon(c: _Change, op: dict):
    """The photo shown for a tag (CoreDB::setTagIcon). Allowed for people too: it changes
    no tag assignment or face data, only which picture represents the tag."""
    tid, photo = c.changeable(op), int(op.get("photo") or 0)
    if photo and not c.con.execute("SELECT 1 FROM Images WHERE id = ? AND status = 1", (photo,)).fetchone():
        raise SyncError("That photo is not in the digiKam database.")
    if photo:
        c.con.execute("UPDATE Tags SET iconkde = ?, icon = ? WHERE id = ?", ("", photo, tid))
    else:
        c.con.execute("UPDATE Tags SET iconkde = ?, icon = NULL WHERE id = ?", ("", tid))
    c.summary.update(id=tid, path=c.tree.full(tid), photo=photo, photos=0, sidecars=0)


def _op_person(c: _Change, op: dict):
    """A keyword tag made a person (FaceTags::ensureIsPerson): it then shows under People, faces can be named
    with it, and digiKam lists it as a person too."""
    tid = c.changeable(op)
    if tid in c.tree.protected:
        raise SyncError("That tag cannot be changed here.")
    if c.tree.subtree(tid) != [tid]:
        raise SyncError(f"“{c.tree.full(tid)}” has tags inside it (a family or a group): only a single tag can be a person.")
    if c.is_person(tid):
        raise SyncError(f"“{c.tree.full(tid)}” is already a person.")
    c.con.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?, 'person', ?)", (tid, c.tree.rows[tid]["name"]))
    photos = c.photos_with([tid])  # their sidecars list it as a person now (a name-only face region)
    c.rewrite(photos, f"Marked as a person: {c.tree.full(tid)}")
    c.summary.update(id=tid, path=c.tree.full(tid), photos=len(photos))


def _op_notperson(c: _Change, op: dict):
    """A keyword tag digiKam marked as a person (its "person" property, set when a face was once named with
    it): the mark goes, the tag and its photos stay. Refused while face boxes still carry the name."""
    tid = c.changeable(op)
    if tid in c.tree.protected:
        raise SyncError("That tag cannot be changed here.")
    if not c.is_person(tid):
        raise SyncError(f"“{c.tree.full(tid)}” is not marked as a person.")
    boxes = c.con.execute("SELECT count(*) FROM ImageTagProperties WHERE tagid = ? AND property IN ('tagRegion', 'autodetectedFace')", (tid,)).fetchone()[0]
    if boxes:
        raise SyncError(f"{boxes} face {'box or suggestion carries' if boxes == 1 else 'boxes or suggestions carry'} the name “{c.tree.rows[tid]['name']}”: rename or remove those faces first.")
    c.con.execute("DELETE FROM TagProperties WHERE tagid = ? AND property IN ('person', 'faceEngineId', 'faceEngineName', 'faceEngineUuid')", (tid,))
    # its photos' sidecars list it as a person (a name-only face region): rewritten as a plain keyword
    photos = c.photos_with([tid])
    c.rewrite(photos, f"No longer a person: {c.tree.full(tid)}")
    c.summary.update(id=tid, path=c.tree.full(tid), photos=len(photos))


def _op_merge(c: _Change, op: dict):
    """One person (or tag) entered twice ("Cynthia Hwang" and "Cynthia Wang"): everything on `id` moves to
    `into` (photos, face boxes, suggestions), then `id` goes. Nothing becomes an unknown face."""
    con, tree = c.con, c.tree
    src, dst = int(op.get("id") or 0), int(op.get("into") or 0)
    for t in (src, dst):
        c.editable(t, allow_people=True)
    if src == dst:
        raise SyncError("Choose two different tags.")
    if (src in tree.people) != (dst in tree.people):
        raise SyncError("A person can only be merged with a person, and a keyword tag with a keyword tag.")
    if src in tree.people and not c.is_person(dst):
        raise SyncError(f"“{tree.full(dst)}” is not a person.")
    if tree.subtree(src) != [src]:
        raise SyncError(f"“{tree.full(src)}” has tags inside it; move them out first.")
    old, new = tree.full(src), tree.full(dst)
    photos = c.photos_with([src])
    images = [r[0] for r in con.execute("SELECT DISTINCT imageid FROM ImageTags WHERE tagid = ?", (src,))]
    for img in images:
        con.execute("REPLACE INTO ImageTags (imageid, tagid) VALUES (?,?)", (img, dst))
    con.execute("DELETE FROM ImageTags WHERE tagid = ?", (src,))
    # face boxes and the like: moved, unless the same one is already there for `into`
    for img, prop, val in con.execute("SELECT imageid, property, value FROM ImageTagProperties WHERE tagid = ?", (src,)).fetchall():
        if con.execute("SELECT 1 FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = ? AND value = ?", (img, dst, prop, val)).fetchone():
            con.execute("DELETE FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = ? AND value = ?", (img, src, prop, val))
        else:
            con.execute("UPDATE ImageTagProperties SET tagid = ? WHERE imageid = ? AND tagid = ? AND property = ? AND value = ?", (dst, img, src, prop, val))
        c.merged_images.append(img)
    # digiKam's unconfirmed suggestions name the person inside the value: "<tag id>,autodetectedFace,<rect>"
    for img, tid, val in con.execute("SELECT imageid, tagid, value FROM ImageTagProperties WHERE property = 'autodetectedPerson' AND value LIKE ?", (f"{src},%",)).fetchall():
        con.execute("UPDATE ImageTagProperties SET value = ? WHERE imageid = ? AND tagid = ? AND property = 'autodetectedPerson' AND value = ?",
                    (f"{dst}," + val.split(",", 1)[1], img, tid, val))
        c.merged_images.append(img)
    src_icon = con.execute("SELECT icon FROM Tags WHERE id = ?", (src,)).fetchone()[0]
    if src_icon and not con.execute("SELECT icon FROM Tags WHERE id = ?", (dst,)).fetchone()[0]:
        con.execute("UPDATE Tags SET icon = ?, iconkde = ? WHERE id = ?", (src_icon, "", dst))
    con.execute("DELETE FROM TagProperties WHERE tagid = ?", (src,))
    con.execute("DELETE FROM Tags WHERE id = ?", (src,))
    c.rewrite(photos, f"Merged: {old} → {new}")
    c.remap = {old: new}
    c.merged_images = list(dict.fromkeys(c.merged_images + list(photos)))
    c.summary.update(id=src, into=dst, path=old, new_path=new, photos=len(photos))


def _op_add(c: _Change, op: dict):
    pid = int(op.get("parent") or 0)
    # a tag may be created under a family group, and a person directly under the People root
    # (the root is protected only against being renamed, moved or deleted)
    if pid and not (pid in c.tree.protected and c.tree.full(pid) == "People"):
        c.editable(pid, "parent tag", allow_people=True)
    name = _check_name(op.get("name"))
    c.free_name(pid, name)
    new_id = c.con.execute("INSERT INTO Tags (pid, name) VALUES (?,?)", (pid, name)).lastrowid
    c.summary.update(id=new_id, path=(c.tree.full(pid) + "/" if pid else "") + name)


def _op_tree(c: _Change, op: dict):
    """rename, move or delete: the tag and everything under it. Renaming and moving are fine for people (regions
    are keyed by tag id, and the sidecars' tag paths are rewritten afterwards); deleting a person stays with digiKam."""
    con, tree, kind = c.con, c.tree, op["op"]
    tid = int(op.get("id") or 0)
    c.editable(tid, allow_people=(kind in ("rename", "move")))
    sub = tree.subtree(tid)
    for t in sub:
        c.editable(t, "sub-tag", allow_people=(kind in ("rename", "move")))
    old_full = {t: tree.full(t) for t in sub}
    photos = c.photos_with(sub)
    if kind == "rename":
        name = _check_name(op.get("name"))
        c.free_name(tree.rows[tid]["pid"], name, ignore=tid)
        con.execute("UPDATE Tags SET name = ? WHERE id = ?", (name, tid))
        # a person's face name follows (TagModificationHelper does the same), or digiKam and the scanner
        # keep matching faces named after the old one to this tag
        con.execute("UPDATE TagProperties SET value = ? WHERE tagid = ? AND property IN ('person', 'faceEngineName')", (name, tid))
        tree.rows[tid]["name"] = name
    elif kind == "move":
        pid = int(op.get("parent") or 0)
        if pid:
            c.editable(pid, "destination tag", allow_people=True)
            if pid in sub:
                raise SyncError("A tag cannot be moved into itself or one of its own sub-tags.")
        if pid == tree.rows[tid]["pid"]:
            raise SyncError("The tag is already there.")
        c.free_name(pid, tree.rows[tid]["name"], ignore=tid)
        con.execute("UPDATE Tags SET pid = ? WHERE id = ?", (pid, tid))  # digiKam's trigger rebuilds TagsTree
        tree.rows[tid]["pid"] = pid
    else:  # delete, children first so each row's own cleanup trigger fires
        for t in reversed(sub):
            con.execute("DELETE FROM Tags WHERE id = ?", (t,))
            # what digiKam's delete trigger does; the viewer's own library tables have no triggers
            con.execute("DELETE FROM ImageTags WHERE tagid = ?", (t,))
            con.execute("DELETE FROM TagProperties WHERE tagid = ?", (t,))
            con.execute("DELETE FROM ImageTagProperties WHERE tagid = ?", (t,))
    # like digiKam, rewrite the full tag list of every affected item afterwards
    what = f"Tag deleted: {old_full[tid]}" if kind == "delete" else f"Tag {'renamed' if kind == 'rename' else 'moved'}: {old_full[tid]} → {tree.full(tid)}"
    c.rewrite(photos, what)
    c.remap = {old_full[t]: (None if kind == "delete" else tree.full(t)) for t in sub}
    c.summary.update(id=tid, path=old_full[tid], new_path=None if kind == "delete" else tree.full(tid),
                     tags=len(sub), photos=len(photos))


OPS = {"add": _op_add, "rename": _op_tree, "move": _op_tree, "delete": _op_tree,
       "icon": _op_icon, "notperson": _op_notperson, "person": _op_person, "merge": _op_merge}


def apply(op: dict, db_path: Path = None, check_running: bool = True, queue: bool = True, backup: bool = True) -> dict:
    """op: {op: add, parent, name} | {op: rename, id, name} | {op: move, id, parent} | {op: delete, id}
       | {op: icon, id, photo} | {op: notperson, id} | {op: person, id} | {op: merge, id, into}. Returns a summary including how many sidecar files will be updated.
    `backup=False` is for callers that run several operations in a row and took one
    database backup themselves before the first."""
    db_path = Path(db_path or dkdb.sqlite_path())
    kind = op.get("op")
    if kind not in OPS:
        raise SyncError("Unknown tag operation.")
    dbsync.preflight(db_path, check_running, "tags")
    if queue and pending_jobs():
        raise SyncError("Sidecar files from the previous tag change are still being updated. Try again in a moment.")
    backup_file = dkdb.backup(db_path, force=(kind in ("delete", "merge"))) if backup else None  # deleting or merging a tag is destructive
    stamp = time.strftime("%Y%m%d-%H%M%S")

    with dbsync.write_transaction(db_path, check_running) as con:
        c = _Change(con, stamp)
        c.summary["op"] = kind
        OPS[kind](c, op)
    jobs, summary = c.jobs, c.summary

    # pending edits that name the changed tags by path follow them; their photos' sidecars are rewritten too
    touched = edits.remap_pending_tags(c.remap)
    if touched:
        have = {j[2] for j in jobs}
        extra = [i for i in touched if i not in have]
        if extra:
            jobs += [(stamp, path, iid) for iid, path in dbsync.photo_paths(extra)]
    summary.update(sidecars=len(jobs), backup=str(backup_file) if backup_file else None)
    if kind == "merge":
        _after_merge(summary["id"], summary["into"], c.merged_images, db_path)
    if queue and jobs:
        appstate.write(lambda c: c.executemany(
            "INSERT INTO tag_rewrite_queue (op, path, image_id) VALUES (?,?,?)", jobs))
        start_worker()
    elif not queue:
        summary["jobs"] = jobs
    return summary


def add_tags(full_paths: list, check_running: bool = True, db_path: Path = None) -> dict:
    """Create every tag path that does not exist yet ("Places/France/Paris" makes the missing parents too), as one
    guarded write: one preflight, one transaction. Returns {full path: tag id} for every path, existing ones included."""
    db_path = Path(db_path or dkdb.sqlite_path())
    dbsync.preflight(db_path, check_running, "tags")
    out = {}
    with dbsync.write_transaction(db_path, check_running) as con:
        tree = _Tree(con)
        by_path = {tree.full(t): t for t in tree.rows}
        for full in sorted(set(full_paths)):  # parents before children
            parts = [p for p in full.split("/") if p]
            pid = 0
            for depth in range(1, len(parts) + 1):
                path = "/".join(parts[:depth])
                tid = by_path.get(path)
                if tid is None:
                    name = _check_name(parts[depth - 1])
                    if any(r["pid"] == pid and r["name"].lower() == name.lower() for r in tree.rows.values()):
                        raise SyncError(f"A tag named “{name}” already exists there.")
                    tid = con.execute("INSERT INTO Tags (pid, name) VALUES (?,?)", (pid, name)).lastrowid
                    tree.rows[tid] = {"pid": pid, "name": name}
                    by_path[path] = tid
                pid = tid
            out[full] = pid
    return out


def _after_merge(src: int, dst: int, images: list, db_path):
    """What the viewer keeps about the merged tag follows it: People groups and face references; the sidecars'
    face regions are rewritten from the database (the tag lists through the queue, like any tag change)."""
    def go(c):
        for (gid,) in c.execute("SELECT group_id FROM people_group_members WHERE tag_id = ?", (src,)).fetchall():
            if c.execute("SELECT 1 FROM people_group_members WHERE group_id = ? AND tag_id = ?", (gid, dst)).fetchone():
                c.execute("DELETE FROM people_group_members WHERE group_id = ? AND tag_id = ?", (gid, src))
            else:
                c.execute("UPDATE people_group_members SET tag_id = ? WHERE group_id = ? AND tag_id = ?", (dst, gid, src))
        c.execute("UPDATE ai_person_references SET tag_id = ? WHERE tag_id = ?", (dst, src))
    appstate.write(go)
    if images:
        from app.faces import facemgr
        facemgr.queue_sidecars(images, db_path)


def _run_group(op: str, rows: list):
    """rows: [(image id, photo path)]. Keep a copy of each sidecar under this operation's
    folder (on top of edits' own first-change backup), then write each photo's complete
    current tag list and its face regions (what digiKam does after a tag changes: a renamed person's name is in the
    regions too)."""
    for _, p in rows:
        side = edits.sidecar_path(p)
        if not os.path.isfile(side):
            continue
        dst = OPS_BACKUP_DIR / op / edits.rel(side)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(side, dst)
    res = facemgr.write_regions_many([i for i, _p in rows])  # stamps the files it wrote: no digiKam rescan needed
    failed = [i for i, v in res.items() if v != "written"]
    if failed:
        progress.setdefault("failed", []).extend(failed)


def start_worker():
    """Process queued sidecar rewrites in the background (safe to call repeatedly)."""
    if not edits.EXIFTOOL or not _worker_lock.acquire(blocking=False):
        return

    def run():
        try:
            progress.pop("failed", None)  # the outcome of the previous run, not this one
            progress.start(pending_jobs())
            while True:
                con = appstate.connect()
                try:
                    first = con.execute("SELECT op FROM tag_rewrite_queue ORDER BY id LIMIT 1").fetchone()
                    if not first:
                        break
                    rows = con.execute("SELECT id, path, image_id FROM tag_rewrite_queue WHERE op = ? ORDER BY id LIMIT 200",
                                       tuple(first)).fetchall()
                finally:
                    con.close()
                try:
                    _run_group(first[0], [(r[2], r[1]) for r in rows])
                except Exception as e:  # leave the rows queued; they are retried on the next start
                    progress.fail(e)
                    break
                appstate.write(lambda c: c.executemany("DELETE FROM tag_rewrite_queue WHERE id = ?", [(r[0],) for r in rows]))
                progress.step(len(rows))
            if OPS_BACKUP_DIR.exists():
                for old in sorted(p for p in OPS_BACKUP_DIR.iterdir() if p.is_dir())[:-KEEP_OPS]:
                    shutil.rmtree(old, ignore_errors=True)
        finally:
            progress.finish()
            _worker_lock.release()

    threading.Thread(target=run, name="tag-sidecars", daemon=True).start()
