#!/bin/bash
# Unit/integration tests (each file in its own process: they configure the app through env vars at import time).
set -e; cd "$(dirname "$0")/.."; PY=${PY:-python3}
for f in test/test_app.py test/test_setup.py test/test_inbound.py test/test_formfill.py test/test_automation.py test/test_registry.py test/test_inbox.py test/test_dns.py test/test_family.py test/test_scanjobs.py test/test_autoscan.py test/test_shots.py test/test_mail.py test/test_chain.py test/test_drop.py test/test_v137.py test/test_v138.py test/test_v139.py test/test_v1310.py; do echo "== $f"; $PY -m pytest -q -p no:cacheprovider "$f"; done
