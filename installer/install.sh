#!/bin/sh
# iopix installer for Linux, version @VERSION@ (self-extracting; built by installer/make-installer.sh).
#
#   sh iopix-install.sh --photos /srv/photos --digikam-db /srv/digikamDB [options]
#
# Options
#   --photos DIR        the photo folder digiKam manages (required)
#   --digikam-db DIR    the folder containing digikam4.db (required unless --mysql)
#   --mysql HOST:PORT/DB:USER:PASSWORD   digiKam on MySQL/MariaDB instead of SQLite
#   --prefix DIR        install location            (default /opt/iopix)
#   --data DIR          the viewer's own data       (default PREFIX/data)
#   --port N            listen port                 (default 8600)
#   --host ADDR         listen address              (default 0.0.0.0)
#   --user NAME         service user                (default: iopix, created if missing)
#   --features LIST     search,places,objects,ocr,captions (default: ask; "none" for browsing only)
#   --admin NAME:PASS   the admin account (default: ask)
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
# admin account) and installs a systemd service. Running it again upgrades the code and keeps DATA. The library is
# only ever read unless you edit metadata from the viewer, which backs every file up first.
set -e
PREFIX=/opt/iopix; DATA=""; PHOTOS=""; DKDB=""; MYSQL=""; PORT=8600; HOST=0.0.0.0; USER_NAME=iopix
MODELS=""; FEATURES=""; ADMIN=""; CAPTIONS=0; READ_ONLY=0; SERVICE=1; SYSDEPS=1; YES=0
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --photos) PHOTOS="$2"; shift 2;; --digikam-db) DKDB="$2"; shift 2;; --mysql) MYSQL="$2"; shift 2;;
    --prefix) PREFIX="$2"; shift 2;; --data) DATA="$2"; shift 2;; --port) PORT="$2"; shift 2;; --host) HOST="$2"; shift 2;;
    --user) USER_NAME="$2"; shift 2;; --models) MODELS="$2"; shift 2;; --features) FEATURES="$2"; shift 2;; --admin) ADMIN="$2"; shift 2;; --captions) CAPTIONS=1; shift;;
    --read-only) READ_ONLY=1; shift;; --no-service) SERVICE=0; shift;; --no-system-deps) SYSDEPS=0; shift;; --yes) YES=1; shift;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown option $1 (try --help)";;
  esac
done
[ -n "$PHOTOS" ] || die "--photos DIR is required"
[ -d "$PHOTOS" ] || die "photo folder not found: $PHOTOS"
if [ -z "$MYSQL" ]; then
  [ -n "$DKDB" ] || die "--digikam-db DIR is required (the folder with digikam4.db), or --mysql"
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
if [ -d "$PREFIX/app" ]; then rm -rf "$PREFIX/app.old"; mv "$PREFIX/app" "$PREFIX/app.old"; fi
mv "$PREFIX/app.new" "$PREFIX/app"; rm -rf "$PREFIX/app.old"
VERSION=$(cat "$PREFIX/app/VERSION")

# --- everything else is the interactive setup script shipped with the app ----------------------
say "Running the setup"
ARGS="--photos $PHOTOS --data $DATA --port $PORT $SD"
if [ -n "$MYSQL" ]; then ARGS="$ARGS --mysql $MYSQL"; else ARGS="$ARGS --sqlite $DKDB"; fi
[ "$MODELS" = none ] && ARGS="$ARGS --features none" 
[ -n "$FEATURES" ] && ARGS="$ARGS --features $FEATURES"
[ "$CAPTIONS" = 1 ] && ARGS="$ARGS --features search,places,objects,ocr,captions"
[ "$SERVICE" = 0 ] && ARGS="$ARGS --no-service"
[ "$YES" = 1 ] && ARGS="$ARGS --yes"
[ -n "$ADMIN" ] && ARGS="$ARGS --admin $ADMIN"
# the service runs as a dedicated user: create it, hand it the folders, run setup as root with the unit's user set below
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$USER_NAME" 2>/dev/null || useradd -r -d "$PREFIX" -s /bin/false "$USER_NAME"
fi
# shellcheck disable=SC2086
sh "$PREFIX/app/setup.sh" $ARGS --no-service
# --read-only: lock the viewer to browsing, whatever the settings say (setup.sh does not ask about it)
[ "$READ_ONLY" = 1 ] && { grep -q '^READ_ONLY=' "$PREFIX/app/iopix.env" || echo "READ_ONLY=1" >> "$PREFIX/app/iopix.env"; }
chown -R "$USER_NAME" "$DATA" "$PREFIX/app/iopix.env" 2>/dev/null || true
if [ "$SERVICE" = 1 ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  say "Installing the systemd service"
  sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@USER@|$USER_NAME|g" -e "s|@HOST@|$HOST|g" -e "s|@PORT@|$PORT|g" "$PREFIX/app/iopix.service" | sed -e "s|EnvironmentFile=.*|EnvironmentFile=$PREFIX/app/iopix.env|" -e "s|ExecStart=.*|ExecStart=$PREFIX/app/.venv/bin/python -m uvicorn app.main:app --host $HOST --port $PORT|" > /etc/systemd/system/iopix.service
  systemctl daemon-reload && systemctl enable --now iopix.service && sleep 2
  systemctl is-active --quiet iopix.service && say "iopix $VERSION is running on port $PORT" || say "the service did not start; see: journalctl -u iopix"
fi
say "Settings: $PREFIX/app/iopix.env   Data: $DATA   Logs: journalctl -u iopix"
exit 0
