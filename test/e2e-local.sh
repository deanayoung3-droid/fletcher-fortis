#!/bin/bash
# Full flow without Docker/sudo: app + mock broker sites on localhost. Nothing real is contacted, DRY_RUN stays on.
# Needs: python3 with fastapi uvicorn jinja2 python-multipart pyyaml playwright bcrypt cryptography (+ chromium).
set -e; cd "$(dirname "$0")/.."; T=$(mktemp -d); PORT=${PORT:-8090}; PW='e2e correct horse battery'; KPW='first pass for k 123'
trap '[ -f test/mock/p/alex-1.gone ] && mv test/mock/p/alex-1.gone test/mock/p/alex-1.html' ERR
export DB_PATH=$T/optout.db SHOT_DIR=$T/shots AUTH_DIR=$T/auth HEALTH_FILE=$T/health.json PROFILES_DIR=$PWD/test/profiles SITES_FILE=$PWD/test/sites.test.yaml \
  SCAN_DELAY_MS=150 SCAN_MAX_PAGES=2 SCHEDULER=false ADMIN_USERNAME=alex ADMIN_PASSWORD="$PW" COOKIE_SECURE=false DRY_RUN=true \
  FF_DATA_KEY=e2e-test-key-0123456789-abcdefghijklmnop BROKER_OUT=$PWD/data/brokers_merged.yaml SKIP_FILE=$PWD/config/skip_ids.txt BROKERS_FILE=$PWD/eraser/data/brokers.yaml PUBLIC_URL=http://localhost:$PORT
python3 -m http.server 8000 -d test/mock >/dev/null 2>&1 & M=$!
(cd optout && exec python3 -m uvicorn main:app --port $PORT >$T/app.log 2>&1) & A=$!
trap 'kill $M $A 2>/dev/null; true' EXIT; sleep 4; curl -sf localhost:$PORT/healthz >/dev/null || { echo "app did not start (port $PORT busy?)"; cat $T/app.log; exit 1; }
B=localhost:$PORT; J=$T/admin.jar; K=$T/sam.jar
login(){ curl -s -c $1 -b $1 -o /dev/null -w "%{http_code} login $2\n" -d "username=$2" --data-urlencode "password=$3" $B/login; }
tok(){ curl -s -b $1 $B${2:-/family} | grep -o 'name="csrf" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"//'; }
U(){ local jar=$1 path=$2; shift 2; curl -s -b $jar -o /dev/null -w "%{http_code} POST $path\n" -X POST "$B$path" -F "csrf=$CS" "$@"; }
G(){ curl -s -b $1 -o /dev/null -w "%{http_code} GET $2\n" "$B$2"; }
db(){ (cd optout && python3 -c "import db,json,sys; c=db.conn(); $1"); }
waitscan(){ for i in $(seq 1 60); do sleep 1; n=$(db "print(c.execute(\"select count(*) from events where kind='scan_complete'\").fetchone()[0])"); [ "$n" -ge "$1" ] && return; done; echo "scan timeout"; }
echo "== auth"; curl -s -o /dev/null -w "%{http_code} GET / logged out (expect 303 to /login)\n" $B/
login $J alex wrong-password-here; login $J alex "$PW"; CS=$(tok $J); [ -n "$CS" ] || { echo "no csrf"; exit 1; }
curl -s -b $J -o /dev/null -w "%{http_code} POST without CSRF (expect 403)\n" -X POST $B/person/alex/scan
echo "== scans"; n=0; for p in alex sam jordan riley; do U $J /person/$p/scan; n=$((n+1)); waitscan $n; done
echo "== profile edit (nickname, past address, phone, email, relative) -> re-scan"
curl -s -b $J -o /dev/null -w "%{http_code} POST /person/alex/profile (SSN, expect 400)\n" -X POST $B/person/alex/profile -d "csrf=$CS&name_kind=legal&name_first=Alex&name_middle=&name_last=Rivera&addr_street=123-45-6789&addr_city=Springfield&addr_state=CA&addr_current=0"
curl -s -b $J -o /dev/null -w "%{http_code} POST /person/alex/profile\n" -X POST $B/person/alex/profile --data-urlencode "csrf=$CS" \
  -d "name_kind=legal&name_first=Alex&name_middle=Quinn&name_last=Rivera&name_kind=nickname&name_first=Alexo&name_middle=&name_last=Rivera" \
  -d "addr_street=&addr_city=Springfield&addr_state=CA&addr_street=123 Fake Example Ln&addr_city=Shelbyville&addr_state=CA&addr_current=0" \
  --data-urlencode "phone=(555) 555-0100" -d "email=alex.test@example.com&relative=Jordan Rivera&relative=Sam Rivera&birth_year=&age_min=13&age_max=19"
waitscan 5
echo "== review"
id(){ db "r=[x['id'] for x in c.execute('select * from listings where person_id=? and site=?',('$1','$2')).fetchall() if '$3' in x['url']]; print(r[0] if r else 0)"; }
L1=$(id alex mockemail alex-1); L2=$(id alex mockform alex-1); L3=$(id sam mockemail sam-1); L6=$(id jordan mockform jordan-1); LD=$(id alex mockdeep alex-p); LS=$(id alex mocksister alex.html); LN=$(id alex mockdeep alexo-p)
echo "ids: email=$L1 captcha=$L2 sam=$L3 jordan=$L6 deep=$LD sister=$LS nickname=$LN"
U $J /listing/$L1/approve; U $J /listing/$L2/approve; U $J /listing/$L3/approve; U $J /listing/$L6/notme; U $J /listing/$LD/approve
U $J /person/alex/toggle/bulk_ccpa; command -v eraser >/dev/null && U $J /person/alex/bulk || echo "skip bulk (no eraser binary on host; it is built into the image)"
printf '%%PDF-1.4\n%%test\n%%%%EOF\n' > $T/auth.pdf; printf 'not a pdf' > $T/bad.pdf; head -c 6000000 /dev/zero | sed 's/^/%PDF-/' > $T/big.pdf
U $J /person/sam/auth -F "pdf=@$T/bad.pdf;type=application/pdf"; U $J /person/sam/auth -F "pdf=@$T/big.pdf;type=application/pdf"; U $J /person/sam/auth -F "pdf=@$T/auth.pdf;type=application/pdf"
echo "== member account"
U $J /family/sam/account -F action=password -F username=sam -F "password=$KPW"
login $K sam "$KPW"; G $K /person/sam; KCS=$(tok $K /account/password)
curl -s -b $K -o /dev/null -w "%{http_code} POST /account/password (member sets own)\n" -X POST $B/account/password --data-urlencode "csrf=$KCS" --data-urlencode "current=$KPW" --data-urlencode "password=my own passphrase 2026 k" --data-urlencode "password2=my own passphrase 2026 k"
for u in /person/sam /person/sam/profile /person/alex /listing/$L1 /shot/$L1 /person/alex/proof.zip /family /sites /calls /; do G $K $u; done
echo "== recheck / reappear"
mv test/mock/p/alex-1.html test/mock/p/alex-1.gone
(cd optout && python3 -c "
import asyncio,scanner
from playwright.async_api import async_playwright
from db import conn
async def m():
  async with async_playwright() as p:
    b=await p.chromium.launch();pg=await b.new_page();await scanner.recheck(pg,conn());await b.close()
asyncio.run(m())")
mv test/mock/p/alex-1.gone test/mock/p/alex-1.html; U $J /person/alex/scan; waitscan 6
echo "== admin pages"
for u in / /person/alex /person/alex/profile /person/sam /listing/$L1 /listing/$LD /family /calls /add /sites /person/alex/proof.zip /person/alex/reminders.ics /account/password /nope; do G $J $u; done
U $J /logout; G $J /person/alex
python3 - "$DB_PATH" <<PY
import sqlite3,sys,re,os
os.chdir("optout"); sys.path.insert(0,".")
import db; c=db.conn()
L={(r['person_id'],r['site'],r['url'].rsplit('/',1)[-1]):r for r in c.execute('select * from listings')}
for k,r in sorted(L.items()): print(k, r['status'], 'score', r['score'])
st={r['state'] for r in c.execute('select state from outbox')}; print('outbox states', st)
def S(p,s,u): return (L.get((p,s,u)) or {}).get('status')
exp={('alex','mockemail','alex-1.html'):'reappeared',('alex','mockform','alex-1.html'):'needs_human',('jordan','mockform','jordan-1.html'):'not_me',
     ('alex','mockdeep','alex-p.html'):'submitted',('alex','mocksister','alex.html'):'submitted',('sam','mockemail','sam-1.html'):'candidate'}
bad=[(k,v,S(*k)) for k,v in exp.items() if S(*k)!=v]
nick=('alex','mockdeep','alexo-p.html') in L; wrongage=('alex','mockdeep','alex-wrongage.html') in L; other=('alex','mockdeep','other.html') in L
ev=L.get(('alex','mockdeep','alex-p.html')); import json; ev=json.loads(ev['fields'])['evidence'] if ev else {}
print('nickname found', nick, '| wrong-age detail rejected', not wrongage, '| Fresno rejected', not other, '| evidence', {k:v for k,v in ev.items() if v})
raw=open(sys.argv[1],'rb').read()+b''.join(open(sys.argv[1]+x,'rb').read() for x in ('-wal',) if os.path.exists(sys.argv[1]+x))
leak=[w for w in (b'Sam',b'Alex Quinn',b'Springfield',b'925',b'555-0142',b'alex.test@example.com',b'Jordan Rivera',b'Fake Example') if w in raw]
print('plaintext PII in raw DB:', leak or 'none')
if bad or st-{'dry_run'} or not nick or wrongage or other or not ev.get('phones') or not ev.get('relatives') or leak: print('FAIL', bad); sys.exit(1)
print('PASS')
PY
cp $T/app.log /tmp/e2e-app.log; grep -Ei "sam|alex\.test|555-0142|925" $T/app.log && { echo "FAIL: PII in app log (lines above)"; exit 1; } || echo "app log: no PII"
