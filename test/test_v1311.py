"""v1.3.11: CAPTCHA-solver errors are temporary (retries with backoff), solver account balance on Heartbeat.
No network, no real email, no real browser."""
import os, sys, tempfile, pathlib, re, json
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver",
                  FROM_ADDRESS="privacy@example.org")
sys.path.insert(0, str(ROOT / "optout"))
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, db, regreq, registries, remover, inbound, regforms, scanjobs, score
APW = "admin correct horse battery"
BROKERS = [{"id": "leadpost", "name": "LeadPost LLC", "email": "privacy@leadpost.invalid", "category": "marketing", "registries": ["CA", "VT"], "channel": "email"},
           {"id": "acme", "name": "Acme Data Inc.", "email": "privacy@acme.invalid", "category": "marketing", "registries": ["CA", "VT"], "channel": "email"},
           {"id": "formonly", "name": "Form Only Co", "email": "", "optout_url": "https://formonly.invalid/privacy", "category": "marketing", "registries": ["VT"], "channel": "form"}]

@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(registries, "brokers", lambda: BROKERS); monkeypatch.setattr(registries, "by_id", lambda: {b["id"]: b for b in BROKERS})
    ov = tmp_path / "ov.yaml"; ov.write_text("leadpost:\n  url: https://app.leadpost.invalid/PrivacyRequest\n  source: test\n"); monkeypatch.setattr(regforms, "OVERRIDES", str(ov))
    monkeypatch.setattr(remover, "DRY", False)
    c = db.conn(); regreq._ensure(c)
    for t in ("listings", "outbox", "inbox", "reg_requests", "broker_forms"): c.execute(f"DELETE FROM {t}")
    c.execute("UPDATE people SET esign_at=NULL, auth_pdf=NULL, send_enabled=0"); c.commit()

def sign(*pids):
    c = db.conn()
    for p in pids: c.execute("UPDATE people SET esign_at=?, send_enabled=1 WHERE id=?", (db.now(), p))
    c.commit()
def out(pid, to, state="sent"):
    c = db.conn(); oid = c.execute("INSERT INTO outbox(ts,person_id,to_addr,subject,body,state,channel) VALUES(?,?,?,?,?,?,?)", (db.now(), pid, to, db.enc("s"), db.enc("b"), state, "registry_email")).lastrowid; c.commit(); return oid
def reg(pid, broker, status, oid=None):
    c = db.conn(); b = next(x for x in BROKERS if x["id"] == broker)
    c.execute("INSERT OR REPLACE INTO reg_requests(person_id,broker,category,status,channel,target,outbox_id,created,updated) VALUES(?,?,?,?,?,?,?,?,?)", (pid, broker, "marketing", status, b["channel"], b["email"] or b.get("optout_url"), oid, db.now(), db.now())); c.commit()
    return c.execute("SELECT id FROM reg_requests WHERE person_id=? AND broker=?", (pid, broker)).fetchone()["id"]
def status(rid): return db.conn().execute("SELECT * FROM reg_requests WHERE id=?", (rid,)).fetchone()

QUOTE = "\n\nOn Wed, Oct 14, 2026 at 3:16 PM Fletcher Fortis Privacy <privacy@example.org> wrote:\n> To Acme Data Inc. privacy team,\n> see https://privacy.example.org/request/123 and https://www.acme.invalid/webform/ours\n"
import automation, config, io, urllib.error, datetime
KEY = "CAP-secret-key-1234567890"

@pytest.fixture
def capsolver(monkeypatch):
    vals = {"CAPTCHA_PROVIDER": "capsolver", "CAPTCHA_API_KEY": KEY}
    real = config.get; monkeypatch.setattr(config, "get", lambda k, d=None: vals.get(k, real(k, d)))
    c = db.conn(); c.execute("DELETE FROM app_meta WHERE k IN ('captcha_balance','regform_solver_requeue')"); c.commit()

def http_error(body, code=400):
    def raise_(*a, **k): raise urllib.error.HTTPError("https://api.capsolver.com/x", code, "Bad Request", {}, io.BytesIO(json.dumps(body).encode()))
    return raise_

def test_solver_error_shows_the_providers_reason_and_never_the_key(capsolver, monkeypatch):
    monkeypatch.setattr(automation.urllib.request, "urlopen", http_error({"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE", "errorDescription": f"Insufficient balance for {KEY}"}))
    tok, why = automation.solve("https://x.invalid/form", '<div class="g-recaptcha" data-sitekey="6Lc_abcdefghijklmnopqrstuvwxyz0123456789"></div>', None, dry=False)
    assert tok is None and why.startswith(automation.SOLVER_ERR) and "ERROR_ZERO_BALANCE" in why and KEY not in why and "***" in why

def test_balance_check_on_heartbeat(capsolver, monkeypatch):
    monkeypatch.setattr(automation, "_post", lambda url, data, js=False, timeout=30: {"errorId": 0, "balance": 3.21} if url.endswith("getBalance") and data["clientKey"] == KEY else {})
    b = automation.check_balance(db.conn(), force=True); assert (b["ok"], b["balance"], b["status"]) == (True, 3.21, "ok") and KEY not in json.dumps(b)
    c = TestClient(main.app, follow_redirects=False); c.post("/login", data={"username": "alex", "password": APW})
    t = c.get("/admin/heartbeat?captcha=1").text
    assert "data-captcha-card" in t and "$3.21" in t and KEY not in t
    monkeypatch.setattr(automation, "_post", lambda *a, **k: {"errorId": 1, "errorCode": "ERROR_KEY_DENIED_ACCESS", "errorDescription": "key is invalid"})
    b = automation.check_balance(db.conn(), force=True); assert not b["ok"] and "ERROR_KEY_DENIED_ACCESS" in b["status"]
    monkeypatch.setattr(automation, "_post", lambda *a, **k: {"errorId": 0, "balance": 0.01})
    assert automation.check_balance(db.conn(), force=True)["status"] == "low balance: top up"

def test_solver_errors_retry_with_backoff_then_needs_you(monkeypatch):
    sign("alex"); rid = reg("alex", "acme", "form_queued")
    err = lambda p, u, i: ("solver_error", "solver error: capsolver: HTTP 500 Internal Server Error")
    nexts = []
    for _ in range(3):
        assert regforms.run(rid, filler=err) == "form_queued"; nexts.append(status(rid)["form_next"])
    gaps = [(datetime.datetime.fromisoformat(n) - datetime.datetime.now()).total_seconds() / 60 for n in nexts]
    assert 25 < gaps[0] < 35 and 110 < gaps[1] < 130 and 350 < gaps[2] < 370   # +30 min, +2 h, +6 h
    started = []; monkeypatch.setattr(scanjobs, "enqueue_regform", lambda r: started.append(r) or True)
    regforms.tick(db.conn()); assert rid not in started   # waits for its backoff
    assert regforms.run(rid, filler=err) == "needs_human" and "kept failing (4 attempts)" in status(rid)["form_detail"]

def test_solver_error_requests_go_back_in_the_queue_once(capsolver):
    sign("alex", "sam"); c = db.conn()
    a = reg("alex", "acme", "needs_human"); b = reg("sam", "acme", "needs_human"); k = reg("alex", "leadpost", "needs_human")
    c = db.conn(); c.execute("UPDATE reg_requests SET form_detail='capsolver error: HTTPError', form_tries=1 WHERE id IN (?,?)", (a, b))
    c.execute("UPDATE reg_requests SET form_detail='form has a CAPTCHA (solver off)' WHERE id=?", (k,)); c.commit()
    assert regforms.requeue_solver_errors(db.conn()) == 2 and regforms.requeue_solver_errors(db.conn()) == 0
    assert [(status(x)["status"], status(x)["form_tries"]) for x in (a, b)] == [("form_queued", 0)] * 2 and status(k)["status"] == "needs_human"
