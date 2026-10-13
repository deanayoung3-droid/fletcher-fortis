#!/bin/bash
# End-to-end DRY_RUN test against the bundled mock site. Usage: bash test/e2e.sh
set -e; cd "$(dirname "$0")/.."
C="docker-compose -f docker-compose.yml -f docker-compose.test.yml"; PW=$(grep -oP 'DASHBOARD_PASSWORD=\K.*' .env)
U(){ curl -s -u alex:$PW -o /dev/null -w "%{http_code} $1\n" -X POST "localhost:8080$1" "${@:2}"; }
$C down >/dev/null 2>&1 || true; rm -rf data/optout.db data/shots data/auth; $C up -d --build >/dev/null 2>&1; sleep 6
for p in alex sam jordan riley; do U /person/$p/scan; sleep 8; done; sleep 6
U /listing/1/approve; U /listing/2/approve; U /listing/3/approve; U /listing/6/notme
U /person/alex/toggle/bulk_ccpa; U /person/alex/bulk
mv test/mock/p/alex-1.html test/mock/p/alex-1.gone
$C exec -T app python -c "
import asyncio,scanner
from playwright.async_api import async_playwright
from db import conn
async def m():
  async with async_playwright() as p:
    b=await p.chromium.launch();pg=await b.new_page();await scanner.recheck(pg,conn());await b.close()
asyncio.run(m())"
mv test/mock/p/alex-1.gone test/mock/p/alex-1.html; U /person/alex/scan; sleep 10
$C exec -T app python -c "
import sqlite3;c=sqlite3.connect('/data/optout.db')
for r in c.execute('select id,person_id,site,status from listings'):print(r)
for r in c.execute('select person_id,to_addr,state from outbox'):print(r)"
