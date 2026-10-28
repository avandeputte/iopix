"""Every date stored for a photo, in the file and its sidecar, set against the date its folder is named for: which
of them agree with the folder and which with the date digiKam uses. Read-only (exiftool reads, nothing is written).

For the "date does not match the folder" check: is it only the one date that is off, or do the file's other dates
(EXIF DateTimeOriginal, CreateDate, XMP, IPTC, QuickTime, GPS, the file's own modification time) disagree as well?
"""
import json
import subprocess
from collections import Counter

from app.photos import edits, organize
from app.digikam.dkdb import chunked
from app.data import index
from app.faces import facequality

# file-system dates say when the file was copied, not when the photo was taken: reported, but kept apart
SYSTEM = ("System:FileModifyDate", "System:FileAccessDate", "System:FileInodeChangeDate", "System:FileCreateDate")
SKIP = ("ExifTool:", "Composite:")  # derived copies of other tags


def _day(v: str):
    """'2005:01:31 10:20:30+01:00' -> '2005-01-31' (None for '0000:00:00' and the like)."""
    if not isinstance(v, str) or len(v) < 10:
        return None
    d = v[:10].replace(":", "-")
    if d.startswith("0000") or d[5:7] == "00":
        return None
    return d


def dates(paths: list) -> dict:
    """{path: {group:tag: value}} of every date/time tag in each file (exiftool -time:all)."""
    out = {}
    for chunk in chunked(paths, 100):
        res = subprocess.run([*edits.exiftool_cmd(), "-j", "-G1", "-a", "-q", "-m", "-time:all", "-api", "LargeFileSupport=1", *chunk],
                             capture_output=True, text=True, timeout=600)
        for rec in json.loads(res.stdout or "[]"):
            out[rec.get("SourceFile")] = {k: v for k, v in rec.items() if k != "SourceFile" and not k.startswith(SKIP)}
    return out


def report(items: list, detail: bool = False) -> dict:
    """items: [{photo, path, taken, start, end}] (start/end: the folder's date span, ISO days). Per photo, which dates
    agree with the folder; summed up: how many photos have some date that matches the folder, and which tags."""
    files, cars = [], {}
    for it in items:
        files.append(it["path"])
        for sc in organize.sidecars_of(it["path"]):
            cars.setdefault(it["path"], []).append(str(sc))
            files.append(str(sc))
    found = dates(files)
    agg = Counter()
    summary = Counter()
    rows = []
    for it in items:
        tags = dict(found.get(it["path"], {}))
        for sc in cars.get(it["path"], []):
            tags.update({f"sidecar {k}": v for k, v in found.get(sc, {}).items() if not k.startswith("System:")})
        match, other, sysmatch = [], [], []
        for k, v in tags.items():
            d = _day(v)
            if not d:
                continue
            inside = it["start"] <= d <= it["end"]
            if k in SYSTEM:
                if inside:
                    sysmatch.append(k)
                continue
            (match if inside else other).append((k, v))
            if inside:
                agg[k] += 1
        dated = [k for k, v in tags.items() if k not in SYSTEM and _day(v)]
        if match:
            summary["some embedded date matches the folder"] += 1
        elif sysmatch:
            summary["only the file's own modification date matches the folder"] += 1
        elif dated:
            summary["no date anywhere matches the folder"] += 1
        else:
            summary["no embedded date at all"] += 1
        if len({_day(tags[k]) for k in dated}) > 1:
            summary["embedded dates disagree with each other"] += 1
        if detail:
            rows.append({"photo": it["photo"], "path": it["path"], "taken": it["taken"], "folder": f"{it['start']}..{it['end']}",
                         "match": dict(match), "other": dict(other), "file_dates_matching": sysmatch})
    out = {"photos": len(items), "summary": dict(summary), "tags_matching_the_folder": dict(agg.most_common())}
    if detail:
        out["items"] = rows
    return out


def wrongfolder(detail: bool = False, limit: int = 0) -> dict:
    """report() for the photos of the "date does not match the folder" quality check."""
    issues = facequality.wrongfolder()
    if limit:
        issues = issues[:limit]
    con = index.open_index()
    try:
        paths = {r[0]: r[1] for r in con.execute("SELECT id, path FROM photos")}
    finally:
        con.close()
    items = []
    for u in issues:
        fd = facequality.folder_date(u["folder"])
        start, end = facequality.folder_span(fd)
        items.append({"photo": u["photo"], "path": paths.get(u["photo"]), "taken": u["taken"], "start": start.isoformat(), "end": end.isoformat()})
    return report([i for i in items if i["path"]], detail)
