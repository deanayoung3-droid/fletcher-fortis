"""v1.2.3 sending fix: Resend calls carry a User-Agent (Cloudflare 1010 otherwise), errors are readable, invites show why they failed,
and Heartbeat has 'Send test email to me' with Resend's exact answer. No real email is sent."""
import os, sys, tempfile, pathlib, re, io, json, time, urllib.error, urllib.request
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

def test_stale_failures_resolved_after_delivery():
    """v1.3.4: a failure older than the latest successful delivery shows as resolved and the pill follows the newest result."""
    import heartbeat
    c = db.conn(); c.execute("DELETE FROM outbox"); c.execute("DELETE FROM events")
    c.execute("INSERT INTO outbox(ts, to_addr, subject, body, state, channel) VALUES ('2026-10-14T13:24:00','x@example.org','s','b','error: HTTP Error 403: Forbidden','email')")
    c.execute("INSERT INTO outbox(ts, to_addr, subject, body, state, channel) VALUES ('2026-10-14T15:16:00','x@example.org','s','b','sent','email')"); c.commit()
    a = TestClient(main.app, follow_redirects=False); a.post("/login", data={"username": "alex", "password": APW})
    hb = a.get("/admin/heartbeat").text
    assert "data-mail-error" not in hb and "1 earlier error, resolved by a later delivery" in hb and "Resolved" in hb
    assert re.search(r"data-mail-state>.*?Working", hb, re.S)
    assert heartbeat.resolved("2026-10-14T13:24:00", "2026-10-14T15:16:00") and not heartbeat.resolved("2026-10-14T16:00:00", "2026-10-14T15:16:00")
    c.execute("INSERT INTO outbox(ts, to_addr, subject, body, state, channel) VALUES ('2026-10-14T16:00:00','x@example.org','s','b','error: Resend 429: Too many requests','email')"); c.commit()
    hb = a.get("/admin/heartbeat").text
    assert "data-mail-error" in hb and re.search(r"data-mail-state>.*?Failing", hb, re.S) and "Resend 429" in hb

def test_errors_card_ignores_recovered_errors():
    import heartbeat
    c = db.conn(); c.execute("DELETE FROM events"); n = db.now()
    import datetime; old = (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
    c.execute("INSERT INTO events(ts, kind, detail) VALUES (?, 'scan_error', 'Timeout')", (old,))
    c.execute("INSERT INTO events(ts, kind, detail) VALUES (?, 'scan_complete', 'ok')", (n,)); c.commit()
    e = {s["key"]: s for s in heartbeat.status(main.C)}["errors"]
    assert e["state"] == "ok" and "1 resolved" in e["value"]
    c.execute("INSERT INTO events(ts, kind, detail) VALUES (?, 'registry_error', 'boom')", (n,)); c.commit()
    e = {s["key"]: s for s in heartbeat.status(main.C)}["errors"]
    assert e["state"] == "warn" and "registry_error" in e["detail"]

def test_delivery_card_green_after_later_send():
    import heartbeat, datetime
    c = db.conn(); c.execute("DELETE FROM outbox"); t = lambda h: (datetime.datetime.now() - datetime.timedelta(hours=h)).isoformat(timespec="seconds")
    c.execute("INSERT INTO outbox(ts, to_addr, subject, body, state) VALUES (?,'x@example.org','s','b','error: HTTP Error 403: Forbidden')", (t(3),))
    c.execute("INSERT INTO outbox(ts, to_addr, subject, body, state) VALUES (?,'x@example.org','s','b','sent')", (t(1),)); c.commit()
    d = {s["key"]: s for s in heartbeat.status(main.C)}["delivery"]; assert d["state"] == "ok" and "resolved" in d["detail"]
    c.execute("INSERT INTO outbox(ts, to_addr, subject, body, state) VALUES (?,'x@example.org','s','b','error: Resend 429')", (t(0),)); c.commit()
    d = {s["key"]: s for s in heartbeat.status(main.C)}["delivery"]; assert d["state"] == "bad" and "resolved" not in d["detail"]

def test_resend_retries_429_then_succeeds_and_is_throttled(monkeypatch):
    calls = []
    def f(rq, timeout=0):
        calls.append(time.monotonic())
        if len(calls) < 3: raise urllib.error.HTTPError(rq.full_url, 429, "x", {}, io.BytesIO(b'{"message":"Too many requests"}'))
        return io.BytesIO(json.dumps({"id": "ok1"}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", f); monkeypatch.setattr(remover, "SEND_BACKOFF", (0, 0, 0))
    monkeypatch.setenv("SEND_INTERVAL_MS", "200")
    assert remover._resend("A <a@example.org>", "b@example.org", "a@example.org", "s", "t", None, "r9") == "sent:ok1" and len(calls) == 3
    assert all(b - a >= 0.19 for a, b in zip(calls, calls[1:]))   # never faster than the send interval
    calls.clear(); monkeypatch.setattr(urllib.request, "urlopen", lambda rq, timeout=0: (calls.append(1), (_ for _ in ()).throw(urllib.error.HTTPError(rq.full_url, 403, "x", {}, io.BytesIO(b'{"message":"no"}'))))[1])
    assert remover._resend("A <a@example.org>", "b@example.org", "a@example.org", "s", "t", None, "r10").startswith("error: Resend 403") and len(calls) == 1   # permanent: no retry
    assert remover.retryable("error: Resend 429: Too many requests") and remover.retryable("error: Resend 502: bad gateway") and not remover.retryable("error: Resend 403: no")
