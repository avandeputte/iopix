"""Merging changes made with Sync with digiKam off back into digiKam's database.

A three-way merge over the library tables (library.py): the base is what digiKam held when the library
was last copied from it (data/library-base.sqlite), ours is the library as it is now, theirs is digiKam's
database. Every difference between base and ours is applied to theirs, in one transaction with a backup
first: new tags, folders and photos (uploads) get new ids in digiKam and every row that refers to them
is remapped; renamed and moved tags, changed ratings, dates, positions, captions, copyright, tag
assignments and face regions are updated, added or removed; deleted photos are removed. Where digiKam
changed the same row meanwhile, ours wins and the row is counted as a conflict.

Afterwards the library is copied from digiKam again (so it carries digiKam's ids), and the viewer's own
data about uploaded photos follows them to their new ids.
"""
import logging
import time

from app.digikam import dbsync, dkdb, library

log = logging.getLogger("app.digikam.merge")

# per-photo tables with one row per photo: (viewer table, digiKam table, columns after imageid)
PER_PHOTO = [("photo_information", "ImageInformation"), ("photo_metadata", "ImageMetadata"), ("photo_positions", "ImagePositions"), ("video_metadata", "VideoMetadata")]


def _cols(table):
    return [c for c, _s, _m in library.SPEC[table][1]]


def _by(rows, cols, keycols):
    idx = [cols.index(k) for k in keycols]
    return {tuple(r[i] for i in idx): r for r in rows}


_WHOLE = ("tag_properties", "photo_tags", "face_regions", "copyright")  # compared as whole rows


def _keys():
    return (("tags", ["id"]), ("folders", ["id"]), ("photos", ["id"]), ("comments", ["imageid", "type", "language", "author"]),
            ("photo_properties", ["imageid", "property"]), *[(t, ["imageid"]) for t, _dk in PER_PHOTO])


def diff() -> dict:
    """Every difference between the base and the library now, by table, before any remapping. Nothing when
    no change was made here since the library last matched digiKam (the base is from before the first one)."""
    if not int(library.info().get("local_changes") or 0):
        return {"conflicts": 0, **{t: {"new": [], "gone": [], "changed": [], "cols": _cols(t)}
                                   for t in [k for k, _ in _keys()] + list(_WHOLE)}}
    base, ours = library.load_base(), library.current()
    if base is None:
        raise dbsync.SyncError("There is no record of what digiKam held when the library was copied, so the changes cannot be told apart. Replace the library with digiKam's, or keep sync off.")
    d = {"conflicts": 0}
    for table, keys in _keys():
        cols = _cols(table)
        b, o = _by(base[table], cols, keys), _by(ours[table], cols, keys)
        d[table] = {"new": [o[k] for k in o if k not in b], "gone": [b[k] for k in b if k not in o],
                    "changed": [(b[k], o[k]) for k in o if k in b and o[k] != b[k]], "cols": cols}
    for table in _WHOLE:
        cols = _cols(table)
        b, o = set(map(tuple, base[table])), set(map(tuple, ours[table]))
        d[table] = {"new": sorted(o - b, key=str), "gone": sorted(b - o, key=str), "changed": [], "cols": cols}
    return d


def plan() -> dict:
    """What a merge would do, for the Settings page."""
    d = diff()
    n = lambda t: {k: len(d[t][k]) for k in ("new", "gone", "changed")}
    out = {t: n(t) for t in d if t != "conflicts"}
    out["summary"] = _summary(out)
    out["total"] = sum(sum(v.values()) for t, v in out.items() if t not in ("summary",))
    return out


def _summary(counts: dict) -> list:
    s = []
    t = counts["tags"]
    if t["new"]: s.append(f"{t['new']} new tags")
    if t["changed"]: s.append(f"{t['changed']} tags renamed or moved")
    if t["gone"]: s.append(f"{t['gone']} tags deleted")
    if counts["folders"]["new"]: s.append(f"{counts['folders']['new']} new folders")
    if counts["photos"]["new"]: s.append(f"{counts['photos']['new']} uploaded photos")
    if counts["photos"]["gone"]: s.append(f"{counts['photos']['gone']} deleted photos")
    pt = counts["photo_tags"]
    if pt["new"] or pt["gone"]: s.append(f"{pt['new']} tags assigned, {pt['gone']} removed")
    fr = counts["face_regions"]
    if fr["new"] or fr["gone"]: s.append(f"{fr['new'] + fr['gone']} face changes")
    pi = counts["photo_information"]
    if pi["changed"]: s.append(f"{pi['changed']} ratings or dates")
    pp = counts["photo_positions"]
    if pp["new"] or pp["changed"] or pp["gone"]: s.append(f"{pp['new'] + pp['changed'] + pp['gone']} locations")
    c = counts["comments"]
    if c["new"] or c["changed"] or c["gone"]: s.append(f"{c['new'] + c['changed'] + c['gone']} titles or captions")
    cr = counts["copyright"]
    if cr["new"] or cr["gone"]: s.append(f"{cr['new'] + cr['gone']} copyright fields")
    return s


def apply(check_running: bool = True) -> dict:
    """Write every change made with sync off into digiKam's database. Returns {applied: {...}, conflicts, remapped}."""
    if not dkdb.sync_enabled():
        raise dbsync.SyncError("Turn Sync with digiKam on first; the merge writes into digiKam's database.")
    d = diff()
    t0 = time.time()
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the library")
    backup = dkdb.backup(db_path, force=True)
    tagmap, albummap, photomap, conflicts, applied = {}, {}, {}, 0, {}
    cnt = lambda k, n=1: applied.__setitem__(k, applied.get(k, 0) + n)
    with dbsync.write_transaction(db_path, check_running) as con:
        q = con.execute
        # ---- tags: new (parents first), changed, deleted
        tc = d["tags"]["cols"]
        for r in sorted(d["tags"]["new"], key=lambda r: r[0]):
            row = dict(zip(tc, r))
            pid = tagmap.get(row["pid"], row["pid"])
            cur = q("INSERT INTO Tags (pid, name, icon, iconkde) VALUES (?,?,?,?)", (pid, row["name"], row["icon"], row["iconkde"]))
            tagmap[row["id"]] = cur.lastrowid
            cnt("tags_new")
        for b, o in d["tags"]["changed"]:
            br, orow = dict(zip(tc, b)), dict(zip(tc, o))
            theirs = q("SELECT pid, name, icon, iconkde FROM Tags WHERE id = ?", (br["id"],)).fetchone()
            if theirs is None:
                continue  # digiKam deleted it meanwhile: nothing to update
            if tuple(theirs) != (br["pid"], br["name"], br["icon"], br["iconkde"]):
                conflicts += 1
            q("UPDATE Tags SET pid = ?, name = ?, icon = ?, iconkde = ? WHERE id = ?",
              (tagmap.get(orow["pid"], orow["pid"]), orow["name"], _pm(photomap, orow["icon"]), orow["iconkde"], br["id"]))
            cnt("tags_changed")
        for r in d["tags"]["gone"]:
            tid = r[0]
            for t in ("ImageTags", "TagProperties", "ImageTagProperties"):
                q(f"DELETE FROM {t} WHERE tagid = ?", (tid,))
            q("DELETE FROM Tags WHERE id = ?", (tid,))
            cnt("tags_gone")
        # ---- tag properties (set)
        for r in d["tag_properties"]["gone"]:
            q("DELETE FROM TagProperties WHERE tagid = ? AND property = ? AND value IS ?" if not con.mysql else "DELETE FROM TagProperties WHERE tagid = ? AND property = ? AND value <=> ?", r)
        for r in d["tag_properties"]["new"]:
            q("INSERT INTO TagProperties (tagid, property, value) VALUES (?,?,?)", (tagmap.get(r[0], r[0]), r[1], r[2]))
        cnt("tag_properties", len(d["tag_properties"]["new"]) + len(d["tag_properties"]["gone"])) if d["tag_properties"]["new"] or d["tag_properties"]["gone"] else None
        # ---- folders: new (parents first), changed caption/icon
        fc = d["folders"]["cols"]
        for r in sorted(d["folders"]["new"], key=lambda r: (r[fc.index("relativePath")].count("/"), r[0])):
            row = dict(zip(fc, r))
            cur = q("INSERT INTO Albums (albumRoot, relativePath, date, caption, collection, icon, modificationDate) VALUES (?,?,?,?,?,?,?)",
                    (row["albumRoot"], row["relativePath"], row["date"], row["caption"], row["collection"], None, row["modificationDate"]))
            albummap[row["id"]] = cur.lastrowid
            cnt("folders_new")
        for b, o in d["folders"]["changed"]:
            orow = dict(zip(fc, o))
            q("UPDATE Albums SET caption = ?, collection = ? WHERE id = ?", (orow["caption"], orow["collection"], orow["id"]))
            cnt("folders_changed")
        # ---- photos: new (uploads), deleted
        pc = d["photos"]["cols"]
        for r in sorted(d["photos"]["new"], key=lambda r: r[0]):
            row = dict(zip(pc, r))
            cur = q("INSERT INTO Images (album, name, status, category, modificationDate, fileSize, uniqueHash, manualOrder) VALUES (?,?,?,?,?,?,?,?)",
                    (albummap.get(row["album"], row["album"]), row["name"], row["status"], row["category"], row["modificationDate"], row["fileSize"], row["uniqueHash"], row["manualOrder"]))
            photomap[row["id"]] = cur.lastrowid
            cnt("photos_new")
        for r in d["photos"]["gone"]:
            pid = r[0]
            for t in ("ImageInformation", "ImageMetadata", "ImagePositions", "VideoMetadata", "ImageComments", "ImageCopyright", "ImageProperties", "ImageTags", "ImageTagProperties"):
                q(f"DELETE FROM {t} WHERE imageid = ?", (pid,))
            q("UPDATE Tags SET icon = NULL WHERE icon = ?", (pid,))
            q("UPDATE Albums SET icon = NULL WHERE icon = ?", (pid,))
            q("DELETE FROM Images WHERE id = ?", (pid,))
            cnt("photos_gone")
        gone_photos = {r[0] for r in d["photos"]["gone"]}
        # ---- one-row-per-photo tables
        for table, dk in PER_PHOTO:
            cols = d[table]["cols"]
            for r in d[table]["new"]:
                row = dict(zip(cols, r))
                row["imageid"] = photomap.get(row["imageid"], row["imageid"])
                q(f"INSERT OR REPLACE INTO {dk} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(row[c] for c in cols))
                cnt(table)
            for b, o in d[table]["changed"]:
                orow, brow = dict(zip(cols, o)), dict(zip(cols, b))
                theirs = q(f"SELECT {', '.join(cols)} FROM {dk} WHERE imageid = ?", (orow["imageid"],)).fetchone()
                if theirs is not None and tuple(_norm(v) for v in theirs) != tuple(_norm(v) for v in b):
                    conflicts += 1
                sets = [c for c in cols if c != "imageid" and _norm(orow[c]) != _norm(brow[c])]
                if sets:
                    q(f"UPDATE {dk} SET {', '.join(c + ' = ?' for c in sets)} WHERE imageid = ?", tuple(orow[c] for c in sets) + (orow["imageid"],))
                    cnt(table)
            for r in d[table]["gone"]:
                if r[0] not in gone_photos:
                    q(f"DELETE FROM {dk} WHERE imageid = ?", (r[0],))
                    cnt(table)
        # ---- comments
        cc = d["comments"]["cols"]
        for r in d["comments"]["new"]:
            row = dict(zip(cc, r))
            q("INSERT INTO ImageComments (imageid, type, language, author, date, comment) VALUES (?,?,?,?,?,?)",
              (photomap.get(row["imageid"], row["imageid"]), row["type"], row["language"], row["author"], row["date"], row["comment"]))
            cnt("comments")
        for b, o in d["comments"]["changed"]:
            row = dict(zip(cc, o))
            q("UPDATE ImageComments SET comment = ?, date = ? WHERE imageid = ? AND type = ? AND language = ? AND " + ("author IS ?" if not con.mysql else "author <=> ?"),
              (row["comment"], row["date"], row["imageid"], row["type"], row["language"], row["author"]))
            cnt("comments")
        for r in d["comments"]["gone"]:
            row = dict(zip(cc, r))
            if row["imageid"] not in gone_photos:
                q("DELETE FROM ImageComments WHERE imageid = ? AND type = ? AND language = ? AND " + ("author IS ?" if not con.mysql else "author <=> ?"),
                  (row["imageid"], row["type"], row["language"], row["author"]))
                cnt("comments")
        # ---- properties, copyright, photo tags, face regions (sets)
        for r in d["photo_properties"]["gone"]:
            if r[0] not in gone_photos:
                q("DELETE FROM ImageProperties WHERE imageid = ? AND property = ?", (r[0], r[1])); cnt("properties")
        for r in d["photo_properties"]["new"] + [o for _b, o in d["photo_properties"]["changed"]]:
            q("DELETE FROM ImageProperties WHERE imageid = ? AND property = ?", (photomap.get(r[0], r[0]), r[1]))
            q("INSERT INTO ImageProperties (imageid, property, value) VALUES (?,?,?)", (photomap.get(r[0], r[0]), r[1], r[2])); cnt("properties")
        for r in d["copyright"]["gone"]:
            if r[1] not in gone_photos:
                q("DELETE FROM ImageCopyright WHERE imageid = ? AND property = ? AND " + ("value IS ? AND extraValue IS ?" if not con.mysql else "value <=> ? AND extraValue <=> ?"), (r[1], r[2], r[3], r[4])); cnt("copyright")
        for r in d["copyright"]["new"]:
            q("INSERT INTO ImageCopyright (imageid, property, value, extraValue) VALUES (?,?,?,?)", (photomap.get(r[1], r[1]), r[2], r[3], r[4])); cnt("copyright")
        for r in d["photo_tags"]["gone"]:
            if r[0] not in gone_photos:
                q("DELETE FROM ImageTags WHERE imageid = ? AND tagid = ?", r); cnt("photo_tags")
        for r in d["photo_tags"]["new"]:
            q("INSERT OR IGNORE INTO ImageTags (imageid, tagid) VALUES (?,?)", (photomap.get(r[0], r[0]), tagmap.get(r[1], r[1]))); cnt("photo_tags")
        for r in d["face_regions"]["gone"]:
            if r[0] not in gone_photos:
                q("DELETE FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = ? AND " + ("value IS ?" if not con.mysql else "value <=> ?"), r); cnt("faces")
        for r in d["face_regions"]["new"]:
            val = r[3]
            if r[2] == "autodetectedPerson" and val and "," in val:  # "<person>,autodetectedFace,<rect>": the person id may be new
                p, rest = val.split(",", 1)
                val = f"{tagmap.get(int(p), p) if p.isdigit() else p},{rest}"
            q("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (photomap.get(r[0], r[0]), tagmap.get(r[1], r[1]), r[2], val)); cnt("faces")
    # ---- afterwards: the viewer's data follows uploaded photos to their digiKam ids, then the library is digiKam's again
    remap = {o: t for o, t in photomap.items() if o != t}
    if remap:  # digiKam has the rows now: whatever fails here is logged, the merge itself is done
        from app.data import index, state
        try:
            with state.lock:
                c = state.connect()
                try:
                    index.rekey(c, remap)
                    c.commit()
                finally:
                    c.close()
        except Exception as e:
            log.warning("the viewer's data about %d uploaded photos was not renumbered (%s); the next index build renumbers by file", len(remap), e)
        try:
            from app.ai import semantic
            semantic.remap_ids(remap)
        except Exception as e:
            log.warning("embeddings of uploaded photos were not renumbered: %s", e)
    library.discard_local_changes()
    from app.data import index
    index.refresh_library()
    index.conflict["digikam"] = None
    log.info("merged the changes made with sync off into digiKam's database in %.1fs: %s (%d conflicts, %d photos renumbered)", time.time() - t0, applied, conflicts, len(remap))
    return {"applied": applied, "conflicts": conflicts, "remapped": len(remap), "backup": str(backup) if backup else None}


def _pm(photomap, v):
    return photomap.get(v, v) if v is not None else None


def _norm(v):
    if isinstance(v, float):
        return round(v, 6)
    return v
