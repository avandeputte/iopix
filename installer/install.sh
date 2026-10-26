#!/bin/sh
# iopix installer for Linux, version @VERSION@ (self-extracting; built by installer/make-installer.sh).
#
#   sh iopix-install.sh --photos /srv/photos --digikam-db /srv/digikamDB [options]
#   sh iopix-install.sh --photos /srv/photos --no-digikam [options]      (you don't use digiKam)
#
# Options
#   --photos DIR        the photo folder (required)
#   --digikam-db DIR    the folder containing digikam4.db (required unless --mysql or --no-digikam)
#   --mysql HOST:PORT/DB:USER:PASSWORD   digiKam on MySQL/MariaDB instead of SQLite
#   --no-digikam        no digiKam at all: iopix keeps the library itself and finds the photos in the folder
#   --prefix DIR        install location            (default /opt/iopix)
#   --data DIR          the viewer's own data       (default PREFIX/data)
#   --port N            listen port                 (default 8600)
#   --host ADDR         listen address              (default 0.0.0.0)
#   --user NAME         service user                (default: iopix, created if missing)
#   --features LIST     search,places,objects,ocr,captions (default: ask; "none" for browsing only)
#   --admin NAME:PASS   the admin account (default: ask); IOPIX_ADMIN=NAME:PASS in the environment does the
#                       same without the password in the process list, and IOPIX_MYSQL=HOST:PORT/DB:USER:PASSWORD stands in for --mysql
#   --iopix-db sqlite|HOST:PORT/DB:USER:PASS  iopix's own database (default: ask; IOPIX_VIEWER_DB in the environment does the same)
#   --models LIST       kept for compatibility: "none" means --features none
#   --captions          shorthand for all features including captions
#   --read-only         lock the viewer to browsing (READ_ONLY=1)
#   --no-service        do not install/start the systemd unit (prints the run command)
#   --no-system-deps    do not touch the package manager (you install python3-venv, ffmpeg, exiftool, libheif)
#   --yes               no questions
#
# What it does: installs the system packages the viewer shells out to (ffmpeg, exiftool,
# libheif) with apt, dnf, zypper or pacman; unpacks the app under PREFIX/app; creates a
# Python venv and installs the packages; writes PREFIX/app/iopix.env;
# then runs the app's own interactive setup (setup.sh: database, features and model downloads,
# admin account) and installs a systemd service. The app folder (with its venv) belongs to the service
# user and a sudoers rule lets that user restart the service, so "Update now" in the web app (update.sh) works.
# Running it again upgrades the code and keeps DATA. The library is
# only ever read unless you edit metadata from the viewer, which backs every file up first.
set -e
PREFIX=/opt/iopix; DATA=""; PHOTOS=""; DKDB=""; MYSQL=""; NODK=0; PORT=8600; HOST=0.0.0.0; USER_NAME=iopix
MODELS=""; FEATURES=""; ADMIN=""; CAPTIONS=0; READ_ONLY=0; SERVICE=1; SYSDEPS=1; YES=0
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --photos) PHOTOS="$2"; shift 2;; --digikam-db) DKDB="$2"; shift 2;; --mysql) MYSQL="$2"; shift 2;; --no-digikam) NODK=1; shift;; --iopix-db) export IOPIX_VIEWER_DB="$2"; shift 2;;
    --prefix) PREFIX="$2"; shift 2;; --data) DATA="$2"; shift 2;; --port) PORT="$2"; shift 2;; --host) HOST="$2"; shift 2;;
    --user) USER_NAME="$2"; shift 2;; --models) MODELS="$2"; shift 2;; --features) FEATURES="$2"; shift 2;; --admin) ADMIN="$2"; shift 2;; --captions) CAPTIONS=1; shift;;
    --read-only) READ_ONLY=1; shift;; --no-service) SERVICE=0; shift;; --no-system-deps) SYSDEPS=0; shift;; --yes) YES=1; shift;;
    -h|--help) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown option $1 (try --help)";;
  esac
done
[ -n "$ADMIN" ] || ADMIN="${IOPIX_ADMIN:-}"
[ -n "$MYSQL" ] || MYSQL="${IOPIX_MYSQL:-}"
[ -n "$PHOTOS" ] || die "--photos DIR is required"
[ -d "$PHOTOS" ] || die "photo folder not found: $PHOTOS"
if [ "$NODK" = 0 ] && [ -z "$MYSQL" ]; then
  [ -n "$DKDB" ] || die "--digikam-db DIR is required (the folder with digikam4.db), or --mysql, or --no-digikam"
  [ -f "$DKDB/digikam4.db" ] || die "digikam4.db not found in $DKDB"
fi
[ -n "$DATA" ] || DATA="$PREFIX/data"
[ "$(id -u)" = 0 ] || die "run as root (sudo); it installs packages and a system service"

# --- system packages are installed by the app's setup.sh (python3, exiftool, ffmpeg, libheif) ---------
[ "$SYSDEPS" = 1 ] && SD="--system-deps" || SD="--no-system-deps"

# --- unpack ------------------------------------------------------------------------
say "Installing into $PREFIX"
mkdir -p "$PREFIX" "$DATA"
LINE=$(grep -a -n '^__PAYLOAD_BELOW__$' "$0" | head -1 | cut -d: -f1)  # -a: the file ends in a binary payload
[ -n "$LINE" ] || die "payload missing: build this file with installer/make-installer.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
tail -n +$((LINE + 1)) "$0" | tar xzf - -C "$TMP"
rm -rf "$PREFIX/app.new"; mv "$TMP/iopix" "$PREFIX/app.new"
if [ -d "$PREFIX/app" ]; then  # a re-run upgrades the code: the settings, the Python environment and the installed commit stay
  for keep in iopix.env .venv COMMIT; do [ -e "$PREFIX/app/$keep" ] && mv "$PREFIX/app/$keep" "$PREFIX/app.new/$keep"; done
  rm -rf "$PREFIX/app.old"; mv "$PREFIX/app" "$PREFIX/app.old"
fi
mv "$PREFIX/app.new" "$PREFIX/app"; rm -rf "$PREFIX/app.old"
VERSION=$(cat "$PREFIX/app/VERSION")

# --- everything else is the interactive setup script shipped with the app ----------------------
say "Running the setup"
set -- --photos "$PHOTOS" --data "$DATA" --port "$PORT" $SD  # the arguments as a list: a folder with a space in its name stays one word
# the passwords go to setup.sh in its environment, not on its command line (visible in the process list)
if [ "$NODK" = 1 ]; then set -- "$@" --no-digikam; elif [ -n "$MYSQL" ]; then export IOPIX_MYSQL="$MYSQL"; else set -- "$@" --sqlite "$DKDB"; fi
[ -n "$ADMIN" ] && export IOPIX_ADMIN="$ADMIN"
[ "$MODELS" = none ] && set -- "$@" --features none
[ -n "$FEATURES" ] && set -- "$@" --features "$FEATURES"
[ "$CAPTIONS" = 1 ] && set -- "$@" --features search,places,objects,ocr,captions
[ "$SERVICE" = 0 ] && set -- "$@" --no-service
[ "$YES" = 1 ] && set -- "$@" --yes
# the service runs as a dedicated user: create it, hand it the folders, run setup as root with the unit's user set below
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$USER_NAME" 2>/dev/null || useradd -r -d "$PREFIX" -s /bin/false "$USER_NAME"
fi
sh "$PREFIX/app/setup.sh" "$@" --no-service
# --read-only: lock the viewer to browsing, whatever the settings say (setup.sh does not ask about it)
[ "$READ_ONLY" = 1 ] && { grep -q '^READ_ONLY=' "$PREFIX/app/iopix.env" || echo "READ_ONLY=1" >> "$PREFIX/app/iopix.env"; }
# the app folder and its venv belong to the service user: update.sh, run from the web app as that user, replaces
# the code and refreshes the packages in place
chown -R "$USER_NAME" "$DATA" "$PREFIX/app" 2>/dev/null || true
# ...and that user may restart the service, nothing else (update.sh calls "sudo -n systemctl restart iopix")
if [ "$SERVICE" = 1 ] && command -v sudo >/dev/null 2>&1 && [ -d /etc/sudoers.d ]; then
  printf '# written by the iopix installer: the service user restarts the service after an in-app update\n%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart iopix, /usr/bin/systemctl restart iopix.service, /bin/systemctl restart iopix, /bin/systemctl restart iopix.service\n' "$USER_NAME" > /etc/sudoers.d/iopix.tmp
  chmod 440 /etc/sudoers.d/iopix.tmp
  if ! command -v visudo >/dev/null 2>&1 || visudo -cf /etc/sudoers.d/iopix.tmp >/dev/null 2>&1; then mv /etc/sudoers.d/iopix.tmp /etc/sudoers.d/iopix; else rm -f /etc/sudoers.d/iopix.tmp; say "could not write the sudoers rule; in-app updates will not restart the service"; fi
elif [ "$SERVICE" = 1 ]; then
  say "sudo is not installed: after an in-app update, restart the service yourself (systemctl restart iopix)"
fi
if [ "$SERVICE" = 1 ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  say "Installing the systemd service"
  sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@USER@|$USER_NAME|g" -e "s|@HOST@|$HOST|g" -e "s|@PORT@|$PORT|g" "$PREFIX/app/iopix.service" | sed -e "s|EnvironmentFile=.*|EnvironmentFile=$PREFIX/app/iopix.env|" -e "s|ExecStart=.*|ExecStart=\"$PREFIX/app/.venv/bin/python\" -m uvicorn app.main:app --host $HOST --port $PORT|" > /etc/systemd/system/iopix.service
  systemctl daemon-reload && systemctl enable --now iopix.service && sleep 2
  systemctl is-active --quiet iopix.service && say "iopix $VERSION is running on port $PORT" || say "the service did not start; see: journalctl -u iopix"
fi
say "Settings: $PREFIX/app/iopix.env   Data: $DATA   Logs: journalctl -u iopix"
exit 0
