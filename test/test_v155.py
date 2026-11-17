"""v1.5.5: public /invite pages load CSS, JS, fonts and icons from /invite/static, not /static (Cloudflare Access blocks /static)."""
import os, sys, tempfile, pathlib, json, re, html.parser
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
os.environ.pop("RESEND_API_KEY", None)
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, db, intake, invites
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

class _Assets(html.parser.HTMLParser):
    """Collect src/href values from tags that load a file (stylesheet, script, icon, font, image)."""
    def __init__(self):
        super().__init__(); self.urls = []
    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "link" and d.get("href"): self.urls.append(d["href"])
        if tag in ("script", "img", "source") and d.get("src"): self.urls.append(d["src"])
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

def asset_urls(text):
    p = _Assets(); p.feed(text)
    for u in re.findall(r"url\(([^)]+)\)", text):
        u = u.strip().strip("\"'")
        if u and not u.startswith("#"): p.urls.append(u)
    return p.urls

def assert_public_assets(text, label):
    urls = asset_urls(text)
    assert urls, f"{label}: expected at least one asset"
    for u in urls:
        assert u.startswith("/invite/") or u.startswith("data:"), f"{label}: asset {u!r} is not under /invite/"
    joined = " ".join(urls)
    assert "app.css" in joined and "theme.js" in joined, f"{label}: missing css/js {urls}"
    assert "?v=" in joined, f"{label}: cache-busting query missing {urls}"

def test_version():
    assert open(ROOT / "VERSION").read().strip() == "1.5.5"

def test_invite_static_serves_public_assets_only():
    c = client()
    css = c.get("/invite/static/app.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "no-store" not in css.headers.get("cache-control", "") and "max-age=" in css.headers.get("cache-control", "")
    assert "/static/" not in css.text
    assert "url(fonts/Geist-Variable.woff2)" in css.text and "url(fonts/GeistMono-Variable.woff2)" in css.text
    js = c.get("/invite/static/app.js?v=1.5.5")
    assert js.status_code == 200 and "data-add-row" in js.text
    theme = c.get("/invite/static/theme.js")
    assert theme.status_code == 200
    ico = c.get("/invite/static/favicon.svg")
    assert ico.status_code == 200 and "svg" in ico.headers["content-type"]
    logo = c.get("/invite/static/logo.svg")
    assert logo.status_code == 200
    font = c.get("/invite/static/fonts/Geist-Variable.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"
    # not in the public allow-list
    assert c.get("/invite/static/OFL.txt").status_code == 404
    assert c.get("/invite/static/../main.py").status_code == 404
    assert c.get("/invite/static/fonts/OFL.txt").status_code == 404
    # signed-in pages still have the original path, also cacheable
    old = c.get("/static/app.css")
    assert old.status_code == 200 and "max-age=" in old.headers.get("cache-control", "")

def test_public_pages_only_reference_invite_assets(admin):
    # intake form
    tok, _, _ = intake.create(None, "outside")
    form = client().get(f"/invite/intake/{tok}")
    assert form.status_code == 200 and "Your details and signature" in form.text
    assert_public_assets(form.text, "intake form")
    # a token that was never issued renders the public error page (also under /invite/static)
    missing = client().get("/invite/intake/not-a-real-token")
    assert missing.status_code == 404
    assert_public_assets(missing.text, "unknown intake link")
    tok2, csrf_tok, _e = intake.create(None, "outside")
    c = db.conn(); c.execute("UPDATE intake_links SET expires=? WHERE token_hash=?", ("2000-01-01T00:00:00", __import__("crypto").token_hash(tok2))); c.commit()
    expired = client().get(f"/invite/intake/{tok2}")
    assert expired.status_code == 410 and "expired" in expired.text.lower()
    assert_public_assets(expired.text, "expired intake link")
    # thank-you after a signed submit
    tok3, csrf_tok, _e = intake.create(None, "outside")
    body = {"csrf": csrf_tok, "full_name": "Morgan Sample", "addr_city": "Springfield", "addr_state": "CA",
            "email": "morgan.sample@example.com", "typed_name": "Morgan Sample", "agree": "yes", "sig": SIG}
    done = client().post(f"/invite/intake/{tok3}", data=body)
    assert done.status_code == 200 and "Signed" in done.text
    assert_public_assets(done.text, "intake thank-you")
    # a used link after submit
    again = client().get(f"/invite/intake/{tok3}")
    assert again.status_code == 410
    assert_public_assets(again.text, "already-used intake link")
    # family invite (password page) also lives under /invite/
    r = admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": "Quinn Example", "city": "Springfield", "state": "CA", "needs_auth": "1", "email": "quinn@example.org"})
    loc = r.headers["location"]; tok4 = re.search(r"inv=([\w-]+)", loc).group(1)
    pw = client().get(f"/invite/{tok4}")
    assert pw.status_code == 200 and "Set your password" in pw.text
    assert_public_assets(pw.text, "family invite")
    # login uses the same public shell
    login = client().get("/login")
    assert login.status_code == 200
    assert_public_assets(login.text, "login")
