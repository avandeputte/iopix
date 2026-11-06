"""The operator's diagnostics, for admins: what one would check in a shell. The
viewer's health, its log and job queue, what is inside one photo's or video's file and which of
its files exist on disk, the proxy sign-in setup, the video repair, and the updater."""
import json
import os
from typing import Literal, Optional

from mcp.server.mcpserver.exceptions import ToolError

from app import __version__, config, jobs, thumbs, users
from iosuite_shared import proxyauth
from app.auth import auth as auth_shared, updater
from iosuite_shared.updates import tail
from app.photos import consistency, edits, repair
from app.data import state as appstate
from app.ai.semantic import semantic, enabled as semantic_enabled
from app.digikam import dbsync, dkdb
from app.ai import models
from app.ai.analysis import progress as analysis_progress
from app.mcp.base import READ_ONLY, UPDATES, WRITES, db, need_admin, server

# the probe outputs an answer keeps: the first lines say what is wrong, the rest is noise in a transcript
FFMPEG_ERRORS_MAX = 1500
LOG_LINES_MAX = 2000  # the most log lines one call returns (more is a download, not an answer)
HEAD_TAIL_BYTES = 64  # a file's first and last bytes: a signature and a cut-off file's end fit in that many
BOXES_MAX = 30  # top-level MP4 boxes listed: a sound file has a handful; more means something is odd


@server.tool(annotations=READ_ONLY)
def diagnostics() -> dict:
    """The viewer's own health: version and commit, platform, database backend, which external tools
    exist (exiftool, ffmpeg, mysqldump), disk space, the background job queue with timing, installed
    models and features, user and permission mode, settings without secrets. Start here for any issue."""
    need_admin()
    import platform
    import shutil as sh
    import sys
    import time
    try:
        free = sh.disk_usage(str(config.DATA_DIR)).free
    except OSError:
        free = None
    q = jobs.status()
    dbc = dkdb.settings()
    out = {
        "version": __version__, "commit": updater.local_commit()[:7], "docker": updater.in_docker(),
        "platform": platform.platform(), "python": sys.version.split()[0], "app_dir": str(config.APP_DIR), "data_dir": str(config.DATA_DIR),
        "database": {"type": dbc["type"], "location": dkdb.describe(), "backup_mode": dbc["backup"], "digikam_check": config.DIGIKAM_CHECK},
        "tools": {"exiftool": bool(edits.EXIFTOOL), "ffmpeg": bool(thumbs.FFMPEG), "mysqldump": bool(sh.which("mysqldump") or sh.which("mariadb-dump"))},
        "disk_free_gb": round(free / 1e9, 1) if free is not None else None,
        "jobs": {**q, "running_for_min": round((time.time() - q["since"]) / 60, 1) if q.get("since") else None},
        "analysis_progress": {k: {"running": v["running"], "done": v["done"], "total": v["total"]} for k, v in analysis_progress.items()},
        "consistency": {"progress": dict(consistency.progress), "summary": consistency.summary()},
        "models": [{"key": m["key"], "installed": m["installed"], "revision": m.get("revision")} for m in models.listing()["models"]],
        "search": {"enabled": semantic_enabled(), "running": semantic.running, "done": semantic.done, "total": semantic.total, "model": semantic.chosen()},
        "users": {"exist": users.any_users(), "count": len(users.list_users())},
        "proxySignIn": auth_shared.proxy.status(),
        "read_only": appstate.read_only(),
        "settings": _public_settings(),
        "log_file": str(config.LOG_FILE or ""), "log_file_exists": bool(config.LOG_FILE and config.LOG_FILE.exists()),
    }
    try:
        con = db()
        try:
            out["index"] = {"photos": con.execute("SELECT count(*) FROM photos").fetchone()[0], "built": con.execute("SELECT value FROM meta WHERE key = 'built'").fetchone()[0]}
        finally:
            con.close()
    except Exception as e:
        out["index"] = {"error": str(e)[:jobs.ERROR_TEXT_MAX]}
    return out


def _public_settings() -> dict:
    """Every stored setting except secrets (passwords, keys, session vouches)."""
    con = appstate.connect()
    try:
        rows = con.execute("SELECT name, value FROM pix_settings").fetchall()
    finally:
        con.close()
    hide = ("password", "key", "secret", "token")
    return {k: ("***" if any(h in k for h in hide) else v) for k, v in rows}


@server.tool(annotations=READ_ONLY)
def server_log(lines: int = 200, contains: str = "", level: Literal["any", "warning", "error", "auth"] = "any") -> dict:
    """The last lines of the viewer's own log (LOG_FILE, data/iopix.log by default, rotating). `contains` keeps only lines
    with that text; `level` keeps only WARNING+ or ERROR+ lines (tracebacks count as errors)."""
    need_admin()
    if config.LOG_FILE is None:
        raise ToolError("no log file (LOG_FILE is empty in config.env)")
    try:
        return tail(config.LOG_FILE, min(int(lines), LOG_LINES_MAX), contains, level)
    except OSError as e:
        raise ToolError(f"cannot read the log: {e}")


@server.tool(annotations=READ_ONLY)
def job_queue() -> dict:
    """What the background worker is doing right now: the running job, its start time and elapsed
    minutes, what is queued, the core budget, and per-job progress with the time of the last
    completed item (a stall shows as a large 'idle_seconds')."""
    need_admin()
    import time
    q = jobs.status()
    now = time.time()
    out = {"running": q["running"], "queued": q["queued"], "threads": q["threads"], "cores": q["cores"],
           "running_for_min": round((now - q["since"]) / 60, 1) if q.get("since") else None,
           "analysis": {k: {"running": v["running"], "done": v["done"], "total": v["total"]} for k, v in analysis_progress.items()},
           "search": {"running": semantic.running, "done": semantic.done, "total": semantic.total}}
    try:  # the thumbnail prewarmer lives in the web app; present when this runs inside it
        from app.main import prewarmer  # lazy: main -> app.mcp would be an import cycle
        out["thumbnails"] = {"running": prewarmer.running, "done": prewarmer.done, "total": prewarmer.total,
                             "idle_seconds": round(now - prewarmer.last) if prewarmer.last else None, "current": list(prewarmer.current.values())[:3]}
    except Exception:
        pass
    return out


@server.tool(annotations=WRITES)
def repair_video(photo_id: int) -> dict:
    """Repair a video that does not play because its wrapper is damaged (inspect_file shows it): the same streams
    rewrapped without re-encoding, checked to play in full before it replaces the file. The original is kept
    (the change log can put it back). Admins only."""
    need_admin(writes=True)
    try:
        return repair.video(int(photo_id))
    except dbsync.SyncError as e:
        raise ToolError(str(e))


@server.tool(annotations=WRITES)
def proxy_signin(mode: Optional[str] = None, cf_access_team: Optional[str] = None, cf_access_aud: Optional[str] = None,
                 trusted: Optional[str] = None, create_role: Optional[str] = None) -> dict:
    """Sign-in at a reverse proxy (Cloudflare Access, Authelia behind HAProxy/nginx/Caddy/Traefik): with no settings
    given, how it is set up and the Cloudflare Access team and application last seen in requests; with settings, saves
    them (mode: off|cloudflare|haproxy|nginx|caddy|traefik|header; an empty string clears one; config.env still wins).
    Admins only."""
    need_admin()
    changes = {k: v for k, v in (("AUTH_PROXY", mode), ("CF_ACCESS_TEAM", cf_access_team), ("CF_ACCESS_AUD", cf_access_aud),
                                 ("AUTH_PROXY_TRUSTED", trusted), ("AUTH_PROXY_CREATE", create_role)) if v is not None}
    if "AUTH_PROXY" in changes and changes["AUTH_PROXY"] not in ("", "off", *proxyauth.PRESETS):
        raise ToolError("mode: off, cloudflare, haproxy, nginx, caddy, traefik or header")
    if changes:
        auth_shared.proxy.save(changes)
    out = auth_shared.proxy.status()
    # which account each person the proxy lets in maps to (matched on email, then name)
    out["accounts"] = [{"name": u["name"], "role": u["role"], "email": u.get("email") or None} for u in users.list_users()]
    if out["seen"].get("email"):
        u = auth_shared.proxy.user_for(out["seen"]["email"]) if auth_shared.proxy.mode() != "off" else None
        out["seen"]["account"] = u["name"] if u else None
    return out


@server.tool(annotations=READ_ONLY)
def inspect_file(photo_id: int) -> dict:
    """What is inside one photo's or video's file, for "it does not show" questions: exiftool's reading of its
    container and streams (warnings included), ffmpeg's decode check of the first seconds, and the file's first and
    last bytes (a QuickTime file needs its moov box; a file cut short has none). Read-only. Admins only."""
    need_admin()
    import shutil
    import subprocess
    con = db()
    try:
        r = con.execute("SELECT path FROM photos WHERE id = ?", (int(photo_id),)).fetchone()
    finally:
        con.close()
    if not r:
        raise ToolError("No such photo")
    path = r[0]
    out = {"path": path}
    try:
        res = subprocess.run([*edits.exiftool_cmd(), "-j", "-G1", "-q", "-m", "-api", "LargeFileSupport=1", "-FileType", "-MIMEType", "-Warning", "-Error",
                              "-MajorBrand", "-CompatibleBrands", "-Duration", "-CompressorID", "-CompressorName", "-ImageWidth", "-ImageHeight",
                              "-AvgBitrate", "-VideoFrameRate", "-AudioFormat", "-HandlerType", "-ContentIdentifier", "-MediaDataSize", "-MovieDataSize", path],
                             capture_output=True, text=True, timeout=120)
        out["exiftool"] = json.loads(res.stdout or "[{}]")[0]
    except Exception as e:  # noqa: BLE001 - a diagnostic: report what failed
        out["exiftool"] = {"error": str(e)[:jobs.ERROR_TEXT_MAX]}
    ff = shutil.which("ffmpeg")
    if ff:
        res = subprocess.run([ff, "-v", "error", "-nostdin", "-t", "3", "-i", path, "-f", "null", "-"], capture_output=True, text=True, timeout=120)
        out["ffmpeg_decode"] = {"ok": res.returncode == 0 and not res.stderr.strip(), "errors": res.stderr.strip()[:FFMPEG_ERRORS_MAX]}
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_TAIL_BYTES)
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - HEAD_TAIL_BYTES))
            tail = f.read(HEAD_TAIL_BYTES)
        out["size"], out["head"], out["tail"] = size, head.hex(), tail.hex()
        boxes, pos = [], 0
        with open(path, "rb") as f:  # the top-level boxes of an MP4 / QuickTime file
            while pos < size and len(boxes) < BOXES_MAX:
                f.seek(pos)
                hd = f.read(16)
                if len(hd) < 8:
                    break
                n, kind = int.from_bytes(hd[:4], "big"), hd[4:8].decode("latin-1")
                if n == 1 and len(hd) >= 16:
                    n = int.from_bytes(hd[8:16], "big")
                elif n == 0:
                    n = size - pos
                boxes.append({"box": kind, "at": pos, "size": n})
                if n < 8:
                    break
                pos += n
        out["boxes"] = boxes
        out["complete"] = bool(boxes) and pos == size
    except OSError as e:
        out["read_error"] = str(e)
    return out


@server.tool(annotations=READ_ONLY)
def photo_file(photo_id: int) -> dict:
    """Checks one photo's files on disk: the image (exists, size, readable), its XMP sidecar, the
    thumbnail cache entry, and any backups the viewer made. For 'this photo does not show' issues."""
    need_admin()
    con = db()
    try:
        r = con.execute("SELECT id, path, name, size, hash, modified, video FROM photos WHERE id = ?", (photo_id,)).fetchone()
    finally:
        con.close()
    if not r:
        raise ToolError(f"No item with id {photo_id}")
    path = r["path"]
    out = {"id": r["id"], "path": path, "indexed_size": r["size"], "video": bool(r["video"])}
    try:
        st = os.stat(path)
        out.update(exists=True, size=st.st_size, readable=os.access(path, os.R_OK), writable=os.access(path, os.W_OK), size_matches=st.st_size == r["size"])
    except OSError as e:
        out.update(exists=False, error=str(e))
    side = edits.sidecar_path(path)
    out["sidecar"] = {"exists": os.path.exists(side), "writable_dir": os.access(os.path.dirname(path), os.W_OK)}
    try:
        tp = thumbs.cache_path("t", r["id"], thumbs.stamp_for(r["hash"], r["size"], r["modified"]))
        out["thumbnail_cached"] = tp.exists()
    except Exception:
        pass
    try:
        out["sidecar_backup"] = edits.backup_path(path).exists()
        out["file_backup"] = edits.file_backup_path(path).exists()
    except Exception:
        pass
    return out


@server.tool(annotations=READ_ONLY)
def check_for_update() -> dict:
    """Installed version and commit versus GitHub's main branch (network), and the outcome of the
    last update run (lastUpdate: ok, text, at)."""
    return {"version": __version__, **updater.check(), "lastUpdate": updater.last_result()}


@server.tool(annotations=UPDATES)  # openWorld: it reaches GitHub
def update_viewer(ref: str = "") -> dict:
    """Update the viewer to the latest commit on GitHub and restart it (update.sh: code, packages,
    service; settings and data are kept). Admins only. ref: the commit sha (or branch) to install; pass the sha
    you just pushed, since GitHub can report the previous commit for a short while after a push.
    Nothing happens when that commit is already installed. The connection drops when the viewer
    restarts; call check_for_update afterwards: lastUpdate carries the outcome and installed the
    commit. Not available in Docker (pull the image)."""
    need_admin()
    try:
        updater.run(ref)
    except RuntimeError as e:
        raise ToolError(str(e))
    return {"started": True, "installed": updater.local_commit()[:7], "ref": ref or "main",
            "note": "update.sh is running; the viewer restarts when it finishes (about a minute). Then call check_for_update: lastUpdate says what happened."}
