"""Metadata editing, written to digiKam's XMP sidecar files (and, when the
owner turns that setting on, into JPEG/PNG/TIFF/WebP files as well, like
digiKam's "sidecar and file" mode).

An edit:
  1. backs up whatever is about to be rewritten, once, before the viewer's
     first change to it: sidecars into data/sidecar-backups and image files
     into data/file-backups, both mirroring the album folders;
  2. rewrites it with one exiftool run, setting the same fields digiKam writes;
  3. is remembered in the viewer's own database (vdb.py) so the viewer shows the
     new values right away. digiKam sees them after "Reread Metadata From File", or after
     "Apply to digiKam database" (dbsync) writes them into its database; once
     the database agrees the remembered edit is dropped automatically.

Editable: rating, pick label, color label, title, caption, date taken, GPS
position, metadata template and keyword tags. Face regions are written by
facemgr, through the same exiftool choke point (`run_exiftool`), which is the
only place files are rewritten and therefore where every backup is taken.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from app import config
from app.digikam import dkdb
from app.data import state as appstate

BACKUP_DIR = config.DATA_DIR / "sidecar-backups"
FILE_BACKUP_DIR = config.DATA_DIR / "file-backups"  # originals of image files before their metadata is rewritten
MIN_FREE_BYTES = 2 * 1024 ** 3  # refuse to back up (and so to write) when the disk is nearly full
SKELETON = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n  <rdf:Description rdf:about=""/>\n'
            ' </rdf:RDF>\n</x:xmpmeta>\n<?xpacket end="w"?>\n')
# Re-entrant: apply() and revert() hold it while they read and update the remembered edits,
# and run_exiftool() takes it too so two exiftool processes never rewrite the same file at once
_lock = threading.RLock()


def _find_exiftool():
    env = os.environ.get("EXIFTOOL")
    # any exiftool works (apt/brew/pip); digiKam's bundled copy is only a convenient fallback on a Mac
    for cand in (env, shutil.which("exiftool"), "/Applications/digiKam.org/digikam.app/Contents/bin/exiftool"):
        if cand and os.path.isfile(cand):
            return cand
    return None


EXIFTOOL = _find_exiftool()


def exiftool_cmd() -> list:
    """How to start exiftool: directly, or through perl when the script is not executable
    (pip's and some bundled copies)."""
    return [EXIFTOOL] if os.access(EXIFTOOL, os.X_OK) else ["perl", EXIFTOOL]


def available() -> bool:
    return bool(EXIFTOOL) and config.EDITING and not appstate.read_only()


def init():
    pass  # the edits table is part of the schema vdb.ensure_schema keeps (state.init)


def signature() -> str:
    """Changes whenever the set of remembered edits changes (part of the index version)."""
    from app.data import vdb
    if not vdb.exists():
        return "e0"
    con = appstate.connect()
    try:
        n, ts = con.execute("SELECT count(*), coalesce(max(ts), 0) FROM pending_edits").fetchone()
        return f"e{n}.{ts}"
    except Exception:
        return "e0"
    finally:
        con.close()


def _next_ts(c) -> int:
    """The stamp for a pending edit written now: never equal to the newest one, so two edits within the same second
    still change signature() (and the index shows the second)."""
    newest = c.execute("SELECT coalesce(max(ts), 0) FROM pending_edits").fetchone()[0] or 0
    return max(int(time.time()), int(newest) + 1)


def save_pending(rows: list):
    """rows: [(photo id, edit dict)]: remembered (replacing what was there), with a fresh stamp."""
    if rows:
        def go(c):
            ts = _next_ts(c)
            c.executemany("INSERT OR REPLACE INTO pending_edits VALUES (?,?,?)", [(int(pid), json.dumps(e), ts) for pid, e in rows])
        appstate._write(go)


def pending() -> dict:
    """photo_id -> remembered edit."""
    from app.data import vdb
    if not vdb.exists():
        return {}
    con = appstate.connect()
    try:
        return {r[0]: json.loads(r[1]) for r in con.execute("SELECT photo_id, data FROM pending_edits")}
    except Exception:
        return {}
    finally:
        con.close()


def pending_count() -> int:
    """How many edits are remembered (what status polls show; cheaper than loading them all)."""
    from app.data import vdb
    if not vdb.exists():
        return 0
    con = appstate.connect()
    try:
        return con.execute("SELECT count(*) FROM pending_edits").fetchone()[0]
    except Exception:
        return 0
    finally:
        con.close()


def newest_pending() -> int:
    """When the most recent remembered edit was made (unix time), 0 when there is none."""
    con = appstate.connect()
    try:
        row = con.execute("SELECT max(ts) FROM pending_edits").fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        con.close()


def forget(photo_ids):
    """Drop remembered edits (digiKam has caught up, or they were undone)."""
    ids = [(int(i),) for i in photo_ids]
    if ids:
        appstate._write(lambda c: c.executemany("DELETE FROM pending_edits WHERE photo_id = ?", ids))


def remap_pending_tags(mapping: dict) -> list:
    """After tags were renamed, moved, merged or deleted in digiKam's database: the pending edits that name them by
    path follow ({old path: new path, or None for a deleted tag}). Returns the photos whose edits changed, whose
    sidecars should then be rewritten."""
    if not mapping:
        return []
    def follow(t):
        return mapping[t] if t in mapping else t
    with _lock:
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
    with _lock:
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
    from app.data import index  # index imports this module; only needed here
    ids = [int(i) for i in photo_ids]
    if snapshot is not None:
        with _lock:
            now = pending()
            ids = [i for i in ids if i in snapshot and now.get(i) == snapshot[i]]
            return _resolved(ids, index)
    return _resolved(ids, index)


def _resolved(ids, index):
    if not ids:
        return
    try:
        con = index.open_index()
        try:
            rows = con.execute(f"SELECT path FROM photos WHERE id IN ({','.join(map(str, ids))})").fetchall()
        finally:
            con.close()
        for (path,) in rows:
            b = _backup_path(path)
            b.unlink(missing_ok=True)
            Path(str(b) + ".created").unlink(missing_ok=True)
    except (sqlite3.Error, OSError):
        pass  # a backup left behind is harmless; the edit is forgotten either way
    forget(ids)


def _rel(path: str) -> Path:
    """Where a library file sits below PHOTOS_ROOT (or below the filesystem root, for
    files outside it): the layout both backup folders mirror."""
    try:
        return Path(path).resolve().relative_to(config.PHOTOS_ROOT)
    except ValueError:
        return Path(*Path(path).resolve().parts[1:])


def _backup_path(photo_path: str) -> Path:
    return BACKUP_DIR / (str(_rel(photo_path)) + ".xmp")


def file_backup_path(photo_path: str) -> Path:
    return FILE_BACKUP_DIR / _rel(photo_path)


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
            src = root / (str(_rel(old_path)) + suffix + tail)
            if src.exists():
                dst = root / (str(_rel(new_path)) + suffix + tail)
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                except OSError:
                    pass  # a backup left behind is harmless


def ensure_backup(target: str):
    """Whatever exiftool is about to rewrite gets a backup first: sidecars under
    sidecar-backups (once, before the viewer's first change), image files under file-backups."""
    if target.endswith(".xmp"):
        photo = target[:-4]
        backup = _backup_path(photo)
        if not backup.exists() and not Path(str(backup) + ".created").exists() and os.path.exists(target):
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
    else:
        backup_original(target)


def sidecar_candidates(photo_path: str) -> tuple:
    """(photo.jpg.xmp, photo.xmp): digiKam's default sidecar name, and its "compatible file name" option."""
    return photo_path + ".xmp", os.path.splitext(photo_path)[0] + ".xmp"


def compat_naming() -> bool:
    """Write new sidecars as photo.xmp (digiKam's "compatible file name" setting): SIDECAR_COMPAT=1 or the setting."""
    if os.environ.get("SIDECAR_COMPAT", "").strip() in ("1", "true", "yes"):
        return True
    try:
        from app.data import state
        return state.get_setting("sidecar_compat", "0") == "1"
    except Exception:
        return False


def _compat_owner(photo_path: str) -> bool:
    """Whether photo.xmp belongs to this photo alone: not when photo.jpg and photo.cr2 would share it."""
    base, _ext = os.path.splitext(photo_path)
    stem, folder = os.path.basename(base), os.path.dirname(photo_path) or "."
    try:
        for f in os.listdir(folder):
            if f != os.path.basename(photo_path) and os.path.splitext(f)[0] == stem and not f.lower().endswith(".xmp"):
                return False
    except OSError:
        return False
    return True


def sidecar_path(photo_path: str) -> str:
    """The sidecar this photo uses: the one that exists (photo.jpg.xmp first, then photo.xmp when it is this
    photo's alone), else the name a new one gets (compatible naming when that setting is on)."""
    a, b = sidecar_candidates(photo_path)
    if os.path.isfile(a):
        return a
    if os.path.isfile(b) and _compat_owner(photo_path):
        return b
    return b if compat_naming() else a


def _prepare(photo_path: str) -> str:
    """Back the sidecar up before the viewer's first change, creating an empty
    sidecar if the photo has none. Returns the sidecar path."""
    side = sidecar_path(photo_path)
    backup = _backup_path(photo_path)
    created = Path(str(backup) + ".created")
    if not os.path.exists(side) and not backup.exists() and not created.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        created.write_text("The viewer created this sidecar; there was none before.\n")  # so revert deletes it
    ensure_backup(side)
    if not os.path.exists(side):
        with open(side, "x", encoding="utf-8") as f:
            f.write(SKELETON)
    return side


# --------------------------------------------------------------------------
# What gets written. This mirrors digiKam 9.1 (DMetadata::setItemRating,
# setItemPickLabel, setItemColorLabel, setItemTitles, setItemComments,
# setItemTagsPath and setACDSeeTagsPath, with digiKam's default namespace
# list): every namespace digiKam writes is written, with the same values.
# --------------------------------------------------------------------------

# Metadata template fields (digiKam's Template): property name in ImageCopyright -> XMP tag,
# in-file tag(s). dc:creator is a list, dc:rights a language alternative.
TEMPLATE_FIELDS = {
    "creator": ("XMP-dc:Creator", ["IFD0:Artist", "IPTC:By-line", "XMP-tiff:Artist"]),
    "copyrightNotice": ("XMP-dc:Rights", ["IFD0:Copyright", "IPTC:CopyrightNotice", "XMP-tiff:Copyright"]),
    "rightsUsageTerms": ("XMP-xmpRights:UsageTerms", []),
    "creatorJobTitle": ("XMP-photoshop:AuthorsPosition", ["IPTC:By-lineTitle"]),
    "provider": ("XMP-photoshop:Credit", ["IPTC:Credit"]),
    "source": ("XMP-photoshop:Source", ["IPTC:Source"]),
    "instructions": ("XMP-photoshop:Instructions", ["IPTC:SpecialInstructions"]),
    "creatorContactInfo.address": ("XMP-iptcCore:CreatorAddress", []),
    "creatorContactInfo.city": ("XMP-iptcCore:CreatorCity", []),
    "creatorContactInfo.provinceState": ("XMP-iptcCore:CreatorRegion", []),
    "creatorContactInfo.postalCode": ("XMP-iptcCore:CreatorPostalCode", []),
    "creatorContactInfo.country": ("XMP-iptcCore:CreatorCountry", []),
    "creatorContactInfo.phone": ("XMP-iptcCore:CreatorWorkTelephone", []),
    "creatorContactInfo.email": ("XMP-iptcCore:CreatorWorkEmail", []),
    "creatorContactInfo.webUrl": ("XMP-iptcCore:CreatorWorkURL", []),
}
COLOR_TEXT = ["NoColor", "Red", "Orange", "Yellow", "Green", "Blue", "Purple", "Gray", "Black", "White"]
MS_RATING = [0, 1, 25, 50, 75, 99]
# Formats digiKam (Exiv2) can write metadata into; everything else is sidecar-only there too.
INFILE_EXT = {".jpg", ".jpeg", ".jpe", ".png", ".tif", ".tiff", ".webp"}


def acdsee_categories(tag_paths: list) -> str:
    """Port of DMetadata::setACDSeeTagsPath."""
    cat = '<Category Assigned="%d">'
    xml = []
    for path in tag_paths:
        parts = path.split("/")
        current = 0
        for i, name in enumerate(parts):
            try:
                idx = xml.index(cat % 0 + name)
            except ValueError:
                try:
                    idx = xml.index(cat % 1 + name)
                except ValueError:
                    idx = -1
            entry = cat % (1 if i == len(parts) - 1 else 0) + name
            if idx == -1:
                if i == 0:
                    xml += [entry, "</Category>"]
                    current = len(xml) - 1
                else:
                    xml.insert(current, entry)
                    xml.insert(current + 1, "</Category>")
                    current += 1
            else:
                if i == len(parts) - 1:
                    xml[idx] = entry
                current = idx + 1
    return "<Categories>" + "".join(xml) + "</Categories>" if xml else ""


def _set(tag: str, value) -> list:
    """One exiftool assignment; a list value replaces the whole list; '' or [] deletes the tag."""
    if isinstance(value, (list, tuple)):
        return [f"-{tag}="] + [f"-{tag}={v}" for v in value] if value else [f"-{tag}="]
    return [f"-{tag}={value}"]


def _dedupe(items: list) -> list:
    return list(dict.fromkeys(i for i in items if i))


def write_args(ch: dict, tag_paths, in_file: bool) -> list:
    """exiftool arguments for the changed components. `tag_paths` is the photo's
    complete tag list after the change (digiKam always writes the full list),
    or None when tags are not being changed. `in_file` adds the EXIF, IPTC and
    JPEG-comment fields digiKam writes into image files."""
    a = []
    if "rating" in ch:
        r = max(0, min(5, int(ch["rating"])))
        a += _set("XMP-xmp:Rating", r) + _set("XMP-acdsee:Rating", r) + _set("XMP-microsoft:RatingPercent", MS_RATING[r])
        if in_file:
            a += _set("IFD0:Rating", r) + _set("IFD0:RatingPercent", MS_RATING[r])
    if "pick" in ch:
        p = int(ch["pick"])
        a += _set("XMP-digiKam:PickLabel", p if p > 0 else "")
        a += _set("XMP-xmpDM:Pick", {3: 1, 1: -1}.get(p, ""))
        a += _set("XMP-xmpDM:Good", {3: "True", 1: "False"}.get(p, ""))
    if "color" in ch:
        c = int(ch["color"])
        a += _set("XMP-digiKam:ColorLabel", c) + _set("XMP-xmp:Label", COLOR_TEXT[c]) + _set("XMP-photoshop:Urgency", c)
    if "title" in ch:
        t = ch["title"] or ""
        a += _set("XMP-dc:Title-x-default", t) + _set("XMP-acdsee:Caption", t)  # x-default only: other languages stay
        if in_file:
            a += _set("IPTC:ObjectName", t[:64] if t.isprintable() else "")
            a += _set("IFD0:XPTitle", "")  # digiKam removes it unless it writes through exiftool
    if "caption" in ch:
        c = ch["caption"] or ""
        a += _set("XMP-digiKam:CaptionsAuthorNames", "")
        a += _set("XMP-digiKam:CaptionsDateTimeStamps", ch.get("caption_date", "") if c else "")
        # the x-default entry only: a caption in another language stays, as digiKam keeps it
        a += (_set("XMP-dc:Description-x-default", c) + _set("XMP-exif:UserComment-x-default", c) + _set("XMP-tiff:ImageDescription-x-default", c)
              + _set("XMP-acdsee:Notes", c) + _set("XMP-acdsee:Author", ""))
        if in_file:
            a += (_set("File:Comment", c) + _set("IFD0:ImageDescription", c) + _set("ExifIFD:UserComment", c)
                  + _set("IFD0:XPComment", "") + _set("IPTC:Caption-Abstract", c[:2000]))
    if "taken" in ch:  # MetaEngine::setImageDateTime(dt, setDateTimeDigitized=False), as MetadataHub calls it
        t = ch["taken"]  # "YYYY-MM-DDTHH:MM:SS"
        x = t.replace("-", ":", 2).replace("T", " ")  # exiftool's form
        a += (_set("XMP-exif:DateTimeOriginal", t) + _set("XMP-photoshop:DateCreated", t) + _set("XMP-tiff:DateTime", t)
              + _set("XMP-xmp:CreateDate", t) + _set("XMP-xmp:ModifyDate", t) + _set("XMP-xmp:MetadataDate", t))
        # (digiKam's Exiv2 also writes Xmp.video dates; exiftool has no such namespace, and the dates above carry it)
        if in_file:
            a += (_set("IFD0:ModifyDate", x) + _set("ExifIFD:DateTimeOriginal", x)
                  + _set("IPTC:DateCreated", x[:10]) + _set("IPTC:TimeCreated", x[11:19] + "+00:00"))
    if "gps" in ch:  # MetaEngine::setGPSInfo / removeGPSInfo
        g = ch["gps"]
        if g:
            lat, lon = float(g["lat"]), float(g["lon"])
            la, lo = f"{abs(lat):.8f} {'N' if lat >= 0 else 'S'}", f"{abs(lon):.8f} {'E' if lon >= 0 else 'W'}"
            # a new position replaces the old one whole (heading, GPS time, precision...), as digiKam's database does
            a += ["-XMP-exif:GPS*="] + (["-GPS:all="] if in_file else [])
            a += _set("XMP-exif:GPSLatitude", la) + _set("XMP-exif:GPSLongitude", lo)
            if g.get("alt") is not None:
                a += _set("XMP-exif:GPSAltitude", abs(float(g["alt"]))) + [f"-XMP-exif:GPSAltitudeRef#={1 if float(g['alt']) < 0 else 0}"]
            else:
                a += _set("XMP-exif:GPSAltitude", "") + _set("XMP-exif:GPSAltitudeRef", "")
            if g.get("accuracy"):  # an approximate position: how far off it may be, in metres (EXIF 2.3's horizontal error)
                a += _set("XMP-exif:GPSHPositioningError", round(float(g["accuracy"]), 1))
            if in_file:
                if g.get("accuracy"):
                    a += _set("GPS:GPSHPositioningError", round(float(g["accuracy"]), 1))
                a += (_set("GPS:GPSLatitude", abs(lat)) + _set("GPS:GPSLatitudeRef", "N" if lat >= 0 else "S")
                      + _set("GPS:GPSLongitude", abs(lon)) + _set("GPS:GPSLongitudeRef", "E" if lon >= 0 else "W")
                      + _set("GPS:GPSVersionID", "2 0 0 0") + _set("GPS:GPSMapDatum", "WGS-84"))
                if g.get("alt") is not None:
                    a += _set("GPS:GPSAltitude", abs(float(g["alt"]))) + [f"-GPS:GPSAltitudeRef#={1 if float(g['alt']) < 0 else 0}"]
                else:
                    a += _set("GPS:GPSAltitude", "") + _set("GPS:GPSAltitudeRef", "")
        else:
            a += ["-XMP-exif:GPS*="]
            if in_file:
                a += ["-GPS:all="]
    if "template" in ch:  # DMetadata::setMetadataTemplate; an empty value removes the field
        for prop, (xmp, infile) in TEMPLATE_FIELDS.items():
            if prop not in ch["template"]:
                continue
            v = (ch["template"][prop] or "").strip()
            a += _set(xmp, [v] if (v and prop == "creator") else v)
            if in_file:
                for t in infile:
                    a += _set(t, v)
    if tag_paths is not None:
        paths = _dedupe(tag_paths)
        leaves = _dedupe([p.rsplit("/", 1)[-1] for p in paths])
        bars = [p.replace("/", "|") for p in paths]
        a += (_set("XMP-digiKam:TagsList", paths) + _set("XMP-microsoft:LastKeywordXMP", paths)
              + _set("XMP-lr:HierarchicalSubject", bars) + _set("XMP-mediapro:CatalogSets", bars)
              + _set("XMP-acdsee:Categories", acdsee_categories(paths)) + _set("XMP-dc:Subject", leaves))
        if in_file:
            a += _set("IPTC:Keywords", [k[:64] for k in leaves]) + _set("IFD0:XPKeywords", "")
    if in_file and any(x.startswith("-IPTC:") for x in a):
        a.append("-IPTC:CodedCharacterSet=UTF8")
    return a


def digikam_tag_paths(image_ids: list) -> dict:
    """{image id: [tag paths]} straight from digiKam's database (read-only):
    every assigned tag digiKam itself would write, i.e. all but its internal ones.
    From the viewer's copy of the library when digiKam's database cannot be read."""
    from app.data import index
    if not dkdb.sync_enabled() or index.offline["since"]:
        return index.mirror_tag_paths(image_ids)
    try:
        src = dkdb.connect(write=False)
    except index.SOURCE_ERRORS:
        return index.mirror_tag_paths(image_ids)
    try:
        tags = dkdb.tag_tree(src)
        internal = {r[0] for r in src.execute("SELECT tagid FROM TagProperties WHERE property = 'internalTag'")}
        out = {}
        for i in range(0, len(image_ids), 500):
            chunk = [int(x) for x in image_ids[i:i + 500]]
            for iid, tid in src.execute(f"SELECT imageid, tagid FROM ImageTags WHERE imageid IN ({','.join(map(str, chunk))})"):
                if tid in tags and tid not in internal:
                    out.setdefault(iid, []).append(dkdb.tag_path(tags, tid))
        return {iid: out.get(iid, []) for iid in (int(x) for x in image_ids)}
    finally:
        src.close()


def _cstr(arg: str) -> str:
    """exiftool arg-file line that survives newlines and backslashes."""
    return "#[CSTR]" + arg.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def run_exiftool(jobs: list) -> list:
    """jobs: [(target file, [args])]. One exiftool process handles them all, each
    target backed up first. Returns the targets that failed (or were skipped because
    they could not be backed up)."""
    if not jobs:
        return []
    with _lock:
        return _run_exiftool(jobs)


def _run_exiftool(jobs: list) -> list:
    lines, skipped = [], []
    for target, args in jobs:
        try:
            ensure_backup(target)  # nothing is rewritten without a copy of what it was
        except OSError:
            skipped.append(target)
            continue
        lines += [_cstr(a) for a in args] + [_cstr(target), "-execute"]
    jobs = [j for j in jobs if j[0] not in skipped]
    if not jobs:
        return skipped
    with tempfile.NamedTemporaryFile("w", suffix=".args", dir=config.DATA_DIR, delete=False, encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        argfile = f.name
    from app.photos import pixelguard
    # image files (not sidecars) get their image data hashed before and after: a write that changed
    # the picture itself is undone (pixelguard)
    errfile = argfile + ".errors"
    with pixelguard.Guard([t for t, _ in jobs]) as guard:
        try:
            res = subprocess.run([*exiftool_cmd(), "-@", argfile, "-common_args", "-overwrite_original", "-m", "-q",
                                  "-charset", "utf8", "-charset", "iptc=utf8", "-efile", errfile],  # -efile: the files that gave errors
                                 capture_output=True, text=True, timeout=3600)
        finally:
            os.unlink(argfile)
        damaged = guard.verify()
    named = set()
    try:
        with open(errfile, encoding="utf-8", errors="replace") as f:
            named = {line.strip() for line in f if line.strip()}
    except OSError:
        pass
    finally:
        try:
            os.unlink(errfile)
        except OSError:
            pass
    if damaged:
        skipped += damaged
        jobs = [j for j in jobs if j[0] not in damaged]
    for target, _ in jobs:
        if target.endswith(".xmp"):
            _keep_exiv2_namespace(target)
    if res.returncode == 0:
        return skipped
    err = res.stderr or ""
    failed = [t for t, _ in jobs if t in named or os.path.abspath(t) in named] or [t for t, _ in jobs if t in err]
    return skipped + (failed or [t for t, _ in jobs])


def _keep_exiv2_namespace(sidecar: str):
    """exiftool "corrects" the MicrosoftPhoto namespace URI by dropping its trailing
    slash, but Exiv2 (and so digiKam) knows that namespace with the slash. Put it back
    so digiKam keeps reading MicrosoftPhoto fields from sidecars the viewer wrote."""
    try:
        with open(sidecar, encoding="utf-8") as f:
            text = f.read()
        fixed = text.replace("xmlns:MicrosoftPhoto='http://ns.microsoft.com/photo/1.0'",
                             "xmlns:MicrosoftPhoto='http://ns.microsoft.com/photo/1.0/'")
        if fixed != text:
            tmp = sidecar + ".viewer-tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(fixed)
            shutil.copystat(sidecar, tmp)
            os.replace(tmp, sidecar)
    except (OSError, UnicodeError):
        pass


def writes_files() -> bool:
    """User setting: also write into the image files, like digiKam's 'sidecar and file' mode."""
    return appstate.get_setting("write_files", "0") == "1" and not appstate.read_only()


def can_write_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in INFILE_EXT and os.access(path, os.W_OK)


def _merge(old: dict, ch: dict) -> dict:
    out = dict(old)
    for k in ("rating", "pick", "color", "title", "caption", "caption_date", "taken", "gps"):
        if k in ch:
            out[k] = ch[k]
    if "template" in ch:
        out["template"] = {**out.get("template", {}), **ch["template"]}
    add, rem = set(out.get("add_tags", [])), set(out.get("remove_tags", []))
    for t in ch.get("add_tags", []):
        add.add(t)
        rem.discard(t)
    for t in ch.get("remove_tags", []):
        rem.add(t)
        add.discard(t)
    out["add_tags"], out["remove_tags"] = sorted(add), sorted(rem)
    return out


def effective_tags(db_paths: list, edit: dict) -> list:
    rem = set(edit.get("remove_tags", []))
    return [t for t in db_paths if t not in rem] + [t for t in edit.get("add_tags", []) if t not in db_paths]


def apply(photos: list, changes: dict, per_photo: dict = None) -> dict:
    """photos: [{id, path}]. Writes the changes to each photo's sidecar and, if
    the user turned that on, into the image file as well. per_photo: {id: changes}
    overriding `changes` for that photo (used for date shifts)."""
    if not available():
        raise RuntimeError("Editing is not available (exiftool not found or EDITING=0)")
    changes = dict(changes)
    per_photo = dict(per_photo or {})
    shift = changes.pop("shift", None)
    if "caption" in changes:
        changes["caption_date"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    in_files = writes_files()
    ok, failed = [], []
    before = _current_values([p["id"] for p in photos])  # for the change log: what each field was
    snaps = {}
    with _lock:
        old = pending()
        if shift:  # each photo's own date moved, from its latest value (a pending one, else digiKam's), under the lock
            from datetime import datetime, timedelta
            fresh = db_state([p["id"] for p in photos if "taken" not in old.get(p["id"], {})])
            for p in photos:
                base = old.get(p["id"], {}).get("taken") or (fresh.get(p["id"]) or {}).get("taken")
                if not base:
                    continue  # no date to shift: left out
                try:
                    per_photo[p["id"]] = {"taken": (datetime.fromisoformat(base[:19]) + timedelta(seconds=int(shift))).strftime("%Y-%m-%dT%H:%M:%S")}
                except ValueError:
                    continue
            photos = [p for p in photos if p["id"] in per_photo]
            if not photos:
                return {"edited": 0, "failed": [], "files": in_files}
        mine = {p["id"]: {**changes, **per_photo.get(p["id"], {})} for p in photos}
        merged = {pid: _merge(old.get(pid, {}), ch) for pid, ch in mine.items()}
        tags_changed = bool(changes.get("add_tags") or changes.get("remove_tags"))
        db_tags = digikam_tag_paths([p["id"] for p in photos]) if tags_changed else {}
        jobs, owner = [], {}
        for p in photos:
            try:
                if not os.path.isfile(p["path"]):
                    raise FileNotFoundError(p["path"])
                side = _prepare(p["path"])
            except OSError as e:
                failed.append({"id": p["id"], "error": str(e)})
                continue
            paths = effective_tags(db_tags.get(p["id"], []), merged[p["id"]]) if tags_changed else None
            from app.photos import history
            snaps[p["id"]] = history.snapshot(p["id"], side)
            jobs.append((side, write_args(mine[p["id"]], paths, False)))
            owner[side] = p["id"]
            if in_files and can_write_file(p["path"]):
                jobs.append((p["path"], write_args(mine[p["id"]], paths, True)))
                owner[p["path"]] = p["id"]
        bad_targets = set(run_exiftool(jobs))
        # the sidecar is the copy digiKam reads; when only the image file could not be written, the edit stands
        # (and is reported), instead of leaving a changed sidecar behind an edit called failed
        bad = {owner[t] for t in bad_targets if t.endswith(".xmp")}
        file_bad = [owner[t] for t in bad_targets if not t.endswith(".xmp") and owner[t] not in bad]
        failed += [{"id": i, "error": "exiftool could not write this file"} for i in bad]
        failed += [{"id": i, "error": "saved in the sidecar, but the image file could not be written", "sidecar": True} for i in file_bad]
        ok = [i for i in dict.fromkeys(owner.values()) if i not in bad]
        if ok:
            save_pending([(pid, merged[pid]) for pid in ok])
            _recheck(ok)
    _log_edits(ok, photos, mine, before, snaps, in_files)
    return {"edited": len(ok), "failed": failed, "files": in_files}


def _current_values(ids: list) -> dict:
    """{photo: {rating, title, caption, taken, gps, pick, color, tags}} as the viewer shows them now."""
    from app.data import index
    out = {}
    if not ids or not config.INDEX_DB.exists():
        return out
    con = index.open_index()
    try:
        for i in range(0, len(ids), 500):
            chunk = ",".join(str(int(x)) for x in ids[i:i + 500])
            for pid, rating, title, caption, taken, lat, lon, alt, pick, color in con.execute(
                    f"SELECT id, rating, title, caption, taken, lat, lon, alt, pick, color FROM photos WHERE id IN ({chunk})"):
                out[pid] = {"rating": max(rating or 0, 0), "title": title or "", "caption": caption or "", "taken": taken,
                            "gps": {"lat": lat, "lon": lon, "alt": alt} if lat is not None else None, "pick": pick, "color": color, "tags": []}
            for pid, full in con.execute(f"SELECT pt.photo_id, t.full FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.photo_id IN ({chunk})"):
                if pid in out:
                    out[pid]["tags"].append(full)
    finally:
        con.close()
    return out


def _log_edits(ok: list, photos: list, mine: dict, before: dict, snaps: dict, in_files: bool):
    from app.photos import history
    paths = {p["id"]: p["path"] for p in photos}
    rows = []
    for pid in ok:
        ch, b = mine[pid], before.get(pid, {})
        undo = {}
        for f, v in ch.items():
            if f in ("rating", "pick", "color", "title", "caption", "taken", "gps") and f in b:
                undo[f] = b[f]
        if ch.get("add_tags"):
            undo["remove_tags"] = [t for t in ch["add_tags"] if t not in b.get("tags", [])]
        if ch.get("remove_tags"):
            undo["add_tags"] = [t for t in ch["remove_tags"] if t in b.get("tags", [])]
        rows.append({"photo_id": pid, "action": "edit", "summary": history.describe_edit(b, ch),
                     "details": {"before": {k: b.get(k) for k in ch if k in b}, "after": {k: v for k, v in ch.items() if k != "caption_date"},
                                 "written_to": "sidecar and file" if in_files else "sidecar"},
                     "undo": undo or None, "sidecar": snaps.get(pid), "path": paths.get(pid)})
    history.record(rows)


def _recheck(ids: list):
    """The metadata consistency results of these photos are out of date: compare them again."""
    try:
        from app.photos import consistency
        consistency.recheck(ids)
    except Exception:  # never fail an edit over the report
        pass


def rewrite_tags(photos: list) -> list:
    """Write each photo's complete, current tag list (from digiKam's database plus
    pending viewer edits) to its sidecar, and to the file if enabled. Used after a
    tag was renamed, moved or deleted. Returns the ids that failed."""
    in_files = writes_files()
    with _lock:  # pending edits and digiKam's tags read and written as one step: an edit made meanwhile is not lost
        old = pending()
        db_tags = digikam_tag_paths([p["id"] for p in photos])
        jobs, owner = [], {}
        for p in photos:
            side = sidecar_path(p["path"])
            paths = effective_tags(db_tags.get(p["id"], []), old.get(p["id"], {}))
            if os.path.isfile(side):
                jobs.append((side, write_args({}, paths, False)))
                owner[side] = p["id"]
            if in_files and os.path.isfile(p["path"]) and can_write_file(p["path"]):
                jobs.append((p["path"], write_args({}, paths, True)))
                owner[p["path"]] = p["id"]
        return [owner[t] for t in run_exiftool(jobs)]


def db_state(image_ids: list) -> dict:
    """{id: {rating, pick, color, title, caption, tags, taken, gps}} as digiKam's database holds them (read-only): what
    a sidecar and file say once the viewer's own edits are taken back. gps is {lat, lon, alt} or None."""
    from app.photos import consistency
    vals = consistency.db_values([int(i) for i in image_ids])
    for v in vals.values():
        v["gps"] = None
    try:
        src = dkdb.connect(write=False)
    except Exception:
        return vals
    try:  # the position at full precision (db_values rounds it for comparing)
        for i in range(0, len(image_ids), 500):
            chunk = ",".join(str(int(x)) for x in image_ids[i:i + 500])
            for pid, lat, lon, alt in src.execute(f"SELECT imageid, latitudeNumber, longitudeNumber, altitude FROM ImagePositions WHERE imageid IN ({chunk})"):
                if pid in vals and lat is not None and lon is not None:
                    vals[pid]["gps"] = {"lat": float(lat), "lon": float(lon), "alt": float(alt) if alt is not None else None}
    finally:
        src.close()
    return vals


def _db_copyright(pid: int) -> dict:
    """{property: value} of the photo's ImageCopyright rows in digiKam's database."""
    try:
        src = dkdb.connect(write=False)
    except Exception:
        return {}
    try:
        return {p: v or "" for p, v in src.execute("SELECT property, value FROM ImageCopyright WHERE imageid = ?", (int(pid),))}
    finally:
        src.close()


def revert(photos: list) -> dict:
    """Take back the viewer's edits that digiKam's database does not have yet: the sidecar (and the image file, when
    the viewer wrote into it) get digiKam's values again for every field the edit changed, and the face regions and
    tag list are rewritten from the database, so face changes made since (which are in the database) stay."""
    done, failed = [], []
    from app.faces import facemgr
    with _lock:
        edits = pending()
        db = db_state([p["id"] for p in photos])
        in_files = writes_files()
        for p in photos:
            pid, side = p["id"], sidecar_path(p["path"])
            backup = _backup_path(p["path"])
            created = Path(str(backup) + ".created")
            e = edits.get(pid)
            if e is None and not backup.exists() and not created.exists():
                continue
            d = db.get(pid, {})
            ch = {k: d.get(k) for k in ("rating", "pick", "color", "title", "caption") if k in (e or {})}
            if e and "taken" in e and d.get("taken"):
                ch["taken"] = d["taken"]
            if e and "gps" in e:
                ch["gps"] = d.get("gps")
            if e and e.get("template"):
                ch["template"] = {k: _db_copyright(pid).get(k, "") for k in e["template"]}
            try:
                jobs = []
                if created.exists() and not backup.exists():
                    if os.path.exists(side):
                        os.remove(side)  # the viewer made it; digiKam never had one
                    created.unlink()
                elif ch and os.path.exists(side):
                    jobs.append((side, write_args(ch, None, False)))
                if ch and in_files and os.path.isfile(p["path"]) and can_write_file(p["path"]):
                    jobs.append((p["path"], write_args(ch, None, True)))
                if jobs and run_exiftool(jobs):
                    raise OSError("exiftool could not write the earlier values back")
                if jobs:  # the sidecar is as digiKam had it again; otherwise the pre-viewer copy is kept
                    backup.unlink(missing_ok=True)
                done.append(pid)
            except OSError as e_:  # the edit stays remembered, so it can be retried
                failed.append({"id": pid, "error": str(e_)})
        forget(done)
    # tags and faces: the database's, now that the edits are forgotten (one exiftool run for all)
    keep = [pid for pid in done if os.path.exists(sidecar_path(next(p["path"] for p in photos if p["id"] == pid)))]
    if keep:
        facemgr.write_regions_many(keep)
    from app.photos import history
    paths = {p["id"]: p["path"] for p in photos}
    history.record([{"photo_id": pid, "action": "revert", "summary": "Viewer edits undone: the sidecar (and file) say what digiKam's database says again",
                     "path": paths.get(pid)} for pid in done])
    return {"reverted": len(done), "failed": failed}
