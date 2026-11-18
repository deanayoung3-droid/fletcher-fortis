"""v1.5.6: intake address rows stack on a phone; public pages must not overflow or have unlabeled controls."""
import os, sys, tempfile, pathlib, json, re, time
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
os.environ.pop("RESEND_API_KEY", None)
sys.path.insert(0, str(ROOT / "optout"))
sys.path.insert(0, str(ROOT / "test"))
import pytest
from fastapi.testclient import TestClient
import main, db, intake, crypto
import shot_invite_flow as shots
APW = "admin correct horse battery"
SIG = json.dumps([[[round(i / 20, 4), 0.42] for i in range(12)]])

def client(): return TestClient(main.app, follow_redirects=False)
def csrf(c, url): return re.search(r'name="csrf" value="([^"]+)"', c.get(url).text).group(1)
@pytest.fixture(autouse=True)
def _limits():
    intake._hits.clear(); intake.POST_LIMIT = 10; intake.GET_LIMIT = 60
    yield
    intake._hits.clear(); intake.POST_LIMIT = 10; intake.GET_LIMIT = 60
@pytest.fixture
def admin():
    c = client(); c.post("/login", data={"username": "alex", "password": APW}); return c

def test_version():
    assert open(ROOT / "VERSION").read().strip() == "1.5.6"

def test_changelog_mentions_mobile_address_rows():
    text = (ROOT / "CHANGELOG.md").read_text()
    assert "## [1.5.6]" in text and "Cities and addresses" in text

def test_intake_markup_uses_address_cards():
    tok, _, _ = intake.create(None, "outside")
    html = client().get(f"/invite/intake/{tok}").text
    assert "addr-card" in html and "addr-current-toggle" in html
    assert "Current address" in html and "This is where I live now" in html
    assert 'for="addr-city-0"' in html and 'for="addr-street-0"' in html and 'for="addr-state-0"' in html
    assert "grid-cols-12" not in html
    assert 'for="full_name"' in html and 'for="email-0"' in html and 'for="sigpad"' in html
    css = client().get("/invite/static/app.css").text
    assert ".addr-card" in css and "min-height: 44px" in css and "font-size: 16px" in css
    js = client().get("/invite/static/app.js").text
    assert "data-lab" in js and "data-busy" in js

def test_missing_intake_link_uses_public_closed_page():
    r = client().get("/invite/intake/not-a-real-token")
    assert r.status_code == 404
    assert "Link not valid" in r.text
    assert "not valid" in r.text.lower()
    assert "Back to dashboard" not in r.text
    assert "/invite/static/app.css" in r.text

def test_expired_and_used_titles():
    tok, _, _ = intake.create(None, "outside")
    c = db.conn(); c.execute("UPDATE intake_links SET expires=? WHERE token_hash=?", ("2000-01-01T00:00:00", crypto.token_hash(tok))); c.commit()
    expired = client().get(f"/invite/intake/{tok}")
    assert expired.status_code == 410 and "Link expired" in expired.text
    tok2, csrf_tok, _ = intake.create(None, "outside")
    body = {"csrf": csrf_tok, "full_name": "Morgan Sample", "addr_city": "Springfield", "addr_state": "CA",
            "email": "morgan.sample@example.com", "typed_name": "Morgan Sample", "agree": "yes", "sig": SIG}
    done = client().post(f"/invite/intake/{tok2}", data=body)
    assert done.status_code == 200 and "Signed" in done.text
    used = client().get(f"/invite/intake/{tok2}")
    assert used.status_code == 410 and "Link already used" in used.text

def test_add_outside_hides_location_fields(admin):
    html = admin.get("/add?relationship=outside").text
    assert "data-location-row" in html and "Name or nickname" in html
    # the location row must carry hidden; a bare .grid class must not un-hide it
    assert "data-location-row hidden" in html or "data-location-row  hidden" in html
    css = admin.get("/static/app.css").text
    assert "[hidden] { display: none !important; }" in css

def test_public_pages_no_overflow_or_unlabeled_controls():
    """Fails if any public intake-flow page overflows horizontally at 375px or has an empty/unlabeled control."""
    from playwright.sync_api import sync_playwright
    server, base = shots.start_server(main.app)
    out = pathlib.Path(os.environ["SHOT_OUT"]) if os.environ.get("SHOT_OUT") else None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            issues = shots.run_audit_and_shots(
                page, context, base, client(), admin_login(), intake, db, csrf, out, widths=(375, 768, 1280)
            )
            browser.close()
    finally:
        shots.stop_server(server)
    assert not issues, "layout issues:\n" + "\n".join(issues)

def admin_login():
    c = client(); c.post("/login", data={"username": "alex", "password": APW}); return c
