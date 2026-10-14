"""First-run setup wizard on a brand-new install (no ADMIN_PASSWORD, no FF_DATA_KEY). Run: python -m pytest test/test_setup.py"""
import os, sys, tempfile, pathlib, re
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
for k in ("ADMIN_PASSWORD", "DASHBOARD_PASSWORD", "FF_DATA_KEY", "PUBLIC_URL", "TZ", "RESEND_API_KEY", "INBOUND_SECRET"): os.environ.pop(k, None)
os.environ.update(DB_PATH=f"{T}/app.db", DATA_DIR=T, FF_DATA_KEY_FILE=f"{T}/no-such-file", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=f"{T}/no-profiles",
                  SITES_FILE=str(ROOT / "test/sites.test.yaml"), SCHEDULER="false", DRY_RUN="true", COOKIE_SECURE="false", SCAN_ON_EDIT="false", UPDATE_CHECK="false")
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, db, onboarding, config, crypto, remover
PW = "a long and quiet harbor 2026"

def client(): return TestClient(main.app, follow_redirects=False)
def csrf(c, path): return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)

@pytest.fixture(scope="module")
def admin():
    c = client(); tok = onboarding._TOKEN[0]
    assert c.get(f"/setup?token={tok}").status_code == 303
    r = c.post("/setup", data={"name": "Alex Rivera", "username": "alex", "email": "alex@example.com", "password": PW, "password2": PW})
    assert r.status_code == 303 and r.headers["location"] == "/setup/2"; return c

def test_key_generated_on_first_boot():
    assert crypto.KEY_SOURCE == "generated" and pathlib.Path(T, "secrets", "ff_data_key").exists()
    assert oct(os.stat(pathlib.Path(T, "secrets", "ff_data_key")).st_mode)[-3:] == "600"

def test_fresh_install_redirects_to_setup_and_needs_token(capsys):
    assert onboarding._TOKEN[0] and len(onboarding._TOKEN[0]) >= 24
    c = client()
    for p in ("/", "/login", "/admin/heartbeat", "/family"): assert c.get(p).headers["location"] == "/setup"
    r = c.get("/setup"); assert r.status_code == 403 and "Setup URL" in r.text           # no token: instructions, no form
    assert 'name="password"' not in r.text
    assert c.get("/setup?token=wrong").status_code == 403
    assert c.post("/setup", data={"name": "Eve", "username": "eve", "email": "e@example.com", "password": PW, "password2": PW}).status_code == 403
    assert not db.conn().execute("SELECT 1 FROM users").fetchone()

def test_token_moves_to_cookie_and_step1_validates():
    c = client(); r = c.get(f"/setup?token={onboarding._TOKEN[0]}")
    assert r.status_code == 303 and r.headers["location"] == "/setup" and "ff_setup=" in r.headers["set-cookie"] and "path=/setup" in r.headers["set-cookie"].lower()
    p = c.get("/setup"); assert p.status_code == 200 and "Create the admin account" in p.text and "data-strength" in p.text
    bad = {"name": "Alex Rivera", "username": "alex", "email": "alex@example.com", "password": "short", "password2": "short"}
    assert c.post("/setup", data=bad).status_code == 400
    assert c.post("/setup", data={**bad, "password": PW, "password2": PW + "x"}).status_code == 400
    assert c.post("/setup", data={**bad, "username": "A!", "password": PW, "password2": PW}).status_code == 400
    assert c.post("/setup", data={**bad, "email": "nope", "password": PW, "password2": PW}).status_code == 400
    assert c.post("/setup", data={**bad, "password": "alex alex alex alex", "password2": "alex alex alex alex"}).status_code == 400

def test_admin_created_token_burned(admin):
    u = db.conn().execute("SELECT * FROM users").fetchall(); assert len(u) == 1 and u[0]["role"] == "admin" and u[0]["name"] == "Alex Rivera"
    raw = __import__("sqlite3").connect(os.environ["DB_PATH"]).execute("SELECT email, name FROM users").fetchone(); assert all(v.startswith("enc1:") for v in raw)
    assert onboarding._TOKEN[0] is None
    assert client().get("/setup").headers["location"] == "/login"          # closed for everyone else
    assert admin.get("/setup").headers["location"] == "/setup/2"           # admin resumes where they left off

def test_step_requires_admin_and_csrf(admin):
    assert client().get("/setup/2").status_code == 303
    assert admin.post("/setup/2", data={"APP_NAME": "X"}).status_code == 403

def test_step2_basics(admin):
    t = csrf(admin, "/setup/2")
    assert admin.post("/setup/2", data={"csrf": t, "action": "skip"}).status_code == 400    # required step
    assert admin.post("/setup/2", data={"csrf": t, "APP_NAME": "Rivera Privacy", "TZ": "Mars/Base", "HOME_STATE": "OR"}).status_code == 400
    assert admin.post("/setup/2", data={"csrf": t, "APP_NAME": "Rivera Privacy", "PUBLIC_URL": "ftp://x", "TZ": "America/Chicago", "HOME_STATE": "OR"}).status_code == 400
    r = admin.post("/setup/2", data={"csrf": t, "APP_NAME": "Rivera Privacy", "PUBLIC_URL": "https://privacy.example.org", "TZ": "America/Chicago", "HOME_STATE": "OR"})
    assert r.headers["location"] == "/setup/3"
    assert config.get("APP_NAME") == "Rivera Privacy" and config.get("TZ") == "America/Chicago" and config.get("HOME_STATE") == "OR" and config.get("MAIL_DOMAIN") == "example.org"
    assert os.environ["TZ"] == "America/Chicago" and "OCPA" in admin.get("/setup/2").text      # Oregon law shown

def test_back_button(admin):
    t = csrf(admin, "/setup/3"); assert admin.post("/setup/3", data={"csrf": t, "action": "back"}).headers["location"] == "/setup/2"
    p = admin.get("/setup/1"); assert p.status_code == 200 and "Your admin account" in p.text and 'name="password"' not in p.text

def test_step3_email_and_test_button(admin, monkeypatch):
    t = csrf(admin, "/setup/3")
    assert admin.post("/setup/3", data={"csrf": t, "RESEND_API_KEY": "sk_wrong", "FROM_ADDRESS": "privacy@example.org"}).status_code == 400
    r = admin.post("/setup/3", data={"csrf": t, "action": "test", "FROM_ADDRESS": "privacy@example.org"})
    assert "add+the+API+key" in r.headers["location"] or "add%20the%20API%20key" in r.headers["location"]
    sent = []
    monkeypatch.setattr(remover, "_resend", lambda frm, to, rto, subj, body, att, ref, h=None: sent.append((frm, to, subj)) or "sent:abc")
    r = admin.post("/setup/3", data={"csrf": t, "action": "test", "RESEND_API_KEY": "re_test_1234567890", "FROM_ADDRESS": "privacy@example.org"})
    assert sent and sent[0][1] == "alex@example.com" and "privacy@example.org" in sent[0][0]
    assert "sent" in admin.get(r.headers["location"]).text
    assert remover.DRY and config.get("DRY_RUN") == "true"                 # test send doesn't flip dry run
    assert "re_test_1234567890" not in admin.get("/setup/3").text          # secret is write-only
    assert admin.post("/setup/3", data={"csrf": t}).headers["location"] == "/setup/4"

def test_step4_inbound_instructions(admin):
    p = admin.get("/setup/4").text; sec = config.get("INBOUND_SECRET")
    assert sec and len(sec) >= 32 and sec in p and "https://privacy.example.org/inbound/email" in p and "message.raw" in p and 'data-copy="wurl"' in p
    t = csrf(admin, "/setup/4"); assert admin.post("/setup/4", data={"csrf": t, "action": "skip"}).headers["location"] == "/setup/5"
    assert 4 in onboarding.state()["skipped"]

def test_step5_optional_keys(admin):
    t = csrf(admin, "/setup/5")
    assert admin.post("/setup/5", data={"csrf": t, "CAPTCHA_PROVIDER": "capsolver"}).status_code == 400     # provider without key
    assert admin.post("/setup/5", data={"csrf": t, "CAPTCHA_PROVIDER": "evil"}).status_code == 400
    r = admin.post("/setup/5", data={"csrf": t, "CAPTCHA_PROVIDER": "off", "UPDATE_CHANNEL": "beta", "AUTO_UPDATE": "true"})
    assert r.headers["location"] == "/setup/6" and config.get("UPDATE_CHANNEL") == "beta" and config.get("AUTO_UPDATE") == "true"

def test_step6_first_person(admin):
    t = csrf(admin, "/setup/6"); p = admin.get("/setup/6").text; assert 'value="Alex Rivera"' in p
    assert admin.post("/setup/6", data={"csrf": t, "full_name": "Alex"}).status_code == 400
    r = admin.post("/setup/6", data={"csrf": t, "full_name": "Alex Rivera", "city": "Portland", "state": "OR", "email": "alex@example.com"})
    assert r.headers["location"] == "/setup/7"
    pe = db.conn().execute("SELECT * FROM people WHERE id='alex'").fetchone(); assert pe and pe["full_name"] == "Alex Rivera" and pe["state"] == "OR" and pe["city"] == "Portland"
    assert "Open my profile" in admin.get("/setup/6").text

def test_step7_done_checklist_and_key(admin):
    p = admin.get("/setup/7").text; key = open(pathlib.Path(T, "secrets", "ff_data_key")).read().strip()
    assert "Save your encryption key" in p and key in p and "Inbound replies" in p and "Dry run switched off" in p
    assert onboarding.show_card(db.conn(), db.conn().execute("SELECT * FROM users").fetchone())
    t = csrf(admin, "/setup/7"); assert admin.post("/setup/7", data={"csrf": t, "action": "finish", "key_ack": "1"}).headers["location"] == "/"
    st = onboarding.state(); assert st["done"] and st["key_ack"]
    h = admin.get("/").text; assert "Setup checklist" in h and "Resume setup" in h     # inbound was skipped, so the card stays
    assert admin.get("/setup").headers["location"] == "/setup/7"

def test_settings_editable_in_heartbeat(admin):
    h = admin.get("/admin/heartbeat?tab=settings").text
    for k in ("HOME_STATE", "f-TZ", "UPDATE_CHANNEL", "CAPTCHA_PROVIDER"): assert k in h

def test_card_dismiss(admin):
    t = csrf(admin, "/"); assert admin.post("/setup-card/dismiss", data={"csrf": t}).status_code == 303
    assert "Setup checklist" not in admin.get("/").text

def test_dry_run_still_on():
    assert remover.DRY is True and main.has_users()
