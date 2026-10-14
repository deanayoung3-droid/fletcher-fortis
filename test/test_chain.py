"""v1.3.0: per-broker method chain (scan -> solver -> stealth -> proxy; form -> email -> registry -> blind), attempts, Coverage."""
import os, sys, tempfile, pathlib, re, asyncio
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver",
                  CHAIN_BACKOFF_MS="0", HEALTH_FILE=f"{T}/health.json")
os.environ.pop("PROXY_URL", None)
sys.path.insert(0, str(ROOT / "optout"))
from fastapi.testclient import TestClient
import main, db, chain, scanner, remover, config, lifecycle
APW = "admin correct horse battery"
def admin():
    c = TestClient(main.app, follow_redirects=False); c.post("/login", data={"username": "alex", "password": APW}); return c
def sign(pid):
    c = db.conn(); c.execute("UPDATE people SET esign_at=? WHERE id=?", (db.now(), pid)); c.commit()
def person(pid): return db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
def site(sid): return next(s for s in scanner.SITES if s["id"] == sid)
def listing(pid, sid, status="candidate"):
    c = db.conn(); lid = c.execute("INSERT INTO listings(person_id,site,url,first_seen,status) VALUES(?,?,?,?,?)", (pid, sid, db.enc(f"https://x.invalid/{sid}/{os.urandom(3).hex()}"), db.now(), status)).lastrowid; c.commit(); return lid
def tries(pid, sid): return [(a["method"], a["result"]) for a in chain.ensure().execute("SELECT * FROM attempts WHERE person_id=? AND site=? ORDER BY id", (pid, sid))]

class Page:
    url = "https://x.invalid/"
    async def wait_for_timeout(self, ms): pass
class Ctx:
    async def new_page(self): return Page()
    async def close(self): pass

def fake_scan(results):
    seq = list(results)
    async def scan(page, c, p, s, dpage=None, chain=False):
        r = seq.pop(0); scanner.LAST.clear(); scanner.LAST.update(result=r, detail=r, url="https://x.invalid/s", html=""); return 1 if r == "found" else 0
    return scan

def test_dry_run_chain_falls_through_captcha_form_to_email():
    sign("alex"); lid = listing("alex", "mockform")
    site("mockform")["optout"]["to"] = "privacy@form.invalid"
    try: st = remover.approve(lid)
    finally: site("mockform")["optout"].pop("to")
    assert ("form", "skipped") in tries("alex", "mockform") and ("email", "dry_run") in tries("alex", "mockform")
    assert db.conn().execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()["status"] == "submitted"
    assert db.conn().execute("SELECT state FROM outbox WHERE listing_id=? ORDER BY id DESC", (lid,)).fetchone()["state"] == "dry_run"   # DRY_RUN gates the send

def test_discovery_chain_stealth_after_block(monkeypatch):
    sign("alex"); monkeypatch.setattr(scanner, "scan_person", fake_scan(["blocked", "not_found"]))
    async def sc(b): return Ctx()
    monkeypatch.setattr(chain, "stealth_context", sc)
    asyncio.run(chain.discover(None, None, Page(), None, db.conn(), person("alex"), site("mockemail")))
    t = tries("alex", "mockemail")
    assert t[-2:] == [("scan", "blocked"), ("stealth", "not_found")]
    assert not db.conn().execute("SELECT 1 FROM listings WHERE site='mockemail' AND status='manual_check'").fetchone()

def test_all_blocked_goes_to_coverage_not_needs_you_and_blind_is_gated(monkeypatch):
    sign("alex"); monkeypatch.setattr(scanner, "scan_person", fake_scan(["captcha", "blocked"]))
    async def sc(b): return Ctx()
    monkeypatch.setattr(chain, "stealth_context", sc)
    asyncio.run(chain.discover(None, None, Page(), None, db.conn(), person("alex"), site("mockdeep")))
    t = tries("alex", "mockdeep")
    assert ("solver", "skipped") in t and ("stealth", "blocked") in t and ("proxy", "skipped") in t
    lid = db.conn().execute("SELECT id FROM listings WHERE site='mockdeep' AND status='manual_check'").fetchone()["id"]
    a = admin(); nt = a.get("/needs-you").text; assert "Check DeepFinder" not in nt
    lifecycle.tick(); assert ("blind", "skipped") in tries("alex", "mockdeep")   # BLIND_REQUESTS off: nothing filed
    assert db.conn().execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()["status"] == "manual_check"
    config.save({"BLIND_REQUESTS": "true"})
    try: lifecycle.tick()
    finally: config.save({"BLIND_REQUESTS": "false"})
    assert ("blind", "dry_run") in tries("alex", "mockdeep") and db.conn().execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()["status"] == "submitted"
    page = a.get(f"/listing/{lid}").text; assert "Methods tried" in page and "Stealth retry" in page and "Blind opt-out" in page

def test_blind_needs_consent():
    c = db.conn(); c.execute("UPDATE people SET esign_at=NULL, auth_pdf=NULL WHERE id='sam'"); c.commit()
    lid = listing("sam", "mockemail", "manual_check"); config.save({"BLIND_REQUESTS": "true"})
    try: lifecycle.tick()
    finally: config.save({"BLIND_REQUESTS": "false"})
    assert db.conn().execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()["status"] == "manual_check" and not tries("sam", "mockemail")

def test_human_only_goes_to_needs_you():
    sign("alex"); s = site("mockemail"); s["optout"]["requires"] = "id"
    try: lid = listing("alex", "mockemail"); remover.approve(lid); page = admin().get("/needs-you").text
    finally: s["optout"].pop("requires")
    assert "upload a photo ID" in page and ("human", "needs_human") in tries("alex", "mockemail")

def test_proxy_only_on_request_and_coverage_page():
    assert "proxy" not in scanner.launch_kw()
    os.environ["PROXY_URL"] = "http://u:p@proxy.invalid:8080"
    try: assert scanner.launch_kw(proxy=True)["proxy"]["server"] == "http://proxy.invalid:8080" and "proxy" not in scanner.launch_kw()
    finally: os.environ.pop("PROXY_URL")
    assert "PROXY_URL" in config.FIELDS and config.FIELDS["PROXY_URL"][2] == "secret"
    rows, tot = chain.coverage()
    assert tot["brokers"] == len(rows) and 0 <= tot["pct"] <= 100 and tot["covered"] >= 1
    page = admin().get("/admin/heartbeat").text; assert 'id="coverage"' in page and "covered" in page

def test_going_live_requeues_dry_run_drafts(monkeypatch):
    sign("alex"); lid = listing("alex", "mockemail"); remover.approve(lid)
    c = db.conn(); assert c.execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()["status"] == "submitted"
    n = lifecycle.requeue_dry(c); assert n >= 1
    assert c.execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()["status"] == "approved"
    assert not c.execute("SELECT 1 FROM outbox WHERE state='dry_run'").fetchone()
    assert lifecycle.requeue_dry(c) == 0   # idempotent

def test_live_chain_is_queued_on_the_single_worker(monkeypatch):
    import scanjobs, threading
    sign("alex"); lid = listing("alex", "mockemail"); q = []
    monkeypatch.setattr(scanjobs, "enqueue_chain", lambda l: q.append(l) or True)
    started = []; monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    monkeypatch.setattr(remover, "DRY", False); monkeypatch.setattr(remover, "can_send", lambda p: (True, ""))
    remover.approve(lid)
    assert q == [lid] and not started   # no thread per listing: a burst can never launch many Chromiums
