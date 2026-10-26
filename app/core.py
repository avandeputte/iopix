"""What every part of the server shares: the index connection, the state the pages poll, the refresh loop, the response helpers and the guards the editing endpoints use."""
import gzip
import hashlib
import logging
import os
import sqlite3
import threading
import time
import traceback
from urllib.parse import parse_qsl
from urllib.parse import quote

import orjson
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import Response

from app.ai import analysis
from app.ai import concepts
from app import config
from app.digikam import dbsync
from app.digikam import dkdb
from app.photos import edits
from app.faces import facedetect
from app.faces import facevecs
from app.data import index
from app.data import vdb
from app import thumbs
from app.data import state as appstate
from app.ai import models as dlmodels
from app.ai.semantic import semantic
from app.ai.semantic import enabled as semantic_enabled
from app.data.query import build_where

CACHES: list = []  # lru caches cleared when the index changes (the browsing routes register theirs)

THUMB_COLS = "id, path, video, duration, w, h, hash, size, modified"
IMG_CACHE = {"Cache-Control": "private, max-age=86400"}
# what a failed write to digiKam's database (SQLite or MySQL) or to the file system raises; the
# writers roll back on any of these, so the route can report the failure as such
DB_ERRORS = (*vdb.Error, OSError)

# version: the index build the API answers from; source: digiKam's database as of
# that build (background jobs restart only when this changes)
state = {"version": None, "error": None, "source": None}
prewarmer = thumbs.Prewarmer()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{quote(str(config.INDEX_DB))}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def resolve(items) -> tuple[str, list, dict | None]:
    """Filter parameters to SQL. Content search ("sem") and "more like this"
    ("like") are answered by the embedding index and become an id list plus a
    ranking."""
    items = list(items)
    d = dict(items)
    rank = None
    if semantic_enabled():  # a plain search also covers the detected concepts of the same name
        for key, text in list(items):
            if key == "q" and text:
                try:
                    items.append(("qids", ",".join(map(str, concepts.photos_matching(text))) or "0"))
                except Exception:
                    traceback.print_exc()
                    items.append(("qids", "0"))  # one qids per q, in order: the next term's ids must not slide onto this one
    if d.get("sem") or d.get("like") or d.get("concept"):
        ids = []
        if semantic_enabled():
            try:
                k = max(1, min(int(d.get("sem_k") or 300), 2000))
                ids = (concepts.photos_for(d["concept"]) if d.get("concept") else
                       semantic.search_text(d["sem"], k) if d.get("sem") else semantic.similar(int(d["like"]), k))
            except Exception:  # an empty result rather than a failed page; the cause goes to the log
                traceback.print_exc()
                ids = []
        items.append(("ids", ",".join(map(str, ids)) or "0"))
        rank = {pid: i for i, pid in enumerate(ids)}
    where, params = build_where(items)
    return where, params, rank


def _start_prewarm():
    con = db()
    try:
        rows = [dict(r) for r in con.execute(f"SELECT {THUMB_COLS} FROM photos ORDER BY ts DESC")]
    finally:
        con.close()
    if config.PREWARM:
        prewarmer.start(rows)  # first: its cache scan is slow over a mount, and thumbnails come first in the queue
    analysis.start(rows)
    facevecs.start()
    semantic.start(rows)


_kick = threading.Event()  # set after an edit so the index is rebuilt right away


_tag_covers: dict = {}  # tag id -> the most typical photo of that tag (filled in the background)
_tag_covers_from = {"sig": None}  # what the covers were computed from: the tags' photo counts and the embeddings file


def _compute_tag_covers(version: str):
    """Pick a cover for each keyword tag that actually looks like the tag: the photo
    closest to the average of the tag's photos, nudged toward the tag's name. One
    query and one text embedding per tag, so skipped when nothing it depends on
    changed since the last run (the same tags with the same counts, the same index)."""
    if not semantic_enabled():
        return
    con = db()
    try:
        tags = con.execute("SELECT id, name, n_all FROM tags WHERE person = 0 AND n_all > 0").fetchall()
        sig = hash((tuple(sorted((t["id"], t["n_all"]) for t in tags)), semantic.key))
        if sig == _tag_covers_from["sig"]:
            return
        found = {}
        for t in tags:
            if state.get("source") != version:
                return
            ids = [r[0] for r in con.execute(
                "SELECT DISTINCT pt.photo_id FROM photo_tags pt JOIN tag_closure c ON c.tag = pt.tag_id "
                "JOIN photos p ON p.id = pt.photo_id WHERE c.anc = ? AND p.video = 0 AND p.pick != 1", (t["id"],))]
            best = concepts.representative(ids, t["name"])
            if best:
                found[t["id"]] = best
    except Exception:  # keep the previous covers; the cause goes to the log
        traceback.print_exc()
        return
    finally:
        con.close()
    _tag_covers.clear()
    _tag_covers.update(found)
    _tag_covers_from["sig"] = sig
    for c in CACHES:
        c.cache_clear()


def _public_version() -> str:
    """What browsers compare to decide whether to reload: steady while analysis
    is still trickling results in, so the page is not refreshed every minute."""
    v = state["version"] or ""
    if any(j["running"] for j in analysis.progress.values()) or semantic.running:
        parts = v.split(":")
        return ":".join(parts[:4] + ["analysing"])
    return v


def _refresh(initial=False):
    """Rebuild the index when digiKam's database (or the viewer's own edits) changed."""
    try:
        if not initial and dkdb.busy_recently():
            # digiKam (or this viewer) wrote the file seconds ago: look again shortly rather than
            # waiting for the next poll, so a write made here shows up without a long delay
            t = threading.Timer(3.5, _kick.set); t.daemon = True; t.start()
            return
        rebuilt = index.ensure_fresh()
        state["error"] = None
        version = index.current_version()
        if rebuilt or version != state["version"]:
            state["version"] = version
            for c in CACHES:
                c.cache_clear()
            # background jobs restart only when the library itself changed, not each
            # time their own results are folded into the index
            source = index.last_source["version"]
            if source != state.get("source"):
                state["source"] = source
                _start_prewarm()
                threading.Thread(target=_compute_tag_covers, args=(source,), daemon=True).start()
        _auto_apply()
    except (*DB_ERRORS, index.Unavailable) as e:  # e.g. database busy, MySQL unreachable and no copy yet; try again later
        state["error"] = str(e)
        if initial and not config.INDEX_DB.exists():
            raise


_auto = {"at": 0.0, "timer": None}


def _auto_apply():
    """Sync with digiKam on, digiKam reachable and closed, edits waiting: write them into digiKam's
    database, what "Apply to digiKam database" does by hand. A minute after the last edit, so a run
    of edits goes in one transaction with one backup, and at most every five minutes."""
    # with no digiKam at all, the library tables are where the edits end up, the same way
    if not (config.DB_WRITE and (dkdb.no_digikam() or index.sync_enabled() and index.last_source["kind"] == "digikam") and not appstate.read_only()):
        return
    if not edits.pending_count():
        return
    due = max(edits.newest_pending() + 60, _auto["at"] + 300)
    now = time.time()
    if now < due:
        if not _auto["timer"] or not _auto["timer"].is_alive():
            t = threading.Timer(due - now + 1, _kick.set)
            t.daemon = True
            t.start()
            _auto["timer"] = t
        return
    _auto["at"] = now
    if dbsync.digikam_running():
        return
    try:
        r = dbsync.apply_pending()
        logging.getLogger("app.sync").info("edits applied to digiKam's database (automatically): %d photos, %d skipped", r.get("applied", 0), len(r.get("skipped", [])))
        threading.Thread(target=_rebuild_after_write, daemon=True).start()
    except (dbsync.SyncError, *DB_ERRORS) as e:
        logging.getLogger("app.sync").warning("edits were not applied to digiKam's database: %s", e)


def _watch():
    while True:
        kicked = _kick.wait(timeout=config.REINDEX_POLL)
        _kick.clear()
        try:
            _refresh(initial=kicked)
        except Exception as e:  # e.g. a wrong database setting (MySQL unreachable): the watcher must outlive it
            state["error"] = str(e)[:300]
            logging.getLogger("app.data.index").warning("index refresh failed: %s", e)


def _models_arrived(keys: list):
    """After a first-start download: pick the search model that just arrived if none is chosen, and kick the jobs."""
    if keys:
        try:
            for k in ("siglip2", "jinaclip", "clip"):
                if k in keys and not appstate.get_setting("search_model"):
                    appstate.set_setting("search_model", k)
                    break
            _start_prewarm()
        except Exception as e:  # no index yet (digiKam unreachable on a first start): the watcher starts the jobs later
            logging.getLogger("app.core").warning("models arrived but the jobs could not start yet: %s", e)
        _kick.set()


def _setup_file_log():
    """A rotating log in the data folder, with uvicorn's lines, readable from Maintenance and the MCP
    diagnostics whatever the platform logs to (journald, launchd, docker)."""
    import logging
    from logging.handlers import RotatingFileHandler
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h.setLevel(logging.INFO)  # the file's own level: INFO and up, whatever the loggers let through
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        if not any(isinstance(x, RotatingFileHandler) and getattr(x, "baseFilename", "") == str(config.LOG_FILE) for x in lg.handlers):
            lg.addHandler(h)
        # a logger nobody configured (NOTSET: the root logger, WARNING by default) opens up to INFO so the app's own
        # lines reach the file; a level somebody set, such as uvicorn's from --log-level, is left alone
        if lg.level == logging.NOTSET:
            lg.setLevel(logging.INFO)



def jresp(request: Request, data) -> Response:
    """JSON, gzipped when large, with an ETag: a client that sends it back (If-None-Match) gets 304 and no body while
    the answer is unchanged (the apps' reloads of meta, photos and state)."""
    body = orjson.dumps(data)
    etag = 'W/"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag, "Vary": "Accept-Encoding, Cookie"}
    if etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    if len(body) > 2048 and "gzip" in request.headers.get("accept-encoding", ""):
        body = gzip.compress(body, compresslevel=3)
        headers["Content-Encoding"] = "gzip"
    return Response(body, media_type="application/json", headers=headers)


async def _form_items(request: Request) -> list:
    """(key, value) pairs of a form-encoded body: the same filters the URL carries,
    for selections too long for a URL."""
    return parse_qsl((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)



APP_NAME_DEFAULT = "iopix"


def app_name() -> str:
    """What the viewer calls itself (Settings › Name): the top bar, the tab title, the installed app."""
    return (appstate.get_setting("app_name", "") or "").strip()[:40] or APP_NAME_DEFAULT




def _app_stamp() -> str:
    """Changes whenever the front-end files change, so an open tab can tell it is stale."""
    try:
        files = [config.STATIC_DIR / f for f in ("app.js", "app.css", "index.html")] + list((config.STATIC_DIR / "js").glob("*.js"))
        return str(max(int(os.stat(f).st_mtime) for f in files))
    except OSError:
        return "0"



def _photo_row(photo_id: int, cols: str = THUMB_COLS) -> dict:
    con = db()
    try:
        row = con.execute(f"SELECT {cols} FROM photos WHERE id = ?", (photo_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404)
    return dict(row)


def _cached_image(kind: str, photo_id: int, dl: bool = False) -> FileResponse:
    row = _photo_row(photo_id)
    try:
        path = thumbs.make(kind, row)
    except FileNotFoundError:
        raise HTTPException(404, "Original file is not available")
    except OSError as e:
        raise HTTPException(503, f"The file could not be read right now: {e}")
    if dl:  # a download of the resized JPEG, named after the photo (sharing a smaller file)
        return FileResponse(path, media_type="image/jpeg", filename=os.path.splitext(row["name"])[0] + "-preview.jpg",
                            content_disposition_type="attachment", headers=IMG_CACHE)
    return FileResponse(path, media_type="image/jpeg", headers=IMG_CACHE)



def _face_detect_allowed():
    if not facedetect.available():
        raise HTTPException(409, f"Face detection needs the {dlmodels.REGISTRY[dlmodels.chosen('face_detector')]['label']} model: download it in Maintenance › AI analysis and models")
    if not config.DB_WRITE or appstate.read_only():
        raise HTTPException(403, "Writing to digiKam's database is turned off")
    if not dkdb.no_digikam() and (not index.sync_enabled() or index.offline["since"]):  # no digiKam: they live in the library tables
        raise HTTPException(409, "Face regions live in digiKam's database, which is unreachable (or Sync with digiKam is off)")



def _organize_allowed():
    if not config.DB_WRITE:
        raise HTTPException(403, "Writing to digiKam's database is turned off (DB_WRITE=0)")
    if index.sync_enabled() and index.offline["since"]:
        raise HTTPException(409, "digiKam's database is unreachable right now")


def _run_organize(fn, *args):
    from app.photos import organize
    try:
        return fn(*args)
    except dbsync.SyncError as e:
        raise HTTPException(409, str(e))
    except DB_ERRORS as e:
        raise HTTPException(500, f"The change failed and was rolled back: {e}")
    finally:
        _ = organize



def _rebuild_after_write():
    """The viewer just wrote into digiKam's database: rebuild the index now rather than wait for the
    change to be noticed, so applied edits stop showing as waiting at once."""
    try:
        index.build()
    except Exception as e:  # the watcher retries; the cause goes to the log
        logging.getLogger("app.data.index").warning("rebuild after a database write failed: %s", e)
    state["version"] = None
    _kick.set()
