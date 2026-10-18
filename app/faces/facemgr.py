"""Face management, mirroring digiKam's FaceTagsEditor row for row.

Face data lives in digiKam's database (ImageTagProperties + ImageTags), so
every operation here writes it, with the same safeguards as the tag manager:
digiKam closed, database backed up first, one transaction, integrity check.

Row shapes (digiKam 9.1, see facetagseditor.cpp / facetagsiface.cpp):
  confirmed  (image, person, 'tagRegion', rect) + (image, person, 'faceToTrain', rect) + ImageTags(image, person)
  unknown    (image, Unknown tag, 'autodetectedFace', rect)
  suggested  (image, person, 'autodetectedFace', rect)
             + (image, Unconfirmed tag, 'autodetectedPerson', "<person>,autodetectedFace,<rect>")
  ignored    (image, Ignored tag, 'ignoredFace', rect)
A region is '<rect x=".." y=".." width=".." height=".."/>' in upright pixels.

After a change the photo's sidecar gets digiKam's face-region XMP rewritten
(MetadataHub::writeFaceTagsMap / DMetadata::setItemFacesMap): every confirmed
face as an mwg-rs region and an MP region, with the rectangle rotated back to
the file's own orientation and normalised to its unrotated size.
"""
import logging
import threading
from pathlib import Path

from app.digikam import dbsync, dkdb
from app.photos import edits, tagmanager
from app.data import index
from app.digikam.dbsync import SyncError

log = logging.getLogger("app.faces.facemgr")

# which ImageTagProperties row holds the rectangle of each kind of face
REGION_PROP = {"confirmed": "tagRegion", "unknown": "autodetectedFace", "suggested": "autodetectedFace", "ignored": "ignoredFace"}
_lock = threading.Lock()


def rect_xml(x, y, w, h) -> str:
    return f'<rect x="{int(x)}" y="{int(y)}" width="{int(w)}" height="{int(h)}"/>'


def _special(con) -> dict:
    out = {}
    for tid, prop in con.execute("SELECT tagid, property FROM TagProperties WHERE property IN ('unknownPerson', 'ignoredPerson', 'unconfirmedPerson')"):
        out[prop] = tid
    if len(out) < 3:
        raise SyncError("digiKam's Unknown / Unconfirmed / Ignored face tags were not found in the database")
    return out


def _is_person(con, tid: int) -> bool:
    return bool(con.execute("SELECT 1 FROM TagProperties WHERE tagid = ? AND property = 'person'", (tid,)).fetchone())


def _full(con, tid: int) -> str:
    return dkdb.tag_path(dkdb.tag_tree(con), tid)


def _ensure_person(con, tid: int):
    """FaceTags::ensureIsPerson: a tag used for a face carries the 'person' property (its name)."""
    if not _is_person(con, tid):
        name = con.execute("SELECT name FROM Tags WHERE id = ?", (tid,)).fetchone()[0]
        con.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?, 'person', ?)", (tid, name))


def _add_prop(con, image, tag, prop, value):
    if not con.execute("SELECT 1 FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = ? AND value = ?",
                       (image, tag, prop, value)).fetchone():
        con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (image, tag, prop, value))


def _remove_prop(con, image, tag, prop, value):
    con.execute("DELETE FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = ? AND value = ?", (image, tag, prop, value))


def _remove_face(con, sp, image, tag, kind, rect, touch_tags: bool):
    """FaceTagsEditor::removeFaceAndTag."""
    _remove_prop(con, image, tag, REGION_PROP[kind], rect)
    _remove_prop(con, image, tag, "faceTagExtendedData", rect)
    if kind == "confirmed":
        _remove_prop(con, image, tag, "faceToTrain", rect)
    if kind == "suggested":
        _remove_prop(con, image, sp["unconfirmedPerson"], "autodetectedPerson", f"{tag},autodetectedFace,{rect}")
    if touch_tags and kind == "confirmed":
        still = con.execute("SELECT 1 FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = 'tagRegion'", (image, tag)).fetchone()
        if not still:
            con.execute("DELETE FROM ImageTags WHERE imageid = ? AND tagid = ?", (image, tag))


def _add_confirmed(con, image, tag, rect, train=True):
    """FaceTagsEditor::add / confirmName: a named face, trained, with the tag assigned."""
    _ensure_person(con, tag)
    _add_prop(con, image, tag, "tagRegion", rect)
    if train:
        _add_prop(con, image, tag, "faceToTrain", rect)
    con.execute("REPLACE INTO ImageTags (imageid, tagid) VALUES (?,?)", (image, tag))


def _find(con, sp, image, rect):
    """Which face (tag, kind) sits at this exact rectangle on this image."""
    for tag, prop in con.execute("SELECT tagid, property FROM ImageTagProperties WHERE imageid = ? AND value = ? "
                                 "AND property IN ('tagRegion', 'autodetectedFace', 'ignoredFace')", (image, rect)):
        if prop == "tagRegion":
            return tag, "confirmed"
        if prop == "ignoredFace":
            return tag, "ignored"
        return tag, ("unknown" if tag == sp["unknownPerson"] else "suggested")
    return None, None


def apply(op: dict, db_path=None, check_running: bool = True, background: bool = False) -> dict:
    """One face change (see apply_many for the operations)."""
    return apply_many([op], db_path, check_running, background)[0]


_side_queue = {}            # image -> db_path, waiting for its sidecar rewrite (latest database state is written)
_side_cond = threading.Condition()
_side_worker = {"thread": None}


def _sidecar_worker():
    while True:
        with _side_cond:
            while not _side_queue:
                _side_cond.wait()
            batch = list(_side_queue.items())[:50]
            for image, _p in batch:
                _side_queue.pop(image, None)
        by_db = {}
        for image, path in batch:
            by_db.setdefault(path, []).append(image)
        for db_path, images in by_db.items():
            try:
                res = write_regions_many(images, db_path)
                bad = [i for i, v in res.items() if v != "written"]
                if bad:
                    log.warning("face regions not written to the sidecars of %s: %s", bad[:10], {i: res[i] for i in bad[:3]})
            except Exception as e:
                log.warning("face regions not written to the sidecars of %s: %s", images[:10], e)


def queue_sidecars(images: list, db_path=None):
    """Rewrite these photos' sidecar regions from the database in the background, in order, one worker."""
    with _side_cond:
        for i in images:
            _side_queue[int(i)] = db_path
        if _side_worker["thread"] is None or not _side_worker["thread"].is_alive():
            _side_worker["thread"] = threading.Thread(target=_sidecar_worker, name="face-sidecars", daemon=True)
            _side_worker["thread"].start()
        _side_cond.notify()


def sidecars_pending() -> int:
    with _side_cond:
        return len(_side_queue)


def apply_many(ops: list, db_path=None, check_running: bool = True, background: bool = False) -> list:
    """Several face changes in one transaction with one backup. Each op:
    {op: name|confirm|ignore|unknown|remove|add|move, image, rect: [x,y,w,h], tag?, new_rect?}
    name:    give the face at rect (unknown, suggested or confirmed) the person `tag`
    confirm: accept a suggestion as it is
    ignore:  mark the face as not a person to name (digiKam's Ignored)
    unknown: forget the name, keep the region as an unknown face
    remove:  delete the region entirely
    add:     a new region for person `tag`
    detected: a new unknown face (face detection), unless a face already sits at that exact rectangle
    move:    change the rectangle of an existing face to new_rect
    untag:   take person `tag` off the photo when no face region carries them (a person tagged without a box)
    tagonly: put person `tag` on the photo without a face region (what undoing untag does)
    Returns one summary per op; a failed op has an "error" and the others still apply.
    background: return right after the database write and rewrite the sidecars afterwards (what the web app
    asks for, so a face change shows at once); otherwise each op carries its sidecar outcome."""
    db_path = Path(db_path or dkdb.sqlite_path())
    if not ops:
        return []
    for op in ops:
        if op.get("op") not in ("name", "confirm", "ignore", "unknown", "remove", "add", "move", "detected", "suggest", "untag", "tagonly"):
            raise SyncError("Unknown face operation")
    dbsync.preflight(db_path, check_running, "faces")
    results, touched = [], []
    with _lock:
        backup = None  # face changes take no full database copy; each is in the change log
        with dbsync.write_transaction(db_path, check_running) as con:
            sp = _special(con)
            for op in ops:
                try:
                    results.append(_apply_one(con, sp, op))
                    touched.append(int(op.get("image") or 0))
                except SyncError as e:
                    results.append({"op": op.get("op"), "image": op.get("image"), "error": str(e)})
            if not any("error" not in r for r in results):
                raise SyncError(results[0]["error"])
    from app.photos import history
    labels = {"name": "Face named {name}", "confirm": "Suggested face confirmed as {name}", "ignore": "Face marked as ignored", "unknown": "Face name removed (now unknown)",
              "remove": "Face region removed", "add": "Face added for {name}", "move": "Face region moved", "detected": "Unnamed face added by face detection",
              "suggest": "Suggestion for {name} put back", "untag": "{name} taken off the photo (no face box)",
              "tagonly": "{name} put back on the photo (no face box)"}
    done = [r for r in results if "error" not in r and not r.get("skipped")]
    try:
        paths = dict(dbsync._photo_paths(list({r["image"] for r in done})))
    except Exception:
        paths = {}
    rows = []
    for r in done:
        src = next((o for o in ops if int(o.get("image") or 0) == r["image"] and o.get("op") == r.get("op")), {})
        det = {k: v for k, v in r.items() if k in ("op", "tag", "name", "was", "from_tag")} | {"rect": src.get("rect")}
        if src.get("new_rect"):
            det["new_rect"] = src["new_rect"]
        inv = inverse_ops(det)
        rows.append({"photo_id": r["image"], "action": "face", "path": paths.get(r["image"]),
                     "summary": labels.get(r.get("op"), "Face changed").format(name=(r.get("name") or "").split("/")[-1] or "a person"),
                     "details": det, "undo": {"face_ops": inv} if inv else None})
    history.record(rows)
    # a person tagged without a box lives in the sidecar's tag list, not its regions: rewrite that list too
    tag_only = [{"id": r["image"], "path": paths[r["image"]]} for r in done if r.get("op") in ("untag", "tagonly") and paths.get(r["image"])]
    if tag_only:
        from app.photos import edits
        edits.rewrite_tags(tag_only)
    if background:
        queue_sidecars(list(dict.fromkeys(touched)), db_path)
        for r in results:
            if "error" not in r:
                r["sidecar"] = "queued"
        return results
    for image in dict.fromkeys(touched):
        try:
            status = write_regions(image, db_path)
        except Exception as e:  # the database change stands; the sidecar can be rewritten later
            status = "error: " + str(e)[:200]
        for r in results:
            if r.get("image") == image and "error" not in r:
                r["sidecar"] = status
                r["backup"] = str(backup) if backup else None
    return results


def inverse_ops(d: dict) -> list:
    """The face operations that take a photo back to how it was before the recorded change d (its history
    details: op, rect, tag, was, from_tag, new_rect). Empty when the change cannot be undone that way."""
    op, at, was, before = d.get("op"), d.get("rect"), d.get("was"), d.get("from_tag")
    if op in ("untag", "tagonly") and d.get("tag"):
        return [{"op": "tagonly" if op == "untag" else "untag", "tag": d["tag"]}]
    if not at:
        return []

    def back_to(kind, t):
        if kind == "confirmed" and t:
            return [{"op": "name", "rect": at, "tag": t}]
        if kind == "unknown":
            return [{"op": "unknown", "rect": at}]
        if kind == "ignored":
            return [{"op": "ignore", "rect": at}]
        if kind == "suggested" and t:
            return [{"op": "remove", "rect": at}, {"op": "suggest", "rect": at, "tag": t}]
        return []
    if op in ("add", "detected", "suggest"):
        return [{"op": "remove", "rect": at}]
    if op in ("name", "confirm", "ignore", "unknown"):
        return back_to(was, before)
    if op == "remove":
        if was == "confirmed" and before:
            return [{"op": "add", "rect": at, "tag": before}]
        if was == "suggested" and before:
            return [{"op": "suggest", "rect": at, "tag": before}]
        if was == "unknown":
            return [{"op": "detected", "rect": at}]
        if was == "ignored":
            return [{"op": "detected", "rect": at}, {"op": "ignore", "rect": at}]
        return []
    if op == "move" and d.get("new_rect"):
        return [{"op": "move", "rect": d["new_rect"], "new_rect": at}]
    return []


def _person_already_there(con, image: int, tag: int, rect) -> bool:
    """Whether this person already has a face region on the photo within a few pixels of rect (the duplicates
    Maintenance › Metadata quality cleans up must not be made here)."""
    from app.faces.facequality import NEAR
    x, y, w, h = map(int, rect)
    for (val,) in con.execute("SELECT value FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = 'tagRegion'", (image, tag)):
        m = index.RECT_RE.search(val or "")
        if m and all(abs(a - b) <= NEAR for a, b in zip(map(int, m.groups()), (x, y, w, h))):
            return True
    return False


def _apply_one(con, sp: dict, op: dict) -> dict:
    kind_op = op.get("op")
    image = int(op.get("image") or 0)
    rect = rect_xml(*op["rect"]) if op.get("rect") else None
    if not con.execute("SELECT 1 FROM Images WHERE id = ? AND status = 1", (image,)).fetchone():
        raise SyncError("That photo is not in the digiKam database.")
    tag, kind = _find(con, sp, image, rect) if rect else (None, None)
    summary = {"op": kind_op, "image": image}
    if kind_op == "detected":  # FaceTagsEditor::add for an unnamed face: the Unknown tag's autodetectedFace row
        if kind is not None:
            summary.update(skipped=True)
            return summary
        _add_prop(con, image, sp["unknownPerson"], "autodetectedFace", rect)
        return summary
    if kind_op == "suggest":  # a suggestion for person `tag` (digiKam's unconfirmed face), as undo puts one back
        new_tag = int(op.get("tag") or 0)
        if kind is not None:
            raise SyncError("There is already a face at exactly that rectangle.")
        _add_prop(con, image, new_tag, "autodetectedFace", rect)
        _add_prop(con, image, sp["unconfirmedPerson"], "autodetectedPerson", f"{new_tag},autodetectedFace,{rect}")
        summary.update(tag=new_tag, name=_full(con, new_tag))
        return summary
    if kind_op in ("untag", "tagonly"):
        person = int(op.get("tag") or 0)
        if person in sp.values() or not con.execute("SELECT 1 FROM TagProperties WHERE tagid = ? AND property = 'person'", (person,)).fetchone():
            raise SyncError("That is not a person.")
        if con.execute("SELECT 1 FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property IN ('tagRegion', 'autodetectedFace')", (image, person)).fetchone():
            raise SyncError("A face box carries that person: remove or rename the face instead.")
        if kind_op == "untag":
            if not con.execute("SELECT 1 FROM ImageTags WHERE imageid = ? AND tagid = ?", (image, person)).fetchone():
                raise SyncError("That person is not on the photo.")
            con.execute("DELETE FROM ImageTags WHERE imageid = ? AND tagid = ?", (image, person))
        else:
            con.execute("REPLACE INTO ImageTags (imageid, tagid) VALUES (?,?)", (image, person))
        summary.update(tag=person, name=_full(con, person))
        return summary
    if kind_op == "add":
        new_tag = int(op.get("tag") or 0)
        if new_tag in sp.values() or not con.execute("SELECT 1 FROM Tags WHERE id = ?", (new_tag,)).fetchone():
            raise SyncError("Choose a person for the new face.")
        if kind is not None:
            raise SyncError("There is already a face at exactly that rectangle.")
        if _person_already_there(con, image, new_tag, op["rect"]):
            raise SyncError("That person already has a face there; a second region would list them twice.")
        _add_confirmed(con, image, new_tag, rect)
        summary.update(tag=new_tag, name=_full(con, new_tag))
        return summary
    if kind is None:
        raise SyncError("No face was found at that rectangle; the photo may have changed in digiKam.")
    if kind_op == "name":
        new_tag = int(op.get("tag") or 0)
        if new_tag in sp.values() or not con.execute("SELECT 1 FROM Tags WHERE id = ?", (new_tag,)).fetchone():
            raise SyncError("Choose a person.")
        if new_tag != tag and _person_already_there(con, image, new_tag, op["rect"]):
            raise SyncError("That person already has a face there; remove this region instead of naming it.")
        _remove_face(con, sp, image, tag, kind, rect, touch_tags=(tag != new_tag))
        _add_confirmed(con, image, new_tag, rect)
        summary.update(tag=new_tag, name=_full(con, new_tag), was=kind)
    elif kind_op == "confirm":
        if kind != "suggested":
            raise SyncError("Only a suggested face can be confirmed.")
        _remove_face(con, sp, image, tag, kind, rect, touch_tags=False)
        _add_confirmed(con, image, tag, rect)
        summary.update(tag=tag, name=_full(con, tag))
    elif kind_op == "ignore":
        _remove_face(con, sp, image, tag, kind, rect, touch_tags=(kind == "confirmed"))
        _add_prop(con, image, sp["ignoredPerson"], "ignoredFace", rect)
    elif kind_op == "unknown":
        _remove_face(con, sp, image, tag, kind, rect, touch_tags=(kind == "confirmed"))
        _add_prop(con, image, sp["unknownPerson"], "autodetectedFace", rect)
    elif kind_op == "remove":
        _remove_face(con, sp, image, tag, kind, rect, touch_tags=True)
    elif kind_op == "move":
        new_rect = rect_xml(*op["new_rect"])
        _remove_face(con, sp, image, tag, kind, rect, touch_tags=False)
        _add_prop(con, image, tag, REGION_PROP[kind], new_rect)
        if kind == "confirmed":
            _add_prop(con, image, tag, "faceToTrain", new_rect)
            con.execute("REPLACE INTO ImageTags (imageid, tagid) VALUES (?,?)", (image, tag))
        elif kind == "suggested":
            _add_prop(con, image, sp["unconfirmedPerson"], "autodetectedPerson", f"{tag},autodetectedFace,{new_rect}")
    summary.update(was=kind, from_tag=tag)
    return summary


def create_person(name: str, parent: int = 0) -> int:
    """A new person tag (under People/<family> usually), marked as a person. Returns its id."""
    r = tagmanager.apply({"op": "add", "parent": parent, "name": name})
    tid = int(r["id"])
    # tagmanager.apply just ran every check and took the backup; this follow-up row is tiny
    with dkdb.write_lock:
        con = dkdb.connect(write=True)
        try:
            con.begin_write()
            _ensure_person(con, tid)
            con.commit()
            dkdb.note_own_write()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
    return tid


# --------------------------------------------------------------------------
# sidecar regions (what digiKam writes with "Save face tags" on)
# --------------------------------------------------------------------------

def _unrotate(x, y, w, h, up_w, up_h, orientation):
    """TagRegion::reverseToOrientation: an upright rectangle back to the file's own pixel grid."""
    o = orientation or 1
    if o == 1:
        return x, y, w, h, up_w, up_h
    if o == 2:
        return up_w - x - w, y, w, h, up_w, up_h
    if o == 3:
        return up_w - x - w, up_h - y - h, w, h, up_w, up_h
    if o == 4:
        return x, up_h - y - h, w, h, up_w, up_h
    if o == 5:
        return y, x, h, w, up_h, up_w
    if o == 6:
        return y, up_w - x - w, h, w, up_h, up_w
    if o == 7:
        return up_h - y - h, up_w - x - w, h, w, up_h, up_w
    if o == 8:
        return up_h - y - h, x, h, w, up_h, up_w
    return x, y, w, h, up_w, up_h


def _struct(v) -> str:
    """exiftool's inline structure syntax, with its escapes for the separators."""
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}={_struct(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ",".join(_struct(x) for x in v) + "]"
    s = str(v)
    return s.replace("|", "||").replace(",", "|,").replace("}", "|}").replace("]", "|]")


def write_regions(image: int, db_path=None) -> str:
    """Rewrite the sidecar's (and file's, if enabled) face regions from the database."""
    return write_regions_many([image], db_path).get(int(image), "failed")


def _region_args(src, image: int, roots: dict, tags: dict, persons: set):
    """(photo path, exiftool arguments) that write this photo's regions as the database has them, or (None, why)."""
    row = src.execute(
        "SELECT i.name, a.albumRoot, a.relativePath, ii.width, ii.height, ii.orientation FROM Images i "
        "JOIN Albums a ON a.id = i.album LEFT JOIN ImageInformation ii ON ii.imageid = i.id WHERE i.id = ?", (image,)).fetchone()
    if not row:
        return None, "no such image"
    path = roots.get(row[1], "") + row[2].rstrip("/") + "/" + row[0]
    faces = []
    for tid, val in src.execute("SELECT tagid, value FROM ImageTagProperties WHERE imageid = ? AND property = 'tagRegion'", (image,)):
        m = index.RECT_RE.search(val or "")
        if m:
            faces.append((dkdb.tag_path(tags, tid).split("/")[-1], tuple(map(int, m.groups()))))
    # people assigned without a region are written as name-only MP regions, as digiKam does
    with_region = {n for n, _ in faces}
    for (tid,) in src.execute("SELECT tagid FROM ImageTags WHERE imageid = ?", (image,)):
        if tid in persons:
            n = dkdb.tag_path(tags, tid).split("/")[-1]
            if n not in with_region:
                faces.append((n, None))
    w, h, orient = row[3] or 0, row[4] or 0, row[5] or 1
    if not w or not h:
        return None, "no image size known"
    # the database stores the unrotated size; the regions are upright
    up_w, up_h = (h, w) if orient in (5, 6, 7, 8) else (w, h)
    mwg, mp = [], []
    for name, r in faces:
        if r is None:
            mp.append({"PersonDisplayName": name})
            continue
        x, y, rw, rh, fw, fh = _unrotate(*r, up_w, up_h, orient)
        mwg.append({"Area": {"X": round((x + rw / 2) / fw, 6), "Y": round((y + rh / 2) / fh, 6), "W": round(rw / fw, 6), "H": round(rh / fh, 6), "Unit": "normalized"}, "Name": name, "Type": "Face"})
        mp.append({"PersonDisplayName": name, "Rectangle": f"{x / fw:.6g}, {y / fh:.6g}, {rw / fw:.6g}, {rh / fh:.6g}"})
    args = []
    if mwg:
        args += [f"-XMP-mwg-rs:RegionInfo={_struct({'AppliedToDimensions': {'W': w, 'H': h, 'Unit': 'pixel'}, 'RegionList': mwg})}"]
    else:
        args += ["-XMP-mwg-rs:RegionInfo="]
    args += [f"-XMP-MP:RegionInfoMP={_struct({'Regions': mp})}" if mp else "-XMP-MP:RegionInfoMP="]
    return path, args


def write_regions_many(images: list, db_path=None, files: bool = None, sidecars: bool = True) -> dict:
    """Rewrite the face regions and the tag list of these photos' sidecars (and image files when `files`, by default
    the "write into files" setting) from the database, in one exiftool run. Returns {image: written|failed|why}."""
    db_path = Path(db_path or dkdb.sqlite_path())
    files = edits.writes_files() if files is None else files
    out, jobs, owner = {}, [], {}
    src = dkdb.connect(write=False, db_path=db_path)
    try:
        roots, tags = dkdb.root_paths(src), dkdb.tag_tree(src)
        persons = {r[0] for r in src.execute("SELECT tagid FROM TagProperties WHERE property = 'person'")}
        built = {int(i): _region_args(src, int(i), roots, tags, persons) for i in dict.fromkeys(images)}
    finally:
        src.close()
    # the tag list goes with the regions, as digiKam writes it: a named face is also a People/... tag on the photo,
    # and a sidecar with the regions but not the tags reads as a mismatch (and loses the name on a rescan)
    ok = [i for i, (path, _a) in built.items() if path is not None]
    db_tags, pend = edits.digikam_tag_paths(ok) if ok else {}, edits.pending()
    paths = {}
    for image, (path, args) in built.items():
        if path is None:
            out[image] = args
            continue
        tag_paths = edits.effective_tags(db_tags.get(image, []), pend.get(image, {}))
        if sidecars:
            side = edits._prepare(path)
            jobs.append((side, args + edits.write_args({}, tag_paths, False))); owner[side] = image
        if files and edits.can_write_file(path):
            jobs.append((path, args + edits.write_args({}, tag_paths, True))); owner[path] = image
        elif not sidecars:
            out[image] = "not a file the viewer writes into"
            continue
        paths[image] = path
    failed = {owner[t] for t in edits.run_exiftool(jobs)} if jobs else set()
    for image in paths:
        out[image] = "failed" if image in failed else "written"
    # digiKam need not rescan a file that now says what its database says
    dkdb.stamp_files([(i, p) for i, p in paths.items() if i not in failed])
    return out
