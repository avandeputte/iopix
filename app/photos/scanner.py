"""The file scanner: what digiKam's collection scanner does, so the library keeps working without digiKam.

Two jobs, as in digiKam (CollectionScanner, ItemScanner, digiKam 9.1):

complete(ids)
    Photos the viewer itself registered (Upload, Import) get their full rows at once: dimensions and format,
    camera and lens (ImageMetadata), position (ImagePositions), title, caption and headline (ImageComments),
    copyright and contact (ImageCopyright), keywords, pick and color labels (ImageTags), face regions
    (ImageTagProperties), video details (VideoMetadata), digiKam's unique hash and the file's modification
    date. A file identical to one already in the library (same hash and size) takes that photo's attributes,
    as digiKam's scanFromIdenticalFile does. This always runs: it is part of adding a photo, not of watching.

scan()
    The setting "File scanner" (off by default): the album folders are walked for what changed outside the
    viewer (Finder, a sync tool, a phone backup). New folders get Albums rows, new files are added as above,
    files whose time or size changed get digiKam's modified-file scan (dimensions, format, hash, time; tags and
    ratings are left alone, as digiKam does unless "rescan if modified" is set), and files that vanished get
    status 3 with no album (CoreDB::removeItems), so digiKam would treat them exactly the same. A root that is
    missing or looks unmounted, or a pass that would remove most of a folder tree, is skipped and reported
    rather than applied. Runs every few minutes while on, and on request (Maintenance > File scanner).

The unique hash is digiKam's own (DImg::createUniqueHashV3 for hash version 3, V2 for 2), checked against
2,000 photos digiKam scanned: identical.
"""
import datetime as _dt
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

from app import config
from app.digikam import dkdb
from app.photos import edits, folders
from app.digikam.dbsync import COLOR_TAGS, PICK_TAGS, SyncError, gps_text

log = logging.getLogger("app.photos.scanner")

SKIP_DIRS = {"@eaDir", "#recycle", "#snapshot", "lost+found", "$RECYCLE.BIN", "System Volume Information"}
COLOR_MODEL = {"JPG": 5}  # DImg::ColorModel: YCbCr for JPEG, RGB (1) otherwise
INTERNAL = "_Digikam_Internal_Tags_/"
TAG_KEYS = ("TagsList", "LastKeywordXMP", "HierarchicalSubject", "CatalogSets")
FLAT_KEYS = ("Subject", "Keywords")
READ_TAGS = [
    "Make", "Model", "LensModel", "Lens", "LensID", "FNumber", "ApertureValue", "FocalLength", "FocalLengthIn35mmFormat",
    "ExposureTime", "ExposureProgram", "ExposureMode", "ISO", "Flash", "WhiteBalance", "ColorTemperature", "MeteringMode",
    "SubjectDistance", "SubjectDistanceRange", "DateTimeOriginal", "CreationDate", "CreateDate", "MediaCreateDate", "TrackCreateDate",
    "Orientation", "ImageSize", "ImageWidth", "ImageHeight", "BitsPerSample", "GPSLatitude", "GPSLongitude", "GPSAltitude", "GPSHPositioningError",
    "Rating", "XMP-dc:Title", "XMP-dc:Description", "ObjectName", "Caption-Abstract", "ImageDescription", "Headline",
    *TAG_KEYS, *FLAT_KEYS, "PickLabel", "ColorLabel", "RegionInfo", "RegionInfoMP",
    "Duration", "VideoFrameRate", "CompressorID", "AudioFormat", "AudioChannels", "AudioSampleRate",
    *[v[0] for v in edits.TEMPLATE_FIELDS.values()],
]
progress = {"running": False, "started": None, "finished": None, "added": 0, "modified": 0, "removed": 0, "folders": 0,
            "skipped": [], "error": None, "last": None}
_lock = threading.Lock()
_wake = threading.Event()


# ---- settings
def enabled() -> bool:
    from app.data import state
    return state.get_setting("file_scanner", "0") == "1"


def interval() -> int:
    from app.data import state
    try:
        return max(1, min(1440, int(state.get_setting("scan_interval", "15"))))
    except ValueError:
        return 15


# ---- digiKam's unique hash
def hash_version(con=None) -> int:
    own = con is None
    con = con or dkdb.connect()
    try:
        r = con.execute("SELECT value FROM Settings WHERE keyword = 'uniqueHashVersion'").fetchone()
        return int(r[0]) if r and str(r[0]).isdigit() else 1
    except Exception:
        return 3  # the viewer's own library: digiKam 9's current version
    finally:
        if own:
            con.close()


def unique_hash(path: str, version: int = 3):
    """DImg::createUniqueHashV3 (the first 100 kB and five 25 kB blocks spread over the rest) or V2 (the first and
    last 100 kB). Version 1 (Exif-based) is not reproduced: None, and digiKam computes it on its next scan."""
    try:
        size = os.path.getsize(path)
        h = hashlib.md5()
        with open(path, "rb") as f:
            if version == 2:
                n = min(size, 100 * 1024)
                if n:
                    h.update(f.read(n))
                    f.seek(size - n)
                    h.update(f.read(n))
            elif version >= 3:
                fsize = min(size, 100 * 1024)
                bsize = size - fsize
                block = min(bsize, 25 * 1024)
                step = (bsize - block) // 5 + 1
                if fsize:
                    for sp in range(6):
                        rsize = fsize if sp == 0 else block
                        rstep = 0 if sp == 0 else step * sp + fsize
                        f.seek(min(rstep, size - rsize))
                        b = f.read(rsize)
                        if b:
                            h.update(b)
                        if f.tell() >= size:
                            break
            else:
                return None
        return h.hexdigest()
    except OSError:
        return None


# ---- reading metadata
def _exif(paths: list) -> dict:
    """{path: exiftool's numeric values}, one exiftool run for the lot."""
    if not paths:
        return {}
    with tempfile.NamedTemporaryFile("w", suffix=".args", dir=config.DATA_DIR, delete=False, encoding="utf-8") as f:
        f.write("\n".join(paths) + "\n")
        argfile = f.name
    try:
        res = subprocess.run([*edits.exiftool_cmd(), "-j", "-n", "-m", "-q", "-struct", "-charset", "filename=utf8", "-api", "LargeFileSupport=1",
                              *["-" + t for t in READ_TAGS], "-@", argfile], capture_output=True, text=True, timeout=3600)
        rows = json.loads(res.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        # never an empty answer: a caller writing rows from {} would wipe the photo's tags, faces, caption and position
        raise SyncError(f"exiftool could not read {len(paths)} file(s): {e}") from e
    finally:
        os.unlink(argfile)
    if paths and not rows:
        raise SyncError(f"exiftool gave nothing back for {len(paths)} file(s)" + (f": {res.stderr.strip()[:200]}" if res.stderr else ""))
    return {r.get("SourceFile"): r for r in rows}


SIDECAR_WINS = {"Rating", "Title", "Description", "Headline", "PickLabel", "ColorLabel", "RegionInfo", "GPSLatitude", "GPSLongitude",
                "GPSAltitude", "DateTimeOriginal", *TAG_KEYS, *FLAT_KEYS, *[v[0].split(":")[1] for v in edits.TEMPLATE_FIELDS.values()]}


def read(paths: list) -> dict:
    """{path: merged metadata}: the file's own, with the sidecar's XMP values winning where it has them
    (digiKam's default: read from the sidecar as well as the file)."""
    files = _exif(paths)
    sides = _exif([edits.sidecar_path(p) for p in paths if os.path.isfile(edits.sidecar_path(p))])
    out = {}
    for p in paths:
        if files.get(p) is None:  # exiftool said nothing about this file: no record, so no caller writes rows from nothing
            continue
        d = dict(files[p])
        s = sides.get(edits.sidecar_path(p)) or {}
        for k, v in s.items():
            if k in SIDECAR_WINS and v not in (None, "", []):
                d[k] = v
        out[p] = d
    return out


def _date(v):
    """exiftool's '2026:05:13 15:19:46+02:00' as digiKam stores it: '2026-05-13T15:19:46.000' (local time as written)."""
    if not v or not isinstance(v, str) or len(v) < 19 or not v[:4].isdigit() or v.startswith("0000"):
        return None
    return v[:4] + "-" + v[5:7] + "-" + v[8:10] + "T" + v[11:19] + ".000"


def _num(v, cast=float):
    try:
        return cast(v) if v not in (None, "", "undef") else None
    except (TypeError, ValueError):
        return None


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def _size(d: dict):
    s = d.get("ImageSize")
    if isinstance(s, str) and " " in s.strip():
        w, h = s.split()[:2]
    elif isinstance(s, str) and "x" in s:
        w, h = s.split("x")[:2]
    else:
        w, h = d.get("ImageWidth"), d.get("ImageHeight")
    return _num(w, int) or 0, _num(h, int) or 0


def _tag_paths(d: dict) -> list:
    """Keyword paths in the order digiKam prefers its sources (DMetadata::getItemTagsPath)."""
    for k in TAG_KEYS:
        v = d.get(k)
        if v not in (None, "", []):
            return [str(x).replace("|", "/") for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
    for k in FLAT_KEYS:
        v = d.get(k)
        if v not in (None, "", []):
            return [str(x) for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
    return []


def _regions(d: dict) -> list:
    """[(name, (x, y, w, h) in the file's own pixel grid, (W, H)) or (name, None, None) for a name-only region], as
    digiKam reads them (DMetadata::getItemFacesMap): the MWG region list, then Microsoft's MP regions for names the MWG
    list does not have (name-only MP regions are people on the photo without a box). Not a face, or the whole frame:
    left out. A region list without its image size is placed on the file's own size. "/" in a name becomes "\\"."""
    fw, fh = _size(d)
    out, names = [], set()

    def name_of(v):
        return str(v or "").strip().replace("/", "\\")

    def add(name, cx, cy, w, h, W, H):
        if None in (cx, cy, w, h) or not W or not H or w <= 0 or h <= 0:
            return
        if w >= 0.99 and h >= 0.99:  # the whole picture: not a face (digiKam ignores it too)
            return
        out.append((name, (round((cx - w / 2) * W), round((cy - h / 2) * H), round(w * W), round(h * H)), (int(W), int(H))))
        if name:
            names.add(name.lower())
    ri = d.get("RegionInfo")
    if isinstance(ri, dict):
        dims = ri.get("AppliedToDimensions") or {}
        W, H = _num(dims.get("W")) or fw, _num(dims.get("H")) or fh
        for r in ri.get("RegionList") or []:
            if not isinstance(r, dict) or (r.get("Type") not in (None, "Face")):
                continue
            a = r.get("Area") or {}
            add(name_of(r.get("Name")), *(_num(a.get(k)) for k in ("X", "Y", "W", "H")), W, H)
    mp = d.get("RegionInfoMP")
    if isinstance(mp, dict):
        for r in mp.get("Regions") or []:
            if not isinstance(r, dict):
                continue
            name = name_of(r.get("PersonDisplayName"))
            if not name or name.lower() in names:
                continue
            rect = [_num(x.strip()) for x in str(r.get("Rectangle") or "").split(",")] if r.get("Rectangle") else []
            if len(rect) == 4 and None not in rect:  # MP rectangles are top-left based, as fractions of the picture
                x, y, w, h = rect
                add(name, x + w / 2, y + h / 2, w, h, fw, fh)
            else:
                out.append((name, None, None))
                names.add(name.lower())
    return out


def _upright(rect, file_wh, orientation):
    """A rectangle in the file's pixel grid turned into upright pixels (TagRegion::adjustToOrientation)."""
    from app.faces.facemgr import _unrotate
    inv = {6: 8, 8: 6}.get(orientation or 1, orientation or 1)
    x, y, w, h, _uw, _uh = _unrotate(*rect, file_wh[0], file_wh[1], inv)
    return int(x), int(y), int(w), int(h)


VIDEO_CODECS = {"avc1": "h264", "avc3": "h264", "hvc1": "hevc", "hev1": "hevc", "mp4v": "mpeg4", "jpeg": "mjpeg", "apcn": "prores", "apch": "prores"}
AUDIO_CODECS = {"mp4a": "aac", "sowt": "pcm_s16le", "twos": "pcm_s16be", "lpcm": "pcm_s16le", "ima4": "adpcm_ima_wav", "alac": "alac", "ac-3": "ac3"}


def rows_for(path: str, d: dict, category: int) -> dict:
    """The rows digiKam's NewScan would write for this file (without the image id)."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    fmt = folders.FORMATS.get(ext, ("RAW-" + ext.upper()) if ext in folders.IMAGE_EXT else ext.upper())
    w, h = _size(d)
    # a phone video's own local time (QuickTime Keys:CreationDate, with its offset) before the QuickTime dates, which
    # are UTC: otherwise an iPhone video files hours off, into the wrong day or month
    created = next((_date(d.get(k)) for k in ("DateTimeOriginal", "CreationDate", "CreateDate", "MediaCreateDate", "TrackCreateDate")
                    if _date(d.get(k))), None)  # the first that is a real date (a "0000:00:00" placeholder is skipped)
    if not created:  # checkCreationDateFromMetadata: the file's own time when the metadata has none
        created = _dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%dT%H:%M:%S.000")
    rating = _num(d.get("Rating"), int)
    out = {"info": {"rating": max(-1, min(5, rating)) if rating is not None else -1, "creationDate": created,
                    "digitizationDate": _date(d.get("CreateDate")) or created, "orientation": _num(d.get("Orientation"), int) or 1,
                    "width": w, "height": h, "format": fmt,
                    "colorDepth": (_num(str(d.get("BitsPerSample") or "8").split()[0], int) or 8) if category == 1 else None,
                    "colorModel": COLOR_MODEL.get(fmt, 1) if category == 1 else None}}
    lens = _first(d, "LensModel", "Lens", "LensID")
    meta = {"make": _first(d, "Make"), "model": _first(d, "Model"), "lens": str(lens) if lens is not None else None,
            "aperture": _num(_first(d, "FNumber", "ApertureValue")), "focalLength": _num(d.get("FocalLength")),
            "focalLength35": _num(d.get("FocalLengthIn35mmFormat")), "exposureTime": _num(d.get("ExposureTime")),
            "exposureProgram": _num(d.get("ExposureProgram"), int), "exposureMode": _num(d.get("ExposureMode"), int),
            "sensitivity": _num(d.get("ISO"), int), "flash": _num(d.get("Flash"), int), "whiteBalance": _num(d.get("WhiteBalance"), int),
            "whiteBalanceColorTemperature": _num(d.get("ColorTemperature"), int), "meteringMode": _num(d.get("MeteringMode"), int),
            "subjectDistance": _num(d.get("SubjectDistance")), "subjectDistanceCategory": _num(d.get("SubjectDistanceRange"), int)}
    out["metadata"] = meta if any(v is not None for v in meta.values()) else None
    lat, lon = _num(d.get("GPSLatitude")), _num(d.get("GPSLongitude"))
    out["position"] = {"lat": lat, "lon": lon, "alt": _num(d.get("GPSAltitude")), "accuracy": _num(d.get("GPSHPositioningError"))} \
        if lat is not None and lon is not None and (lat or lon) else None
    out["title"] = _first(d, "Title", "ObjectName")
    out["caption"] = _first(d, "Description", "Caption-Abstract", "ImageDescription")
    out["headline"] = _first(d, "Headline")
    out["copyright"] = {}
    for prop, (xmp, _infile) in edits.TEMPLATE_FIELDS.items():
        v = d.get(xmp.split(":")[1])
        if v not in (None, "", []):
            out["copyright"][prop] = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
    out["tags"] = _tag_paths(d)
    out["pick"] = _num(d.get("PickLabel"), int)
    out["color"] = _num(d.get("ColorLabel"), int)
    out["faces"] = [(n, _upright(r, wh, out["info"]["orientation"]) if r is not None else None) for n, r, wh in _regions(d)] if category == 1 else []
    if category == 2:
        dur = _num(d.get("Duration"))
        rate = _num(d.get("VideoFrameRate"))
        ch = _num(d.get("AudioChannels"), int)
        out["video"] = {"aspectRatio": f"{w / h:.5g}" if w and h else None, "audioBitRate": str(_num(d.get("AudioSampleRate"), int)) if d.get("AudioSampleRate") else None,
                        "audioChannelType": {1: "Mono", 2: "Stereo"}.get(ch, str(ch) if ch else None),
                        "audioCompressor": AUDIO_CODECS.get(str(d.get("AudioFormat") or "").lower(), d.get("AudioFormat")),
                        "duration": str(int(round(dur * 1000))) if dur else None, "frameRate": f"{rate:.4g}" if rate else None,
                        "videoCodec": VIDEO_CODECS.get(str(d.get("CompressorID") or "").lower(), d.get("CompressorID"))}
    return out


# ---- writing
class _Tags:
    """Tag lookups and creation inside one write transaction."""

    def __init__(self, con):
        self.con = con
        self.tree = dkdb.tag_tree(con)
        self.by_path = {dkdb.tag_path(self.tree, t): t for t in self.tree}
        self.persons = {}
        for tid, val in con.execute("SELECT tagid, value FROM TagProperties WHERE property = 'person'"):
            self.persons.setdefault((val or "").strip().lower(), tid)
            self.persons.setdefault(dkdb.tag_path(self.tree, tid).split("/")[-1].lower(), tid)

    def path(self, p: str) -> int:
        p = "/".join(s.strip() for s in p.split("/") if s.strip())
        if not p:
            return None
        if p in self.by_path:
            return self.by_path[p]
        parent, _, leaf = p.rpartition("/")
        pid = self.path(parent) if parent else 0
        tid = self.con.execute("INSERT INTO Tags (pid, name) VALUES (?,?)", (pid, leaf)).lastrowid
        self.by_path[p] = tid
        return tid

    def person_for(self, name: str, keyword_path: str = None) -> int:
        """The person tag for a face named `name`: an existing person of that name; else the photo's own keyword
        ending in that name, made a person (digiKam's commitFaces); else a new person under People."""
        tid = self.persons.get(name.lower())
        if tid:
            return tid
        if keyword_path:
            tid = self.path(keyword_path)
            if tid:
                self.con.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?, 'person', ?)", (tid, name))
                self.persons[name.lower()] = tid
                return tid
        return self.person(name)

    def person(self, name: str) -> int:
        tid = self.persons.get(name.lower())
        if tid:
            return tid
        people = self.by_path.get("People")
        tid = self.path(("People/" if people else "") + name)
        self.con.execute("INSERT INTO TagProperties (tagid, property, value) VALUES (?, 'person', ?)", (tid, name))
        self.persons[name.lower()] = tid
        return tid


def _write_rows(con, iid: int, r: dict, tags: _Tags, special: dict, full: bool):
    """Write the scanned rows of one photo. full: every table (a new file); else only what a modified file updates."""
    i = r["info"]
    if full:
        con.execute("DELETE FROM ImageInformation WHERE imageid = ?", (iid,))
        con.execute("INSERT INTO ImageInformation (imageid, rating, creationDate, digitizationDate, orientation, width, height, format, colorDepth, colorModel) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (iid, i["rating"], i["creationDate"], i["digitizationDate"], i["orientation"], i["width"], i["height"],
                                                      i["format"], i["colorDepth"], i["colorModel"]))
    else:  # ItemScanner::scanItemInformation in ModifiedScan: dimensions, format, depth, colour model
        con.execute("UPDATE ImageInformation SET width = ?, height = ?, format = ?, colorDepth = ?, colorModel = ? WHERE imageid = ?",
                    (i["width"], i["height"], i["format"], i["colorDepth"], i["colorModel"], iid))
    if "video" in r:
        con.execute("DELETE FROM VideoMetadata WHERE imageid = ?", (iid,))
        v = r["video"]
        con.execute("INSERT INTO VideoMetadata (imageid, aspectRatio, audioBitRate, audioChannelType, audioCompressor, duration, frameRate, videoCodec) "
                    "VALUES (?,?,?,?,?,?,?,?)", (iid, v["aspectRatio"], v["audioBitRate"], v["audioChannelType"], v["audioCompressor"], v["duration"], v["frameRate"], v["videoCodec"]))
    if not full:
        return
    con.execute("DELETE FROM ImageMetadata WHERE imageid = ?", (iid,))
    if r["metadata"]:
        cols = list(r["metadata"])
        con.execute(f"INSERT INTO ImageMetadata (imageid, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})", (iid, *r["metadata"].values()))
    con.execute("DELETE FROM ImagePositions WHERE imageid = ?", (iid,))
    if r["position"]:
        p = r["position"]
        con.execute("INSERT INTO ImagePositions (imageid, latitude, latitudeNumber, longitude, longitudeNumber, altitude, accuracy) VALUES (?,?,?,?,?,?,?)",
                    (iid, gps_text(True, p["lat"]), p["lat"], gps_text(False, p["lon"]), p["lon"], p["alt"], p.get("accuracy")))
    con.execute("DELETE FROM ImageComments WHERE imageid = ?", (iid,))
    for ctype, text in ((1, r["caption"]), (2, r["headline"]), (3, r["title"])):  # DatabaseComment: Comment, Headline, Title
        if text:
            con.execute("INSERT INTO ImageComments (imageid, type, language, author, date, comment) VALUES (?,?,?,?,?,?)",
                        (iid, ctype, "x-default", None, None, str(text)))
    con.execute("DELETE FROM ImageCopyright WHERE imageid = ?", (iid,))
    for prop, value in r["copyright"].items():
        con.execute("INSERT INTO ImageCopyright (imageid, property, value, extraValue) VALUES (?,?,?,?)",
                    (iid, prop, value, "x-default" if prop in ("copyrightNotice", "rightsUsageTerms") else None))
    relabel = {tags.by_path.get(INTERNAL + n) for n in (*PICK_TAGS, *COLOR_TAGS)} - {None}
    internal = {tid for p, tid in tags.by_path.items() if p.startswith(INTERNAL)} - relabel
    keep_tags = [t for (t,) in con.execute("SELECT tagid FROM ImageTags WHERE imageid = ?", (iid,)) if t in internal]
    keep_props = con.execute("SELECT tagid, property, value FROM ImageTagProperties WHERE imageid = ? AND property != 'tagRegion'", (iid,)).fetchall()
    con.execute("DELETE FROM ImageTags WHERE imageid = ?", (iid,))
    con.execute("DELETE FROM ImageTagProperties WHERE imageid = ?", (iid,))
    want = set(keep_tags)
    for p in r["tags"]:
        if p.startswith(INTERNAL) or p == "_Digikam_Internal_Tags_":
            continue
        tid = tags.path(p)
        if tid:
            want.add(tid)
    for group, names, value in (("pick", PICK_TAGS, r["pick"]), ("color", COLOR_TAGS, r["color"])):
        if value is not None and 0 <= value < len(names):
            tid = tags.by_path.get(INTERNAL + names[value])
            if tid:
                want.add(tid)
    # the photo's own keywords, for ItemScanner::commitFaces's first choice: a keyword whose last part is the name
    own = {}
    for p in r.get("tags") or []:
        leaf = p.rsplit("/", 1)[-1].strip().lower()
        if leaf and not p.startswith(INTERNAL):
            own.setdefault(leaf, p)
    for name, rect in r["faces"]:
        if name:
            tid = tags.person_for(name, own.get(name.lower()))
            want.add(tid)
            if rect is not None:
                con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (iid, tid, "tagRegion", folders_rect(rect)))
        elif rect is not None and special.get("unknownPerson"):
            # an unnamed face, on digiKam's Unknown tag without assigning that tag (FaceTagsEditor::addManually)
            con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (iid, special["unknownPerson"], "autodetectedFace", folders_rect(rect)))
    con.executemany("INSERT OR IGNORE INTO ImageTags (imageid, tagid) VALUES (?,?)", [(iid, t) for t in want])
    for tid, prop, val in keep_props:
        if not con.execute("SELECT 1 FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = ? AND value = ?", (iid, tid, prop, val)).fetchone():
            con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (iid, tid, prop, val))


def folders_rect(rect) -> str:
    from app.faces.facemgr import rect_xml
    return rect_xml(*rect)


COPY_COLS = {  # digiKam's columns (core/data/database/dbconfig.xml.cmake.in), the image id excluded
    "ImageInformation": "rating, creationDate, digitizationDate, orientation, width, height, format, colorDepth, colorModel",
    "ImageMetadata": "make, model, lens, aperture, focalLength, focalLength35, exposureTime, exposureProgram, exposureMode, sensitivity, flash, "
                     "whiteBalance, whiteBalanceColorTemperature, meteringMode, subjectDistance, subjectDistanceCategory",
    "VideoMetadata": "aspectRatio, audioBitRate, audioChannelType, audioCompressor, duration, frameRate, exposureProgram, videoCodec",
    "ImagePositions": "latitude, latitudeNumber, longitude, longitudeNumber, altitude, orientation, tilt, roll, accuracy, description",
    "ImageComments": "type, language, author, date, comment",
    "ImageCopyright": "property, value, extraValue",
    "ImageTags": "tagid",
    "ImageTagProperties": "tagid, property, value",
    "ImageProperties": "property, value",
}


def _copy_attributes(con, src: int, dst: int):
    """CoreDB::copyImageAttributes: a new file identical to a known one takes over its attributes."""
    for t, cols in COPY_COLS.items():
        con.execute(f"DELETE FROM {t} WHERE imageid = ?", (dst,))
        con.execute(f"INSERT INTO {t} (imageid, {cols}) SELECT ?, {cols} FROM {t} WHERE imageid = ?", (dst, src))


def complete(ids: list, check_running: bool = True) -> dict:
    """Full rows for photos the viewer registered itself (modificationDate NULL). Returns {completed, copied, failed}."""
    from app.digikam import dbsync
    ids = [int(i) for i in ids]
    if not ids:
        return {"completed": 0, "copied": 0, "failed": 0}
    con = dkdb.connect()
    try:
        roots = dkdb.root_paths(con)
        version = hash_version(con)
        q = ",".join(map(str, ids))
        rows = con.execute(f"SELECT i.id, i.category, i.album, a.albumRoot, a.relativePath, i.name FROM Images i JOIN Albums a ON a.id = i.album WHERE i.id IN ({q})").fetchall()
    finally:
        con.close()
    items = []
    for iid, cat, album, root, rel, name in rows:
        if root in roots:
            items.append((iid, cat, album, str(Path(roots[root]) / rel.strip("/") / name)))
    meta = read([p for *_x, p in items])
    hashes = {iid: unique_hash(p, version) for iid, _c, _a, p in items}
    done = copied = failed = 0
    db_path = dkdb.sqlite_path()
    with dbsync.write_transaction(db_path, check_running) as con:
        tags = _Tags(con)
        try:
            from app.faces.facemgr import _special
            special = _special(con)
        except SyncError:
            special = {}
        for iid, cat, album, path in items:
            try:
                uh, size = hashes.get(iid), os.path.getsize(path)
                twin = None
                if uh:
                    r = con.execute("SELECT id, album FROM Images WHERE uniqueHash = ? AND fileSize = ? AND id != ? AND status IN (1, 3) ORDER BY (album = ?) DESC, status, id DESC LIMIT 1",
                                    (uh, size, iid, album)).fetchone()
                    twin = r[0] if r else None
                if twin:
                    _copy_attributes(con, twin, iid)
                    copied += 1
                elif not meta.get(path):
                    raise SyncError("the file could not be read; tried again on the next pass")
                else:
                    _write_rows(con, iid, rows_for(path, meta[path], cat), tags, special, full=True)
                stamp = dkdb.file_stamp(path)
                con.execute("UPDATE Images SET uniqueHash = ?, fileSize = ?, modificationDate = ? WHERE id = ?", (uh, size, stamp[0] if stamp else None, iid))
                done += 1
            except Exception as e:
                failed += 1
                log.warning("scanner: could not complete %s (%s)", path, e)
    return {"completed": done, "copied": copied, "failed": failed}


# ---- the collection scan
def _sweep_temp(path: str):
    try:
        if time.time() - os.path.getmtime(path) > 3600:
            os.unlink(path)
            log.info("scanner: removed a stale temporary file %s", path)
    except OSError:
        pass


_nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731  (APFS/HFS+ list NFD names; digiKam stores NFC)


def _walk(root: Path):
    """{relative folder ('/' or '/a/b'): (every file name there, NFC; the photo and video names)} under root (hidden
    folders, NAS housekeeping folders and the trash left out; symlinked folders followed, as digiKam does). A folder
    that could not be listed is left out and named in `unreadable`: nothing in it may count as gone."""
    out, unreadable = {}, []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True, onerror=lambda e: unreadable.append(getattr(e, "filename", None) or str(e))):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        sub = os.path.relpath(dirpath, root)
        rel = "/" if sub in (".", "") else "/" + sub.replace(os.sep, "/")
        files, names = [], set()
        for n in filenames:
            if n.endswith((".viewer-tmp", "_exiftool_tmp")):  # left behind by a write that died: gone after an hour
                _sweep_temp(os.path.join(dirpath, n))
                continue
            names.add(_nfc(n))  # a file of a kind the viewer does not read (.crw, .jxl, .flv…) is still there
            if n.startswith(".") or "." not in n or n.lower().endswith(".xmp"):
                continue
            try:
                folders._category(n.rsplit(".", 1)[-1].lower())
            except SyncError:
                continue
            files.append(n)
        out[rel] = (names, files)
    out["__unreadable__"] = unreadable
    return out


def _same_time(a, b) -> bool:
    """digiKam's s_modificationDateEquals: within a second."""
    if not a or not b:
        return False
    try:
        return abs(_dt.datetime.fromisoformat(str(a)[:23]).timestamp() - _dt.datetime.fromisoformat(str(b)[:23]).timestamp()) <= 1.0
    except ValueError:
        return False


def scan(check_running: bool = True) -> dict:
    """One pass over every album root. Returns the summary (also kept in progress)."""
    from app.digikam import dbsync
    from app.photos import history, importer
    if not _lock.acquire(blocking=False):
        raise SyncError("A scan is already running")
    progress.update(running=True, started=int(time.time()), finished=None, added=0, modified=0, removed=0, folders=0, skipped=[], error=None)
    try:
        if check_running and dbsync.digikam_running():
            progress["skipped"].append("digiKam is running; it scans the collection itself")
            return progress
        con = dkdb.connect()
        try:
            roots = dkdb.root_paths(con)
            version = hash_version(con)
            albums = {(r[1], r[2]): r[0] for r in con.execute("SELECT id, albumRoot, relativePath FROM Albums")}
            albums_before = dict(albums)
            known = {}
            for iid, album, name, mdate, size, root, rel in con.execute(
                    "SELECT i.id, i.album, i.name, i.modificationDate, i.fileSize, a.albumRoot, a.relativePath FROM Images i JOIN Albums a ON a.id = i.album WHERE i.status = 1"):
                known[(root, rel, name)] = (iid, mdate, size, album)
        finally:
            con.close()
        new_files, modified, vanished = [], [], []
        for root_id, root_dir in roots.items():
            rd = Path(root_dir)
            mine = {k: v for k, v in known.items() if k[0] == root_id}
            if not rd.is_dir() or (mine and not any(rd.iterdir())):
                progress["skipped"].append(f"{root_dir}: not there or empty (unmounted?); left alone")
                continue
            disk = _walk(rd)
            unreadable = disk.pop("__unreadable__")
            if unreadable:
                progress["skipped"].append(f"{root_dir}: {len(unreadable)} folder(s) could not be listed ({unreadable[0]}); nothing counted as gone there")
            gone = [] if unreadable else [k for k in mine if _nfc(k[2]) not in disk.get(_nfc(k[1]), (set(), []))[0]]
            if len(gone) > 20 and len(gone) > 0.3 * len(mine):
                progress["skipped"].append(f"{root_dir}: {len(gone)} of {len(mine)} photos would be removed; left alone (a disk not fully mounted?)")
                continue
            # (id, album, name, path) as snapshotted: checked again before the row changes
            vanished += [(mine[k][0], mine[k][3], k[2], str(rd / k[1].strip("/") / k[2]) if k[1] != "/" else str(rd / k[2])) for k in gone]
            for rel, (_names, files) in disk.items():
                for name in files:
                    path = str(rd / rel.strip("/") / name) if rel != "/" else str(rd / name)
                    k = (root_id, rel, name)
                    if k not in mine:
                        new_files.append((root_id, rel, name, path))
                        continue
                    iid, mdate, size, _album = mine[k]
                    if mdate is None:
                        continue  # registered by the viewer: completed below, not "modified"
                    st = dkdb.file_stamp(path)
                    if st and (not _same_time(st[0], mdate) or st[1] != size):
                        modified.append((iid, path))
        # new folders first (parents before children), then the files in them
        made = {}
        for root_id, rel, name, path in sorted(new_files):
            key = (root_id, rel)
            if key in albums or key in made:
                continue
            root_row = importer._root_folder(root_id, check_running)
            made[key] = root_row["id"] if rel == "/" else importer._ensure_folder(rel, root_row, check_running)
        albums.update(made)
        con = dkdb.connect()
        try:
            progress["folders"] = con.execute("SELECT count(*) FROM Albums").fetchone()[0] - len(albums_before)
        finally:
            con.close()
        added_ids = []
        by_album = {}
        for root_id, rel, name, path in new_files:
            by_album.setdefault(albums[(root_id, rel)], []).append(Path(path))
        for album_id, paths in by_album.items():
            staged = []
            for p in paths:
                try:
                    staged.append(folders.staged_entry(p))
                except (SyncError, OSError) as e:
                    log.warning("scanner: skipped %s (%s)", p, e)
            if staged:
                regs = folders.register(album_id, staged, check_running)
                added_ids += [a["id"] for a in regs]
        if added_ids:
            complete(added_ids, check_running)
            history.record([{"photo_id": i, "action": "added", "summary": "Found by the file scanner"} for i in added_ids])
        progress["added"] = len(added_ids)
        if modified:
            meta = read([p for _i, p in modified])
            with dbsync.write_transaction(dkdb.sqlite_path(), check_running) as con:
                cats = dict(con.execute(f"SELECT id, category FROM Images WHERE id IN ({','.join(str(i) for i, _p in modified)})").fetchall())
                for iid, path in modified:
                    if not meta.get(path):  # unreadable right now: left as it is, tried again on the next pass
                        log.warning("scanner: %s could not be read; its rows are left as they are", path)
                        continue
                    r = rows_for(path, meta[path], cats.get(iid, 1))
                    _write_rows(con, iid, r, None, {}, full=False)
                    st = dkdb.file_stamp(path)
                    con.execute("UPDATE Images SET uniqueHash = ?, fileSize = ?, modificationDate = ? WHERE id = ?",
                                (unique_hash(path, version), st[1] if st else None, st[0] if st else None, iid))
            progress["modified"] = len(modified)
        if vanished:  # CoreDB::removeItems: status Trashed, no album; digiKam's own cleanup later marks them obsolete
            # the walk took a while: a photo moved, renamed or trashed meanwhile (its row changed) or whose file is
            # back is left alone; only a row still as snapshotted, with nothing on disk, is marked
            still = []
            with dbsync.write_transaction(dkdb.sqlite_path(), check_running) as con:
                for iid, album, name, path in vanished:
                    row = con.execute("SELECT album, name FROM Images WHERE id = ? AND status = 1", (iid,)).fetchone()
                    if not row or row[0] != album or row[1] != name or os.path.exists(path):
                        continue
                    con.execute("UPDATE Images SET status = 3, album = NULL WHERE id = ?", (iid,))
                    still.append((iid, path))
            history.record([{"photo_id": i, "action": "deleted", "summary": "No longer on disk (found by the file scanner)", "path": p, "details": {"path": p}} for i, p in still])
            progress["removed"] = len(still)
        if added_ids or modified or vanished or made:
            log.info("scanner: %d added, %d modified, %d removed, %d folders", len(added_ids), len(modified), progress["removed"], progress["folders"])
    except Exception as e:
        progress["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        log.warning("scanner: the scan stopped: %s", e, exc_info=True)
    finally:
        progress.update(running=False, finished=int(time.time()))
        progress["last"] = {k: progress[k] for k in ("finished", "added", "modified", "removed", "folders", "skipped", "error")}
        _lock.release()
    return dict(progress)


def run_soon():
    _wake.set()


def start(after_change):
    """The background loop: a pass every interval() minutes while the setting is on, or when asked.
    SCANNER_LOOP=0 (the test suite) leaves it out: passes then run only when scan() is called."""
    if os.environ.get("SCANNER_LOOP", "1") == "0":
        return

    def loop():
        time.sleep(30)  # let the start-up index build go first
        while True:
            asked = _wake.wait(timeout=interval() * 60)
            _wake.clear()
            if not (enabled() or asked):
                continue
            try:
                s = scan()
                if s.get("added") or s.get("modified") or s.get("removed"):
                    after_change()
            except Exception as e:  # a pass that failed (digiKam running, a database hiccup) is tried again next time
                log.warning("scanner: pass skipped: %s", e)
    threading.Thread(target=loop, name="file-scanner", daemon=True).start()
