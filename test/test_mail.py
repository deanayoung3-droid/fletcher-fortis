"""v1.2.3 sending fix: Resend calls carry a User-Agent (Cloudflare 1010 otherwise), errors are readable, invites show why they failed,
and Heartbeat has 'Send test email to me' with Resend's exact answer. No real email is sent."""
import os, sys, tempfile, pathlib, re, io, json, urllib.error, urllib.request
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver",
                  RESEND_API_KEY="re_test_dummy_key", FROM_ADDRESS="privacy@example.org")
sys.path.insert(0, str(ROOT / "optout"))
from fastapi.testclient import TestClient
import main, remover, invites, db
APW = "admin correct horse battery"

def fake_urlopen(status, body, seen):
    def f(rq, timeout=0):
        seen.append(dict(rq.header_items()))
        if status == 200: return io.BytesIO(json.dumps({"id": "abc123"}).encode())
        raise urllib.error.HTTPError(rq.full_url, status, "x", {}, io.BytesIO(body))
    return f

def test_user_agent_and_readable_errors(monkeypatch):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(403, b'{"statusCode":403,"message":"The example.org domain is not verified. Please, add and verify your domain.","name":"validation_error"}', seen))
    st = remover._resend("A <privacy@example.org>", "me@example.org", "privacy@example.org", "s", "t", None, "r1")
    assert st == "error: Resend 403: The example.org domain is not verified. Please, add and verify your domain."
    ua = {k.lower(): v for k, v in seen[0].items()}["user-agent"]; assert ua.startswith("FletcherFortis/") and "urllib" not in ua.lower()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(403, b"error code: 1010", seen))
    assert "Cloudflare 1010" in remover._resend("A <a@example.org>", "b@example.org", "a@example.org", "s", "t", None, "r2")
    assert "re_test_dummy_key" not in remover.resend_error(401, b'{"message":"API key is invalid"}')

def test_invite_failure_shown_and_heartbeat_test(monkeypatch):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(403, b'{"message":"The example.org domain is not verified.","name":"validation_error"}', seen))
    monkeypatch.setattr(invites, "email_ready", lambda: True)
    a = TestClient(main.app, follow_redirects=False); a.post("/login", data={"username": "alex", "password": APW})
    t = re.search(r'name="csrf" value="([^"]+)"', a.get("/add").text).group(1)
    r = a.post("/add", data={"csrf": t, "full_name": "Pat Example", "city": "Springfield", "state": "CA", "needs_auth": "1", "email": "pat@example.org"})
    assert "ist=failed" in r.headers["location"] and seen   # invites go out in dry run (family, not brokers)
    page = a.get(r.headers["location"]).text; assert "Not sent: Resend 403: The example.org domain is not verified." in page
    hb = a.get("/admin/heartbeat").text; assert "Send test email to me" in hb and "domain is not verified" in hb and "1 family invite failed" in hb
    c = db.conn(); c.execute("UPDATE users SET email=? WHERE username='alex'", (db.enc("alex@example.org"),)); c.commit()
    t = re.search(r'name="csrf" value="([^"]+)"', hb).group(1)
    r = a.post("/admin/heartbeat/test-email", data={"csrf": t}); assert "Test%20email%20failed%3A%20Resend%20403" in r.headers["location"]
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(200, b"", seen))
    r = a.post("/admin/heartbeat/test-email", data={"csrf": t}); assert "abc123" in r.headers["location"]
