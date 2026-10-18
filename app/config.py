"""Paths and tunables. Everything can be overridden with environment variables.

Everything this app writes on its own (index, caches, state, models, backups)
lives under DATA_DIR, inside the app's own folder by default. The library is
only ever read, except for the explicit edits the owner asks for (sidecars,
image files when enabled, digiKam's database while digiKam is closed), which
are backed up first and can be locked out with READ_ONLY=1.
"""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


def _path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser().resolve()


DIGIKAM_DB_DIR = _path("DIGIKAM_DB_DIR", Path.home() / "Pictures" / "digikamDB")
DIGIKAM_DB = DIGIKAM_DB_DIR / "digikam4.db"
# Used when the library has a single album root; otherwise digiKam's own
# per-root paths are used.
PHOTOS_ROOT = _path("PHOTOS_ROOT", Path.home() / "Pictures" / "photos")

DATA_DIR = _path("DATA_DIR", APP_DIR / "data")
INDEX_DB = DATA_DIR / "index.db"
CACHE_DIR = DATA_DIR / "cache"
STATIC_DIR = APP_DIR / "static"

# Grid thumbnails: fixed height, width follows the aspect ratio up to a cap.
THUMB_HEIGHT = int(os.environ.get("THUMB_HEIGHT", 360))
THUMB_MAX_WIDTH = int(os.environ.get("THUMB_MAX_WIDTH", 1080))
THUMB_QUALITY = int(os.environ.get("THUMB_QUALITY", 76))
# Lightbox previews: longest edge.
PREVIEW_SIZE = int(os.environ.get("PREVIEW_SIZE", 2560))
PREVIEW_QUALITY = int(os.environ.get("PREVIEW_QUALITY", 86))
# Previews are only made for photos you open; the oldest are pruned at startup
# once the folder grows past this many megabytes.
PREVIEW_CACHE_MAX_MB = int(os.environ.get("PREVIEW_CACHE_MAX_MB", 8000))

# Build all grid thumbnails in the background after startup.
PREWARM = os.environ.get("PREWARM", "1") not in ("0", "false", "no")
# cores the one running background job may use (Settings can change it); half the machine by default
BG_THREADS = int(os.environ.get("BG_THREADS", max(1, (os.cpu_count() or 4) // 2)))
PREWARM_WORKERS = int(os.environ["PREWARM_WORKERS"]) if os.environ.get("PREWARM_WORKERS") else None  # None: BG_THREADS

# How often to check whether digiKam changed its database (seconds).
REINDEX_POLL = int(os.environ.get("REINDEX_POLL", 20))

for _d in (DATA_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
for _p in (DIGIKAM_DB_DIR, PHOTOS_ROOT):
    if DATA_DIR == _p or _p in DATA_DIR.parents:
        raise SystemExit(f"DATA_DIR ({DATA_DIR}) must not be inside the read-only library folder {_p}")
# AUTO_MODELS: models to download on first start when missing, e.g. "siglip2,yolox,geonames"
# (keys as in app/ai/models.py). Containers and installers set it; the Mac service leaves it empty.
AUTO_MODELS = [k.strip() for k in os.environ.get("AUTO_MODELS", "").split(",") if k.strip()]
LOG_FILE = DATA_DIR / "viewer.log"  # rotating copy of the server's own log lines (Maintenance / MCP can show it)
# READ_ONLY=1 locks the viewer to browsing: no edits, tags, faces or database writes, whatever the settings say
READ_ONLY = os.environ.get("READ_ONLY", "0") in ("1", "true", "yes")
# Allow metadata edits (XMP sidecars; image files too when the write_files setting is on).
EDITING = os.environ.get("EDITING", "1") not in ("0", "false", "no")
# Allow "Apply to digiKam database" (only ever runs on request, and only while digiKam is closed).
DB_WRITE = os.environ.get("DB_WRITE", "1") not in ("0", "false", "no")
SEMANTIC = os.environ.get("SEMANTIC", "1") not in ("0", "false", "no")
ANALYSIS = os.environ.get("ANALYSIS", "1") not in ("0", "false", "no")
# Threads for sharpness scoring and object detection; by default the shared BG_THREADS budget.
ANALYSIS_WORKERS = int(os.environ["ANALYSIS_WORKERS"]) if os.environ.get("ANALYSIS_WORKERS") else None  # None: BG_THREADS
# How to tell whether digiKam is running before writing its database: "pgrep" looks for the
# process on this machine; "none" (Docker, a container, digiKam on another machine: it cannot be
# seen) does not check. "ask", the old name for the container mode, now means "none".
DIGIKAM_CHECK = os.environ.get("DIGIKAM_CHECK", "pgrep").strip().lower()
if DIGIKAM_CHECK == "ask":
    DIGIKAM_CHECK = "none"
# Describe every photo in a sentence with a local captioning model (BLIP base), for search.
CAPTIONS = os.environ.get("CAPTIONS", "1") not in ("0", "false", "no")
