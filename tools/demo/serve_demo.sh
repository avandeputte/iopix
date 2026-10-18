#!/bin/sh
# Run iopix against the demo library (tools/demo/build_demo.py) on port 8610, next to any real instance.
# The demo keeps its own data folder; downloaded models and place names are shared with ./data when present.
set -e
cd "$(dirname "$0")/../.."
DEMO="${DEMO:-$HOME/iopix-demo}"
mkdir -p "$DEMO/data"
for d in models geonames; do [ -e "$DEMO/data/$d" ] || { [ -d "data/$d" ] && ln -s "$(pwd)/data/$d" "$DEMO/data/$d"; } || true; done
export DIGIKAM_DB_TYPE=sqlite DIGIKAM_DB_DIR="$DEMO/db" PHOTOS_ROOT="$DEMO/photos" DATA_DIR="$DEMO/data" DIGIKAM_CHECK=none \
       AUTO_MODELS="${AUTO_MODELS:-yunet,sface,geonames}" UPDATE_CHECK=0
unset DIGIKAM_DB_HOST DIGIKAM_DB_NAME DIGIKAM_DB_PORT DIGIKAM_DB_USER DIGIKAM_DB_PASSWORD
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8610}"
