"""v1.2.1: deleting a person (cascade, files, audit without PII) and family invites (always created, 7-day links, status, consent gate)."""
import os, sys, tempfile, pathlib, re, json
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
os.environ.pop("RESEND_API_KEY", None)
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, db, invites, remover, mailer, auth
APW = "admin correct horse battery"

def client(): return TestClient(main.app, follow_redirects=False)
def csrf(c, url): return re.search(r'name="csrf" value="([^"]+)"', c.get(url).text).group(1)
@pytest.fixture
def admin():
    c = client(); c.post("/login", data={"username": "alex", "password": APW}); return c

def add(admin, name, email):
    return admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": name, "city": "Springfield", "state": "CA", "needs_auth": "1", "email": email})

def test_add_requires_email(admin):
    r = add(admin, "Pat Example", ""); assert r.status_code == 400 and "email" in r.text.lower()

def test_add_always_creates_pending_invite_without_email_setup(admin):
    r = add(admin, "Casey Example", "casey@example.org"); assert r.status_code == 303
    loc = r.headers["location"]; pid = re.search(r"/person/(\w+)", loc).group(1); assert "ist=pending" in loc
    tok = re.search(r"inv=([\w-]+)", loc).group(1)
    u = db.conn().execute("SELECT * FROM users WHERE person_id=?", (pid,)).fetchone()
    assert u["invite_state"] == "pending" and u["invite_expires"][:10] == (__import__("datetime").date.today() + __import__("datetime").timedelta(7)).isoformat()
    page = admin.get(loc).text; assert "Invite pending: email not set up" in page and f"/invite/{tok}" in page and "Resend invite" in page
    p = db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone(); assert p["needs_auth"] == 1
    assert remover.can_send(p)[0] is False   # no removals before they sign
    # opening the link marks Opened; resending makes a new link and kills the old one
    assert client().get(f"/invite/{tok}").status_code == 200
    assert invites.status(p, db.conn().execute("SELECT * FROM users WHERE person_id=?", (pid,)).fetchone())[0] == "Opened"
    r = admin.post(f"/person/{pid}/invite", data={"csrf": csrf(admin, f"/person/{pid}")}); tok2 = re.search(r"inv=([\w-]+)", r.headers["location"]).group(1)
    assert tok2 != tok and client().get(f"/invite/{tok}").status_code == 404 and client().get(f"/invite/{tok2}").status_code == 200

def test_invite_sends_in_dry_run_when_email_configured(admin, monkeypatch):
    sent = []
    monkeypatch.setattr(invites, "email_ready", lambda: True)
    monkeypatch.setattr(remover, "provider_ready", lambda: True)
    monkeypatch.setattr(remover, "_resend", lambda *a, **k: sent.append(a[1]) or "sent: id")
    assert remover.DRY
    r = add(admin, "Robin Example", "robin@example.org"); assert "ist=sent" in r.headers["location"] and sent == ["robin@example.org"]
    pid = re.search(r"/person/(\w+)", r.headers["location"]).group(1)
    assert "Sent" in admin.get(f"/person/{pid}").text
    o = db.conn().execute("SELECT state FROM outbox WHERE channel='onboarding' AND person_id=?", (pid,)).fetchone(); assert o["state"] == "sent"

def test_broker_mail_still_blocked_by_dry_run(monkeypatch):
    monkeypatch.setattr(remover, "provider_ready", lambda: True); called = []
    monkeypatch.setattr(remover, "_resend", lambda *a, **k: called.append(1) or "sent")
    assert mailer.send("x@example.org", "s", "t", "<p>t</p>", kind="mail") .startswith("dry run") and not called

def test_delete_person_cascades(admin):
    r = add(admin, "Drew Example", "drew@example.org"); pid = re.search(r"/person/(\w+)", r.headers["location"]).group(1)
    c = db.conn(); os.makedirs(os.environ["SHOT_DIR"], exist_ok=True); os.makedirs(os.environ["AUTH_DIR"], exist_ok=True)
    shot = os.path.join(os.environ["SHOT_DIR"], f"{pid}_spokeo_abc.png"); open(shot, "wb").write(b"x")
    pdf = os.path.join(os.environ["AUTH_DIR"], f"{pid}-esign.pdf"); open(pdf, "wb").write(b"x")
    other = os.path.join(os.environ["SHOT_DIR"], "alex_spokeo_zzz.png"); open(other, "wb").write(b"x")
    lid = c.execute("INSERT INTO listings(person_id,site,url,screenshot,first_seen,status) VALUES(?,?,?,?,?,?)", (pid, "spokeo", db.enc("https://x.invalid/a"), shot, db.now(), "approved")).lastrowid
    oid = c.execute("INSERT INTO outbox(ts,person_id,to_addr,subject,body,state,listing_id) VALUES(?,?,?,?,?,?,?)", (db.now(), pid, "p@broker.invalid", db.enc("s"), db.enc("b"), "queued", lid)).lastrowid
    c.execute("INSERT INTO inbox(ts,person_id,listing_id,outbox_id,from_addr,subject,kind) VALUES(?,?,?,?,?,?,?)", (db.now(), pid, lid, oid, db.enc("a@b.invalid"), db.enc("re"), "ack"))
    c.execute("INSERT INTO snooze(k,until) VALUES(?,?)", (f"l:{lid}", "2099")); c.execute("INSERT INTO retry(listing_id) VALUES(?)", (lid,)); c.commit()
    t = csrf(admin, f"/person/{pid}")
    page = admin.get(f"/person/{pid}").text; assert "Delete person" in page and 'data-match="Drew Example"' in page
    r = admin.post(f"/person/{pid}/delete", data={"csrf": t, "confirm_name": "Drew"}); assert "delerr=name" in r.headers["location"]
    assert c.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone()
    r = admin.post(f"/person/{pid}/delete", data={"csrf": t, "confirm_name": "  drew   example "}); assert r.headers["location"] == "/family?deleted=1"
    for tbl, col in (("people", "id"), ("listings", "person_id"), ("outbox", "person_id"), ("inbox", "person_id"), ("users", "person_id"), ("reg_requests", "person_id")):
        assert not c.execute(f"SELECT 1 FROM {tbl} WHERE {col}=?", (pid,)).fetchone(), tbl
    assert not c.execute("SELECT 1 FROM snooze WHERE k=?", (f"l:{lid}",)).fetchone() and not c.execute("SELECT 1 FROM retry WHERE listing_id=?", (lid,)).fetchone()
    assert not os.path.exists(shot) and not os.path.exists(pdf) and os.path.exists(other)
    ev = c.execute("SELECT * FROM events WHERE kind='person_deleted' ORDER BY id DESC").fetchone()
    assert ev["person_id"] is None and "Drew" not in ev["detail"] and pid not in ev["detail"] and json.loads(ev["detail"])["queued_cancelled"] == 1
    assert admin.get(f"/person/{pid}").status_code == 404

def test_delete_guards(admin):
    t = csrf(admin, "/person/alex")
    assert admin.post("/person/alex/delete", data={"csrf": t, "confirm_name": "x"}).headers["location"].endswith("delerr=self")
    m = client(); assert m.post("/person/sam/delete", data={"csrf": "x", "confirm_name": "x"}).status_code in (303, 401, 403)
    assert db.conn().execute("SELECT 1 FROM people WHERE id='sam'").fetchone()

def test_setup_email_steps_render(admin, monkeypatch):
    import golive
    r = admin.get("/setup/4"); assert r.status_code == 200 and "Variable name (Key)" in r.text and "Send to a Worker" in r.text and "+ Add" in r.text and "Test inbound" in r.text
    monkeypatch.setattr(golive, "doh_records", lambda n, t: [("CNAME", "send.forge.rmta.net."), ("MX", "10 feedback.forge.rmta.net.")] if n.startswith("send.") and t == "MX" else [])
    r = admin.get("/setup/3?dns=1"); assert r.status_code == 200 and "DNS only" in r.text and "OK" in r.text and "Missing" in r.text
    w = open(ROOT / "deploy/cloudflare-email-worker.js").read(); assert "Portainer" not in w and "app.example.com" not in w
