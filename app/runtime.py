"""What the running server shares: the index connection the API answers from, the state the
pages poll, the refresh loop that rebuilds the index when digiKam (or the viewer's own edits)
changed it, the background jobs that restart when the library changes (thumbnail prewarm,
analysis, embeddings, tag covers), and the automatic apply of waiting edits.

Nothing here is HTTP: the response helpers live in app/http.py and the write gates in
app/guards.py. The route modules import from those two; the background modules (filecheck,
models) import this one for the index connection and the job restarts."""
import logging
import os
import sqlite3
import threading
import time
import traceback
from urllib.parse import quote

from app.ai import analysis, concepts
from app import config, events, jobs, thumbs
from app.digikam import dbsync, dkdb
from app.photos import edits
from app.faces import ages, facevecs
from app.data import index, state as appstate, vdb
from app.ai.semantic import enabled as semantic_enabled, semantic
from app.data.query import build_where

CACHES: list = []  # lru caches cleared when the index changes (the browsing routes register theirs)

THUMB_COLS = "id, path, video, duration, w, h, hash, size, modified"
# what a failed write to digiKam's database (SQLite or MySQL) or to the file system raises; the
# writers roll back on any of these, so the route can report the failure as such
DB_ERRORS = (*vdb.Error, OSError)

# version: the index build the API answers from; source: digiKam's database as of
# that build (background jobs restart only when this changes)
state = {"version": None, "error": None, "source": None}
prewarmer = thumbs.Prewarmer()  # the thumbnail cache filler; Maintenance and the MCP diagnostics read its progress


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


def start_prewarm():
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
    ages.start()


def run_analysis_soon():
    """Queue the analysis jobs again in the background (a model they use was just downloaded)."""
    def work():
        con = db()
        try:
            rows = [dict(r) for r in con.execute(f"SELECT {THUMB_COLS} FROM photos ORDER BY ts DESC")]
        finally:
            con.close()
        analysis.start(rows)
    threading.Thread(target=work, name="analysis-restart", daemon=True).start()


kick = threading.Event()  # set after an edit so the index is rebuilt right away (events.kick() from the writers)


tag_covers: dict = {}  # tag id -> the most typical photo of that tag (filled in the background)
tag_covers_from = {"sig": None}  # what the covers were computed from: the tags' photo counts and the embeddings file


def compute_tag_covers(version: str):
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
        if sig == tag_covers_from["sig"]:
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
    tag_covers.clear()
    tag_covers.update(found)
    tag_covers_from["sig"] = sig
    for c in CACHES:
        c.cache_clear()


def public_version() -> str:
    """What browsers compare to decide whether to reload: steady while analysis
    is still trickling results in, so the page is not refreshed every minute."""
    v = state["version"] or ""
    if any(j["running"] for j in analysis.progress.values()) or semantic.running:
        parts = v.split(":")
        return ":".join(parts[:4] + ["analysing"])
    return v


def refresh(initial=False):
    """Rebuild the index when digiKam's database (or the viewer's own edits) changed."""
    try:
        if not initial and dkdb.busy_recently():
            # digiKam (or this viewer) wrote the file seconds ago: look again shortly rather than
            # waiting for the next poll, so a write made here shows up without a long delay
            t = threading.Timer(3.5, kick.set); t.daemon = True; t.start()
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
                start_prewarm()
                threading.Thread(target=compute_tag_covers, args=(source,), daemon=True).start()
        auto_apply()
    except (*DB_ERRORS, index.Unavailable) as e:  # e.g. database busy, MySQL unreachable and no copy yet; try again later
        state["error"] = str(e)
        if initial and not config.INDEX_DB.exists():
            raise


auto = {"at": 0.0, "timer": None}


def auto_apply():
    """Sync with digiKam on, digiKam reachable and closed, edits waiting: write them into digiKam's
    database, what "Apply to digiKam database" does by hand. A minute after the last edit, so a run
    of edits goes in one transaction with one backup, and at most every five minutes."""
    # with no digiKam at all, the library tables are where the edits end up, the same way
    if not (config.DB_WRITE and (dkdb.no_digikam() or index.sync_enabled() and index.last_source["kind"] == "digikam") and not appstate.read_only()):
        return
    if not edits.pending_count():
        return
    due = max(edits.newest_pending() + 60, auto["at"] + 300)
    now = time.time()
    if now < due:
        if not auto["timer"] or not auto["timer"].is_alive():
            t = threading.Timer(due - now + 1, kick.set)
            t.daemon = True
            t.start()
            auto["timer"] = t
        return
    auto["at"] = now
    if dbsync.digikam_running():
        return
    try:
        r = dbsync.apply_pending()
        logging.getLogger("app.sync").info("edits applied to digiKam's database (automatically): %d photos, %d skipped", r.get("applied", 0), len(r.get("skipped", [])))
        threading.Thread(target=rebuild_after_write, daemon=True).start()
    except (dbsync.SyncError, *DB_ERRORS) as e:
        logging.getLogger("app.sync").warning("edits were not applied to digiKam's database: %s", e)


def watch():
    while True:
        kicked = kick.wait(timeout=config.REINDEX_POLL)
        kick.clear()
        try:
            refresh(initial=kicked)
        except Exception as e:  # e.g. a wrong database setting (MySQL unreachable): the watcher must outlive it
            state["error"] = str(e)[:jobs.ERROR_TEXT_MAX]
            logging.getLogger("app.data.index").warning("index refresh failed: %s", e)


def models_arrived(keys: list):
    """After a first-start download: pick the search model that just arrived if none is chosen, and kick the jobs."""
    if keys:
        try:
            for k in ("siglip2", "jinaclip", "clip"):
                if k in keys and not appstate.get_setting("search_model", ""):  # "": none chosen yet (the table default would say clip)
                    appstate.set_setting("search_model", k)
                    break
            start_prewarm()
        except Exception as e:  # no index yet (digiKam unreachable on a first start): the watcher starts the jobs later
            logging.getLogger("app.runtime").warning("models arrived but the jobs could not start yet: %s", e)
        kick.set()


def setup_file_log():
    """A rotating log in the data folder, with uvicorn's lines, readable from Maintenance and the MCP
    diagnostics whatever the platform logs to (journald, launchd, docker)."""
    import logging
    from logging.handlers import RotatingFileHandler
    if config.LOG_FILE is None:  # LOG_FILE= with nothing after it: no file
        return
    try:
        config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
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


APP_NAME_DEFAULT = "ioPix"


def app_name() -> str:
    """What the viewer calls itself (Settings › Name): the top bar, the tab title, the installed app."""
    name = (appstate.get_setting("app_name", "") or "").strip()[:40]
    return APP_NAME_DEFAULT if not name or name.lower() == APP_NAME_DEFAULT.lower() else name  # "iopix" typed in any case shows as the wordmark ioPix


def app_stamp() -> str:
    """Changes whenever the front-end files change, so an open tab can tell it is stale."""
    try:
        files = [config.STATIC_DIR / f for f in ("app.js", "app.css", "index.html")] + list((config.STATIC_DIR / "js").glob("*.js"))
        return str(max(int(os.stat(f).st_mtime) for f in files))
    except OSError:
        return "0"


def rebuild_after_write():
    """The viewer just wrote into digiKam's database: rebuild the index now rather than wait for the
    change to be noticed, so applied edits stop showing as waiting at once. Done before the caller
    answers: what it reports (counts, what is in the trash) comes from the rebuilt index."""
    try:
        index.build()
    except Exception as e:  # the watcher retries; the cause goes to the log
        logging.getLogger("app.data.index").warning("rebuild after a database write failed: %s", e)
    state["version"] = None
    kick.set()


events.on("after_write", rebuild_after_write)  # writers call events.after_write() without importing the server
events.on("kick", kick.set)
