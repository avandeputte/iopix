"""Import: photos and videos dropped on the Import page, or shared to the installed app, go into folders by
date, duplicates are left out, and each new file gets a sidecar.

1. Files arrive in a staging folder (data/import-staging/<token>/), from the page or from the Android share
   target (POST /share). Nothing touches the library yet.
2. plan(): each file is probed (the date it was taken, from its metadata, else its file time) and compared with
   the library: a file of the same size whose contents hash the same (SHA-256) is a duplicate of that photo.
   The destination folder follows the layout setting (import_layout, default "{yyyy}/{yyyy}{mm}", the layout
   this library uses: 2006/200610) under the album root.
3. run(): folders that do not exist yet are created (directory and Albums row, parents first, as the Folders
   page does), each file is moved in under a free name, a sidecar is written beside it holding the file's own
   metadata (exiftool -tagsfromfile: dates, camera, GPS, keywords...), and the photo is registered exactly as an
   upload is (modificationDate NULL, so digiKam scans it fully on its next run). Every photo gets a change-log
   entry; the staging folder is removed.
"""
import hashlib
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from app import config
from app.digikam import dkdb
from app.photos import edits, folders
from app.digikam.dbsync import SyncError

log = logging.getLogger("app.photos.importer")
STAGING = config.DATA_DIR / "import-staging"
LAYOUTS = {
    "year-yearmonth": ("{yyyy}/{yyyy}{mm}", "2006/200610 (digiKam's and this library's layout)"),
    "year-month": ("{yyyy}/{mm}", "2006/10"),
    "year-date": ("{yyyy}/{yyyy}-{mm}-{dd}", "2006/2006-10-16"),
    "yearmonth": ("{yyyy}{mm}", "200610"),
    "year": ("{yyyy}", "2006"),
}
MAX_AGE = 2 * 86400  # staged files nobody imported are dropped after two days
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def layout() -> str:
    from app.data import state
    key = state.get_setting("import_layout", "")
    return key if key in LAYOUTS else "year-yearmonth"


def set_layout(key: str):
    from app.data import state
    if key not in LAYOUTS:
        raise ValueError("Unknown folder layout")
    state.set_setting("import_layout", key)


_TOKENS = ("yyyy", "mm", "dd")
_BAD_CHARS = re.compile(r"[\\\x00-\x1f\x7f]")


def check_pattern(pattern: str) -> str:
    """A folder pattern an app sends instead of a layout key: a relative path whose segments may carry {yyyy},
    {mm} and {dd} ("{yyyy}/{yyyy}{mm}", "Uploads", "Phone/{yyyy}-{mm}"). Returns it cleaned, or raises
    ValueError in plain words. Folders that do not exist yet are created at import time."""
    p = (pattern or "").strip()
    if not p:
        raise ValueError("Give a folder, like Uploads or {yyyy}/{yyyy}{mm}")
    if len(p) > 200:
        raise ValueError("The folder pattern is too long (200 characters at most)")
    if _BAD_CHARS.search(p):
        raise ValueError("The folder pattern cannot contain backslashes or control characters")
    if p.startswith("/") or p.endswith("/"):
        raise ValueError("The folder pattern is relative to the library: no slash at the start or the end")
    for seg in p.split("/"):
        if seg in ("", ".", ".."):
            raise ValueError("Every part of the folder pattern needs a name; . and .. are not allowed")
    for tok in re.findall(r"\{([^{}]*)\}", p):
        if tok not in _TOKENS:
            raise ValueError(f"Unknown placeholder {{{tok}}}: use {{yyyy}}, {{mm}} and {{dd}}")
    if p.count("{") != p.count("}") or re.search(r"\{[^}]*\{|\}[^{]*\}(?=[^{]*\})", p) or re.search(r"\{[^}]*$", p):
        raise ValueError("Unbalanced braces in the folder pattern; placeholders are {yyyy}, {mm} and {dd}")
    return p


def pattern_of(key: str = None) -> str:
    """The {yyyy}/{mm}-style pattern for a layout key, or a custom pattern (already checked) passed as the key."""
    if key is None:
        return LAYOUTS[layout()][0]
    return LAYOUTS[key][0] if key in LAYOUTS else check_pattern(key)


def folder_for(when: str, key: str = None) -> str:
    """The relative folder (/2006/200610) a photo taken at `when` (ISO text) goes to; `key` is a layout key or a
    custom pattern (check_pattern)."""
    y, m, d = when[0:4], when[5:7], when[8:10]
    return "/" + pattern_of(key).format(yyyy=y, mm=m, dd=d)


# ---- staging
def _dir(token: str) -> Path:
    if not _TOKEN.match(token or ""):
        raise SyncError("Unknown import")
    return STAGING / token


def stage(files: list, token: str = None) -> str:
    """files: [(name, bytes or an open binary file)]. Returns the token of the staging folder (new, or the given one
    to add to). An open file is copied in chunks, so a multi-gigabyte upload never sits in memory."""
    token = token or secrets.token_urlsafe(16)
    d = _dir(token)
    d.mkdir(parents=True, exist_ok=True)
    for name, data in files:
        name = os.path.basename(name or "") or "photo"
        try:
            name = folders._clean_name(name, "file")
        except SyncError:
            name = "photo-" + secrets.token_hex(3) + os.path.splitext(name)[1]
        dst = d / folders._free_name(d, name)
        _store(dst, data)
    _sweep()
    return token


def _store(dst, data):
    """Write bytes, or copy an open file in chunks, to dst."""
    if isinstance(data, (bytes, bytearray)):
        dst.write_bytes(data)
        return
    data.seek(0)
    with open(dst, "wb") as out:
        shutil.copyfileobj(data, out, 1024 * 1024)


def _sweep():
    """Staging folders nobody finished with."""
    if not STAGING.is_dir():
        return
    now = time.time()
    for d in STAGING.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > MAX_AGE:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def discard(token: str):
    shutil.rmtree(_dir(token), ignore_errors=True)


# ---- the plan
def _sha256(path, limit: int = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _same_size(sizes: set) -> dict:
    """{size: [(photo id, path)]} for library photos of these sizes."""
    if not sizes:
        return {}
    from app.data import index
    con = index.open_index()
    try:
        out = {}
        for i in range(0, len(sizes), 500):
            chunk = ",".join(str(int(s)) for s in list(sizes)[i:i + 500])
            for pid, path, size in con.execute(f"SELECT id, path, size FROM photos WHERE size IN ({chunk})"):
                out.setdefault(size, []).append((pid, path))
        return out
    finally:
        con.close()


def plan(token: str, key: str = None) -> dict:
    """What importing the staged files would do: [{name, size, when, folder, duplicate_of, kind}]."""
    d = _dir(token)
    if not d.is_dir():
        raise SyncError("Those files are no longer staged (imports are kept for two days)")
    files = sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith("."))
    sizes = {p.stat().st_size for p in files}
    same = _same_size(sizes)
    hashes = {}
    items = []
    for p in files:
        ext = p.name.rsplit(".", 1)[-1].lower() if "." in p.name else ""
        try:
            kind = folders._category(ext)
        except SyncError as e:
            items.append({"name": p.name, "size": p.stat().st_size, "error": str(e)})
            continue
        dup = None
        cands = same.get(p.stat().st_size) or []
        if cands:
            mine = _sha256(p)
            for pid, path in cands:
                if path not in hashes:
                    try:
                        hashes[path] = _sha256(path)
                    except OSError:
                        hashes[path] = None
                if hashes[path] == mine:
                    dup = pid
                    break
        info = folders._probe(p)
        items.append({"name": p.name, "size": p.stat().st_size, "when": info["when"], "folder": folder_for(info["when"], key),
                      "duplicate_of": dup, "kind": "video" if kind == 2 else "photo"})
    return {"token": token, "layout": key or layout(), "items": items,
            "new": sum(1 for i in items if not i.get("duplicate_of") and not i.get("error")), "duplicates": sum(1 for i in items if i.get("duplicate_of"))}


# ---- doing it
def _root_folder(root_id: int = None, check_running: bool = True) -> dict:
    """The album root folder (relativePath '/') to import under: the given root, else the first mounted one.
    digiKam always has that row; if it is missing (a root digiKam has not scanned yet), it is added."""
    con = dkdb.connect()
    try:
        roots = dkdb.root_paths(con)
        if root_id is not None:
            roots = {k: v for k, v in roots.items() if k == int(root_id)}
        if not roots:
            raise SyncError("No album root to import into (Settings › Album roots)")
        rows = con.execute("SELECT id, albumRoot FROM Albums WHERE relativePath = '/'").fetchall()
        rows = [r for r in rows if r[1] in roots]
    finally:
        con.close()
    if rows:
        r = rows[0]
        return {"id": r[0], "root": r[1], "dir": Path(roots[r[1]])}
    from app.digikam import dbsync
    root = sorted(roots)[0]
    db_path = dkdb.sqlite_path()
    dkdb.backup(db_path)
    with dbsync.write_transaction(db_path, check_running) as con:
        cur = con.execute("INSERT INTO Albums (albumRoot, relativePath, date, caption, collection) VALUES (?, '/', ?, NULL, NULL)",
                          (root, time.strftime("%Y-%m-%d")))
        return {"id": cur.lastrowid, "root": root, "dir": Path(roots[root])}


def _ensure_folder(rel: str, root: dict, check_running: bool) -> int:
    """The Albums id for /a/b under the root, creating a and a/b (directories and rows) when missing."""
    con = dkdb.connect()
    try:
        row = con.execute("SELECT id FROM Albums WHERE albumRoot = ? AND relativePath = ?", (root["root"], rel)).fetchone()
    finally:
        con.close()
    if row:
        return row[0]
    parent_rel = rel.rsplit("/", 1)[0] or "/"
    parent_id = root["id"] if parent_rel == "/" else _ensure_folder(parent_rel, root, check_running)
    return folders.create(parent_id, rel.rsplit("/", 1)[1], check_running)["id"]


def make_sidecar(path: str) -> bool:
    """A sidecar beside a new file, holding the file's own metadata (what digiKam would write to it)."""
    side = edits.sidecar_path(path)
    if os.path.exists(side):
        return True
    try:
        res = subprocess.run([*edits.exiftool_cmd(), "-q", "-m", "-tagsfromfile", path, "-all:all", "-xmp:all", "-charset", "filename=utf8", side],
                             capture_output=True, text=True, timeout=120)
        if res.returncode != 0 or not os.path.exists(side):
            Path(side).write_text(edits.SKELETON, encoding="utf-8")  # at least an empty one, as an edit would create
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("import: no sidecar for %s (%s)", path, e)
        return False


def sidecars_from_db(ids: list):
    """Once a new file's rows are written (scanner.complete, which also reads IPTC and EXIF): its sidecar says the
    same as digiKam's database, caption, title, date, position, tags and face regions included. exiftool's copy of
    the file's metadata holds only what the file had as XMP, so an IPTC-only JPEG would get an empty one."""
    from app.faces import facemgr
    ids = [int(i) for i in ids]
    if not ids:
        return
    vals = edits.db_state(ids)
    from app.digikam import dbsync
    jobs = []
    for pid, path in dbsync._photo_paths(ids):
        side, v = edits.sidecar_path(path), vals.get(pid) or {}
        if not os.path.exists(side):
            continue
        ch = {k: v[k] for k in ("rating", "title", "caption") if v.get(k)}
        if v.get("taken"):
            ch["taken"] = v["taken"]
        if v.get("gps"):
            ch["gps"] = v["gps"]
        if ch:
            jobs.append((side, edits.write_args(ch, None, False)))
    if jobs:
        edits.run_exiftool(jobs)
    facemgr.write_regions_many(ids, files=False)  # tags and faces


def run(token: str, key: str = None, include: list = None, skip_duplicates: bool = True, root_id: int = None, check_running: bool = True) -> dict:
    """Import the staged files (all, or the names in include). Returns {added: [{id, name, path, folder}], skipped: [{name, reason}]}."""
    from app.digikam import dbsync
    from app.photos import history
    p = plan(token, key)
    want = set(include) if include is not None else None
    d = _dir(token)
    db_path = dkdb.sqlite_path()
    dbsync.preflight(db_path, check_running, "the photo list")
    root = _root_folder(root_id, check_running)
    added, skipped = [], []
    by_folder = {}
    for it in p["items"]:
        if want is not None and it["name"] not in want:
            continue
        if it.get("error"):
            skipped.append({"name": it["name"], "reason": it["error"]})
            (d / it["name"]).unlink(missing_ok=True)  # dealt with: nothing more will happen to it
        elif it.get("duplicate_of") and skip_duplicates:
            skipped.append({"name": it["name"], "reason": "already in the library", "duplicate_of": it["duplicate_of"]})
            (d / it["name"]).unlink(missing_ok=True)
        else:
            by_folder.setdefault(it["folder"], []).append(it)
    for rel, items in by_folder.items():
        try:
            album_id = _ensure_folder(rel, root, check_running)
        except SyncError as e:
            skipped += [{"name": it["name"], "reason": f"folder {rel}: {e}"} for it in items]
            continue
        target = root["dir"] / rel.strip("/")
        staged = []
        for it in items:
            try:
                final = folders._free_name(target, it["name"])
                dst = target / final
                shutil.move(str(d / it["name"]), dst)
                make_sidecar(str(dst))
                staged.append((final, 2 if it["kind"] == "video" else 1, final.rsplit(".", 1)[-1].lower(), dst, folders._probe(dst)))
            except (OSError, SyncError) as e:
                skipped.append({"name": it["name"], "reason": str(e)})
        if staged:
            regs = folders.register(album_id, staged, check_running)
            from app.photos import scanner
            scanner.complete([a["id"] for a in regs], check_running)  # the full rows digiKam's scan would write
            sidecars_from_db([a["id"] for a in regs])
            for a, (final, _c, _e, dst, _i) in zip(regs, staged):
                added.append({"id": a["id"], "name": final, "path": str(dst), "folder": rel})
            history.record([{"photo_id": a["id"], "action": "added", "summary": f"Imported into {rel} (sidecar created)", "path": str(dst)} for a, (_f, _c, _e, dst, _i) in zip(regs, staged)])
    try:
        if not any(x.is_file() for x in d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
    log.info("import: %d added, %d skipped", len(added), len(skipped))
    return {"added": added, "skipped": skipped}


def layouts() -> list:
    return [{"key": k, "pattern": v[0], "example": v[1]} for k, v in LAYOUTS.items()]

