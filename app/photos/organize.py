"""Moving, renaming and trashing photos and folders, the way digiKam does it, so both programs agree.

Trash: digiKam keeps a trash per album root (DTrash): the file goes to <root>/.dtrash/files/<name>.<ext> and a
record <root>/.dtrash/info/<name>.dtrashinfo holds {"path": the file's full original path, "deletiontimestamp":
ISO time, "imageid": "<Images.id>"}; the Images row stays, with status 3 (Trashed) and album NULL
(CoreDB::removeItems). digiKam's Trash view lists what is there and restores by moving the file back and making
the row visible again. The viewer writes and reads exactly that, so a photo trashed here shows in digiKam's
trash and the other way round. Restoring puts the file back where it was (a folder created again if needed)
and the row back to status 1 in that folder. Emptying deletes the files, their records and the rows.

Moving a photo: the file and its sidecar(s) move on disk, then the row's album and name follow
(CoreDB::moveItem); the id is kept, so favorites, albums, edits and the change log stay attached. Renaming a
photo is the same with the folder unchanged. Renaming or moving a folder renames the directory and updates
relativePath for the folder and every folder below it (AlbumManager::renamePAlbum). Sidecars named photo.xmp
(digiKam's other style) move along when no other file shares the base name.

Every operation is one guarded transaction (dbsync.write_transaction) after the files moved, and each photo
gets a change-log entry. With Sync with digiKam off the same statements go to the viewer's library tables.
"""
import json
import logging
import os
import shutil
import time
from pathlib import Path

from app.digikam import dbsync, dkdb
from app.digikam.dbsync import SyncError

log = logging.getLogger("app.photos.organize")
TRASHED, VISIBLE = 3, 1
IMAGE_SIDE_MARKERS = (".xmp",)


def _rows(con, ids: list) -> list:
    """[{id, album, name, root, rel, dir, path}] for these Images ids (status 1), with their folder on disk."""
    roots = dkdb.root_paths(con)
    out = []
    for i in range(0, len(ids), 500):
        chunk = ",".join(str(int(x)) for x in ids[i:i + 500])
        for iid, album, name, root, rel in con.execute(
                f"SELECT i.id, i.album, i.name, a.albumRoot, a.relativePath FROM Images i JOIN Albums a ON a.id = i.album WHERE i.id IN ({chunk}) AND i.status = 1"):
            if root not in roots:
                continue
            d = Path(roots[root]) / rel.strip("/")
            out.append({"id": iid, "album": album, "name": name, "root": root, "rel": rel, "dir": d, "path": str(d / name), "root_path": str(roots[root]).rstrip("/")})
    return out


def sidecars_of(path: str) -> list:
    from app.photos import deletion
    return deletion.sidecars(path)


def _free(d: Path, name: str) -> str:
    if not (d / name).exists():
        return name
    stem, ext = os.path.splitext(name)
    for i in range(2, 1000):
        cand = f"{stem} ({i}){ext}"
        if not (d / cand).exists():
            return cand
    raise SyncError("Too many files with that name already.")


def _clean(name: str) -> str:
    from app.photos.folders import _clean_name
    return _clean_name(name, "file")


# ---- trash
def _trash_dirs(root_path: str) -> tuple:
    base = Path(root_path) / ".dtrash"
    files, info = base / "files", base / "info"
    files.mkdir(parents=True, exist_ok=True)
    info.mkdir(parents=True, exist_ok=True)
    return files, info


def _trash_base(info_dir: Path, name: str) -> str:
    """digiKam's naming: the file's base name, then base1, base2... when taken (getAvialableJsonFilePathInTrash)."""
    stem = Path(name).stem if "." in name else name
    base, n = stem, 0
    while (info_dir / f"{base}.dtrashinfo").exists():
        n += 1
        base = f"{stem}{n}"
    return base


def trash(ids: list, check_running: bool = True) -> dict:
    """Move these photos to their album root's trash, as digiKam does. Returns {trashed, failed}."""
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the photos")
    con = dkdb.connect()
    try:
        rows = _rows(con, ids)
    finally:
        con.close()
    moved, failed = [], []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for r in rows:
        try:
            if not os.path.exists(r["path"]):
                raise SyncError("the file is not on disk")
            files_dir, info_dir = _trash_dirs(r["root_path"])
            base = _trash_base(info_dir, r["name"])
            suffix = r["name"].split(".", 1)[1] if "." in r["name"] else ""
            dst = files_dir / (f"{base}.{suffix}" if suffix else base)
            rec = info_dir / f"{base}.dtrashinfo"
            rec.write_text(json.dumps({"path": r["path"], "deletiontimestamp": now, "imageid": str(r["id"])}, indent=4) + "\n")
            try:
                os.chmod(rec, 0o600)
            except OSError:
                pass
            sides = sidecars_of(r["path"])
            shutil.move(r["path"], dst)
            side_moves, side_from = [], []
            for s in sides:  # sidecars ride along, beside the file, under the trash name
                tail = s[len(r["path"]):] if s.startswith(r["path"]) else ".xmp"
                sdst = files_dir / (dst.name + tail) if tail.startswith(".") and s.startswith(r["path"]) else files_dir / (base + ".xmp")
                shutil.move(s, sdst)
                side_moves.append(str(sdst)); side_from.append(s)
            moved.append({**r, "trash": str(dst), "info": str(rec), "sides": side_moves, "sides_from": side_from})
        except (OSError, SyncError) as e:
            failed.append({"id": r["id"], "error": str(e)})
    if moved:
        try:
            with dbsync.write_transaction(db_path, check_running) as con:
                for r in moved:
                    con.execute("UPDATE Images SET status = ?, album = NULL WHERE id = ?", (TRASHED, r["id"]))
        except BaseException:  # the database refused: the files go back where they were
            for r in moved:
                _move_back([(r["trash"], r["path"]), *zip(r["sides"], r["sides_from"])], r["info"])
            raise
        from app.photos import history
        history.record([{"photo_id": r["id"], "action": "trashed", "summary": "Moved to the trash", "path": r["path"],
                         "details": {"trash": r["trash"], "info": r["info"], "album": r["album"], "name": r["name"]}} for r in moved])
        _forget_index_only([r["id"] for r in moved])
    return {"trashed": [r["id"] for r in moved], "infos": [r["info"] for r in moved], "failed": failed}


def _forget_index_only(ids: list):
    """Thumbnails of trashed photos stay (the trash shows them); nothing else to drop: the row remains."""
    return


def listing() -> list:
    """What is in the trash of every album root: [{id, name, path, when, trash, info, root, size, exists}], newest first."""
    con = dkdb.connect()
    try:
        roots = dkdb.root_paths(con)
        status = {r[0]: r[1] for r in con.execute("SELECT id, status FROM Images WHERE status = ?", (TRASHED,)).fetchall()}
    finally:
        con.close()
    out = []
    for rid, rpath in roots.items():
        info = Path(rpath) / ".dtrash" / "info"
        files = Path(rpath) / ".dtrash" / "files"
        if not info.is_dir():
            continue
        for rec in info.glob("*.dtrashinfo"):
            try:
                d = json.loads(rec.read_text())
            except (OSError, ValueError):
                continue
            base = rec.stem
            orig = d.get("path") or ""
            suffix = os.path.basename(orig).split(".", 1)[1] if "." in os.path.basename(orig) else ""
            f = files / (f"{base}.{suffix}" if suffix else base)
            try:
                iid = int(d.get("imageid") or -1)
            except ValueError:
                iid = -1
            out.append({"id": iid if iid in status or iid > 0 else None, "name": os.path.basename(orig), "path": orig, "when": d.get("deletiontimestamp"),
                        "trash": str(f), "info": str(rec), "root": rid, "exists": f.exists(), "size": f.stat().st_size if f.exists() else 0})
    out.sort(key=lambda x: x.get("when") or "", reverse=True)
    return out


def _by_info(infos: list) -> list:
    want = set(infos)
    return [t for t in listing() if t["info"] in want]


def restore(infos: list, check_running: bool = True) -> dict:
    """Put trashed photos back where they were (their record names the path). Returns {restored: [ids], failed}."""
    from app.photos import folders
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the photos")
    items = _by_info(infos)
    done, failed = [], []
    for t in items:
        try:
            if not t["exists"]:
                raise SyncError("the file is no longer in the trash")
            dest = Path(t["path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            album = folders.album_for_dir(dest.parent)
            if album is None:  # the folder was removed meanwhile: make it known again
                parent = folders.album_for_dir(dest.parent.parent)
                if parent is None:
                    raise SyncError(f"the folder {dest.parent} is not in the library any more")
                album = folders.create(parent, dest.parent.name, check_running)["id"]
            if dest.exists():
                dest = dest.parent / _free(dest.parent, dest.name)
            shutil.move(t["trash"], dest)
            tf = Path(t["trash"])
            moves = [(t["trash"], str(dest))]
            for s in [Path(x) for x in (str(tf) + ".xmp", str(tf.with_suffix(".xmp"))) if os.path.isfile(x)]:  # no glob: names may hold [ ] * ?
                sdst = str(dest) + ".xmp" if s.name.startswith(tf.name) else str(dest.with_suffix(".xmp"))
                shutil.move(s, sdst)
                moves.append((str(s), sdst))
            done.append({**t, "album": album, "dest": dest, "moves": moves})
        except (OSError, SyncError) as e:
            failed.append({"info": t["info"], "error": str(e)})
    ids = []
    if done:
        try:
            _restore_rows(done, ids, db_path, check_running)
        except BaseException:  # the database refused: back into the trash, records kept
            for t in done:
                _move_back([(dst, src) for src, dst in t["moves"]], None)
            raise
        _restore_new(done, ids, check_running)
        for t in done:  # the trash records go only now that the rows are back
            try:
                os.remove(t["info"])
            except OSError:
                pass
    return {"restored": ids, "failed": failed}


def _move_back(pairs: list, record: str):
    """Undo file moves after a refused database write: each (where it is now, where it was); record: a trash
    record to drop. Best effort, logged."""
    for now, was in pairs:
        try:
            if os.path.exists(now):
                shutil.move(now, was)
        except OSError as e:
            log.warning("could not put %s back to %s: %s", now, was, e)
    if record:
        try:
            os.remove(record)
        except OSError:
            pass


def _restore_rows(done: list, ids: list, db_path, check_running: bool):
    """The one database write of a restore: rows marked visible again (a refusal leaves nothing half done)."""
    with dbsync.write_transaction(db_path, check_running) as con:
        for t in done:
            if t["id"] and con.execute("SELECT 1 FROM Images WHERE id = ?", (t["id"],)).fetchone():
                con.execute("UPDATE Images SET status = ?, album = ?, name = ? WHERE id = ?", (VISIBLE, t["album"], t["dest"].name, t["id"]))
                ids.append(t["id"])
            else:  # trashed by digiKam before this row was known here, or the row is gone: register the file anew
                t["new"] = True


def _restore_new(done: list, ids: list, check_running: bool):
    """After the rows are back: photos with no row any more are registered anew, and the edit log gets its lines."""
    from app.photos import folders
    if done:
        fresh = [t for t in done if t.get("new")]
        if fresh:
            from app.photos import scanner
            for t in fresh:
                added = folders.register(t["album"], [folders.staged_entry(t["dest"])], check_running)
                ids += [a["id"] for a in added]
                scanner.complete([a["id"] for a in added], check_running)  # dates, position, tags, faces from file and sidecar
        from app.photos import history
        history.record([{"photo_id": t["id"], "action": "restore", "summary": "Restored from the trash", "path": str(t["dest"])} for t in done if t["id"] and not t.get("new")])
    return ids



def empty(infos: list = None, check_running: bool = True) -> dict:
    """Delete these trash entries for good (all of them when infos is None): files, records and database rows."""
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the photos")
    items = _by_info(infos) if infos is not None else listing()
    gone, failed = [], []
    for t in items:
        try:
            tf = Path(t["trash"])
            for s in [Path(x) for x in (str(tf) + ".xmp", str(tf.with_suffix(".xmp"))) if os.path.isfile(x)]:  # no glob: names may hold [ ] * ?
                s.unlink(missing_ok=True)
            tf.unlink(missing_ok=True)
            os.remove(t["info"])
            gone.append(t)
        except OSError as e:
            failed.append({"info": t["info"], "error": str(e)})
    ids = [t["id"] for t in gone if t["id"]]
    if ids:
        from app.photos import deletion
        with dbsync.write_transaction(db_path, check_running) as con:
            for i in range(0, len(ids), 300):
                chunk = ",".join(map(str, ids[i:i + 300]))
                for tbl in deletion.PHOTO_TABLES:
                    con.execute(f"DELETE FROM {tbl} WHERE imageid IN ({chunk})")
                con.execute(f"UPDATE Tags SET icon = NULL WHERE icon IN ({chunk})")
                con.execute(f"UPDATE Albums SET icon = NULL WHERE icon IN ({chunk})")
                con.execute(f"DELETE FROM Images WHERE id IN ({chunk})")
        from app.photos import history
        history.record([{"photo_id": t["id"], "action": "deleted", "summary": "Deleted for good from the trash", "path": t["path"]} for t in gone if t["id"]])
        deletion.forget(ids)
    return {"deleted": len(gone), "failed": failed}


# ---- moving and renaming photos
def move_photos(ids: list, album_id: int, check_running: bool = True, new_name: str = None) -> dict:
    """Move these photos into the folder album_id (their sidecars too); with new_name (one photo) rename it as well.
    Returns {moved: [{id, name, path}], failed}."""
    from app.photos.folders import _album
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the photos")
    con = dkdb.connect()
    try:
        folder = _album(con, int(album_id))
        rows = _rows(con, ids)
    finally:
        con.close()
    if not folder["dir"].is_dir():
        raise SyncError(f"The folder is not on disk here: {folder['dir']}")
    if new_name is not None and len(rows) != 1:
        raise SyncError("A new name goes with one photo at a time.")
    moved, failed = [], []
    for r in rows:
        try:
            name = r["name"]
            if new_name is not None:
                name = _clean(new_name)
                old_ext, new_ext = os.path.splitext(r["name"])[1].lower(), os.path.splitext(name)[1].lower()
                if new_ext != old_ext:
                    name = os.path.splitext(name)[0] + os.path.splitext(r["name"])[1]  # the extension stays: it says what the file is
            if folder["id"] == r["album"] and name == r["name"]:
                continue
            if not os.path.exists(r["path"]):
                raise SyncError("the file is not on disk")
            final = name if (folder["dir"] / name) == Path(r["path"]) else _free(folder["dir"], name)
            dst = folder["dir"] / final
            sides = sidecars_of(r["path"])
            shutil.move(r["path"], dst)
            moves = [(r["path"], str(dst))]
            for s in sides:
                tail = s[len(r["path"]):] if s.startswith(r["path"]) else None
                sdst = str(dst) + tail if tail else str(dst.with_suffix(".xmp"))
                shutil.move(s, sdst)
                moves.append((s, sdst))
            if str(dst) != r["path"]:
                from app.photos import edits
                edits.move_backups(r["path"], str(dst))
            moved.append({**r, "dst": str(dst), "final": final, "to_album": folder["id"], "moves": moves})
        except (OSError, SyncError) as e:
            failed.append({"id": r["id"], "error": str(e)})
    if moved:
        try:
            with dbsync.write_transaction(db_path, check_running) as con:
                for r in moved:
                    con.execute("DELETE FROM Images WHERE album = ? AND name = ? AND id != ?", (r["to_album"], r["final"], r["id"]))  # a stale row at the destination
                    con.execute("UPDATE Images SET album = ?, name = ? WHERE id = ?", (r["to_album"], r["final"], r["id"]))
        except BaseException:  # the database refused: the files go back
            from app.photos import edits
            for r in moved:
                _move_back([(dst, src) for src, dst in r["moves"]], None)
                if r["dst"] != r["path"]:
                    edits.move_backups(r["dst"], r["path"])
            raise
        from app.photos import history
        history.record([{"photo_id": r["id"], "action": "moved", "summary": (f"Renamed to {r['final']}" if r["to_album"] == r["album"] else f"Moved to {folder['rel']}") + (f" (was {r['name']})" if r["final"] != r["name"] and r["to_album"] != r["album"] else ""),
                         "path": r["dst"], "details": {"from": r["path"], "to": r["dst"]}, "undo": {"move": {"album": r["album"], "name": r["name"]}}} for r in moved])
    return {"moved": [{"id": r["id"], "name": r["final"], "path": r["dst"]} for r in moved], "failed": failed}


def rename_photo(photo_id: int, new_name: str, check_running: bool = True) -> dict:
    con = dkdb.connect()
    try:
        rows = _rows(con, [int(photo_id)])
    finally:
        con.close()
    if not rows:
        raise SyncError("That photo is not in the library.")
    return move_photos([int(photo_id)], rows[0]["album"], check_running, new_name)


# ---- folders
def _folder(con, album_id: int) -> dict:
    from app.photos.folders import _album
    return _album(con, int(album_id))


def rename_by_pattern(ids: list, pattern: str, check_running: bool = True) -> dict:
    """Rename library files by a date pattern (export.name_for), each a normal rename: the id stays, sidecars move
    along, every one is in the change log with undo. Returns {renamed: [{id, name}], unchanged, failed}."""
    from app.photos import export
    from app.data import index
    if pattern not in ("date", "date-name", "date-seq"):
        raise SyncError("Unknown naming pattern")
    con = index.open_index()
    try:
        rows = {r[0]: r for r in con.execute(f"SELECT id, album_id, name, ts FROM photos WHERE id IN ({','.join(str(int(i)) for i in ids)})")}
        taken = {}
        for aid, name in con.execute(f"SELECT album_id, name FROM photos WHERE album_id IN ({','.join(str(r[1]) for r in rows.values()) or '0'})"):
            taken.setdefault(aid, set()).add(name.lower())
    finally:
        con.close()
    out = {"renamed": [], "unchanged": 0, "failed": []}
    for iid in sorted(rows, key=lambda i: (rows[i][3] or 0, i)):
        _id, aid, name, ts = rows[iid]
        used = taken.setdefault(aid, set())
        used.discard(name.lower())
        new = export.name_for(pattern, ts, name, used)
        if new == name:
            out["unchanged"] += 1
            continue
        try:
            r = rename_photo(iid, new, check_running)
            got = (r.get("moved") or [{}])[0].get("name", new)
            used.add(got.lower())
            out["renamed"].append({"id": iid, "name": got, "was": name})
        except (SyncError, OSError) as e:
            used.add(name.lower())
            out["failed"].append({"id": iid, "error": str(e)})
    return out


def rename_folder(album_id: int, new_name: str, check_running: bool = True) -> dict:
    """Rename a folder on disk and in the database (its subfolders' paths follow)."""
    return move_folder(album_id, None, check_running, new_name)


def move_folder(album_id: int, new_parent_id, check_running: bool = True, new_name: str = None) -> dict:
    """Move a folder under another (new_parent_id; None keeps its parent), optionally with a new name."""
    from app.photos.folders import _clean_name
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "folders")
    con = dkdb.connect()
    try:
        src = _folder(con, album_id)
        if src["rel"] == "/":
            raise SyncError("The root folder cannot be moved or renamed.")
        if new_parent_id is None:
            parent_rel = src["rel"].rsplit("/", 1)[0] or "/"
            parent = {"root": src["root"], "rel": parent_rel, "dir": src["dir"].parent}
        else:
            parent = _folder(con, new_parent_id)
            if parent["root"] != src["root"]:
                raise SyncError("Folders can be moved within one album root only.")
            if parent["rel"] == src["rel"] or parent["rel"].startswith(src["rel"].rstrip("/") + "/"):
                raise SyncError("A folder cannot be moved into itself.")
        name = _clean_name(new_name, "folder") if new_name is not None else src["dir"].name
        new_rel = (parent["rel"].rstrip("/") + "/" + name) if parent["rel"] != "/" else "/" + name
        if new_rel == src["rel"]:
            return {"id": src["id"], "path": src["rel"]}
        if con.execute("SELECT 1 FROM Albums WHERE albumRoot = ? AND relativePath = ?", (src["root"], new_rel)).fetchone() or (parent["dir"] / name).exists():
            raise SyncError("A folder with that name is already there.")
        children = con.execute("SELECT id, relativePath FROM Albums WHERE albumRoot = ? AND (relativePath = ? OR relativePath LIKE ?)",
                               (src["root"], src["rel"], src["rel"].rstrip("/") + "/%")).fetchall()
        prefix = src["rel"].rstrip("/") + "/"
        children = [(aid, rel) for aid, rel in children if rel == src["rel"] or rel.startswith(prefix)]  # LIKE treats _ and % in names as wildcards
    finally:
        con.close()
    if not src["dir"].is_dir():
        raise SyncError(f"The folder is not on disk here: {src['dir']}")
    new_dir = parent["dir"] / name
    shutil.move(str(src["dir"]), str(new_dir))
    try:
        with dbsync.write_transaction(db_path, check_running) as con:
            for aid, rel in children:
                con.execute("UPDATE Albums SET relativePath = ? WHERE id = ?", (new_rel + rel[len(src["rel"]):] if rel != src["rel"] else new_rel, aid))
    except BaseException:
        shutil.move(str(new_dir), str(src["dir"]))  # the database refused: the directory goes back
        raise
    from app.photos import edits
    edits.move_backups(str(src["dir"]), str(new_dir))  # the photos' sidecar and file backups follow the folder
    return {"id": src["id"], "path": new_rel, "folders": len(children)}
