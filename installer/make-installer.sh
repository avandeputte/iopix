#!/bin/sh
# Builds dist/iopix-install.sh: a single self-extracting installer for Linux
# (the install script followed by a tarball of the application). Run it on the
# target machine with sh; see installer/install.sh for options.
#
#   ./installer/make-installer.sh            -> dist/iopix-install.sh
#
# The archive carries the code, static files, pyproject.toml and README; no models
# (they are downloaded at install time) and nothing from data/ or .venv/.
set -e
cd "$(dirname "$0")/.."
# the app's own version number (update.sh reads it the same way); the date only when it cannot be read
VERSION="${VERSION:-$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' app/__init__.py)}"
[ -n "$VERSION" ] || VERSION="$(date +%Y.%m.%d)"
mkdir -p dist
OUT="dist/iopix-install.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/iopix"
cp -R app static pyproject.toml README.md setup.sh run.sh update.sh "$TMP/iopix/"
git rev-parse HEAD > "$TMP/iopix/COMMIT" 2>/dev/null || echo unknown > "$TMP/iopix/COMMIT"
find "$TMP/iopix" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$TMP/iopix" -name '.DS_Store' -delete 2>/dev/null || true
cp installer/iopix.service "$TMP/iopix/iopix.service"
printf '%s\n' "$VERSION" > "$TMP/iopix/VERSION"
(cd "$TMP" && tar czf payload.tgz iopix)
{
  sed "s/@VERSION@/$VERSION/" installer/install.sh
  printf '\n__PAYLOAD_BELOW__\n'
  cat "$TMP/payload.tgz"
} > "$OUT"
chmod 755 "$OUT"
echo "built $OUT ($(du -h "$OUT" | cut -f1), version $VERSION)"
