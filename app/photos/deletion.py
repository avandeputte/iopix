"""Deleting photos: from the disk, from digiKam's database (or the viewer's library with Sync with digiKam
off) and from everything the viewer keeps about them, with a backup first.

The backup, before anything is removed:
- the image file and its sidecar(s) are copied to data/deleted-photos/<time>/, under their folder path,
  with manifest.json listing what was deleted (path, digiKam id, hash, date, rating, title, caption, tags);
- digiKam's database gets a fresh backup (db-backups), as for any bulk write.

Then, in this order, so a failure never leaves a photo half deleted in the database:
1. one transaction removes the Images row and every row about the photo (information, metadata,
   position, video, captions, copyright, properties, tags, faces) and clears it as a tag's or folder's
   picture. digiKam's own triggers would do the same; the viewer's library tables have none;
2. the file and its sidecars are removed from the disk: "photo.jpg.xmp" always, and "photo.xmp" when no
   other file in the folder shares that base name (digiKam's other naming);
3. the viewer forgets the photo: favorites, albums, edits, AI results, face vectors, look-alike pairs,
   the consistency report, search embeddings, thumbnails.
"""
import json
import logging
import os
import shutil
import time
from pathlib import Path

from app import config
from app.digikam import dbsync, dkdb
from app.data import vdb

log = logging.getLogger("app.photos.deletion")
BACKUP_DIR = config.DATA_DIR / "deleted-photos"
PHOTO_TABLES = ("ImageInformation", "ImageMetadata", "ImagePositions", "VideoMetadata", "ImageComments", "ImageCopyright",
                "ImageProperties", "ImageTags", "ImageTagProperties")
VIEWER_TABLES = (("favorite_photos", "photo_id"), ("viewer_album_photos", "photo_id"), ("pending_edits", "photo_id"), ("ai_scans", "photo_id"), ("ai_quality", "photo_id"),
                 ("ai_text", "photo_id"), ("ai_face_count", "photo_id"), ("ai_objects", "photo_id"), ("ai_descriptions", "photo_id"), ("ai_looks", "photo_id"),
                 ("ai_face_embeddings", "photo_id"), ("ai_person_references", "photo_id"), ("metadata_check", "photo_id"), ("image_data_checksums", "photo_id"), ("quality_accepted", "photo_id"),
                 ("ai_similar_photos", "a"), ("ai_similar_photos", "b"))


def sidecars(path: str) -> list:
    """The sidecar files that belong to this photo."""
    out = []
    a = path + ".xmp"
    if os.path.isfile(a):
        out.append(a)
    base, _ext = os.path.splitext(path)
    b = base + ".xmp"
    if os.path.isfile(b):
        stem = os.path.basename(base)
        siblings = [f for f in os.listdir(os.path.dirname(path) or ".")
                    if f != os.path.basename(path) and os.path.splitext(f)[0] == stem and not f.lower().endswith(".xmp")]
        if not siblings:
            out.append(b)
    return out


def _roots() -> list:
    try:
        con = dkdb.connect()
        try:
            return [Path(v) for v in dkdb.root_paths(con).values()]
        finally:
            con.close()
    except Exception:
        return [Path(str(config.PHOTOS_ROOT))]


def _root_of(path: str) -> Path:
    """The album root this file sits in (the first root when none matches, as before there were several)."""
    p = Path(path)
    for r in _roots():
        try:
            p.relative_to(r)
            return r
        except ValueError:
            continue
    return Path(str(config.PHOTOS_ROOT))


def _rel(path: str) -> Path:
    p = Path(path)
    try:
        return p.relative_to(_root_of(path))
    except ValueError:
        return Path(*p.parts[1:]) if p.is_absolute() else p


def delete(photos: list, check_running: bool = True) -> dict:
    """photos: index rows (id, path, name, hash, size, taken, rating, title, caption, tags). Returns
    {deleted, failed: [{id, error}], backup}."""
    if not photos:
        return {"deleted": 0, "failed": [], "backup": None}
    db_path = Path(dkdb.sqlite_path())
    dbsync.preflight(db_path, check_running, "the photos")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(str(dest)).free
    need = sum(int(p.get("size") or 0) for p in photos) * 1.2 + 50 * 1024 ** 2
    if free < need:
        raise dbsync.SyncError(f"Not enough free disk space for the backup of these photos ({need / 1e9:.1f} GB needed); nothing was deleted.")
    # 1. the backup: files, sidecars, a manifest; and digiKam's database
    manifest, ok, failed = [], [], []
    for p in photos:
        try:
            files = [p["path"]] + sidecars(p["path"]) if os.path.exists(p["path"]) else sidecars(p["path"])
            for f in files:
                t = dest / _rel(f)
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, t)
            manifest.append({**{k: p.get(k) for k in ("id", "path", "name", "hash", "size", "taken", "rating", "title", "caption")},
                             "tags": p.get("tags", []), "files": [str(_rel(f)) for f in files]})
            ok.append(p)
        except OSError as e:
            failed.append({"id": p["id"], "error": f"could not back it up: {e}"})
    (dest / "manifest.json").write_text(json.dumps({"deleted": stamp, "photos": manifest}, indent=1))
    if not ok:
        return {"deleted": 0, "failed": failed, "backup": str(dest)}
    db_backup = dkdb.backup(db_path, force=True)
    # 2. the database rows, in one transaction
    ids = [int(p["id"]) for p in ok]
    with dbsync.write_transaction(db_path, check_running) as con:
        for i in range(0, len(ids), 300):
            chunk = ",".join(map(str, ids[i:i + 300]))
            for t in PHOTO_TABLES:
                con.execute(f"DELETE FROM {t} WHERE imageid IN ({chunk})")
            con.execute(f"UPDATE Tags SET icon = NULL WHERE icon IN ({chunk})")
            con.execute(f"UPDATE Albums SET icon = NULL WHERE icon IN ({chunk})")
            con.execute(f"DELETE FROM Images WHERE id IN ({chunk})")
    # 3. the files
    done = []
    for p in ok:
        try:
            for f in sidecars(p["path"]) + ([p["path"]] if os.path.exists(p["path"]) else []):
                os.remove(f)
            done.append(p["id"])
        except OSError as e:
            failed.append({"id": p["id"], "error": f"removed from the database, but the file could not be deleted: {e}"})
            done.append(p["id"])
    from app.photos import history
    man = {m["id"]: m for m in manifest}
    history.record([{"photo_id": pid, "action": "deleted", "summary": "Photo deleted (files and sidecar copied to deleted-photos first)",
                     "details": {"backup": str(dest), "root": str(_root_of(next((p["path"] for p in ok if p["id"] == pid), ""))),
                                 **{k: man.get(pid, {}).get(k) for k in ("name", "taken", "rating", "title", "caption", "tags", "files")}},
                     "path": next((p["path"] for p in ok if p["id"] == pid), None)} for pid in done])
    forget(done)
    log.info("deleted %d photos (backup in %s%s)", len(done), dest, f", database backup {db_backup}" if db_backup else "")
    return {"deleted": len(done), "failed": failed, "backup": str(dest), "database_backup": str(db_backup) if db_backup else None}


def forget(ids: list):
    """Everything the viewer keeps about these photos."""
    if not ids:
        return
    from app.ai import analysis, semantic
    from app import thumbs
    with analysis._write_lock:
        con = vdb.connect()
        try:
            for i in range(0, len(ids), 300):
                chunk = ",".join(str(int(x)) for x in ids[i:i + 300])
                for table, col in VIEWER_TABLES:
                    con.execute(f"DELETE FROM {table} WHERE {col} IN ({chunk})")
            con.commit()
        finally:
            con.close()
    try:
        semantic.remove_ids(ids)
    except Exception as e:
        log.warning("search embeddings of deleted photos were not removed: %s", e)
    for pid in ids:  # thumbnails and previews: <kind>/<id % 256>/<id>-<stamp>.jpg
        for d in ("t", "p"):
            folder = config.CACHE_DIR / d / f"{int(pid) % 256:02x}"
            for f in folder.glob(f"{int(pid)}-*.jpg") if folder.exists() else []:
                try:
                    f.unlink()
                except OSError:
                    pass
    _ = thumbs
