#!/bin/sh
# iopix setup: one interactive script that asks everything and does all the work.
#
#   ./setup.sh                 ask the questions, then install packages, download models,
#                              write the settings, create the admin and start the service
#   ./setup.sh --yes ...       no questions: defaults and the flags below
#
# Flags (each one skips its question): --photos DIR  --sqlite DIR | --mysql HOST:PORT/DB:USER:PASS
#   --features search,places,objects,ocr,captions  --search-model clip|siglip2  --port N
#   --admin NAME:PASSWORD | --no-admin   --service | --no-service   --data DIR   --python PATH
#   --system-deps | --no-system-deps   install (or skip) python3, exiftool, ffmpeg and the MariaDB client with brew/apt/dnf/zypper/pacman
# Run it again at any time: it keeps what is installed and only adds what you choose.
set -e
cd "$(dirname "$0")"
APP="$(pwd)"
OS="$(uname -s)"
say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
YES=0; PHOTOS=""; SQLITE=""; MYSQL=""; FEATURES=""; SEARCH_MODEL=""; PORT=""; ADMIN=""; NO_ADMIN=0; SERVICE=""; DATA=""; PY=""; SYSDEPS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1; shift;; --photos) PHOTOS="$2"; shift 2;; --sqlite) SQLITE="$2"; shift 2;; --mysql) MYSQL="$2"; shift 2;;
    --features) FEATURES="$2"; shift 2;; --search-model) SEARCH_MODEL="$2"; shift 2;; --port) PORT="$2"; shift 2;;
    --admin) ADMIN="$2"; shift 2;; --no-admin) NO_ADMIN=1; shift;; --service) SERVICE=1; shift;; --no-service) SERVICE=0; shift;;
    --data) DATA="$2"; shift 2;; --python) PY="$2"; shift 2;; --system-deps) SYSDEPS=1; shift;; --no-system-deps) SYSDEPS=0; shift;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown option $1 (try --help)";;
  esac
done
# questions are read from standard input: the terminal, or piped answers (one per line); --yes takes every default
if [ "$YES" = 1 ]; then TTY=""; else TTY=/dev/stdin; fi
ask() {  # ask VAR "question" "default"
  _v="$1"; _q="$2"; _d="$3"
  if [ "$YES" = 1 ] || [ -z "$TTY" ]; then eval "$_v=\"\$_d\""; return; fi
  printf '%s [%s]: ' "$_q" "$_d"; read -r _a < "$TTY" || _a=""
  [ -n "$_a" ] && eval "$_v=\"\$_a\"" || eval "$_v=\"\$_d\""
}
yesno() {  # yesno "question" y|n -> returns 0 for yes
  _d="$2"
  if [ "$YES" = 1 ] || [ -z "$TTY" ]; then [ "$_d" = y ]; return; fi
  printf '%s [%s]: ' "$1" "$([ "$_d" = y ] && echo Y/n || echo y/N)"; read -r _a < "$TTY" || _a=""
  case "${_a:-$_d}" in y|Y|yes|YES) return 0;; *) return 1;; esac
}
secret() {  # secret VAR "question"
  if [ -z "$TTY" ]; then eval "$1=\"\""; return; fi
  printf '%s: ' "$2"; stty -echo 2>/dev/null < "$TTY" || true; read -r _a < "$TTY" || _a=""; stty echo 2>/dev/null < "$TTY" || true; echo; eval "$1=\"\$_a\""
}

say "iopix setup"
echo "This asks a few questions, installs the Python packages, downloads the AI models you choose, writes the"
echo "settings and starts the viewer as a service. Nothing in your photo library or digiKam database is changed."

# --- system packages: python3, exiftool, ffmpeg ---------------------------------------------------
pyok() { [ -n "$1" ] && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }
findpy() { for c in python3.13 python3.12 python3.11 python3.10 python3; do p="$(command -v $c 2>/dev/null || true)"; pyok "$p" && { echo "$p"; return 0; }; done; return 0; }  # prints nothing when none qualifies (never fails under set -e)
[ -n "$PY" ] || PY="$(findpy)"
MISSING=""
pyok "$PY" || MISSING="$MISSING python3"
command -v exiftool >/dev/null 2>&1 || MISSING="$MISSING exiftool"
command -v ffmpeg >/dev/null 2>&1 || MISSING="$MISSING ffmpeg"
command -v jpegtran >/dev/null 2>&1 || [ -x /opt/homebrew/bin/jpegtran ] || [ -x /opt/local/bin/jpegtran ] || MISSING="$MISSING jpegtran"
command -v mysqldump >/dev/null 2>&1 || command -v mariadb-dump >/dev/null 2>&1 || [ -x /opt/homebrew/opt/mysql-client/bin/mysqldump ] || MISSING="$MISSING mariadb-client"
if [ -n "$MISSING" ]; then
  say "System packages"
  echo "Missing:$MISSING (python3 3.10+ runs the viewer; exiftool edits metadata; ffmpeg makes video thumbnails; jpegtran (libjpeg-turbo) rotates and crops JPEGs without loss; mariadb-client backs a MySQL/MariaDB digiKam database up before writes)."
  if [ -z "$SYSDEPS" ]; then yesno "Install them now?" y && SYSDEPS=1 || SYSDEPS=0; fi
  if [ "$SYSDEPS" = 1 ]; then
    SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
    case "$OS" in
      Darwin)
        if ! command -v brew >/dev/null 2>&1; then
          if yesno "Homebrew is not installed; install it (https://brew.sh)?" y; then
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || die "Homebrew install failed"
            eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
          else die "install python3 (3.10+), exiftool and ffmpeg yourself, then run setup again"; fi
        fi
        PKGS=""; case "$MISSING" in *python3*) PKGS="$PKGS python@3.12";; esac
        case "$MISSING" in *exiftool*) PKGS="$PKGS exiftool";; esac; case "$MISSING" in *ffmpeg*) PKGS="$PKGS ffmpeg";; esac; case "$MISSING" in *jpegtran*) PKGS="$PKGS jpeg-turbo";; esac; case "$MISSING" in *mariadb-client*) PKGS="$PKGS mysql-client";; esac
        echo "brew install$PKGS"; brew install $PKGS
        [ -x /opt/homebrew/opt/python@3.12/bin/python3.12 ] && PY=/opt/homebrew/opt/python@3.12/bin/python3.12 || PY="$(findpy)";;
      *)
        if command -v apt-get >/dev/null 2>&1; then
          echo "apt-get install python3 python3-venv python3-pip libimage-exiftool-perl ffmpeg libjpeg-turbo-progs libheif1 libglib2.0-0 mariadb-client"
          $SUDO env DEBIAN_FRONTEND=noninteractive sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 python3-venv python3-pip libimage-exiftool-perl ffmpeg libjpeg-turbo-progs libheif1 libglib2.0-0 mariadb-client ca-certificates curl >/dev/null'
        elif command -v dnf >/dev/null 2>&1; then
          echo "dnf install python3 python3-pip perl-Image-ExifTool ffmpeg libheif"
          $SUDO dnf install -y -q python3 python3-pip perl-Image-ExifTool libjpeg-turbo-utils libheif glib2 mariadb ca-certificates curl >/dev/null
          $SUDO dnf install -y -q ffmpeg-free >/dev/null 2>&1 || $SUDO dnf install -y -q ffmpeg >/dev/null 2>&1 || echo "ffmpeg not available from the enabled repositories (video thumbnails need it): enable RPM Fusion and install ffmpeg"
        elif command -v zypper >/dev/null 2>&1; then
          echo "zypper install python3 python3-pip ffmpeg exiftool libheif1"; $SUDO zypper -n -q install python3 python3-pip ffmpeg exiftool libjpeg-turbo libheif1 mariadb-client ca-certificates curl >/dev/null
        elif command -v pacman >/dev/null 2>&1; then
          echo "pacman -S python python-pip ffmpeg perl-image-exiftool libheif"; $SUDO pacman -Sy --noconfirm --needed python python-pip ffmpeg perl-image-exiftool libjpeg-turbo libheif mariadb-clients ca-certificates curl >/dev/null
        else
          die "no known package manager; install python3 (3.10+), exiftool and ffmpeg yourself, then run setup again"
        fi
        PY="$(findpy)";;
    esac
  else
    pyok "$PY" || die "python3 (3.10 or newer) is needed"
  fi
fi
pyok "$PY" || die "python3 (3.10 or newer) is needed (found ${PY:-none})"
"$PY" -c 'import venv' 2>/dev/null || die "the python3 venv module is missing (Debian/Ubuntu: apt install python3-venv)"
command -v exiftool >/dev/null 2>&1 || echo "note: exiftool is still not installed; metadata editing and the consistency checker will be unavailable"
command -v ffmpeg >/dev/null 2>&1 || echo "note: ffmpeg is still not installed; video thumbnails will be unavailable"

# --- the library ------------------------------------------------------------------------------------
say "Your library"
ask PHOTOS "Folder with the photos digiKam manages" "${PHOTOS:-$HOME/Pictures/photos}"
[ -d "$PHOTOS" ] || die "folder not found: $PHOTOS"
if [ -z "$MYSQL" ] && [ -z "$SQLITE" ]; then
  if yesno "Is digiKam's database on MySQL/MariaDB (no: SQLite file digikam4.db)?" n; then
    ask MH "MySQL host" "127.0.0.1"; ask MP "port" "3306"; ask MD "database" "digikam"; ask MU "user" "digikam"; secret MW "password"
    MYSQL="$MH:$MP/$MD:$MU:$MW"
  else
    ask SQLITE "Folder containing digikam4.db" "$HOME/Pictures/digikamDB"
  fi
fi
[ -z "$SQLITE" ] || [ -f "$SQLITE/digikam4.db" ] || die "digikam4.db not found in $SQLITE"
ask DATA "Folder for the viewer's own data (index, thumbnails, models, backups)" "${DATA:-$APP/data}"
ask PORT "Port" "${PORT:-8600}"

# --- features ---------------------------------------------------------------------------------------
say "Features (each one downloads a model into $DATA/models)"
if [ -z "$FEATURES" ]; then
  F=""
  yesno "Content search: find photos by what is in them (SigLIP 2, ~1.5 GB; or CLIP, ~600 MB)" y && F="$F,search"
  yesno "Places: town/region/country for GPS positions, offline (GeoNames, ~30 MB)" y && F="$F,places"
  yesno "Object detection: people, animals, vehicles... (YOLOX, 35 MB)" y && F="$F,objects"
  yesno "Text in photos: signs, jerseys, documents (RapidOCR, ~60 MB)" y && F="$F,ocr"
  yesno "Photo descriptions: one sentence per photo (BLIP + PyTorch, ~1.5 GB, slow without a GPU)" n && F="$F,captions"
  FEATURES="${F#,}"
fi
has() { case ",$FEATURES," in *",$1,"*) return 0;; *) return 1;; esac; }
if has search && [ -z "$SEARCH_MODEL" ]; then
  if yesno "Use SigLIP 2 for content search (slightly better; CLIP indexes 4x faster)?" y; then SEARCH_MODEL=siglip2; else SEARCH_MODEL=clip; fi
fi

# --- admin and service ------------------------------------------------------------------------------
say "Accounts and service"
if [ -z "$ADMIN" ] && [ "$NO_ADMIN" = 0 ]; then
  if yesno "Create an admin account now (visitors then browse read-only; editors and admins sign in)?" y; then
    ask AN "Admin user name" "admin"; secret AW "Admin password (8 characters or more)"
    [ -n "$AW" ] && ADMIN="$AN:$AW" || echo "no password given; skipping the admin account (Settings can create it later)"
  fi
fi
if [ -z "$SERVICE" ]; then
  case "$OS" in Darwin) SVC="a launchd login item";; *) SVC="a systemd service";; esac
  yesno "Start the viewer at login as $SVC?" y && SERVICE=1 || SERVICE=0
fi

# --- do the work ------------------------------------------------------------------------------------
say "Python environment"
VENV="$APP/.venv"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
VPY="$VENV/bin/python"
pipi() { if command -v uv >/dev/null 2>&1; then uv pip install -q -p "$VPY" "$@"; else "$VPY" -m pip install -q "$@"; fi; }
"$VPY" -m pip --version >/dev/null 2>&1 || command -v uv >/dev/null 2>&1 || "$VPY" -m ensurepip -q
G=""; has search && G="$G,search"; has objects && G="$G,objects"; has ocr && G="$G,ocr"
G="${G#,}"; echo "installing the packages (pyproject.toml${G:+, groups: $G})"; if [ -n "$G" ]; then pipi -e ".[$G]"; else pipi -e .; fi
has captions && { echo "installing PyTorch and transformers (large)"; pipi --extra-index-url https://download.pytorch.org/whl/cpu -e ".[captions]"; }

say "Settings"
ENVF="$APP/iopix.env"
{
  echo "# iopix settings, written by setup.sh (variable names as in the README). Edit and restart the service."
  echo "PHOTOS_ROOT=$PHOTOS"
  if [ -n "$MYSQL" ]; then
    hp=${MYSQL%%/*}; rest=${MYSQL#*/}; db=${rest%%:*}; rest=${rest#*:}; us=${rest%%:*}; pw=${rest#*:}
    echo "DIGIKAM_DB_TYPE=mysql"; echo "DIGIKAM_DB_HOST=${hp%%:*}"; echo "DIGIKAM_DB_PORT=${hp##*:}"; echo "DIGIKAM_DB_NAME=$db"; echo "DIGIKAM_DB_USER=$us"; echo "DIGIKAM_DB_PASSWORD=$pw"
  else
    echo "DIGIKAM_DB_DIR=$SQLITE"
  fi
  echo "DATA_DIR=$DATA"
  echo "HF_HUB_OFFLINE=1"
  echo "CAPTIONS=$(has captions && echo 1 || echo 0)"
  M=""; has search && M="$M,$SEARCH_MODEL"; has objects && M="$M,yolox"; has places && M="$M,geonames"; has captions && M="$M,blip"
  echo "# models fetched when missing at start (setup downloads them now)"; echo "AUTO_MODELS=${M#,}"
} > "$ENVF"
chmod 600 "$ENVF"
mkdir -p "$DATA"
# load what was just written for the commands below (not sourced: a path with spaces would break `. file`)
while IFS= read -r line; do case "$line" in ''|'#'*|*[!A-Za-z0-9_]*=*) ;; *=*) export "$line";; esac; done < "$ENVF"
echo "wrote $ENVF"
if [ -n "$MYSQL" ]; then
  echo "testing the database connection"
  "$VPY" -c "from app import dkdb; r = dkdb.test_connection(); print(f\"  OK: {r['images']} photos, {r['tags']} tags, DBVersion {r['dbVersion']}, root {r['roots']}\")" || die "could not connect to the database with those settings"
fi

say "Models"
for key in $(echo "$AUTO_MODELS" | tr ',' ' '); do
  echo "downloading $key"
  HF_HUB_OFFLINE=0 "$VPY" -m app.ai.models download "$key" || echo "  download of $key failed; retry later from Maintenance > Models and data"
done
[ -n "$SEARCH_MODEL" ] && "$VPY" -c "from app import state; state.init(); state.set_setting('search_model', '$SEARCH_MODEL')"

if [ -n "$ADMIN" ]; then
  say "Admin account"
  "$VPY" -c "
import sys; from app import state, users
state.init(); users.init()
name, pw = sys.argv[1].split(':', 1)
print('  created', users.create(name, pw, 'admin')['name']) if not users.any_users() else print('  users already exist; not touched')
" "$ADMIN"
fi

say "Service"
if [ "$SERVICE" = 1 ] && [ "$OS" = Darwin ]; then
  PL="$HOME/Library/LaunchAgents/com.iopix.plist"
  "$VPY" - "$PL" "$APP" "$VPY" "$PORT" "$ENVF" <<'PYEOF'
import plistlib, sys, os
pl, app, vpy, port, envf = sys.argv[1:6]
env = {"PATH": "/opt/homebrew/opt/mysql-client/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}  # mysql-client is keg-only
for line in open(envf):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k] = v
d = {"Label": "com.iopix", "ProgramArguments": [vpy, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)],
     "WorkingDirectory": app, "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "EnvironmentVariables": env,
     "StandardOutPath": os.path.join(env["DATA_DIR"], "server.log"), "StandardErrorPath": os.path.join(env["DATA_DIR"], "server.log")}
os.makedirs(os.path.dirname(pl), exist_ok=True)
with open(pl, "wb") as f: plistlib.dump(d, f)
print("  wrote", pl)
PYEOF
  launchctl bootout "gui/$(id -u)/com.iopix" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/com.digikam-viewer" 2>/dev/null && rm -f "$HOME/Library/LaunchAgents/com.digikam-viewer.plist"  # the name before the rename
  launchctl bootstrap "gui/$(id -u)" "$PL" && echo "  started (login item)"
elif [ "$SERVICE" = 1 ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  UNIT=/etc/systemd/system/iopix.service
  if [ "$(id -u)" = 0 ]; then SUDO=""; else SUDO="sudo"; fi
  $SUDO sh -c "cat > $UNIT" <<UEOF
[Unit]
Description=iopix
After=network.target

[Service]
Type=simple
User=$(id -un)
EnvironmentFile=$ENVF
WorkingDirectory=$APP
ExecStart=$VPY -m uvicorn app.main:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
UEOF
  if [ -f /etc/systemd/system/digikam-viewer.service ]; then  # the service under its name before the rename
    $SUDO systemctl disable --now digikam-viewer 2>/dev/null || true; $SUDO rm -f /etc/systemd/system/digikam-viewer.service
  fi
  $SUDO systemctl daemon-reload && $SUDO systemctl enable --now iopix && echo "  started (systemd)"
else
  echo "  not installed as a service. Start the viewer with:"
  echo "    cd $APP && set -a && . $ENVF && set +a && $VPY -m uvicorn app.main:app --host 0.0.0.0 --port $PORT"
fi
say "Done: http://localhost:$PORT   settings: $ENVF   data: $DATA"
