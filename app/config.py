"""Paths and tunables: config.env in the app folder (setup.sh writes it; config.env.example lists them all), or the
environment, which wins. The settings every app of the suite shares come from iosuite_shared.config.standard.

Everything this app writes on its own (index, caches, state, models, backups)
lives under DATA_DIR, inside the app's own folder by default. The library is
only ever read, except for the explicit edits the owner asks for (sidecars,
image files when enabled, digiKam's database while digiKam is closed), which
are backed up first and can be locked out with READ_ONLY=1.
"""
import os
import re
from pathlib import Path

from iosuite_shared import config as shared

APP = "iopix"
APP_DIR = Path(__file__).resolve().parent.parent
_PROCESS_ENV = dict(os.environ)  # what the service or container set, before config.env adds to it (see from_environment)
CONFIG_FILE = shared.load_file(APP_DIR)  # first: iosuite_shared reads its settings (the Shared ioSuite database, COOKIE_DOMAIN) from it too
env, env_flag, env_int = shared.env, shared.env_flag, shared.env_int
# APP_DIR, DATA_DIR, STATIC_DIR, LOG_FILE, HOST, PORT, PUBLIC_URL, BASE_URL, FORWARDED_ALLOW_IPS, TLS_CERT_FILE, TLS_KEY_FILE,
# MCP_FORCED, ADMIN_USER, ADMIN_PASSWORD: the same names in every app (iosuite_shared.config.standard says what each is)
_std = shared.standard(APP, APP_DIR)
DATA_DIR, STATIC_DIR, LOG_FILE, HOST, PORT = _std["DATA_DIR"], _std["STATIC_DIR"], _std["LOG_FILE"], _std["HOST"], _std["PORT"]
PUBLIC_URL, BASE_URL, FORWARDED_ALLOW_IPS = _std["PUBLIC_URL"], _std["BASE_URL"], _std["FORWARDED_ALLOW_IPS"]
TLS_CERT_FILE, TLS_KEY_FILE, MCP_FORCED, ADMIN_USER, ADMIN_PASSWORD = _std["TLS_CERT_FILE"], _std["TLS_KEY_FILE"], _std["MCP_FORCED"], _std["ADMIN_USER"], _std["ADMIN_PASSWORD"]


def _path(name: str, default: Path) -> Path:
    return Path(env(name, str(default))).expanduser().resolve()


DIGIKAM_DB_DIR = _path("DIGIKAM_DB_DIR", Path.home() / "Pictures" / "digikamDB")
DIGIKAM_DB = DIGIKAM_DB_DIR / "digikam4.db"
# Used when the library has a single album root; otherwise digiKam's own
# per-root paths are used.
PHOTOS_ROOT = _path("PHOTOS_ROOT", Path.home() / "Pictures" / "photos")

INDEX_DB = DATA_DIR / "index.db"
CACHE_DIR = DATA_DIR / "cache"

# Grid thumbnails: fixed height, width follows the aspect ratio up to a cap.
THUMB_HEIGHT = env_int("THUMB_HEIGHT", 360)
THUMB_MAX_WIDTH = env_int("THUMB_MAX_WIDTH", 1080)
THUMB_QUALITY = env_int("THUMB_QUALITY", 76)
# Lightbox previews: longest edge.
PREVIEW_SIZE = env_int("PREVIEW_SIZE", 2560)
PREVIEW_QUALITY = env_int("PREVIEW_QUALITY", 86)
# Previews are only made for photos you open; the oldest are pruned at startup
# once the folder grows past this many megabytes.
PREVIEW_CACHE_MAX_MB = env_int("PREVIEW_CACHE_MAX_MB", 8000)

# Build all grid thumbnails in the background after startup.
PREWARM = env_flag("PREWARM", True)
# cores the one running background job may use (Settings can change it); half the machine by default
def cpu_count() -> int:
    """The cores this process may use: the container's share under Docker/LXC, not the host's count."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return os.cpu_count() or 4


BG_THREADS = int(os.environ.get("BG_THREADS", max(1, cpu_count() // 2)))
PREWARM_WORKERS = int(os.environ["PREWARM_WORKERS"]) if os.environ.get("PREWARM_WORKERS") else None  # None: BG_THREADS

# How often to check whether digiKam changed its database (seconds).
REINDEX_POLL = env_int("REINDEX_POLL", 20)

for _d in (DATA_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
for _p in (DIGIKAM_DB_DIR, PHOTOS_ROOT):
    if DATA_DIR == _p or _p in DATA_DIR.parents:
        raise SystemExit(f"DATA_DIR ({DATA_DIR}) must not be inside the read-only library folder {_p}")
# AUTO_MODELS: models to download on first start when missing, e.g. "siglip2,yolox,geonames"
# (keys as in app/ai/models.py). Containers and installers set it; the Mac service leaves it empty.
AUTO_MODELS = [k.strip() for k in os.environ.get("AUTO_MODELS", "").split(",") if k.strip()]
# READ_ONLY=1 locks the viewer to browsing: no edits, tags, faces or database writes, whatever the settings say
READ_ONLY = env_flag("READ_ONLY")
# Allow metadata edits (XMP sidecars; image files too when the write_files setting is on).
EDITING = env_flag("EDITING", True)
# Allow "Apply to digiKam database" (only ever runs on request, and only while digiKam is closed).
DB_WRITE = env_flag("DB_WRITE", True)
SEMANTIC = env_flag("SEMANTIC", True)
ANALYSIS = env_flag("ANALYSIS", True)
# Threads for sharpness scoring and object detection; by default the shared BG_THREADS budget.
ANALYSIS_WORKERS = int(os.environ["ANALYSIS_WORKERS"]) if os.environ.get("ANALYSIS_WORKERS") else None  # None: BG_THREADS
# How to tell whether digiKam is running before writing its database: "pgrep" looks for the
# process on this machine; "none" (Docker, a container, digiKam on another machine: it cannot be
# seen) does not check.
DIGIKAM_CHECK = env("DIGIKAM_CHECK", "pgrep").strip().lower()
# Describe every photo in a sentence with a local captioning model (BLIP base), for search.
CAPTIONS = env_flag("CAPTIONS", True)


# Folders that sync tools, NAS systems and operating systems keep beside the photos: their contents (GoodSync's
# backup copies in _gsdata_/_saved_, a Synology's @eaDir thumbnails, recycle bins...) are not the library. iopix
# leaves them out of every listing, the index and its jobs, and its file scanner does not go into them; nothing in
# them is changed or deleted. Folders whose name starts with a dot are skipped as well.
SYSTEM_DIRS = frozenset({"_gsdata_", "@eaDir", "#recycle", "@Recycle", "@Recently-Snapshot", "#snapshot", "lost+found",
                         "$RECYCLE.BIN", "System Volume Information"})


def is_system_folder(rel: str) -> bool:
    """A library-relative folder path ("/2017/_gsdata_/_saved_") inside one of SYSTEM_DIRS or a dot folder."""
    return any(seg in SYSTEM_DIRS or seg.startswith(".") for seg in (rel or "").split("/") if seg)


# --------------------------------------------------------------------------
# The other settings the app honours, read once here so config.env is documented in one
# place. The functions at the end are the few an admin (or the test suite) may change while the
# process runs; they read the environment on every call.
# --------------------------------------------------------------------------
# digiKam's database (dkdb.py): the environment is the starting point, Settings › digiKam database can override it
DIGIKAM_DB_TYPE = os.environ.get("DIGIKAM_DB_TYPE", "sqlite").lower()  # sqlite, mysql, or none (no digiKam at all)
DIGIKAM_DB_HOST = os.environ.get("DIGIKAM_DB_HOST", "127.0.0.1")
DIGIKAM_DB_PORT = os.environ.get("DIGIKAM_DB_PORT", "3306")
DIGIKAM_DB_NAME = os.environ.get("DIGIKAM_DB_NAME", "digikam")
DIGIKAM_DB_USER = os.environ.get("DIGIKAM_DB_USER", "digikam")
DIGIKAM_DB_PASSWORD = os.environ.get("DIGIKAM_DB_PASSWORD", "")
# auto (a full copy before destructive operations) or skip (never)
DIGIKAM_DB_BACKUP = os.environ.get("DIGIKAM_DB_BACKUP", "auto")
# a command that writes a full MySQL dump to stdout, used instead of mysqldump (e.g. through docker exec)
DIGIKAM_DB_DUMP = os.environ.get("DIGIKAM_DB_DUMP", "").strip()
# the GitHub repository and branch update.sh follows and the update check compares against
IOPIX_REPO = env("IOPIX_REPO") or "avandeputte/iopix"
IOPIX_BRANCH = env("IOPIX_BRANCH") or "main"
MCP_USER = env("MCP_USER")  # the account `python -m app.mcp` (stdio) acts as
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")  # the browser-facing key, when not entered in Settings
GOOGLE_SERVER_KEY = os.environ.get("GOOGLE_SERVER_KEY", "")  # the key for the APIs this server calls, when not entered in Settings
SCANNER_LOOP = os.environ.get("SCANNER_LOOP", "1") != "0"  # SCANNER_LOOP=0 (the test suite): file scanner passes only on request
EXIFTOOL = os.environ.get("EXIFTOOL")  # a particular exiftool binary; else the one on PATH or digiKam's bundled copy


# UPDATE_CHECK=0 (the daily version check locked off) is read by iosuite_shared.updates (app.auth.updater)


def sidecar_compat_env() -> bool:
    """SIDECAR_COMPAT=1: new sidecars are named photo.xmp (digiKam's "compatible file name" setting)."""
    return os.environ.get("SIDECAR_COMPAT", "").strip() in ("1", "true", "yes")


def mysql_dates_env() -> str:
    """DIGIKAM_MYSQL_DATES: "utc" or "local" settles how a MySQL library stores its dates; empty: detected."""
    return os.environ.get("DIGIKAM_MYSQL_DATES", "").strip().lower()


def proxy_env(name: str):
    """One of iosuite_shared.proxyauth.KEYS (AUTH_PROXY, CF_ACCESS_TEAM...) from config.env, or None when not set there."""
    v = os.environ.get(name)
    return v if v not in (None, "") else None


# the names of iopix's own database settings in config.env, by vdb.KEYS
DB_ENV = {"type": "DB_TYPE", "host": "DB_HOST", "port": "DB_PORT", "database": "DB_NAME", "user": "DB_USER", "password": "DB_PASSWORD"}


def db_env(key: str):
    """DB_TYPE/HOST/PORT/NAME/USER/PASSWORD: iopix's own database (config.env, or the environment)."""
    return os.environ.get(DB_ENV[key]) or None


def file_values(path: Path = None) -> dict:
    """The KEY=value lines of config.env as they are on disk now (read the way iosuite_shared.config reads them)."""
    out = {}
    try:
        lines = Path(path or CONFIG_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = (x.strip() for x in line.split("=", 1))
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k:
                out[k] = v
    return out


def from_environment(name: str) -> bool:
    """Whether the service's or container's environment sets `name` to something config.env does not say (a
    docker-compose `environment:` entry, for instance): that value wins, so writing config.env would not change it.
    A systemd unit that reads config.env (EnvironmentFile) sets the file's own values, which does not count."""
    v = _PROCESS_ENV.get(name)
    return v not in (None, "") and file_values().get(name) != v


def _quoted(v: str) -> str:
    """A value as config.env holds it, read back the same by iosuite_shared and by systemd's EnvironmentFile: as it
    is when plain, else in single quotes, or in double quotes when it holds a single quote itself."""
    if "\n" in v or "\r" in v:
        raise ValueError("A setting cannot span lines")
    if not re.search(r"[\\'\"]", v) and v == v.strip():
        return v
    if "'" not in v:
        return f"'{v}'"
    if '"' not in v and "\\" not in v:
        return f'"{v}"'
    raise ValueError("A value with a single quote cannot also hold double quotes or backslashes")


def save_values(values: dict, path: Path = None):
    """Write KEY=value settings into config.env: each replaces its line (or a commented-out `# KEY=` line), the
    rest is added at the end; comments and every other line stay. The file stays readable by its owner only, and
    the environment of this process follows, until the restart that reads the file again. Through a symlink (the
    Docker image keeps config.env in the data volume), the file it points at is written."""
    p = Path(path or CONFIG_FILE).resolve()
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    todo = {k: _quoted(str(v)) for k, v in values.items()}
    for i, line in enumerate(lines):  # the active lines first, so a commented example further up is left alone
        m = re.match(r"\s*(export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(2) in todo:
            k = m.group(2)
            lines[i] = f"{k}={todo.pop(k)}" if k in todo else None
    lines = [x for x in lines if x is not None]  # a key on two lines: the first one is kept
    for i, line in enumerate(lines):
        m = re.match(r"\s*#\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in todo:
            lines[i] = f"{m.group(1)}={todo.pop(m.group(1))}"
    lines += [f"{k}={v}" for k, v in todo.items()]
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    for k, v in values.items():
        os.environ[k] = str(v)
