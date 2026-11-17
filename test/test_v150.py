"""v1.5.0: family vs outside/managed, migration keeps existing people and consents, single-use intake links, consent lock, no SSN/DOB/ID on the intake form."""
import os, sys, tempfile, pathlib, sqlite3, json, re
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = f"{T}/app.db"
os.environ.update(DB_PATH=DB, SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
os.environ.pop("RESEND_API_KEY", None)
# An already-populated 1.4.0 database: no circle / consent_verified_at / intake_links columns. Plaintext PII, one signed person, one unsigned, one listing.
raw = sqlite3.connect(DB)
raw.executescript("""
CREATE TABLE people(id TEXT PRIMARY KEY, full_name TEXT, first TEXT, last TEXT, city TEXT, state TEXT,
  prior_cities TEXT, email TEXT, phone TEXT, age_min INT, age_max INT, enabled INT DEFAULT 1,
  needs_auth INT DEFAULT 0, auth_pdf TEXT, bulk_ccpa INT DEFAULT 0, notes TEXT, profile TEXT,
  esign_name TEXT, esign_at TEXT, esign_ip TEXT, esign_user TEXT, esign_sig TEXT);
CREATE TABLE listings(id INTEGER PRIMARY KEY, person_id TEXT, site TEXT, url TEXT, fields TEXT, screenshot TEXT,
  status TEXT DEFAULT 'candidate', first_seen TEXT, last_seen TEXT, last_checked TEXT, submitted_at TEXT, confirmed_at TEXT);
CREATE TABLE app_meta(k TEXT PRIMARY KEY, v TEXT);
INSERT INTO app_meta(k,v) VALUES('version','1.4.0');
""")
CASEY = json.dumps({"names": [{"kind": "legal", "first": "Casey", "middle": "", "last": "Placeholder"}],
                    "addresses": [{"street": "1 Example Way", "city": "Springfield", "state": "CA", "current": True}],
                    "phones": ["5555550199"], "emails": ["casey@example.com"], "relatives": [], "birth_year": None, "age_min": 30, "age_max": 40})
raw.execute("""INSERT INTO people(id,full_name,first,last,city,state,email,phone,age_min,age_max,enabled,needs_auth,profile,esign_name,esign_at,esign_ip)
               VALUES (?,?,?,?,?,?,?,?,?,?,1,1,?,?,?,?)""",
            ("casey", "Casey Placeholder", "Casey", "Placeholder", "Springfield", "CA", "casey@example.com", "(555) 555-0199", 30, 40, CASEY, "Casey Placeholder", "2024-06-01T12:00:00", "203.0.113.8"))
raw.execute("""INSERT INTO people(id,full_name,first,last,city,state,enabled,needs_auth) VALUES (?,?,?,?,?,?,1,1)""",
            ("unsigned", "Unsigned Example", "Unsigned", "Example", "Shelbyville", "CA"))
raw.execute("INSERT INTO listings(person_id,site,url,status,first_seen) VALUES (?,?,?,?,?)",
            ("casey", "example", "https://example.invalid/casey", "candidate", "2024-06-01T12:00:00"))
raw.commit(); raw.close()
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, db, intake, remover, profiles, circle, summary, mailer
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

def test_migration_keeps_family_consent_and_encrypted_rows():
    casey = db.conn().execute("SELECT * FROM people WHERE id='casey'").fetchone()
    unsigned = db.conn().execute("SELECT * FROM people WHERE id='unsigned'").fetchone()
    assert casey["circle"] == "family" and unsigned["circle"] == "family"
    assert casey["full_name"] == "Casey Placeholder" and casey["esign_at"] == "2024-06-01T12:00:00"
    assert casey["esign_name"] == "Casey Placeholder" and casey["city"] == "Springfield"
    assert json.loads(casey["profile"])["addresses"][0]["street"] == "1 Example Way"
    assert casey["age_min"] == 30 and casey["consent_verified_at"] and remover.has_auth(casey)
    assert not unsigned["esign_at"] and not unsigned["consent_verified_at"] and not remover.has_auth(unsigned)
    assert db.conn().execute("SELECT url, status FROM listings WHERE person_id='casey'").fetchone()["url"] == "https://example.invalid/casey"
    stored = sqlite3.connect(DB).execute("SELECT full_name, profile, esign_name, esign_ip, email FROM people WHERE id='casey'").fetchone()
    assert all(str(v).startswith("enc1:") for v in stored)   # esign_at stays a timestamp; the name, profile, signature name, IP and email are encrypted
    # seeded household from the test profiles is family too, and nothing was dropped
    ids = {r["id"] for r in db.conn().execute("SELECT id, circle FROM people")}
    assert {"casey", "unsigned", "alex", "sam"} <= ids
    assert all(r["circle"] == "family" for r in db.conn().execute("SELECT circle FROM people"))

def test_outside_people_stay_out_of_family_totals(admin):
    r = admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": "Quinn Outsider", "city": "Springfield", "state": "CA", "needs_auth": "1", "relationship": "outside", "email": ""})
    assert r.status_code == 303 and "/person/" in r.headers["location"]
    pid = re.search(r"/person/(\w+)", r.headers["location"]).group(1)
    assert db.conn().execute("SELECT circle FROM people WHERE id=?", (pid,)).fetchone()["circle"] == "outside"
    c = db.conn()
    c.execute("INSERT INTO listings(person_id,site,url,status,first_seen,confirmed_at) VALUES (?,?,?,?,?,?)",
              (pid, "example", "https://example.invalid/quinn", "confirmed_removed", "2026-11-01T00:00:00", "2026-11-02T00:00:00"))
    c.execute("UPDATE people SET esign_at=?, consent_verified_at=? WHERE id=?", ("2026-11-01T00:00:00", "2026-11-01T00:00:00", pid)); c.commit()
    home = admin.get("/").text
    fam_table = home.split(">Family ·", 1)[1].split('id="outside"', 1)[0]
    outside = home.split('id="outside"', 1)[1]
    assert "Quinn Outsider" not in fam_table and "Quinn Outsider" in outside and "Outside / managed" in outside
    assert "Outside people are not included" in home
    fam_page = admin.get("/family").text
    assert fam_page.count(f"/person/{pid}") == 1   # sidebar only; not a household card
    # the confirmed removal belongs to Quinn, not the household total
    assert main.stats().get("confirmed_removed", 0) == 0
    assert main.stats(pid).get("confirmed_removed", 0) == 1
    fam_people = [p for p in c.execute("SELECT * FROM people").fetchall() if circle.family(p)]
    _subj, html, text = summary.build(c, fam_people, family=True)
    assert "Outsider" not in text and "Outsider" not in html and "family" in _subj
    recips = summary.recipients(c)
    assert recips and all(circle.family(p) for group, _is_fam, _to, _sender in recips for p in group)
    prof_page = admin.get("/person/alex/profile").text
    relatives = re.findall(r'name="relative"[^>]*value="([^"]*)"', prof_page)
    assert "Outsider" not in " ".join(relatives)   # an outside person's name is not suggested as a relative

def test_switch_circle(admin):
    r = admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": "Riley Household", "city": "Springfield", "state": "CA", "needs_auth": "1", "email": "riley.household@example.com"})
    pid = re.search(r"/person/(\w+)", r.headers["location"]).group(1)
    assert "Family" in admin.get(f"/person/{pid}").text
    assert admin.post(f"/person/{pid}/circle", data={"csrf": csrf(admin, f"/person/{pid}"), "relationship": "outside"}).status_code == 303
    page = admin.get(f"/person/{pid}").text
    assert "Outside / managed" in page and db.conn().execute("SELECT circle FROM people WHERE id=?", (pid,)).fetchone()["circle"] == "outside"
    assert admin.get("/family").text.count(f"/person/{pid}") == 1

def test_intake_link_is_single_use_and_expires(admin, monkeypatch):
    sent = []
    monkeypatch.setattr(remover, "provider_ready", lambda: True)
    monkeypatch.setattr(remover, "_resend", lambda *a, **k: sent.append(a[1]) or "sent:id")
    assert remover.DRY
    r = admin.post("/intake/new", data={"csrf": csrf(admin, "/"), "relationship": "outside", "email": "pat.outsider@example.com", "send": ""})
    assert r.status_code == 303 and "ink=" in r.headers["location"] and sent == []
    assert db.conn().execute("SELECT COUNT(*) n FROM outbox WHERE channel='intake'").fetchone()["n"] == 0
    tok = re.search(r"ink=([\w-]+)", r.headers["location"]).group(1)
    page = client().get(f"/invite/intake/{tok}")
    assert page.status_code == 200 and 'name="ssn"' not in page.text and 'name="dob"' not in page.text and 'name="license"' not in page.text and "birth_year" not in page.text and "Social Security" in page.text
    link_csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    body = {"csrf": link_csrf, "full_name": "Pat Outsider", "prev_name": "Pat Example", "addr_city": "Springfield", "addr_state": "CA", "addr_street": "9 Fake St", "addr_current": "0",
            "phone": "5555550100", "email": "pat.outsider@example.com", "age_min": "31", "age_max": "40", "typed_name": "Pat Outsider", "agree": "yes", "sig": SIG}
    pub = client()
    ok = pub.post(f"/invite/intake/{tok}", data=body, headers={"User-Agent": "TestBrowser/1.5"})
    assert ok.status_code == 200 and "Signed" in ok.text and "Pat" in ok.text
    p = db.conn().execute("SELECT * FROM people WHERE full_name LIKE ?", ()).fetchone() if False else next(x for x in db.conn().execute("SELECT * FROM people") if x["full_name"] == "Pat Outsider")
    prof = json.loads(p["profile"])
    assert p["circle"] == "outside" and p["esign_at"] and p["esign_ua"] == "TestBrowser/1.5" and p["esign_ip"] and remover.has_auth(p)
    assert p["city"] == "Springfield" and prof["names"][1]["last"] == "Example" and prof["addresses"][0]["street"] == "9 Fake St" and prof["age_min"] == 31
    assert os.path.exists(os.path.join(os.environ["AUTH_DIR"], f"{p['id']}-esign.pdf"))
    stored = sqlite3.connect(DB).execute("SELECT full_name, esign_ua, profile FROM people WHERE id=?", (p["id"],)).fetchone()
    assert all(str(v).startswith("enc1:") for v in stored)
    assert pub.get(f"/invite/intake/{tok}").status_code == 410
    assert pub.post(f"/invite/intake/{tok}", data=body).status_code == 410
    # email only when the admin presses send, and dry run does not hold it (it is not broker mail)
    r = admin.post(f"/person/{p['id']}/intake-link", data={"csrf": csrf(admin, f"/person/{p['id']}"), "send": "1", "email": "pat.outsider@example.com"})
    assert "istate=sent" in r.headers["location"] and sent == ["pat.outsider@example.com"]
    assert db.conn().execute("SELECT state FROM outbox WHERE channel='intake' ORDER BY id DESC").fetchone()["state"] == "sent"
    # expired link
    tok2, _csrf, _exp = intake.create(None, "outside")
    c = db.conn(); c.execute("UPDATE intake_links SET expires=? WHERE token_hash=?", ("2000-01-01T00:00:00", __import__("crypto").token_hash(tok2))); c.commit()
    assert client().get(f"/invite/intake/{tok2}").status_code == 410
    assert "expired" in client().get(f"/invite/intake/{tok2}").text
    # bad csrf does not consume a fresh link
    tok3, good, _e = intake.create(None, "family")
    bad = client().post(f"/invite/intake/{tok3}", data={**body, "csrf": "nope", "full_name": "Other Person", "typed_name": "Other Person"})
    assert bad.status_code == 400 and intake.state(intake.fetch(tok3)) == "ok"
    assert client().post(f"/invite/intake/{tok3}", data={**body, "csrf": good, "full_name": "Other Person", "typed_name": "Other Person"}).status_code == 200

def test_intake_refuses_ssn_dob_and_id(admin):
    tok, link_csrf, _e = intake.create(None, "outside")
    pub = client()
    base = {"csrf": link_csrf, "full_name": "Sam Sample", "typed_name": "Sam Sample", "agree": "yes", "sig": SIG}
    for extra in ({"ssn": "123-45-6789"}, {"dob": "01/02/1990"}, {"date_of_birth": "1990-01-02"}, {"driver_license": "A1234567"}, {"license_number": "D1234567"}):
        r = pub.post(f"/invite/intake/{tok}", data={**base, **extra})
        assert r.status_code == 400, extra
        assert "123-45-6789" not in r.text and "01/02/1990" not in r.text and "A1234567" not in r.text
    assert intake.state(intake.fetch(tok)) == "ok"
    # a forbidden value in an ordinary field is rejected and not stored
    r = pub.post(f"/invite/intake/{tok}", data={**base, "addr_city": "123-45-6789", "addr_state": "CA"})
    assert r.status_code == 400 and "123-45-6789" not in r.text
    blob = open(DB, "rb").read()
    assert b"123-45-6789" not in blob and b"A1234567" not in blob and b"01/02/1990" not in blob
    assert not any(p["full_name"] == "Sam Sample" for p in db.conn().execute("SELECT * FROM people"))

def test_consent_lock_blocks_removals_until_a_real_authorization(admin, monkeypatch):
    r = admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": "Drew Managed", "city": "Springfield", "state": "CA", "needs_auth": "1", "relationship": "outside"})
    pid = re.search(r"/person/(\w+)", r.headers["location"]).group(1)
    p = db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    assert remover.can_send(p)[0] is False
    assert admin.post(f"/person/{pid}/scan", data={"csrf": csrf(admin, f"/person/{pid}")}).status_code == 403
    # marking consent with nothing attached does nothing
    v = admin.post(f"/person/{pid}/authorization/verify", data={"csrf": csrf(admin, f"/person/{pid}/authorize")})
    assert "cerr=" in v.headers["location"]
    assert not remover.has_auth(db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone())
    assert "Nothing is attached" in admin.get(f"/person/{pid}/authorize").text or "Attach a signed" in admin.get(v.headers["location"]).text
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    up = admin.post(f"/person/{pid}/authorization/upload", data={"csrf": csrf(admin, f"/person/{pid}/authorize")}, files={"file": ("signed.pdf", pdf, "application/pdf")})
    assert up.status_code == 303 and "uploaded=1" in up.headers["location"]
    p = db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    assert p["auth_pdf"] and not p["consent_verified_at"] and not remover.has_auth(p)   # the file alone is not consent
    db.init()   # a restart must not turn that upload into consent
    p = db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    assert not p["consent_verified_at"] and not remover.has_auth(p)
    assert admin.post(f"/person/{pid}/authorization/verify", data={"csrf": csrf(admin, f"/person/{pid}/authorize")}).status_code == 303
    p = db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    assert remover.has_auth(p) and p["consent_verified_at"]
    monkeypatch.setattr(remover, "DRY", False)
    monkeypatch.setattr(remover, "_resend", lambda *a, **k: "sent:id")
    c = db.conn(); c.execute("UPDATE people SET send_enabled=1 WHERE id=?", (pid,)); c.commit()
    assert remover.send_email(db.conn(), db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone(), "privacy@example.invalid", "Request", "body").startswith("sent")
    # clearing both the signature and the verified file blocks the next send, family or outside
    c = db.conn(); c.execute("UPDATE people SET esign_at=NULL, auth_pdf=NULL, consent_verified_at=NULL WHERE id=?", (pid,)); c.commit()
    st = remover.send_email(db.conn(), db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone(), "privacy@example.invalid", "Request", "body")
    assert st.startswith("blocked")
    fam = db.conn().execute("SELECT * FROM people WHERE id='unsigned'").fetchone()
    assert not remover.has_auth(fam) and remover.can_send(fam)[0] is False

def test_intake_rate_limit(monkeypatch):
    intake._hits.clear(); intake.POST_LIMIT = 1
    tok, csrf_tok, _e = intake.create(None, "outside")
    body = {"csrf": csrf_tok, "full_name": "Rate Limit", "addr_city": "Springfield", "addr_state": "CA", "email": "rate.limit@example.com", "typed_name": "Rate Limit", "agree": "yes", "sig": SIG}
    assert client().post(f"/invite/intake/{tok}", data=body).status_code == 200
    assert client().post(f"/invite/intake/{tok}", data=body).status_code == 429
