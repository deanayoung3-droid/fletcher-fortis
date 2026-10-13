#!/bin/bash
# Local app + mock brokers for browser QA (DRY_RUN on, nothing real contacted). Ctrl-C to stop.
cd "$(dirname "$0")/.."; T=${QA_DIR:-$(mktemp -d)}; PORT=${PORT:-8091}
export DB_PATH=$T/optout.db SHOT_DIR=$T/shots AUTH_DIR=$T/auth HEALTH_FILE=$T/health.json PROFILES_DIR=$PWD/test/profiles SITES_FILE=$PWD/test/sites.test.yaml \
  SCAN_DELAY_MS=150 SCHEDULER=false ADMIN_USERNAME=alex ADMIN_PASSWORD='qa correct horse battery' COOKIE_SECURE=false DRY_RUN=true \
  FF_DATA_KEY=qa-test-key-0123456789-abcdefghijklmnop BROKER_OUT=$PWD/data/brokers_merged.yaml SKIP_FILE=$PWD/config/skip_ids.txt BROKERS_FILE=$PWD/eraser/data/brokers.yaml PUBLIC_URL=http://localhost:$PORT
python3 -m http.server 8000 -d test/mock >/dev/null 2>&1 & M=$!; trap 'kill $M 2>/dev/null' EXIT
cd optout && exec python3 -m uvicorn main:app --port $PORT
