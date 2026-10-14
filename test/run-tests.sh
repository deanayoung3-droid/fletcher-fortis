#!/bin/bash
# Unit/integration tests (each file in its own process: they configure the app through env vars at import time).
set -e; cd "$(dirname "$0")/.."; PY=${PY:-python3}
for f in test/test_app.py test/test_setup.py test/test_inbound.py test/test_formfill.py test/test_automation.py test/test_registry.py test/test_inbox.py; do echo "== $f"; $PY -m pytest -q -p no:cacheprovider "$f"; done
