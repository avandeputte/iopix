"""Metadata consistency: compares what digiKam's database, the XMP sidecar and
the image file itself say about each photo, and can bring them into line in a
direction you choose.

Compared fields: rating, pick label, color label, title, caption, tags.
Values are read with exiftool from the sidecar and from the file (its embedded
XMP), and straight from digiKam's database (read-only). Results are stored in
the viewer's own database (vdb.py) so the report can be browsed without rescanning.

Syncing writes with the same code paths as editing: digiKam's writers for the
sidecar / file (edits.write_args), and dbsync for the database (digiKam closed,
backup first).
"""
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import threading
import time

from app import config
from app.ai import analysis
from app.digikam import dbsync, dkdb
from app.photos import edits
from app.data import index, vdb
from app.digikam.dkdb import chunked

CHECK_VERSION = 2  # bumped when the check compares more: every photo is looked at again by "Check new and changed photos"
FIELDS = ["rating", "title", "caption", "tags", "taken", "gps"]  # what the viewer shows and edits (digiKam's pick and colour labels are not offered)
SOURCES = ["db", "sidecar", "file"]
log = logging.getLogger("app.photos.consistency")
progress = {"running": False, "done": 0, "total": 0, "mismatched": 0, "scanned_at": None, "error": None}
_run = {"gen": 0}
TAGS_XMP = ["XMP-digiKam:TagsList", "XMP-microsoft:LastKeywordXMP", "XMP-lr:HierarchicalSubject", "XMP-mediapro:CatalogSets"]
READ_TAGS = ["-XMP-xmp:Rating", "-XMP-digiKam:PickLabel", "-XMP-digiKam:ColorLabel", "-XMP-dc:Title", "-XMP-dc:Description",
             *["-" + t for t in TAGS_XMP], "-DateTimeOriginal", "-GPSLatitude", "-GPSLongitude", "-GPSAltitude"]
# exiftool's JSON keys for READ_TAGS; the tag lists in the order digiKam prefers them
TAG_KEYS = ("TagsList", "LastKeywordXMP", "HierarchicalSubject", "CatalogSets")
VALUE_KEYS = ("Rating", "PickLabel", "ColorLabel", "Title", "Description", *TAG_KEYS)


def init():
    pass  # the consistency table is part of the schema vdb.ensure_schema keeps (state.init)


# --------------------------------------------------------------------------
# reading the three sources
# --------------------------------------------------------------------------

def _blank() -> dict:
    """What a photo with no metadata at all looks like (a fresh copy each time: tags is a list)."""
    return {"rating": 0, "pick": 0, "color": 0, "title": "", "caption": "", "tags": [], "taken": None, "gps": "none"}


def _taken(v):
    """'2026:05:13 15:19:46.12+02:00' or '2026-05-13T15:19:46.000' as '2026-05-13T15:19:46', or None."""
    t = str(v or "").strip()
    if len(t) < 19 or not t[:4].isdigit() or t.startswith("0000"):
        return None
    return t[:4] + "-" + t[5:7] + "-" + t[8:10] + "T" + t[11:19]


def _gps(lat, lon, alt):
    """A position as compared and shown: 'lat, lon' (6 decimals) plus ', alt m' when there is one; 'none'."""
    try:
        if lat in (None, "") or lon in (None, ""):
            return "none"
        out = f"{float(lat):.6f}, {float(lon):.6f}"
        return out + (f", {float(alt):.1f} m" if alt not in (None, "") else "")
    except (TypeError, ValueError):
        return "none"


def _gps_parts(v):
    """('lat, lon[, alt m]' or 'none') -> {lat, lon, alt} or None."""
    if not v or v == "none":
        return None
    parts = [x.strip().removesuffix(" m").strip() for x in str(v).split(",")]
    return {"lat": float(parts[0]), "lon": float(parts[1]), "alt": float(parts[2]) if len(parts) > 2 else None}


def same_value(field: str, a, b) -> bool:
    """Whether two normalised values say the same (positions within about a metre, altitude within a metre)."""
    if field != "gps" or a == b:
        return a == b
    pa, pb = _gps_parts(a), _gps_parts(b)
    if pa is None or pb is None:
        return pa is pb
    if abs(pa["lat"] - pb["lat"]) > 1e-5 or abs(pa["lon"] - pb["lon"]) > 1e-5:
        return False
    if (pa["alt"] is None) != (pb["alt"] is None):
        return False
    return pa["alt"] is None or abs(pa["alt"] - pb["alt"]) <= 1.0


def edit_value(field: str, v):
    """A normalised value as an edit carries it (edits.write_args / dbsync)."""
    if field == "gps":
        return _gps_parts(v)
    return v


def norm(field: str, v):
    """A comparable form of a value: labels 0/absent/-1 are all 'none', text is stripped,
    tags are a sorted list of paths."""
    if field in ("rating", "pick", "color"):
        try:
            n = int(float(v)) if v not in (None, "") else 0
        except (TypeError, ValueError):
            n = 0
        return max(n, 0)
    if field == "tags":
        items = v if isinstance(v, list) else ([v] if v else [])
        return sorted({str(t).replace("|", "/").strip() for t in items if str(t).strip()})
    if field == "taken":
        return _taken(v)
    if field == "gps":
        return v if v else None
    return (str(v).strip() if v else "")


def _boxed_people(ids: list) -> dict:
    """{photo id: {tag paths of the people with a face box on it}}, from digiKam's database."""
    out = {}
    if not ids:
        return out
    src = dkdb.connect()
    try:
        tags = dkdb.tag_tree(src)
        for part in chunked(ids, 500):
            chunk = ",".join(str(int(x)) for x in part)
            for pid, tid in src.execute(f"SELECT imageid, tagid FROM ImageTagProperties WHERE imageid IN ({chunk}) AND property IN ('tagRegion', 'autodetectedFace')"):
                if tid in tags:
                    out.setdefault(pid, set()).add(dkdb.tag_path(tags, tid))
    finally:
        src.close()
    return out


def db_values(photo_ids: list) -> dict:
    """{id: {field: value}} from digiKam's database (read-only), as digiKam itself would write them."""
    src = dkdb.connect(write=False)
    out = {}
    try:
        tags = dkdb.tag_tree(src)
        internal = {r[0] for r in src.execute("SELECT tagid FROM TagProperties WHERE property = 'internalTag'")}
        pick_ids = {tid: index.PICK[n] for tid, (_, n) in tags.items() if n in index.PICK and tid in internal}
        color_ids = {tid: index.COLOR[n] for tid, (_, n) in tags.items() if n in index.COLOR and tid in internal}
        for part in chunked(photo_ids, 500):
            chunk = ",".join(str(int(x)) for x in part)
            for pid, rating in src.execute(f"SELECT imageid, rating FROM ImageInformation WHERE imageid IN ({chunk})"):
                out[pid] = {**_blank(), "rating": rating}
            for pid, tid in src.execute(f"SELECT imageid, tagid FROM ImageTags WHERE imageid IN ({chunk})"):
                d = out.setdefault(pid, _blank())
                if tid in pick_ids:
                    d["pick"] = pick_ids[tid]
                elif tid in color_ids:
                    d["color"] = color_ids[tid]
                elif tid in tags and tid not in internal:
                    d["tags"].append(dkdb.tag_path(tags, tid))
            for pid, typ, text in src.execute(
                    f"SELECT imageid, type, comment FROM ImageComments WHERE language = 'x-default' AND type IN (1, 3) "
                    f"AND imageid IN ({chunk}) ORDER BY CASE WHEN author IS NULL OR author = '' THEN 0 ELSE 1 END, id"):
                d = out.setdefault(pid, _blank())
                key = "title" if typ == 3 else "caption"
                if not d[key]:
                    d[key] = text or ""
            for pid, created in src.execute(f"SELECT imageid, creationDate FROM ImageInformation WHERE imageid IN ({chunk})"):
                out.setdefault(pid, _blank())["taken"] = _taken(created)
            for pid, lat, lon, alt in src.execute(f"SELECT imageid, latitudeNumber, longitudeNumber, altitude FROM ImagePositions WHERE imageid IN ({chunk})"):
                out.setdefault(pid, _blank())["gps"] = _gps(lat, lon, alt)
    finally:
        src.close()
    return out


def exif_read(paths: list) -> dict:
    """{path: {field: value}} for every readable path; a path with no XMP block gives {}."""
    if not paths:
        return {}
    # the paths go in an argument file (one per line), as every exiftool run here does: a sync of thousands of
    # photos would otherwise exceed the command line's length
    with tempfile.NamedTemporaryFile("w", suffix=".args", dir=config.DATA_DIR, delete=False, encoding="utf-8") as f:
        f.write("\n".join(READ_TAGS) + "\n--\n" + "\n".join(paths) + "\n")
        argfile = f.name
    try:
        res = subprocess.run([*edits.exiftool_cmd(), "-j", "-m", "-q", "-charset", "utf8", "-charset", "filename=utf8", "-n", "-@", argfile],
                             capture_output=True, text=True, timeout=1800)
    finally:
        os.unlink(argfile)
    out = {}
    try:
        rows = json.loads(res.stdout or "[]")
    except ValueError:
        rows = []
    for r in rows:
        tags = None
        for key in TAG_KEYS:
            if r.get(key) not in (None, "", []):
                tags = r[key] if isinstance(r[key], list) else [r[key]]
                break
        out[r.get("SourceFile")] = {
            "rating": r.get("Rating"), "pick": r.get("PickLabel"), "color": r.get("ColorLabel"),
            "title": r.get("Title"), "caption": r.get("Description"), "tags": tags if tags is not None else [],
            # date and position: only when the copy has them (a sidecar without a date says nothing about it)
            "taken": _taken(r.get("DateTimeOriginal")),
            "gps": _gps(r.get("GPSLatitude"), r.get("GPSLongitude"), r.get("GPSAltitude")) if r.get("GPSLatitude") not in (None, "") else None,
            "_present": any(k in r for k in VALUE_KEYS) or r.get("DateTimeOriginal") not in (None, "") or r.get("GPSLatitude") not in (None, ""),
            "_tags_present": tags is not None,
        }
    return out


def _can_hold_metadata(path: str) -> bool:
    """A format digiKam writes into (whether or not this copy is writable right now)."""
    return os.path.splitext(path)[1].lower() in edits.INFILE_EXT


def compare(photos: list) -> list:
    """photos: rows with id, path, hash, size, modified. Returns one record per photo."""
    dbv = db_values([p["id"] for p in photos])
    sidecars = [edits.sidecar_path(p["path"]) for p in photos if os.path.isfile(edits.sidecar_path(p["path"]))]
    files = [p["path"] for p in photos if _can_hold_metadata(p["path"])]
    read = exif_read(sidecars + files)
    records = []
    for p in photos:
        side = read.get(edits.sidecar_path(p["path"]))
        fil = read.get(p["path"])
        d = dbv.get(p["id"], _blank())
        vals = {"db": d, "sidecar": side, "file": fil if fil and fil.get("_present") else (None if fil is None else {})}
        fields, mism = {}, []
        for f in FIELDS:
            row = {}
            for s in SOURCES:
                v = vals[s]
                row[s] = None if v is None or (s == "file" and not v) else norm(f, v.get(f))
            present = [row[s] for s in SOURCES if row[s] is not None]
            same = all(same_value(f, x, present[0]) for x in present)
            fields[f] = {**row, "ok": same}
            if not same:
                mism.append(f)
        records.append({"id": p["id"], "name": p.get("name"), "path": p["path"], "has_sidecar": side is not None,
                        "file_xmp": bool(fil), "file_editable": edits.can_write_file(p["path"]),
                        "fields": fields, "mismatch": mism})
    return records


# --------------------------------------------------------------------------
# scanning (background) and the stored report
# --------------------------------------------------------------------------

def _cstamp(p: dict) -> str:
    """The image file's stamp plus the sidecar's size and time: a sidecar rewritten by the viewer or by
    digiKam (the image untouched) makes the photo "changed" for "Check new and changed photos"."""
    try:
        st = os.stat(edits.sidecar_path(p["path"]))
        side = f"{st.st_size}.{int(st.st_mtime)}"
    except OSError:
        side = "-"
    return f"{CHECK_VERSION}|{analysis.stamp(p)}|{side}"


def scan(photos: list, rescan_all: bool = True):
    """Compare every given photo (rows with id, path, name, hash, size, modified) in the background."""
    init()
    _run["gen"] += 1
    gen = _run["gen"]
    todo = list(photos)  # videos too: their sidecar against digiKam (the file itself is not read)
    if not rescan_all:
        con = analysis.connect()
        try:
            have = dict(con.execute("SELECT photo_id, stamp FROM metadata_check").fetchall())
        finally:
            con.close()
        todo = [p for p in todo if have.get(p["id"]) != _cstamp(p)]
    progress.update(running=True, done=0, total=len(todo), error=None, failed=0, started=int(time.time()), rescan_all=bool(rescan_all))
    log.info("consistency: checking %d photos (%s)", len(todo), "all" if rescan_all else "new and changed only")

    def work():
        try:
            for chunk in chunked(todo, 200):
                if gen != _run["gen"]:
                    return
                batch = chunk
                try:
                    recs = compare(batch)
                except Exception as e:  # one bad batch must not end the scan, but it must be visible
                    progress["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                    progress["failed"] = progress.get("failed", 0) + len(batch)
                    log.warning("consistency: a batch of %d photos could not be compared", len(batch), exc_info=True)
                    progress["done"] += len(batch)
                    continue
                now = int(time.time())
                rows = [(r["id"], _cstamp(p), _report(r), len(r["mismatch"]), r["has_sidecar"], r["file_xmp"], now)
                        for p, r in zip(batch, recs)]
                with vdb.writer() as con:
                    con.executemany("INSERT OR REPLACE INTO metadata_check VALUES (?,?,?,?,?,?,?)", rows)
                progress["done"] += len(batch)
            progress["scanned_at"] = int(time.time())
        except Exception as e:
            progress["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            log.warning("consistency: the scan stopped", exc_info=True)
        finally:
            if gen == _run["gen"]:
                progress["running"] = False
                progress["mismatched"] = summary()["mismatched"]
                log.info("consistency: done, %d of %d photos checked, %d disagree%s", progress["done"] - progress.get("failed", 0),
                         progress["total"], progress["mismatched"], f", {progress['failed']} failed ({progress['error']})" if progress.get("failed") else "")

    threading.Thread(target=work, name="consistency", daemon=True).start()


def summary() -> dict:
    empty = {"scanned": 0, "mismatched": 0, "by_field": {}, "no_sidecar": 0, "scanned_at": None}
    if not vdb.exists():
        return empty
    con = analysis.connect()
    try:
        scanned, mism, no_side, last = con.execute(
            "SELECT count(*), sum(mismatch > 0), sum(has_sidecar = 0), max(checked) FROM metadata_check").fetchone()
        by_field = {f: 0 for f in FIELDS}
        mism = 0  # counted from the stored records: results from before a field was dropped may name it
        for (data,) in con.execute("SELECT data FROM metadata_check WHERE mismatch > 0"):
            fs = [f for f in json.loads(data)["mismatch"] if f in by_field]
            for f in fs:
                by_field[f] += 1
            mism += bool(fs)
        return {"scanned": int(scanned or 0), "mismatched": mism, "no_sidecar": int(no_side or 0), "by_field": by_field, "scanned_at": int(last) if last else None}
    except Exception:
        return empty
    finally:
        con.close()


def report(field: str = None, offset: int = 0, limit: int = 100) -> dict:
    """The photos that disagree (on `field`, or on any), newest first; field "nosidecar": those without a sidecar."""
    con = analysis.connect()
    try:
        if field == "nosidecar":
            rows = con.execute("SELECT data FROM metadata_check WHERE has_sidecar = 0 ORDER BY photo_id DESC").fetchall()
            recs = [json.loads(d) for (d,) in rows]
            return {"total": len(recs), "offset": offset, "items": recs[offset:offset + limit]}
        rows = con.execute("SELECT data FROM metadata_check WHERE mismatch > 0 ORDER BY photo_id DESC").fetchall()
    finally:
        con.close()
    recs = []
    for (data,) in rows:
        r = json.loads(data)
        r["mismatch"] = [f for f in r["mismatch"] if f in FIELDS]
        if r["mismatch"]:
            r["fields"] = {f: v for f, v in r["fields"].items() if f in FIELDS}
            recs.append(r)
    if field:
        recs = [r for r in recs if field in r["mismatch"]]
    return {"total": len(recs), "offset": offset, "items": recs[offset:offset + limit]}


def recheck(photo_ids: list):
    """Compare these photos again now, in the background (after the viewer changed them), if they were compared before."""
    ids = [int(i) for i in photo_ids]
    if not ids:
        return
    con = analysis.connect()
    try:
        known = {r[0] for r in con.execute(f"SELECT photo_id FROM metadata_check WHERE photo_id IN ({','.join(map(str, ids))})")}
    finally:
        con.close()
    if not known:
        return
    icon = index.open_index()
    try:
        icon.row_factory = sqlite3.Row
        photos = [dict(r) for r in icon.execute(f"SELECT id, path, name, hash, size, modified, video FROM photos WHERE id IN ({','.join(map(str, known))})")]
    finally:
        icon.close()

    def work():
        time.sleep(2)  # let the write (sidecar, database) settle
        try:
            recs = compare(photos)
            now = int(time.time())
            rows = [(r["id"], _cstamp(p), _report(r), len(r["mismatch"]), r["has_sidecar"], r["file_xmp"], now) for p, r in zip(photos, recs)]
            with vdb.writer() as c:
                c.executemany("INSERT OR REPLACE INTO metadata_check VALUES (?,?,?,?,?,?,?)", rows)
        except Exception:
            log.warning("consistency: re-checking %d edited photos failed", len(photos), exc_info=True)
    threading.Thread(target=work, name="consistency-recheck", daemon=True).start()


def forget(photo_ids: list):
    """Drop stored results so those photos are compared again on the next scan."""
    if photo_ids:
        with vdb.writer() as con:
            con.executemany("DELETE FROM metadata_check WHERE photo_id = ?", [(int(i),) for i in photo_ids])


# --------------------------------------------------------------------------
# syncing
# --------------------------------------------------------------------------

def _changes_from(values: dict, fields: list, db_tags: list) -> dict:
    """An edit dict (as edits.apply / dbsync use) that makes a target look like `values`."""
    ch = {}
    for f in fields:
        if f == "tags":
            want = set(norm("tags", values.get("tags")))
            have = set(db_tags)
            ch["add_tags"] = sorted(want - have)
            ch["remove_tags"] = sorted(have - want)
        elif f in ("taken", "gps"):
            v = norm(f, values.get(f))
            if v is not None:  # a copy without a date or position says nothing about it
                ch[f] = edit_value(f, v)
        else:
            ch[f] = norm(f, values.get(f))
    return ch


def _report(r: dict):
    """The stored report: the full comparison for a photo whose copies disagree or that has no sidecar (what the pages
    and the sync read), nothing for one that agrees. That keeps the table at a few hundred KB instead of tens of MB."""
    return json.dumps(r) if r["mismatch"] or not r["has_sidecar"] else None


def sync(photo_ids: list, source: str, targets: list, fields: list = None) -> dict:
    """Copy the chosen fields from `source` (db|sidecar|file) to each of `targets`."""
    if source not in SOURCES or any(t not in SOURCES or t == source for t in targets) or not targets:
        raise ValueError("Choose a source and at least one different target")
    fields = [f for f in (fields or FIELDS) if f in FIELDS]
    con = analysis.connect()
    try:
        stored = {}
        for part in chunked(photo_ids, 500):
            chunk = ",".join(str(int(x)) for x in part)
            for pid, data in con.execute(f"SELECT photo_id, data FROM metadata_check WHERE photo_id IN ({chunk})"):
                if data:  # photos that agree have no stored report (nothing to copy anyway)
                    stored[pid] = json.loads(data)
    finally:
        con.close()
    done, skipped = [], []
    # fresh values from the source, not the stored report, in case something changed since the scan
    photos = [{"id": pid, "path": stored[pid]["path"], "name": stored[pid]["name"]} for pid in photo_ids if pid in stored]
    dbv = db_values([p["id"] for p in photos]) if source == "db" or "db" in targets else {}
    src_path = (lambda p: edits.sidecar_path(p["path"])) if source == "sidecar" else (lambda p: p["path"])
    read = exif_read([src_path(p) for p in photos]) if source != "db" else {}

    def source_values(p):
        if source == "db":
            return dbv.get(p["id"])
        v = read.get(src_path(p))
        return v if v and v.get("_present") else None  # no metadata block at all: nothing to copy (never "blank")

    # a person with a face box on the photo is there because of the box, which lives in digiKam's database only: a
    # sync never adds or removes such a person in a sidecar or file tag list, nor takes them off the database. A
    # person without a box (tagged on a video, or on a photo without marking a face) is a keyword like any other
    boxed = _boxed_people([p["id"] for p in photos]) if "tags" in fields else {}
    is_person = (lambda pid, t: t in boxed.get(pid, ()))
    db_people = {pid: sorted(boxed.get(pid, ())) for pid in boxed} if "tags" in fields else {}

    def fields_for(src_vals):
        # a file whose metadata has no tag list (IPTC keywords only, or none) says nothing about tags
        return [f for f in fields if f != "tags" or source == "db" or src_vals.get("_tags_present")]

    if "db" in targets:
        pending = {}
        for p in photos:
            src_vals = source_values(p)
            if src_vals is None:
                skipped.append({"id": p["id"], "reason": f"no {source} metadata to copy"})
                continue
            cur = dbv.get(p["id"], {"tags": []})
            ch = _changes_from(src_vals, fields_for(src_vals), norm("tags", cur.get("tags")))
            if "remove_tags" in ch:
                ch["remove_tags"] = [t for t in ch["remove_tags"] if not is_person(p["id"], t)]
            if "add_tags" in ch:
                ch["add_tags"] = [t for t in ch["add_tags"] if not is_person(p["id"], t)]
            if "caption" in ch:
                ch["caption_date"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            pending[p["id"]] = ch
        if pending:
            res = dbsync.apply_pending(pending=pending)  # raises SyncError if digiKam is running
            done += [pid for pid in pending if not any(s["id"] == pid for s in res["skipped"])]
            skipped += res["skipped"]

    file_targets = [t for t in targets if t in ("sidecar", "file")]
    if file_targets:
        jobs, owner = [], {}
        for p in photos:
            src_vals = source_values(p)
            if src_vals is None:
                if not any(s["id"] == p["id"] for s in skipped):
                    skipped.append({"id": p["id"], "reason": f"no {source} metadata to copy"})
                continue
            use = fields_for(src_vals)
            ch = {f: norm(f, src_vals.get(f)) for f in use if f not in ("tags", "taken", "gps")}
            ch.update({k: v for k, v in _changes_from(src_vals, [f for f in use if f in ("taken", "gps")], []).items()})
            if "caption" in ch:
                ch["caption_date"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            tag_paths = None
            if "tags" in use:
                kw = [t for t in norm("tags", src_vals.get("tags")) if not is_person(p["id"], t)]
                tag_paths = sorted(set(kw) | set(db_people.get(p["id"], [])))
            for t in file_targets:
                if t == "sidecar":
                    try:
                        side = edits.prepare(p["path"])  # backs the sidecar up first
                    except OSError:
                        skipped.append({"id": p["id"], "reason": "cannot create the sidecar"})
                        continue
                    jobs.append((side, edits.write_args(ch, tag_paths, False)))
                    owner[side] = p["id"]
                elif edits.can_write_file(p["path"]):
                    jobs.append((p["path"], edits.write_args(ch, tag_paths, True)))
                    owner[p["path"]] = p["id"]
                else:
                    skipped.append({"id": p["id"], "reason": "this file type cannot hold metadata (sidecar only)"})
        failed = {owner[t] for t in edits.run_exiftool(jobs)}
        skipped += [{"id": i, "reason": "exiftool could not write this file"} for i in failed]
        done += [i for i in dict.fromkeys(owner.values()) if i not in failed]
    done = sorted(set(done))
    if source == "db" and done:  # the database was chosen as the truth: edits not in it yet are given up for these fields
        edits.drop_pending_fields(done, fields)
    recheck(done)
    from app.photos import history
    labels = {"db": "the database", "sidecar": "the sidecar", "file": "the image file"}
    history.record([{"photo_id": pid, "action": "sync", "summary": f"Synced {', '.join(fields)} from {labels[source]} to {' and '.join(labels[t] for t in targets)}",
                     "path": stored.get(pid, {}).get("path")} for pid in done])
    return {"synced": len(done), "skipped": skipped}
