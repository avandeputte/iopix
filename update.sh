#!/bin/sh
# Update an installed iopix in place (standalone checkout, the Linux installer's
# /opt/iopix/app, or the Proxmox container; Docker users pull the image instead).
#
#   ./update.sh            fetch the latest code, refresh the packages, restart the service
#   ./update.sh --check    only say whether something newer exists (IOPIX_REF is honoured)
#   ./update.sh --force    reinstall and restart even when the installed commit is already the latest
#   IOPIX_REF=<sha|branch> ./update.sh   install that commit instead of the tip of main
#     (right after a push GitHub may still report the previous commit; naming the sha avoids that)
#   IOPIX_RESULT=<file>  write the outcome ("ok: ..." or "error: ...") there before the
#     restart, so the viewer can show it afterwards (its own child process dies in the restart)
#
# Keeps iopix.env (or digikam-viewer.env from before the rename), the data folder and the Python environment. A git checkout is
# pulled; an installed copy (no .git) is replaced from the GitHub tarball of the main branch.
# The order protects the install: the Python packages the new version asks for (its pyproject.toml) are
# installed first, and the code moves only once that succeeded, so a failed package install leaves the
# installed version running as it was; a failed swap puts the previous files back.
# Maintenance > Viewer version runs this same script from the web app.
set -e
cd "$(dirname "$0")"
APP="$(pwd)"
# IOPIX_*; the DIGIKAM_VIEWER_* names from before the rename still work
REPO="${IOPIX_REPO:-${DIGIKAM_VIEWER_REPO:-avandeputte/iopix}}"
[ "$REPO" = avandeputte/digikam-viewer ] && REPO=avandeputte/iopix  # the repository was renamed
BRANCH="${IOPIX_BRANCH:-${DIGIKAM_VIEWER_BRANCH:-main}}"
REF="${IOPIX_REF:-${DIGIKAM_VIEWER_REF:-$BRANCH}}"
RESULT="${IOPIX_RESULT:-${DIGIKAM_VIEWER_RESULT:-}}"
case "$REF" in -*|*..*|*/*/*) printf 'error: bad ref: %s\n' "$REF" >&2; exit 1;; esac  # a commit id or a branch, never an option or a path
FORCE=0; [ "$1" = "--force" ] && FORCE=1
TMP=""; NOTED=0
# on a failure the result file gets die's own words; the generic line is for failures that never reached die (set -e)
cleanup() { rc=$?; [ -n "$TMP" ] && rm -rf "${TMP:?}"; [ $rc -ne 0 ] && [ "$NOTED" = 0 ] && [ -n "$RESULT" ] && printf 'error: update.sh failed (exit %s); see the log\n' "$rc" > "$RESULT" 2>/dev/null; true; }
trap cleanup EXIT
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
note() { [ -n "$RESULT" ] && printf '%s\n' "$*" > "$RESULT" 2>/dev/null || true; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; note "error: $*"; NOTED=1; exit 1; }
local_commit() { if [ -d .git ]; then git rev-parse HEAD 2>/dev/null; elif [ -f COMMIT ]; then cat COMMIT; else echo unknown; fi; }
# network access through the viewer's own Python (no curl needed inside a container)
PYF="$(ls -d .venv/bin/python 2>/dev/null || command -v python3)"
fetch() { "$PYF" -c 'import sys, urllib.request
req = urllib.request.Request(sys.argv[1], headers={"User-Agent": "iopix-update", "Accept": "application/vnd.github+json"})
with urllib.request.urlopen(req, timeout=60) as r, open(sys.argv[2], "wb") as f:
    f.write(r.read())' "$1" "$2"; }
remote_commit() { "$PYF" -c 'import sys, json, urllib.request
req = urllib.request.Request(sys.argv[1], headers={"User-Agent": "iopix-update", "Accept": "application/vnd.github+json"})
with urllib.request.urlopen(req, timeout=30) as r:
    print(json.load(r)["sha"])' "https://api.github.com/repos/$REPO/commits/${1:-$BRANCH}" 2>/dev/null; }
if [ "$1" = "--check" ]; then
  L="$(local_commit)"; R="$(remote_commit "$REF")" || R=""  # a failed lookup must reach die, not set -e
  [ -n "$R" ] || die "could not reach GitHub (api.github.com) from here, or no such commit: $REF"
  if [ "$L" = "$R" ]; then echo "up to date ($(echo "$L" | cut -c1-7))"; else echo "update available: $(echo "$R" | cut -c1-7) (installed $(echo "$L" | cut -c1-7))"; fi
  exit 0
fi

VPY="$APP/.venv/bin/python"
[ -x "$VPY" ] || die "no .venv here; run ./setup.sh first"
TMP="$(mktemp -d)"
STAGE=""

say "Fetching the latest code"
if [ -d .git ]; then
  git fetch -q origin "$REF" || die "git fetch failed (could not reach GitHub?)"
  if [ "$FORCE" = 0 ] && [ "$(git rev-parse HEAD)" = "$(git rev-parse FETCH_HEAD)" ]; then
    say "Already up to date ($(local_commit | cut -c1-7)); nothing to do"; note "ok: already up to date ($(local_commit | cut -c1-7))"; exit 0
  fi
  # what would make the merge fail is checked now, before any package is touched
  git merge-base --is-ancestor HEAD FETCH_HEAD || die "this checkout has commits that are not on $REF (not a fast-forward); update it by hand"
  git diff --name-only HEAD -- | while IFS= read -r f; do
    [ -n "$f" ] && ! git diff --quiet HEAD FETCH_HEAD -- "$f" && { printf '  locally changed and also changed by the update: %s\n' "$f" >&2; exit 1; }
    :
  done || die "local changes to files the update also changes (git stash them first)"
  git show FETCH_HEAD:pyproject.toml > "$TMP/pyproject.toml" 2>/dev/null || die "the fetched commit has no pyproject.toml"
else
  # the archive is fetched by commit id, so the code and the recorded COMMIT always match
  R="$(remote_commit "$REF")" || R=""; [ -n "$R" ] || die "could not reach GitHub (or no such commit: $REF)"
  if [ "$FORCE" = 0 ] && [ "$R" = "$(local_commit)" ]; then
    say "Already up to date ($(echo "$R" | cut -c1-7)); nothing to do"; note "ok: already up to date ($(echo "$R" | cut -c1-7))"; exit 0
  fi
  fetch "https://github.com/$REPO/archive/$R.tar.gz" "$TMP/src.tgz" || die "download failed"
  tar xzf "$TMP/src.tgz" -C "$TMP"
  SRC="$(ls -d "$TMP"/*/ | head -1)"
  # stage everything first; the swap comes after the packages are in. Settings, data and the
  # environment live outside these items and are never touched
  STAGE="$APP/.update-stage"; rm -rf "${STAGE:?}"; mkdir -p "$STAGE"
  for item in app static pyproject.toml README.md setup.sh run.sh update.sh installer/iopix.service; do
    [ -e "$SRC/$item" ] && { mkdir -p "$STAGE/$(dirname "$item")"; cp -R "$SRC/$item" "$STAGE/$item"; }
  done
  [ -d "$STAGE/app" ] && [ -d "$STAGE/static" ] && [ -f "$STAGE/pyproject.toml" ] || { rm -rf "${STAGE:?}"; die "the downloaded tarball looks incomplete"; }
  cp "$STAGE/pyproject.toml" "$TMP/pyproject.toml"
fi

say "Refreshing the Python packages (the new version's, before the code moves)"
G=""
"$VPY" -c "import fastembed" 2>/dev/null && G="$G,search"
"$VPY" -c "import rapidocr" 2>/dev/null && G="$G,ocr"
"$VPY" -c "import onnxruntime" 2>/dev/null && G="$G,objects"
"$VPY" -c "import torch, transformers" 2>/dev/null && G="$G,captions"
G="${G#,}"
# the dependency list of the fetched pyproject.toml, base set plus the groups already installed here
"$VPY" - "$TMP/pyproject.toml" "$G" > "$TMP/requirements.txt" <<'PYEOF' || die "could not read the fetched pyproject.toml"
import re, sys
path, groups = sys.argv[1], [g for g in sys.argv[2].split(",") if g]
try:
    import tomllib
except ImportError:  # Python 3.10: a plain reading of the two array shapes pyproject.toml uses
    tomllib = None
deps = []
if tomllib:
    with open(path, "rb") as f:
        proj = tomllib.load(f).get("project", {})
    deps += proj.get("dependencies", [])
    for g in groups:
        deps += proj.get("optional-dependencies", {}).get(g, [])
else:
    text = open(path, encoding="utf-8").read()
    def arr(key):
        m = re.search(r"^\s*" + re.escape(key) + r'\s*=\s*\[((?:[^\]"]|"[^"]*")*)\]', text, re.M)
        return re.findall(r'"([^"]+)"', m.group(1)) if m else []
    deps += arr("dependencies")
    for g in groups:
        deps += arr(g)
print("\n".join(deps))
PYEOF
pipi() { if command -v uv >/dev/null 2>&1; then uv pip install -q -p "$VPY" "$@"; else "$VPY" -m pip install -q "$@"; fi; }
pipi -r "$TMP/requirements.txt" || { [ -n "$STAGE" ] && rm -rf "${STAGE:?}"; die "installing the Python packages failed; the installed version is unchanged (code and packages)"; }

say "Installing the new code"
if [ -d .git ]; then
  git merge -q --ff-only FETCH_HEAD || die "git merge failed (local changes?); the packages were refreshed, the code is unchanged"
else
  MOVED=""
  swap_back() { for item in $MOVED; do rm -rf "${APP:?}/${item:?}"; [ -e "$APP/$item.old" ] && mv "$APP/$item.old" "$APP/$item"; done; rm -rf "${STAGE:?}"; }
  for item in app static pyproject.toml README.md setup.sh run.sh update.sh; do
    [ -e "$STAGE/$item" ] || continue
    rm -rf "${APP:?}/${item:?}.old"
    if [ -e "$APP/$item" ]; then mv "$APP/$item" "$APP/$item.old" || { swap_back; die "could not set $item aside; the previous files are back in place"; }; fi
    MOVED="$MOVED $item"
    mv "$STAGE/$item" "$APP/$item" || { swap_back; die "could not install $item; the previous files are back in place"; }
  done
  for item in $MOVED; do rm -rf "${APP:?}/${item:?}.old"; done
  [ -f "$STAGE/installer/iopix.service" ] && cp "$STAGE/installer/iopix.service" "$APP/iopix.service"
  rm -rf "${STAGE:?}"
  echo "$R" > COMMIT
fi
chmod 755 setup.sh update.sh run.sh 2>/dev/null || true
# the editable install's metadata follows the new pyproject.toml (its import path already points here, and the
# packages are in place, so this is bookkeeping: the viewer runs either way)
pipi --no-deps -e . 2>/dev/null || echo "  note: the package metadata could not be refreshed (pip install -e .); the viewer runs regardless"

NEWV="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' app/__init__.py)"
# the outcome goes to the result file now: the service restart below also ends this script
# when the viewer started it
note "ok: updated to v$NEWV on $(date '+%Y-%m-%d %H:%M')"
# jpegtran (libjpeg-turbo) turns and crops JPEGs without loss; newer than most installs, so it is added here when it can be
if ! command -v jpegtran >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1 && [ "$(id -u)" = 0 ]; then
  say "Installing jpegtran (libjpeg-turbo-progs) for lossless JPEG rotate and crop"
  DEBIAN_FRONTEND=noninteractive sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends libjpeg-turbo-progs >/dev/null' || echo "  could not install it; JPEGs are turned by their orientation flag until it is installed"
fi

say "Restarting the service"
# as the service user (the Linux installer's layout) the restart goes through sudo: the installer allows exactly this command
restart_unit() { if [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1; then sudo -n systemctl restart "$1"; else systemctl restart "$1"; fi; }
SVC=""; for s in iopix digikam-viewer; do command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$s" 2>/dev/null && { SVC="$s"; break; }; done
if [ -n "$SVC" ]; then
  if restart_unit "$SVC"; then echo "  systemd: restarted $SVC"; else
    echo "  could not restart $SVC from here; run: sudo systemctl restart $SVC"
    note "ok: updated to v$NEWV; the service could not be restarted from here: sudo systemctl restart $SVC"
  fi
elif [ "$(uname -s)" = Darwin ]; then
  L="$(launchctl list 2>/dev/null | grep -o 'com\.[a-z.]*\(iopix\|digikam-viewer\)' | head -1)"
  if [ -n "$L" ]; then launchctl kickstart -k "gui/$(id -u)/$L" && echo "  launchd: restarted $L"; else echo "  no service found; restart the viewer yourself"; fi
else
  echo "  no service found; restart the viewer yourself"
fi
say "Updated to v$NEWV ($(local_commit | cut -c1-7))"
