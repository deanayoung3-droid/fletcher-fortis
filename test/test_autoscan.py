"""v1.2.3: a scan is queued as soon as a person is added and again when they finish onboarding (SCAN_ON_CREATE, default on)."""
import os, sys, tempfile, pathlib, re
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
os.environ.pop("SCAN_ON_CREATE", None); os.environ.pop("RESEND_API_KEY", None)
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, scanjobs, config, db, remover
APW = "admin correct horse battery"; PW = "orchid lantern river 2026"
def client(): return TestClient(main.app, follow_redirects=False)
def csrf(c, url): return re.search(r'name="csrf" value="([^"]+)"', c.get(url).text).group(1)

@pytest.fixture
def q(monkeypatch):
    calls = []; monkeypatch.setattr(scanjobs, "enqueue", lambda pid, why="manual": calls.append((pid, why)) or True); return calls

@pytest.fixture
def admin():
    c = client(); c.post("/login", data={"username": "alex", "password": APW}); return c

def test_default_on(): assert config.get("SCAN_ON_CREATE") == "true" and "SCAN_ON_CREATE" in config.FIELDS

def _sig(): return '[[' + ','.join(f'[{0.1 + i * 0.06:.2f},{0.3 + (i % 3) * 0.1:.2f}]' for i in range(12)) + ']]'

def test_flow_accept_sign_details_then_first_scan(admin, q):
    r = admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": "Quinn Example", "city": "Springfield", "state": "CA", "needs_auth": "1", "email": "quinn@example.org"})
    pid = re.search(r"/person/(\w+)", r.headers["location"]).group(1); tok = re.search(r"inv=([\w-]+)", r.headers["location"]).group(1)
    assert q == []                                    # never scanned before consent
    page = admin.get(f"/person/{pid}").text
    assert "Waiting for Quinn to accept the invite and sign. Nothing will be scanned or removed until then." in page and "Resend invite" in page
    assert "/scan\"" not in page and "/approve" not in page and "proof.zip" not in page
    t = csrf(admin, f"/person/{pid}")
    assert admin.post(f"/person/{pid}/scan", data={"csrf": t}).status_code == 403
    assert admin.post(f"/person/{pid}/add-listing", data={"csrf": t, "site": "mockemail", "url": "https://x.invalid/a"}).status_code == 403
    import db
    c = db.conn(); lid = c.execute("INSERT INTO listings(person_id,site,url,first_seen) VALUES(?,?,?,?)", (pid, "mockemail", db.enc("https://x.invalid/q"), db.now())).lastrowid; c.commit()
    r = admin.post(f"/listing/{lid}/approve", data={"csrf": t}); assert r.status_code == 403 and "Nothing will be scanned or removed" in r.text
    assert admin.get(f"/listing/{lid}").status_code == 403
    m = client(); r = m.post(f"/invite/{tok}", data={"password": PW, "password2": PW}); assert r.headers["location"] == f"/person/{pid}/authorize?onboard=1"
    sp = m.get(f"/person/{pid}/authorize?onboard=1").text; assert "Accept" in sp and "Sign" in sp and "Your details" in sp
    t = csrf(m, f"/person/{pid}/authorize?onboard=1")
    r = m.post(f"/person/{pid}/authorize", data={"csrf": t, "typed_name": "Quinn Example", "agree": "yes", "onboard": "1", "sig": _sig()})
    assert r.headers["location"] == f"/person/{pid}/profile?onboard=1" and q == []   # signed, but no scan until details are saved
    dp = m.get(f"/person/{pid}/profile?onboard=1").text; assert "Your details" in dp and "Quinn" in dp
    t = csrf(m, f"/person/{pid}/profile?onboard=1")
    base = {"csrf": t, "onboard": "1", "name_kind": "legal", "name_first": "Quinn", "name_middle": "", "name_last": "Example", "addr_street": "", "addr_city": "", "addr_state": "CA", "addr_current": "0"}
    r = m.post(f"/person/{pid}/profile", data=base); assert r.status_code == 400 and "city" in r.text and q == []
    r = m.post(f"/person/{pid}/profile", data=dict(base, addr_city="Springfield", phone=["", ""])); assert r.status_code == 400 and "phone number or email" in r.text
    r = m.post(f"/person/{pid}/profile", data=dict(base, addr_city="Springfield", email="quinn@example.org"))
    assert r.headers["location"] == f"/person/{pid}?welcome=1" and q == [(pid, "onboarding: details saved")]
    assert "Nothing will be scanned" not in admin.get(f"/person/{pid}").text   # unlocked after consent

def test_onboard_invite_queues(admin, q):
    r = admin.post("/family/onboard", data={"csrf": csrf(admin, "/family"), "full_name": "Rory Example", "email": "rory@example.org"})
    assert q == []   # not before consent

def test_setting_off(admin, q, monkeypatch):
    monkeypatch.setitem(config._cache, "SCAN_ON_CREATE", "false")
    admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": "Sky Example", "city": "Springfield", "state": "CA", "needs_auth": "1", "email": "sky@example.org"})
    assert q == []

def test_first_scan_label(admin, monkeypatch):
    import db; c = db.conn(); c.execute("UPDATE people SET esign_at=? WHERE id='alex'", (db.now(),)); c.commit()
    monkeypatch.setattr(scanjobs, "progress", lambda pid: {"state": "running", "done": 3, "total": 40, "why": "new person"})
    assert "First scan running… 3 of 40 sites" in admin.get("/person/alex").text
    monkeypatch.setattr(scanjobs, "progress", lambda pid: {"state": "running", "done": 3, "total": 40, "why": "manual"})
    t = admin.get("/person/alex").text; assert "Scanning… 3 of 40 sites" in t and "First scan running" not in t
