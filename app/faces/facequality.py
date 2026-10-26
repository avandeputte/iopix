"""Face metadata quality: problems in the face regions digiKam's database holds, and the clean-up of the one
that can be fixed safely without a person looking.

Near-duplicates are the same person's face stored twice on a photo, a few pixels apart (each coordinate within
NEAR pixels). They come from a rounding round trip in digiKam: regions live in files as fractions of the image
size and in the database as pixels; a rescan read a region back one pixel off, an older digiKam added it next to
the one it had, and from then on both were written to the file and read back on every rescan
(ItemScanner::commitFaces keeps two regions that differ). On screen they look like one box; in the people list
the person appears twice.

The clean-up keeps one region of each such group and removes the others from digiKam's database, then rewrites
the photo's sidecar regions from the database (not the image files) and records the photo's new file date (as
digiKam does after writing metadata), so digiKam does not import the pair again. One full database backup is
taken first; every photo gets a change-log entry with a copy of its sidecar from before.

Reported, not changed: the same person in two clearly separate places on one photo (usually a misnamed face),
and rectangles that are empty or fall outside the image.
"""
import datetime as dt
import logging
import os
import re
import threading
import time
from pathlib import Path

from app.digikam import dbsync, dkdb
from app.data import index

log = logging.getLogger("app.faces.facequality")
NEAR = 3          # pixels: regions of one person this close on every coordinate are the same face
MARGIN = 5        # pixels a rectangle may stick out of the image before it counts as outside
FILE_DONE = "Duplicate face regions removed from the image file"
progress = {"running": False, "files": False, "done": 0, "total": 0, "removed": 0, "photos": 0, "errors": 0, "started": None, "finished": None, "error": None, "backup": None}
_lock = threading.Lock()


def _rect(value):
    m = index.RECT_RE.search(value or "")
    return tuple(map(int, m.groups())) if m else None


def _near(a, b) -> bool:
    return all(abs(a[k] - b[k]) <= NEAR for k in range(4))


def _groups(rects: list) -> list:
    """rects: [(value, rect)] of one person on one photo -> lists of near-identical ones (single-member groups too)."""
    groups = []
    for v, r in rects:
        for g in groups:
            if any(_near(r, r2) for _v, r2 in g):
                g.append((v, r))
                break
        else:
            groups.append([(v, r)])
    return groups


def scan() -> dict:
    """Every face region in digiKam's database (or the library with sync off), checked."""
    con = dkdb.connect()
    try:
        tags = dkdb.tag_tree(con)
        special = {r[0] for r in con.execute("SELECT tagid FROM TagProperties WHERE property IN ('unknownPerson', 'ignoredPerson', 'unconfirmedPerson')")}
        rows = con.execute(
            "SELECT p.imageid, p.tagid, p.property, p.value, ii.width, ii.height, ii.orientation FROM ImageTagProperties p "
            "JOIN Images i ON i.id = p.imageid AND i.status = 1 LEFT JOIN ImageInformation ii ON ii.imageid = p.imageid "
            "WHERE p.property IN ('tagRegion', 'autodetectedFace', 'autodetectedPerson', 'ignoredFace')").fetchall()
    finally:
        con.close()
    by, invalid = {}, []
    faces_of = {}  # photo -> [named faces, unnamed faces] (ignored ones do not count)
    for img, tag, prop, val, w, h, orient in rows:
        if prop == "tagRegion" and tag not in special:
            faces_of.setdefault(img, [0, 0])[0] += 1
        elif prop == "autodetectedFace":  # an unknown face, or one digiKam suggests a name for
            faces_of.setdefault(img, [0, 0])[1] += 1
        r = _rect(val)
        up_w, up_h = ((h, w) if (orient or 1) in (5, 6, 7, 8) else (w, h))
        if prop == "autodetectedPerson":  # the suggestion's own row; its box is the person's autodetectedFace, checked there
            continue
        if r is None or r[2] <= 0 or r[3] <= 0 or (up_w and up_h and (r[0] < -MARGIN or r[1] < -MARGIN or r[0] + r[2] > up_w + MARGIN or r[1] + r[3] > up_h + MARGIN)):
            invalid.append({"photo": img, "name": dkdb.tag_path(tags, tag).split("/")[-1] if tag in tags else "?", "rect": list(r) if r else None, "size": [up_w, up_h],
                            "fix": "fit" if r and r[2] > 0 and r[3] > 0 and up_w and up_h else "remove" if r else None})
            continue
        if prop == "tagRegion":
            by.setdefault((img, tag), []).append((val, r))
    near, separate = [], []
    for (img, tag), rects in by.items():
        if len(rects) < 2:
            continue
        groups = _groups(rects)
        name = dkdb.tag_path(tags, tag).split("/")[-1] if tag in tags else "?"
        for g in groups:
            if len(g) > 1:
                near.append({"photo": img, "tag": tag, "name": name, "keep": g[0][0], "drop": [v for v, _r in g[1:]],
                             "apart": max(abs(a[k] - b[k]) for _va, a in g for _vb, b in g for k in range(4))})
        if len(groups) > 1:
            separate.append({"photo": img, "name": name, "rects": [list(g[0][1]) for g in groups]})
    bad_dates = _baddate()
    acc_d = _accepted("baddate")
    bad_shown = [u for u in bad_dates if acc_d.get(u["photo"]) != u["signature"]]
    wrong = _wrongfolder()
    acc_w = _accepted("wrongfolder")
    wrong_shown = [u for u in wrong if acc_w.get(u["photo"]) != u["signature"]]
    noloc = _nolocation()
    acc_l = _accepted("nolocation")
    noloc_shown = [u for u in noloc if acc_l.get(u["photo"]) != u["signature"]]
    untagged = _untagged()
    acc_u = _accepted("untagged")
    untagged_shown = [u for u in untagged if acc_u.get(u["photo"]) != u["signature"]]
    accepted = _accepted("unnamed")
    unnamed = [{"photo": img, "faces": n[1], "signature": f"{n[0]}:{n[1]}"} for img, n in sorted(faces_of.items()) if n[1] and not n[0]]
    shown = [u for u in unnamed if accepted.get(u["photo"]) != u["signature"]]
    return {"checked": len(rows), "near": near, "separate": separate, "invalid": invalid, "unnamed": shown,
            "unnamed_accepted": len(unnamed) - len(shown), "untagged": untagged_shown, "untagged_accepted": len(untagged) - len(untagged_shown),
            "nolocation": noloc_shown, "nolocation_accepted": len(noloc) - len(noloc_shown),
            "wrongfolder": wrong_shown, "wrongfolder_accepted": len(wrong) - len(wrong_shown),
            "baddate": bad_shown, "baddate_accepted": len(bad_dates) - len(bad_shown),
            "at": int(time.time())}


def _untagged() -> list:
    """Photos in which object detection sees people, with no face region of any kind and no person tagged:
    [{photo, people, signature}], the most people first."""
    con = index.open_index()
    try:
        rows = con.execute(
            "SELECT p.id, p.npeople FROM photos p WHERE p.video = 0 AND p.npeople > 0 "
            "AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.photo_id = p.id) "
            "AND NOT EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.photo_id = p.id AND t.person = 1) "
            "ORDER BY p.npeople DESC, p.ts DESC").fetchall()
    except Exception:  # an index from before object detection: nothing to say
        return []
    finally:
        con.close()
    return [{"photo": r[0], "people": r[1], "signature": f"p{r[1]}"} for r in rows]


def _nolocation() -> list:
    """Photos and videos with no position: [{photo, ts, name, signature}], the newest first. Accepting one keeps it
    off the list for good (a photo that gets a location leaves it anyway)."""
    con = index.open_index()
    try:
        rows = con.execute("SELECT id, ts, name FROM photos WHERE lat IS NULL OR lon IS NULL ORDER BY ts DESC, id DESC").fetchall()
    finally:
        con.close()
    return [{"photo": r[0], "ts": r[1], "name": r[2], "signature": "nogps"} for r in rows]


# a folder segment that starts with a date: 2020, 202001, 2020-01, 2020-01-15 Trip, 20200115 (the deepest one counts)
FOLDER_DATE = re.compile(r"^(1[89]\d\d|20\d\d)(?:[-_. ]?(0[1-9]|1[0-2])(?:[-_. ]?(0[1-9]|[12]\d|3[01]))?)?(?!\d|s\b)")
EVENT_DAYS = 31   # a folder named for a day holds that day and the trip or event that started on it


def folder_date(path: str):
    """(year, month or None, day or None) that a folder's path names, from its deepest dated segment, or None."""
    found = None
    for seg in (path or "").strip("/").split("/"):
        m = FOLDER_DATE.match(seg)
        if m:
            found = (int(m[1]), int(m[2]) if m[2] else None, int(m[3]) if m[3] else None)
    return found


def _folder_span(fd):
    """The dates a folder's own date covers: a day and the month after it, a month, or a year; a day of slack on
    each side for clocks and time zones."""
    y, mo, d = fd
    try:
        if d:
            start = dt.date(y, mo, d); end = start + dt.timedelta(days=EVENT_DAYS)
        elif mo:
            start = dt.date(y, mo, 1); end = (dt.date(y + (mo == 12), mo % 12 + 1, 1)) - dt.timedelta(days=1)
        else:
            start, end = dt.date(y, 1, 1), dt.date(y, 12, 31)
    except ValueError:  # 2021-02-30
        return None
    return start - dt.timedelta(days=1), end + dt.timedelta(days=1)


def _wrongfolder() -> list:
    """Photos and videos whose date is outside the date their folder is named for (2005-01-01 in 2020/202001): the
    date or the folder is wrong. [{photo, ts, name, taken, folder, folder_id, expect, suggest, signature}], the
    furthest off first. suggest is the existing folder named for the photo's own date, when there is one."""
    con = index.open_index()
    try:
        albums = con.execute("SELECT id, path, n FROM albums").fetchall()
        # a date marked unknown (dateprecision.py) is a placeholder on purpose: nothing to report
        rows = con.execute("SELECT id, ts, name, taken, album_id FROM photos WHERE taken IS NOT NULL AND taken != '' AND (dprec IS NULL OR dprec < 3)").fetchall()
    finally:
        con.close()
    spans, by_date = {}, {}
    for aid, path, n in albums:
        fd = folder_date(path)
        if not fd:
            continue
        span = _folder_span(fd)
        if span:
            spans[aid] = (span, fd, path)
            by_date.setdefault(fd, []).append((n or 0, aid, path))
    out = []
    for pid, ts, name, taken, aid in rows:
        if aid not in spans:
            continue
        try:
            day = dt.date.fromisoformat(taken[:10])
        except ValueError:
            continue
        (start, end), fd, path = spans[aid]
        if start <= day <= end:
            continue
        sug = None
        for key in ((day.year, day.month, day.day), (day.year, day.month, None), (day.year, None, None)):
            if by_date.get(key):
                n, sid, spath = max(by_date[key])
                sug = {"id": sid, "path": spath}
                break
        expect = f"{fd[0]:04d}" + (f"-{fd[1]:02d}" if fd[1] else "") + (f"-{fd[2]:02d}" if fd[2] else "")
        off = min(abs((day - start).days), abs((day - end).days))
        out.append({"photo": pid, "ts": ts, "name": name, "taken": taken[:19], "folder": path, "folder_id": aid, "expect": expect,
                    "suggest": sug, "off": off, "signature": f"{taken[:10]}@{aid}"})
    out.sort(key=lambda u: (-u["off"], u["folder"], u["name"]))
    return out


# dates cameras and programs write when they know none: the start of the Mac, Unix, DOS and Y2K clocks
PLACEHOLDER_DATES = ("1904-01-01T00:00:00", "1970-01-01T00:00:00", "1980-01-01T00:00:00", "2000-01-01T00:00:00")
EARLIEST_YEAR = 1901  # a photo dated before this is almost always a missing or broken date (1900-01-01 and the like)


def _baddate() -> list:
    """Photos and videos whose date cannot be right: before 1901, in the future, or exactly a clock's starting point
    (1970-01-01 00:00:00...). A date its folder is named for (a /1970 folder of scans) is taken as meant.
    [{photo, ts, name, taken, folder, folder_id, reason, folder_date, signature}]."""
    con = index.open_index()
    try:
        albums = {aid: path for aid, path in con.execute("SELECT id, path FROM albums")}
        # a date marked unknown (dateprecision.py) is a placeholder on purpose: nothing to report
        rows = con.execute("SELECT id, ts, name, taken, album_id FROM photos WHERE taken IS NOT NULL AND taken != '' AND (dprec IS NULL OR dprec < 3)").fetchall()
    finally:
        con.close()
    soon = (dt.datetime.now() + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    out = []
    for pid, ts, name, taken, aid in rows:
        t = taken[:19]
        if t[:4] < str(EARLIEST_YEAR):
            reason = f"dated {t[:4]}"
        elif t > soon:
            reason = "dated in the future"
        elif t in PLACEHOLDER_DATES:
            reason = "a camera's or program's default date"
        else:
            continue
        path = albums.get(aid, "")
        fd = folder_date(path)
        if fd and fd[0] == int(t[:4]) and reason != "dated in the future":
            continue  # the folder is named for that year: meant (scans of old prints, a year-only date)
        out.append({"photo": pid, "ts": ts, "name": name, "taken": t, "folder": path, "folder_id": aid, "reason": reason,
                    "folder_date": f"{fd[0]:04d}-{(fd[1] or 1):02d}-{(fd[2] or 1):02d}T12:00:00" if fd else None, "signature": t})
    out.sort(key=lambda u: (u["taken"], u["folder"], u["name"]))
    return out


def _accepted(check: str) -> dict:
    """{photo: signature} the owner accepted for this check."""
    from app.data import vdb
    con = vdb.connect()
    try:
        return dict(con.execute("SELECT photo_id, signature FROM quality_accepted WHERE check_name = ?", (check,)).fetchall())
    finally:
        con.close()


def ignore_unnamed(photos: list) -> dict:
    """Mark every unnamed face on these photos as not a face to name (digiKam's Ignored), through the normal
    face operations (change log, undo). Returns {ignored, failed}."""
    from app.faces import facemgr
    con = dkdb.connect()
    try:
        ops = []
        for i in range(0, len(photos), 500):
            chunk = ",".join(str(int(p)) for p in photos[i:i + 500])
            for img, val in con.execute(f"SELECT imageid, value FROM ImageTagProperties WHERE property = 'autodetectedFace' AND imageid IN ({chunk})"):
                r = _rect(val)
                if r:
                    ops.append({"op": "ignore", "image": img, "rect": list(r)})
    finally:
        con.close()
    out = {"ignored": 0, "failed": 0}
    for i in range(0, len(ops), 200):
        for r in facemgr.apply_many(ops[i:i + 200], background=True):
            out["failed" if "error" in r else "ignored"] += 1
    return out


def accept(photos: list, check: str = "unnamed", on: bool = True) -> int:
    """Accept these photos' finding as it is (they leave the list until their faces change), or take it back."""
    from app.photos import history
    from app.data import vdb
    if check not in ("unnamed", "untagged", "nolocation", "wrongfolder", "baddate"):
        raise ValueError("Unknown check")
    want = {int(p) for p in photos}
    current = {u["photo"]: u["signature"] for u in scan()[check]} if on else {}
    con = vdb.connect()
    try:
        if on:
            sig = current.copy()
            missing = want - set(sig)
            if missing:  # already accepted, or not a finding (any more): keep what is there
                want -= missing
            con.executemany("INSERT OR REPLACE INTO quality_accepted (photo_id, check_name, signature, user_name, ts) VALUES (?,?,?,?,?)",
                            [(p, check, sig[p], history._who(), int(time.time())) for p in want])
        else:
            con.executemany("DELETE FROM quality_accepted WHERE photo_id = ? AND check_name = ?", [(p, check) for p in want])
        con.commit()
    finally:
        con.close()
    return len(want)


def items(check: str, offset: int = 0, limit: int = 120) -> dict:
    """The next entries of one list of the page: near | separate | invalid | unnamed | untagged."""
    s = scan()
    if check == "near":
        rows = [{"photo": n["photo"], "name": n["name"], "apart": n["apart"]} for n in s["near"]]
    elif check in ("separate", "invalid", "unnamed", "untagged", "nolocation", "wrongfolder", "baddate"):
        rows = s[check]
    else:
        raise ValueError("Unknown list")
    return {"total": len(rows), "items": rows[max(0, offset):max(0, offset) + max(1, min(limit, 500))]}


def summary(s: dict = None, limit: int = 120) -> dict:
    s = s or scan()
    photos = sorted({n["photo"] for n in s["near"]})
    return {"checked": s["checked"], "at": s["at"],
            "near": {"photos": len(photos), "groups": len(s["near"]), "extra": sum(len(n["drop"]) for n in s["near"]),
                     "examples": [{"photo": n["photo"], "name": n["name"], "apart": n["apart"]} for n in s["near"][:limit]]},
            "separate": {"count": len(s["separate"]), "items": s["separate"][:limit]},
            "invalid": {"count": len(s["invalid"]), "items": s["invalid"][:limit]},
            "unnamed": {"count": len(s["unnamed"]), "faces": sum(u["faces"] for u in s["unnamed"]), "items": s["unnamed"][:limit], "accepted": s["unnamed_accepted"]},
            "untagged": {"count": len(s["untagged"]), "items": s["untagged"][:limit], "accepted": s["untagged_accepted"]},
            "nolocation": {"count": len(s["nolocation"]), "items": s["nolocation"][:limit], "accepted": s["nolocation_accepted"]},
            "wrongfolder": {"count": len(s["wrongfolder"]), "items": s["wrongfolder"][:limit], "accepted": s["wrongfolder_accepted"]},
            "baddate": {"count": len(s["baddate"]), "items": s["baddate"][:limit], "accepted": s["baddate_accepted"]},
            "progress": dict(progress)}


def _file_regions(paths: list) -> dict:
    """{path: (mwg regions [(name, x, y, w, h) in pixels], mp regions [(name, x, y, w, h) as fractions])} embedded in
    the image files themselves (not their sidecars)."""
    import json
    import subprocess
    from app.photos import edits
    out = {}
    if not paths:
        return out
    res = subprocess.run([*edits.exiftool_cmd(), "-j", "-struct", "-charset", "filename=UTF8", "-XMP-mwg-rs:RegionInfo", "-XMP-MP:RegionInfoMP", *paths],
                         capture_output=True, text=True, timeout=600)
    try:
        items = json.loads(res.stdout or "[]")
    except ValueError:
        return out
    for it in items:
        mwg, mp = [], []
        info = it.get("RegionInfo") or {}
        dim = info.get("AppliedToDimensions") or {}
        W, H = float(dim.get("W") or 0), float(dim.get("H") or 0)
        for r in info.get("RegionList") or []:
            a = r.get("Area") or {}
            try:
                w, h = float(a["W"]) * W, float(a["H"]) * H
                mwg.append((r.get("Name") or "", float(a["X"]) * W - w / 2, float(a["Y"]) * H - h / 2, w, h))
            except (KeyError, TypeError, ValueError):
                pass
        for r in (it.get("RegionInfoMP") or {}).get("Regions") or []:
            try:
                x, y, w, h = map(float, str(r["Rectangle"]).split(","))
                mp.append((r.get("PersonDisplayName") or "", x, y, w, h))
            except (KeyError, ValueError):
                pass
        out[it.get("SourceFile")] = (mwg, mp)
    return out


def _has_duplicates(mwg: list, mp: list) -> bool:
    """A name twice with near-identical rectangles: within NEAR pixels (MWG), or within 0.2% of the size (MP)."""
    def dup(rs, tol):
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                if rs[i][0] and rs[i][0] == rs[j][0] and all(abs(rs[i][k] - rs[j][k]) <= tol for k in range(1, 5)):
                    return True
        return False
    return dup(mwg, NEAR + 0.5) or dup(mp, 0.002)


def fix_files(after=None) -> int:
    """After the clean-up: the image files of the photos it cleaned that still carry the duplicate regions get
    their regions rewritten from the database too (the file is backed up first, and its pixels are checked
    unchanged). Runs in the background. Returns how many photos it will look at."""
    from app.photos import edits
    from app.data import vdb
    if progress["running"]:
        raise dbsync.SyncError("The clean-up is still running; the image files follow when it has finished.")
    con = vdb.connect()
    try:
        cleaned = [r[0] for r in con.execute("SELECT DISTINCT photo_id FROM photo_history WHERE action = 'face' AND summary LIKE 'Duplicate face regions removed:%'")]
        done = {r[0] for r in con.execute("SELECT DISTINCT photo_id FROM photo_history WHERE summary = ?", (FILE_DONE,))}
    finally:
        con.close()
    todo = [(pid, p) for pid, p in dbsync._photo_paths([p for p in cleaned if p not in done]) if edits.can_write_file(p)]
    if not todo:
        return 0
    import shutil
    need = sum(os.path.getsize(p) for _i, p in todo if os.path.exists(p)) * 1.1 + 200 * 1024 ** 2
    from app import config
    if shutil.disk_usage(str(config.DATA_DIR)).free < need:
        raise dbsync.SyncError(f"Not enough free space for the backups of the original files ({need / 1e9:.1f} GB needed).")
    progress.update(running=True, files=True, done=0, total=len(todo), removed=0, photos=0, errors=0, started=int(time.time()), finished=None, error=None)

    def job():
        try:
            _run_files(todo)
        except Exception as e:
            log.warning("face clean-up of the image files stopped: %s", e, exc_info=True)
            progress["error"] = str(e)[:300]
        finally:
            progress.update(running=False, finished=int(time.time()))
            if after:
                after()
    threading.Thread(target=job, name="face-cleanup-files", daemon=True).start()
    return len(todo)


def _run_files(todo: list):
    from app.faces import facemgr
    from app.photos import history
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        regions = _file_regions([p for _i, p in batch])
        dirty = [(pid, p) for pid, p in batch if p in regions and _has_duplicates(*regions[p])]
        status = facemgr.write_regions_many([pid for pid, _p in dirty], files=True, sidecars=False) if dirty else {}
        rows = []
        for pid, p in dirty:
            if status.get(pid) == "written":
                progress["photos"] += 1
                rows.append({"photo_id": pid, "action": "face", "path": p, "summary": FILE_DONE})
            else:
                progress["errors"] += 1
        history.record(rows)
        progress["done"] += len(batch)
    log.info("face clean-up of the image files: %d files rewritten, %d failed, %d already clean", progress["photos"], progress["errors"],
             progress["total"] - progress["photos"] - progress["errors"])


def fit_inside(photos: list = None) -> dict:
    """Face rectangles that stick out of their photo are trimmed to its edges, and empty ones removed (all of
    them, or those on the given photos). Through the normal face operations, so each is in the change log
    with undo. Returns {fitted, removed, failed}."""
    from app.faces import facemgr
    want = {int(p) for p in photos} if photos else None
    ops = []
    for it in scan()["invalid"]:
        if want is not None and it["photo"] not in want or not it["rect"]:
            continue
        x, y, w, h = it["rect"]
        if it["fix"] == "fit":
            W, H = it["size"]
            nx, ny = max(0, x), max(0, y)
            nw, nh = min(x + w, W) - nx, min(y + h, H) - ny
            if nw >= 8 and nh >= 8:
                ops.append({"op": "move", "image": it["photo"], "rect": [x, y, w, h], "new_rect": [nx, ny, nw, nh]})
                continue
        ops.append({"op": "remove", "image": it["photo"], "rect": [x, y, w, h]})
    out = {"fitted": 0, "removed": 0, "failed": 0}
    for i in range(0, len(ops), 200):
        res = facemgr.apply_many(ops[i:i + 200], background=True)
        for o, r in zip(ops[i:i + 200], res):
            if "error" in r:
                out["failed"] += 1
            else:
                out["fitted" if o["op"] == "move" else "removed"] += 1
    return out


def fix_near_duplicates(after=None, check_running: bool = True) -> int:
    """Start the clean-up in the background. Returns how many photos it will touch."""
    if progress["running"]:
        raise dbsync.SyncError("The clean-up is already running.")
    s = scan()
    todo = {}
    for n in s["near"]:
        todo.setdefault(n["photo"], []).append(n)
    if not todo:
        return 0
    db_path = Path(dkdb.sqlite_path())
    dbsync.preflight(db_path, check_running, "faces")
    progress.update(running=True, files=False, done=0, total=len(todo), removed=0, photos=0, errors=0, started=int(time.time()), finished=None, error=None, backup=None)

    def job():
        try:
            _run(todo, db_path, check_running)
        except Exception as e:
            log.warning("face clean-up stopped: %s", e, exc_info=True)
            progress["error"] = str(e)[:300]
        finally:
            progress.update(running=False, finished=int(time.time()))
            if after:
                after()
    threading.Thread(target=job, name="face-cleanup", daemon=True).start()
    return len(todo)


def _run(todo: dict, db_path: Path, check_running: bool):
    from app.photos import edits, history
    from app.faces import facemgr
    backup = dkdb.backup(db_path, force=True)  # thousands of regions removed in one go: one full copy first
    progress["backup"] = str(backup) if backup else None
    photos = list(todo)
    paths = dict(dbsync._photo_paths(photos))
    for i in range(0, len(photos), 100):
        batch = photos[i:i + 100]
        snaps = {pid: history.snapshot(pid, paths[pid] + ".xmp") for pid in batch if pid in paths}
        removed = {}
        with facemgr._lock:
            with dbsync.write_transaction(db_path, check_running) as con:
                for pid in batch:
                    for n in todo[pid]:
                        gone = 0
                        for v in dict.fromkeys(n["drop"]):
                            if v == n["keep"]:
                                continue
                            con.execute("DELETE FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = 'tagRegion' AND value = ?", (pid, n["tag"], v))
                            # and its training and extended rows, as removing a face does (FaceTagsEditor)
                            con.execute("DELETE FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property IN ('faceToTrain', 'faceTagExtendedData') AND value = ?",
                                        (pid, n["tag"], v))
                            gone += 1
                        # exact copies of the kept value: remove them all, put one back
                        same = sum(1 for v in n["drop"] if v == n["keep"])
                        if same:
                            con.execute("DELETE FROM ImageTagProperties WHERE imageid = ? AND tagid = ? AND property = 'tagRegion' AND value = ?", (pid, n["tag"], n["keep"]))
                            con.execute("INSERT INTO ImageTagProperties (imageid, tagid, property, value) VALUES (?,?,?,?)", (pid, n["tag"], "tagRegion", n["keep"]))
                            gone += same
                        removed.setdefault(pid, []).append((n["name"], gone, n["apart"], n["keep"]))
        status = facemgr.write_regions_many(batch, db_path, files=False)
        rows = []
        for pid in batch:
            done = removed.get(pid, [])
            k = sum(g for _n, g, _a, _v in done)
            progress["removed"] += k
            if status.get(pid) != "written":
                progress["errors"] += 1
            rows.append({"photo_id": pid, "action": "face", "path": paths.get(pid), "sidecar": snaps.get(pid),
                         "summary": "Duplicate face regions removed: " + ", ".join(f"{n} ({a} px apart)" for n, _g, a, _v in done),
                         "details": {"removed": k, "kept": {n: v for n, _g, _a, v in done}, "sidecar": status.get(pid)}})
        history.record(rows)
        progress["photos"] += len(batch)
        progress["done"] += len(batch)
    log.info("face clean-up: %d duplicate regions removed on %d photos (%d sidecars not rewritten)", progress["removed"], progress["photos"], progress["errors"])
    _ = edits
