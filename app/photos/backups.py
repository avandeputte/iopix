"""Backups of what the viewer is about to rewrite, taken once, before its first change: a
sidecar's copy under data/sidecar-backups (or a ".created" marker when the viewer made the sidecar
itself, so revert deletes it), an image file's copy under data/file-backups. Both mirror the album
folders (app.photos.paths), move with a renamed photo or folder (move_backups), and are what
Revert and the change log's "Restore original file" put back.

A backup is refused, and so is the write, when the data disk is nearly full (MIN_FREE_BYTES):
a half-copied original would be worse than no edit.
"""
import os
import shutil
from pathlib import Path

from app import config
from app.photos.paths import BACKUP_DIR, FILE_BACKUP_DIR, rel, backup_path, file_backup_path, sidecar_path

MIN_FREE_BYTES = 2 * 1024 ** 3  # refuse to back up (and so to write) when the disk is nearly full
SKELETON = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n  <rdf:Description rdf:about=""/>\n'
            ' </rdf:RDF>\n</x:xmpmeta>\n<?xpacket end="w"?>\n')


def backup_original(photo_path: str) -> Path:
    """Copy an image file aside before its metadata is rewritten for the first time.
    One copy per file is kept (the true original); later edits do not replace it."""
    dst = file_backup_path(photo_path)
    if dst.exists():
        return dst
    size = os.stat(photo_path).st_size
    if shutil.disk_usage(str(config.DATA_DIR)).free < size + MIN_FREE_BYTES:
        raise OSError(f"Not enough free disk space to back up {os.path.basename(photo_path)} before editing it")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    shutil.copy2(photo_path, tmp)
    os.replace(tmp, dst)
    return dst


def move_backups(old_path: str, new_path: str):
    """A photo (or a folder of them) moved or renamed: its backups follow, so Revert and the change log's restore
    links still find them, and the next edit does not take an already-edited file for the original."""
    for root in (BACKUP_DIR, FILE_BACKUP_DIR):
        suffix = ".xmp" if root == BACKUP_DIR and not os.path.isdir(new_path) else ""
        for tail in ("", ".created") if suffix else ("",):
            src = root / (str(rel(old_path)) + suffix + tail)
            if src.exists():
                dst = root / (str(rel(new_path)) + suffix + tail)
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                except OSError:
                    pass  # a backup left behind is harmless


def _photo_of_sidecar(side: str) -> str:
    """The photo a sidecar belongs to: photo.jpg for photo.jpg.xmp; for photo.xmp (the compatible naming) the
    image file beside it with that stem, so the backup is named like backup_path(photo) everywhere."""
    stem = side[:-4]
    if os.path.isfile(stem):
        return stem
    d, name = os.path.split(stem)
    try:
        for n in os.listdir(d or "."):
            if n != name + ".xmp" and os.path.splitext(n)[0] == name and not n.lower().endswith(".xmp"):
                return os.path.join(d, n)
    except OSError:
        pass
    return stem


def ensure_backup(target: str, photo_path: str = None):
    """Whatever exiftool is about to rewrite gets a backup first: sidecars under
    sidecar-backups (once, before the viewer's first change), image files under file-backups."""
    if target.endswith(".xmp"):
        photo = photo_path or _photo_of_sidecar(target)
        backup = backup_path(photo)
        if not backup.exists() and not Path(str(backup) + ".created").exists() and os.path.exists(target):
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
    else:
        backup_original(target)


def prepare(photo_path: str) -> str:
    """Back the sidecar up before the viewer's first change, creating an empty
    sidecar if the photo has none. Returns the sidecar path."""
    side = sidecar_path(photo_path)
    backup = backup_path(photo_path)
    created = Path(str(backup) + ".created")
    if not os.path.exists(side) and not backup.exists() and not created.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        created.write_text("The viewer created this sidecar; there was none before.\n")  # so revert deletes it
    ensure_backup(side, photo_path)
    if not os.path.exists(side):
        with open(side, "x", encoding="utf-8") as f:
            f.write(SKELETON)
    return side
