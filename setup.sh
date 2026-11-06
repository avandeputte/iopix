#!/bin/sh
# iopix setup: one interactive script that asks everything and does all the work.
#
#   ./setup.sh                 ask the questions, then install packages, download models,
#                              write the settings, create the admin and start the service
#   ./setup.sh --yes ...       no questions: defaults and the flags below
#
# Flags (the same in every app of the suite; each one skips its question):
#   --db local | HOST:PORT/DB:USER:PASSWORD   iopix's own database (settings, favorites, albums, AI results...) is
#     always on MySQL/MariaDB: "local" installs mariadb-server on this machine with apt (Debian, Ubuntu, Proxmox; run
#     as root) and creates the database `iopix` and a user with a random password; otherwise the details of an
#     existing server; the database is created when the user may. Written as DB_* to config.env; a later run keeps
#     them. Without any, the viewer's set-up page asks.
#   --suite-db HOST:PORT/DB:USER:PASSWORD  the Shared ioSuite database (accounts, sessions; default database and
#     user: iosuite, on the --db server); kept as it is in config.env when not given
#   --admin NAME:PASS | --no-admin   --port 8600   --public-url URL   --cookie-domain DOMAIN
#   --service | --no-service   --python PATH   --system-deps | --no-system-deps   --yes
#   --also APP,APP | all | none   also install other apps of the suite here (iosuite, iocalendar, iocontacts, ionotes, iohealth), beside this one,
#                                 with the same database server (each app its own database), Shared ioSuite database,
#                                 sign-in domain and admin: those questions are asked once (IOPIX_ALSO does the same)
# iopix's own: --photos DIR   --data DIR   --features search,places,objects,ocr,captions   --search-model clip|siglip2
#   --digikam-db HOST:PORT/DB:USER:PASS | --digikam-sqlite DIR | --no-digikam   digiKam's database (--no-digikam: iopix
#   keeps the library itself and finds the photos in the folder)
# Secrets can stay off the command line (process lists show arguments): IOPIX_DB, IOPIX_SUITE_DB, IOPIX_ADMIN and
#   IOPIX_DIGIKAM_DB in the environment do what the flags do; a value given without its password part takes it from
#   IOPIX_DB_PASSWORD / IOPIX_DIGIKAM_DB_PASSWORD. The flags win when both are given.
# Run it again at any time: it keeps what is installed and only adds what you choose.
set -e
cd "$(dirname "$0")"
APP="$(pwd)"
OS="$(uname -s)"
say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
YES=0; PHOTOS=""; SQLITE=""; MYSQL=""; NODK=0; VIEWERDB=""; SUITE=""; FEATURES=""; SEARCH_MODEL=""; PORT=""; PUBURL=""; COOKIE=""; ADMIN=""; NO_ADMIN=0; SERVICE=""; DATA=""; PY=""; SYSDEPS=""; ALSO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1; shift;; --photos) PHOTOS="$2"; shift 2;; --digikam-sqlite) SQLITE="$2"; shift 2;; --digikam-db) MYSQL="$2"; shift 2;; --no-digikam) NODK=1; shift;; --db) VIEWERDB="$2"; shift 2;; --suite-db) SUITE="$2"; shift 2;;
    --features) FEATURES="$2"; shift 2;; --search-model) SEARCH_MODEL="$2"; shift 2;; --port) PORT="$2"; shift 2;; --public-url) PUBURL="$2"; shift 2;; --cookie-domain) COOKIE="$2"; shift 2;;
    --admin) ADMIN="$2"; shift 2;; --no-admin) NO_ADMIN=1; shift;; --service) SERVICE=1; shift;; --no-service) SERVICE=0; shift;;
    --data) DATA="$2"; shift 2;; --python) PY="$2"; shift 2;; --also) ALSO="$2"; shift 2;; --system-deps) SYSDEPS=1; shift;; --no-system-deps) SYSDEPS=0; shift;;
    -h|--help) sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown option $1 (try --help)";;
  esac
done
# the same answers from the environment, for installers that would rather not put passwords on a command line
[ -n "$ADMIN" ] || ADMIN="${IOPIX_ADMIN:-}"
[ -n "$ALSO" ] || ALSO="${IOPIX_ALSO:-}"
[ -n "$MYSQL" ] || MYSQL="${IOPIX_DIGIKAM_DB:-}"
[ -n "$VIEWERDB" ] || VIEWERDB="${IOPIX_DB:-}"
[ -n "$SUITE" ] || SUITE="${IOPIX_SUITE_DB:-}"
[ "$VIEWERDB" != sqlite ] || die "iopix's own database is on MySQL/MariaDB only (no SQLite file): --db local installs MariaDB here, or give HOST:PORT/DB:USER:PASS"
if [ -n "$VIEWERDB" ] && [ "$VIEWERDB" != local ] && [ -n "${IOPIX_DB_PASSWORD:-}" ]; then
  case "${VIEWERDB#*/}" in *:*:*) ;; *) VIEWERDB="$VIEWERDB:$IOPIX_DB_PASSWORD";; esac
fi
if [ -n "$MYSQL" ] && [ -n "${IOPIX_DIGIKAM_DB_PASSWORD:-}" ]; then
  case "${MYSQL#*/}" in *:*:*) ;; *) MYSQL="$MYSQL:$IOPIX_DIGIKAM_DB_PASSWORD";; esac  # DB:USER given, the password comes from the environment
fi
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
ask PHOTOS "Folder with your photos" "${PHOTOS:-$HOME/Pictures/photos}"
[ -d "$PHOTOS" ] || die "folder not found: $PHOTOS"
if [ "$NODK" = 0 ] && [ -z "$MYSQL" ] && [ -z "$SQLITE" ] && [ "$YES" = 0 ] && [ -n "$TTY" ]; then
  yesno "Do you use digiKam with these photos (no: iopix keeps the library itself and finds the photos in the folder)?" y || NODK=1
fi
[ "$NODK" = 0 ] || { MYSQL=""; SQLITE=""; echo "No digiKam: iopix starts its own library on $PHOTOS and its file scanner finds the photos (new ones too)."; }
if [ "$NODK" = 0 ] && [ -z "$MYSQL" ] && [ -z "$SQLITE" ]; then
  if yesno "Is digiKam's database on MySQL/MariaDB (no: SQLite file digikam4.db)?" n; then
    ask MH "MySQL host" "127.0.0.1"; ask MP "port" "3306"; ask MD "database" "digikam"; ask MU "user" "digikam"; secret MW "password"
    MYSQL="$MH:$MP/$MD:$MU:$MW"
  else
    ask SQLITE "Folder containing digikam4.db" "$HOME/Pictures/digikamDB"
  fi
fi
[ -z "$SQLITE" ] || [ -f "$SQLITE/digikam4.db" ] || die "digikam4.db not found in $SQLITE"
ask DATA "Folder for the viewer's own data (index, thumbnails, models, backups)" "${DATA:-$APP/data}"
# iopix's own database (settings, accounts, favorites, albums, AI results, and the library without digiKam) lives on
# MySQL/MariaDB. Asked once: a later run keeps the DB_* lines of config.env (or data/viewer-db.json, written by
# the Settings page).
ENVF="$APP/config.env"
OLDVDB="$(grep '^DB_' "$ENVF" 2>/dev/null || true)"
CANLOCAL=0; [ "$OS" != Darwin ] && [ "$(id -u)" = 0 ] && command -v apt-get >/dev/null 2>&1 && CANLOCAL=1  # Debian, Ubuntu, Proxmox as root
if [ -z "$VIEWERDB" ] && [ -z "$OLDVDB" ] && [ ! -f "$DATA/viewer-db.json" ]; then
  if [ "$CANLOCAL" = 1 ]; then
    ask VH "iopix's own database: 'local' installs MariaDB on this machine, or the host of an existing MySQL/MariaDB server" "local"
    [ "$VH" != local ] || VIEWERDB=local
  elif [ "$YES" = 1 ] || [ -z "$TTY" ]; then
    echo "note: no --db given: iopix's own database is not configured yet; the viewer's set-up page asks for a MySQL/MariaDB server"
  else
    echo "iopix keeps its own database (settings, accounts, favorites, albums, AI results) on MySQL/MariaDB; the database is created if the user may."
    if [ -n "$MYSQL" ]; then dhp=${MYSQL%%/*}; dh=${dhp%%:*}; else dh=127.0.0.1; fi
    ask VH "MySQL/MariaDB host for iopix's database" "$dh"
  fi
  if [ -z "$VIEWERDB" ] && [ -n "${VH:-}" ]; then
    if [ -n "$MYSQL" ]; then dhp=${MYSQL%%/*}; drest=${MYSQL#*/}; drest=${drest#*:}; dp=${dhp##*:}; du=${drest%%:*}; else dp=3306; du=iopix; fi
    ask VP "port" "$dp"; ask VD "database (created if the user may)" "iopix"; ask VU "user" "$du"; secret VW "password"
    VIEWERDB="$VH:$VP/$VD:$VU:$VW"
  fi
elif [ -z "$VIEWERDB" ]; then
  echo "iopix's own database: kept as configured ($([ -n "$OLDVDB" ] && echo "$ENVF" || echo "$DATA/viewer-db.json"); Settings › Database switches)"
fi
if [ "$VIEWERDB" = local ]; then
  [ "$CANLOCAL" = 1 ] || die "--db local needs apt (Debian, Ubuntu, Proxmox) and root; give --db HOST:PORT/DB:USER:PASS of a MySQL/MariaDB server instead"
  say "MariaDB for iopix's database"
  if ! command -v mariadbd >/dev/null 2>&1 && [ ! -x /usr/sbin/mariadbd ]; then
    echo "apt-get install mariadb-server"
    env DEBIAN_FRONTEND=noninteractive sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends mariadb-server >/dev/null' || die "could not install mariadb-server"
  fi
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then systemctl enable --now mariadb >/dev/null 2>&1 || true; else service mariadb start >/dev/null 2>&1 || true; fi
  VW="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24)"  # letters and digits only: safe in SQL, the env file and the DSN
  # root connects over the socket without a password (unix_socket authentication, the Debian default); the SQL goes
  # in on standard input, so the new password is not in the process list. A re-run sets a fresh password.
  mariadb <<SQL || die "could not create the database and user on the local MariaDB (is it running? systemctl status mariadb)"
CREATE DATABASE IF NOT EXISTS \`iopix\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'iopix'@'localhost' IDENTIFIED BY '$VW'; ALTER USER 'iopix'@'localhost' IDENTIFIED BY '$VW';
CREATE USER IF NOT EXISTS 'iopix'@'127.0.0.1' IDENTIFIED BY '$VW'; ALTER USER 'iopix'@'127.0.0.1' IDENTIFIED BY '$VW';
GRANT ALL PRIVILEGES ON \`iopix\`.* TO 'iopix'@'localhost'; GRANT ALL PRIVILEGES ON \`iopix\`.* TO 'iopix'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
  echo "  created the database iopix and the user iopix (random password) on this machine's MariaDB"
  VIEWERDB="127.0.0.1:3306/iopix:iopix:$VW"
fi
if [ -n "$VIEWERDB" ]; then  # a quick test with the command-line client, when there is one (the venv's pymysql tests again later and creates the database)
  vhp=${VIEWERDB%%/*}; vrest=${VIEWERDB#*/}; vrest=${vrest#*:}; VU=${vrest%%:*}; VW=${vrest#*:}; VH=${vhp%%:*}; VP=${vhp##*:}
  MCLI="$(command -v mariadb 2>/dev/null || command -v mysql 2>/dev/null || { [ -x /opt/homebrew/opt/mysql-client/bin/mysql ] && echo /opt/homebrew/opt/mysql-client/bin/mysql; } || true)"
  if [ -n "$MCLI" ]; then
    MYSQL_PWD="$VW" "$MCLI" --protocol=tcp -h "$VH" -P "$VP" -u "$VU" -e "SELECT 1" >/dev/null 2>&1 || die "could not connect to MySQL/MariaDB at $VH:$VP as $VU (checked with $MCLI); check the host, port, user and password"
    echo "  MySQL/MariaDB at $VH:$VP answers as $VU"
  fi
fi
# the Shared ioSuite database: accounts, sessions and the suite-wide settings, shared by every app of the suite
say "Shared ioSuite database"
SUITE_SET=0; grep -qE '^IOSUITE_DB_' "$ENVF" 2>/dev/null && SUITE_SET=1
if [ -z "$SUITE" ]; then
  if [ "$SUITE_SET" = 1 ] && ! yesno "config.env already names the Shared ioSuite database; change it?" n; then
    SUITE="keep"
  elif [ "$YES" = 1 ] || [ -z "$TTY" ]; then
    SUITE="keep"; [ "$SUITE_SET" = 1 ] || echo "not named: iopix uses the defaults (its own database's server, database and user iosuite); set IOSUITE_DB_* in config.env"
  else
    case "$VIEWERDB" in ''|local) SH=127.0.0.1;; *) SH="${VIEWERDB%%/*}"; SH="${SH%%:*}";; esac
    ask SH "Shared ioSuite database host" "$SH"; ask SP "port" "3306"; ask SD "database (created if the user may)" "iosuite"; ask SU "user" "iosuite"; secret SW "password"
    SUITE="$SH:$SP/$SD:$SU:$SW"
  fi
fi
getenv() { [ -f "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF" | tail -n 1; true; }
ask PORT "Port" "${PORT:-$(getenv PORT | grep . || echo 8600)}"
[ -n "$PUBURL" ] || ask PUBURL "Public address, e.g. https://photo.example.com (empty: as reached)" "$(getenv PUBLIC_URL)"
[ -n "$COOKIE" ] || ask COOKIE "Domain the sign-in is shared on, e.g. example.com (empty: this host only)" "$(getenv COOKIE_DOMAIN)"

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
if [ -z "$ALSO" ]; then
  say "The other apps of the suite can be installed here too, beside iopix, with the same database server (each app gets its own database), the same Shared ioSuite database, sign-in domain and admin"
  ask ALSO "Also install (iosuite, iocalendar, iocontacts, ionotes, iohealth; comma-separated, 'all', or 'none')" "none"
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
umask 077  # the settings file holds the database password: private from the first byte
# what a person (or an earlier run) put in config.env besides what this run writes is kept, at the end
WRITTEN="PHOTOS_ROOT DIGIKAM_DB_TYPE DIGIKAM_DB_HOST DIGIKAM_DB_PORT DIGIKAM_DB_NAME DIGIKAM_DB_USER DIGIKAM_DB_PASSWORD DIGIKAM_DB_DIR DATA_DIR PORT PUBLIC_URL COOKIE_DOMAIN HF_HUB_OFFLINE CAPTIONS AUTO_MODELS"
[ -n "$VIEWERDB" ] && WRITTEN="$WRITTEN DB_TYPE DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD"
case "$SUITE" in ''|keep) ;; *) WRITTEN="$WRITTEN IOSUITE_DB_HOST IOSUITE_DB_PORT IOSUITE_DB_NAME IOSUITE_DB_USER IOSUITE_DB_PASSWORD";; esac
KEPT=""
if [ -f "$ENVF" ]; then
  KEPT="$(while IFS= read -r line; do
    case "$line" in ''|'#'*) continue;; *=*) ;; *) continue;; esac
    case " $WRITTEN " in *" ${line%%=*} "*) continue;; esac
    printf '%s\n' "$line"
  done < "$ENVF")"
fi
{
  echo "# iopix settings, written by setup.sh (config.env.example lists them all). Edit and restart the service."
  echo "PHOTOS_ROOT=$PHOTOS"
  if [ "$NODK" = 1 ]; then
    echo "# no digiKam: the library lives in iopix's own database (Settings > digiKam database can point at one later)"
    echo "DIGIKAM_DB_TYPE=none"
  elif [ -n "$MYSQL" ]; then
    hp=${MYSQL%%/*}; rest=${MYSQL#*/}; db=${rest%%:*}; rest=${rest#*:}; us=${rest%%:*}; pw=${rest#*:}
    echo "DIGIKAM_DB_TYPE=mysql"; echo "DIGIKAM_DB_HOST=${hp%%:*}"; echo "DIGIKAM_DB_PORT=${hp##*:}"; echo "DIGIKAM_DB_NAME=$db"; echo "DIGIKAM_DB_USER=$us"; echo "DIGIKAM_DB_PASSWORD=$pw"
  else
    echo "DIGIKAM_DB_DIR=$SQLITE"
  fi
  echo "DATA_DIR=$DATA"
  echo "PORT=$PORT"
  echo "PUBLIC_URL=$PUBURL"
  echo "COOKIE_DOMAIN=$COOKIE"
  # iopix's own database (MySQL/MariaDB); these win over $DATA/viewer-db.json, which the Settings page writes
  if [ -n "$VIEWERDB" ]; then
    vhp=${VIEWERDB%%/*}; vrest=${VIEWERDB#*/}; vdb=${vrest%%:*}; vrest=${vrest#*:}; vus=${vrest%%:*}; vpw=${vrest#*:}
    echo "DB_TYPE=mysql"; echo "DB_HOST=${vhp%%:*}"; echo "DB_PORT=${vhp##*:}"; echo "DB_NAME=$vdb"; echo "DB_USER=$vus"; echo "DB_PASSWORD=$vpw"
  elif [ -z "$OLDVDB" ]; then
    echo "# DB_TYPE=mysql DB_HOST= DB_PORT=3306 DB_NAME= DB_USER= DB_PASSWORD=: iopix's own database, or $DATA/viewer-db.json (Settings)"
  fi
  case "$SUITE" in ''|keep) ;; *)
    shp=${SUITE%%/*}; srest=${SUITE#*/}; sdb=${srest%%:*}; srest=${srest#*:}; sus=${srest%%:*}; spw=${srest#*:}
    echo "IOSUITE_DB_HOST=${shp%%:*}"; echo "IOSUITE_DB_PORT=${shp##*:}"; echo "IOSUITE_DB_NAME=$sdb"; echo "IOSUITE_DB_USER=$sus"; echo "IOSUITE_DB_PASSWORD=$spw";;
  esac
  echo "HF_HUB_OFFLINE=1"
  echo "CAPTIONS=$(has captions && echo 1 || echo 0)"
  M=""; has search && M="$M,$SEARCH_MODEL"; has objects && M="$M,yolox"; has places && M="$M,geonames"; has captions && M="$M,blip"
  echo "# models fetched when missing at start (setup downloads them now)"; echo "AUTO_MODELS=${M#,}"
  [ -z "$KEPT" ] || printf '%s\n' "$KEPT"
} > "$ENVF.new"
mv "$ENVF.new" "$ENVF"
chmod 600 "$ENVF"
mkdir -p "$DATA"
# load what was just written for the commands below (not sourced: a path with spaces would break `. file`)
while IFS= read -r line; do k="${line%%=*}"; case "$line" in ''|'#'*) ;; *=*) case "$k" in ''|*[!A-Za-z0-9_]*) ;; *) export "$line";; esac;; esac; done < "$ENVF"  # the key decides; a value may hold = too
echo "wrote $ENVF"
if [ -n "$MYSQL" ]; then
  echo "testing the database connection"
  "$VPY" -c "from app.digikam import dkdb; r = dkdb.test_connection(); print(f\"  OK: {r['images']} photos, {r['tags']} tags, DBVersion {r['dbVersion']}, root {r['roots']}\")" || die "could not connect to the database with those settings"
fi

VDB_OK=0  # whether iopix's own database answers: the search-model setting and the admin account below need it
if [ -n "${DB_HOST:-}" ] || [ -f "$DATA/viewer-db.json" ]; then
  echo "iopix's own database on MySQL/MariaDB: testing, creating it if needed"
  "$VPY" - <<'PYEOF' && VDB_OK=1 || echo "  could not use that MySQL/MariaDB database for iopix (see above); run setup.sh again, or fix it later on the viewer's set-up page"
import sys
from app.data import vdb
c = vdb.settings()  # DB_* from config.env (exported above; the password is not in the process list), else viewer-db.json
try:
    vdb.test_connection(c)
except Exception as e:
    if "1049" not in str(e) and "Unknown database" not in str(e):
        raise SystemExit(f"  {e}")
    import pymysql  # the database is not there yet: create it, if this user may
    con = pymysql.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"], connect_timeout=8)
    try:
        con.cursor().execute(f"CREATE DATABASE IF NOT EXISTS `{c['database']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    except Exception as e2:
        raise SystemExit(f"  the database {c['database']} does not exist and {c['user']} may not create it ({e2}); create it, then run setup.sh again")
    finally:
        con.close()
    print(f"  created the database {c['database']}")
r = vdb.test_connection(c)
print(f"  OK: {r['version']}; iopix keeps its data in {c['database']} on {c['host']}:{c['port']} ({len(r['tables'])} of its tables already there)")
PYEOF
else
  echo "iopix's own database: not configured; the viewer starts with a set-up page that asks for a MySQL/MariaDB server (or run setup.sh --db ...)"
fi

say "Models"
for key in $(echo "$AUTO_MODELS" | tr ',' ' '); do
  echo "downloading $key"
  HF_HUB_OFFLINE=0 "$VPY" -m app.ai.models download "$key" || echo "  download of $key failed; retry later from Maintenance > Models and data"
done
if [ -n "$SEARCH_MODEL" ] && [ "$VDB_OK" = 1 ]; then "$VPY" -c "from app.data import state; state.init(); state.set_setting('search_model', '$SEARCH_MODEL')"
elif [ -n "$SEARCH_MODEL" ]; then echo "  the search model ($SEARCH_MODEL) is chosen in Settings once the database is set up"; fi

say "Shared ioSuite database and admin account"
"$VENV/bin/iosuite-shared" --config "$APP" check || die "the Shared ioSuite database does not answer with those settings (IOSUITE_DB_* in config.env)"
if [ -n "$ADMIN" ]; then
  # the password through the environment, never on a command line
  ADMIN_PASSWORD="${ADMIN#*:}" "$VENV/bin/iosuite-shared" --config "$APP" admin "${ADMIN%%:*}" --app iopix || die "could not create the admin account"
fi

say "Service"
if [ "$SERVICE" = 1 ] && [ "$OS" = Darwin ]; then
  PL="$HOME/Library/LaunchAgents/com.iopix.plist"
  # the settings stay in config.env, which the app reads itself: the login item only starts `python -m app`
  "$VPY" - "$PL" "$APP" "$VPY" "$DATA" <<'PYEOF'
import plistlib, sys, os
pl, app, vpy, data = sys.argv[1:5]
env = {"PATH": "/opt/homebrew/opt/mysql-client/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}  # mysql-client is keg-only
d = {"Label": "com.iopix", "ProgramArguments": [vpy, "-m", "app"],
     "WorkingDirectory": app, "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "EnvironmentVariables": env,
     "StandardOutPath": os.path.join(data, "server.log"), "StandardErrorPath": os.path.join(data, "server.log")}
os.makedirs(os.path.dirname(pl), exist_ok=True)
with open(pl, "wb") as f: plistlib.dump(d, f)
print("  wrote", pl)
PYEOF
  launchctl bootout "gui/$(id -u)/com.iopix" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PL" || die "launchctl could not start the login item (see above)"
  echo "  started (login item)"
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
ExecStart="$VPY" -m app
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
UEOF
  $SUDO systemctl daemon-reload || die "systemctl daemon-reload failed"
  $SUDO systemctl enable --now iopix || die "the iopix service could not be started (see above; journalctl -u iopix)"
  echo "  started (systemd)"
else
  echo "  not installed as a service. Start the viewer with:"
  echo "    cd \"$APP\" && ./run.sh"
fi
if [ -n "${DB_HOST:-}" ]; then VDBWHERE="MySQL/MariaDB ${DB_USER}@${DB_HOST}:${DB_PORT:-3306}/${DB_NAME} (in $ENVF)"
elif [ -f "$DATA/viewer-db.json" ]; then VDBWHERE="as in $DATA/viewer-db.json"; else VDBWHERE="not set up yet: the viewer asks on its first page"; fi
say "Done: http://localhost:$PORT   settings: $ENVF   data: $DATA   iopix database: $VDBWHERE"

# ---- the other apps of the suite, beside this one (iosuite-shared install: the shared answers from config.env)
case "$ALSO" in ''|none|no) ;;
  *) say "Installing the other apps: $ALSO"
     if [ "$SERVICE" = 1 ]; then SVC="--service"; else SVC="--no-service"; fi
     if [ -n "$ADMIN" ]; then
       ADMIN_PASSWORD="${ADMIN#*:}" "$VENV/bin"/iosuite-shared --config "$APP" install "$ALSO" --admin "${ADMIN%%:*}" --from iopix $SVC
     else
       "$VENV/bin"/iosuite-shared --config "$APP" install "$ALSO" --from iopix $SVC
     fi || die "not every app could be installed; see the lines above (each one can be set up again with its own setup.sh)";;
esac
