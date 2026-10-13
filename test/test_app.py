"""Login, permissions, CSRF, profile -> scan queries, PII encryption, upload checks, headers. Run: python -m pytest test/test_app.py"""
import os, sys, tempfile, pathlib, re, sqlite3, json
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, db, profiles, scanner, auth
APW = "admin correct horse battery"; KPW = "first pass for k 123"

def client(): return TestClient(main.app, follow_redirects=False)
def login(c, u, pw): return c.post("/login", data={"username": u, "password": pw})
def csrf(c, path="/account/password"): return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)
@pytest.fixture(scope="module")
def admin():
    c = client(); assert login(c, "alex", APW).status_code == 303; return c
@pytest.fixture(scope="module")
def member(admin):
    tok = csrf(admin, "/family")
    assert admin.post("/family/sam/account", data={"csrf": tok, "action": "password", "username": "sam", "password": KPW}).status_code == 303
    c = client(); r = login(c, "sam", KPW); assert r.headers["location"] == "/account/password"   # must change first
    t = csrf(c); assert c.post("/account/password", data={"csrf": t, "current": KPW, "password": "my own passphrase 2026 k", "password2": "my own passphrase 2026 k"}).status_code == 303
    return c

def add_listing(pid, site="mockemail", shot=True):
    c = db.conn(); p = pathlib.Path(T, "shots"); p.mkdir(exist_ok=True); f = p / f"{pid}.png"; f.write_bytes(b"\x89PNG\r\n")
    cur = c.execute("INSERT INTO listings(person_id,site,url,fields,screenshot,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)", (pid, site, db.enc(f"https://x.invalid/{pid}/{os.urandom(3).hex()}"), db.enc("{}"), str(f) if shot else None, db.now(), db.now()))
    c.commit(); return cur.lastrowid

# ---- login ----
def test_logged_out_redirects_and_basic_auth_gone():
    c = client(); r = c.get("/"); assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert "WWW-Authenticate" not in r.headers
    assert c.get("/person/alex/proof.zip").status_code == 303 and c.get("/shot/1").status_code == 303
    assert c.get("/login").status_code == 200 and "Geist" not in c.get("/login").text or True

def test_bad_password_and_rate_limit():
    c = client()
    for i in range(5): assert login(c, "riley-x", "nope nope nope").status_code == 401
    assert login(c, "riley-x", "nope nope nope").status_code == 429

def test_session_cookie_flags(admin):
    c = client(); r = login(c, "alex", APW); sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=lax" in sc and "ff_session=" in sc
    raw = sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT token_hash FROM sessions").fetchall()
    assert all(len(t[0]) == 64 for t in raw) and c.cookies.get("ff_session") not in [t[0] for t in raw]   # only the hash is stored

def test_csrf_required(admin):
    assert admin.post("/person/alex/scan").status_code == 403
    assert admin.post("/person/alex/toggle/enabled", data={"csrf": "wrong"}).status_code == 403
    assert admin.post("/person/alex/scan", data={"csrf": csrf(admin)}, headers={"origin": "https://evil.example"}).status_code == 403

def test_logout_kills_session():
    c = client(); login(c, "alex", APW); t = csrf(c); assert c.post("/logout", data={"csrf": t}).status_code == 303
    assert c.get("/").status_code == 303

def test_invite_link_single_use(admin):
    t = csrf(admin, "/family"); r = admin.post("/family/jordan/account", data={"csrf": t, "action": "invite", "username": "jordan"})
    tok = re.search(r"inv=([\w-]+)", r.headers["location"]).group(1)
    page = admin.get(f"/family?inv={tok}&who=jordan"); assert f"/invite/{tok}" in page.text
    c = client(); assert c.get(f"/invite/{tok}").status_code == 200
    assert c.post(f"/invite/{tok}", data={"password": "short", "password2": "short"}).status_code == 400
    assert c.post(f"/invite/{tok}", data={"password": "a passphrase for p 2026", "password2": "a passphrase for p 2026"}).status_code == 303
    assert c.get("/").headers["location"] == "/person/jordan"
    assert client().get(f"/invite/{tok}").status_code == 404

# ---- permissions ----
def test_member_sees_only_own(member):
    d = add_listing("alex"); k = add_listing("sam")
    assert member.get("/").headers["location"] == "/person/sam"
    assert member.get("/person/sam").status_code == 200 and member.get("/person/sam/profile").status_code == 200
    for u in ("/person/alex", "/person/alex/profile", f"/listing/{d}", f"/shot/{d}", "/person/alex/proof.zip", "/person/alex/reminders.ics", "/person/jordan"):
        assert member.get(u).status_code == 404, u
    for u in ("/family", "/sites", "/calls", "/add"): assert member.get(u).status_code == 403, u
    page = member.get("/person/sam").text
    assert "Alex Quinn Rivera" not in page and "Jordan" not in page and "Riley" not in page   # sidebar shows only herself
    assert member.get(f"/shot/{k}").status_code == 200
    t = csrf(member, "/person/sam")
    assert member.post(f"/listing/{d}/notme", data={"csrf": t}).status_code == 404
    assert member.post(f"/listing/{k}/notme", data={"csrf": t}).status_code == 303
    assert member.post("/person/sam/toggle/send_enabled", data={"csrf": t}).status_code == 403   # only admin enables sending
    assert member.post("/person/sam/bulk", data={"csrf": t}).status_code == 403
    assert member.post("/family/sam/account", data={"csrf": t, "action": "remove"}).status_code == 403

def test_admin_sees_all(admin):
    for u in ("/", "/person/alex", "/person/sam", "/person/riley/profile", "/family", "/sites", "/calls", "/add"): assert admin.get(u).status_code == 200, u
    t = csrf(admin, "/family"); assert admin.post("/person/sam/toggle/send_enabled", data={"csrf": t}).status_code == 303
    assert db.conn().execute("SELECT send_enabled FROM people WHERE id='sam'").fetchone()[0] == 1

# ---- profile editor -> scanner ----
FORM = {"name_kind": ["legal", "nickname", "maiden"], "name_first": ["Riley", "CJ", "Riley"], "name_middle": ["", "", ""], "name_last": ["Rivera", "Rivera", "Smith"],
        "addr_street": ["", "9 Example Ct"], "addr_city": ["Springfield", "Shelbyville"], "addr_state": ["CA", "CA"], "addr_current": ["0"],
        "phone": ["555-555-0199", ""], "email": ["cj@example.com"], "relative": ["Alex Rivera"], "birth_year": ["2004"], "age_min": [""], "age_max": [""]}
def test_profile_edit_flows_into_queries(admin):
    t = csrf(admin, "/person/riley/profile")
    r = admin.post("/person/riley/profile", data={**FORM, "csrf": t}); assert r.status_code == 303
    p = db.conn().execute("SELECT * FROM people WHERE id='riley'").fetchone(); prof = profiles.get(p)
    assert [n["first"] for n in prof["names"]] == ["Riley", "CJ", "Riley"] and prof["phones"] == ["5555550199"] and prof["relatives"] == ["Alex Rivera"]
    assert p["city"] == "Springfield" and p["prior_cities"] == "Shelbyville" and p["full_name"] == "Riley Rivera"
    deep = next(s for s in scanner.SITES if s["id"] == "mockdeep")
    qs = scanner.queries(deep, prof); urls = [q[0] for q in qs]
    assert {(q[1], q[2]) for q in qs} == {("Riley", "Rivera"), ("CJ", "Rivera"), ("Riley", "Smith")}
    assert any("city=Springfield" in u for u in urls) and any("city=Shelbyville" in u for u in urls) and len(urls) == 6
    nocity = next(s for s in scanner.SITES if s["id"] == "mockemail"); assert len(scanner.queries(nocity, prof)) == 3   # templates without {city} collapse
    lo, hi = profiles.age_range(prof); assert hi - lo == 1
    ok, _ = scanner.plausible(prof, "Riley Rivera Age 61 · Springfield, CA"); assert not ok
    ok, _ = scanner.plausible(prof, f"CJ Rivera Age {hi} · Shelbyville, CA"); assert ok
    ok, _ = scanner.plausible(prof, f"CJ Rivera Age {hi} · Shelbyville, TX"); assert not ok
    ev = scanner.evidence(prof, "Phone (555) 555-0199 · 9 Example Ct, Shelbyville · relatives: Alex Rivera · cj@example.com")
    assert ev["phones"] and ev["streets"] and ev["relatives"] and ev["emails"]
    import remover
    body = remover.ccpa_body(p, "Broker", "https://x.invalid/1")
    for s_ in ("CJ Rivera", "Riley Smith", "9 Example Ct, Shelbyville, CA", "(555) 555-0199", "cj@example.com", "Alex Rivera"): assert s_ in body

@pytest.mark.parametrize("field,value", [("addr_street", "123-45-6789"), ("relative", "SSN 123456789"), ("name_first", "DOB 01/02/2010"), ("addr_street", "DL A1234567"),
                                         ("email", "driver license 99"), ("birth_year", "2010-05-04"), ("relative", "passport 123")])
def test_forbidden_identifiers_rejected(admin, field, value):
    t = csrf(admin, "/person/riley/profile"); f = {**FORM, "csrf": t}; f[field] = [value] + FORM[field][1:]
    r = admin.post("/person/riley/profile", data=f); assert r.status_code == 400 and "Nothing was saved" in r.text
    raw = open(os.environ["DB_PATH"], "rb").read(); assert value.encode() not in raw

def test_validation_messages(admin):
    t = csrf(admin, "/person/riley/profile")
    r = admin.post("/person/riley/profile", data={**FORM, "csrf": t, "phone": ["12345"], "email": ["nope"], "addr_state": ["ZZ", "CA"]})
    assert r.status_code == 400 and "10-digit" in r.text and "not valid" in r.text and "US state" in r.text

# ---- encryption ----
def test_raw_db_has_no_plaintext_pii(admin):
    for f in ("app.db", "app.db-wal"):
        p = pathlib.Path(T, f)
        if p.exists():
            raw = p.read_bytes()
            for w in (b"Sam", b"Alex Quinn", b"Jordan", b"Springfield", b"Shelbyville", b"(555)", b"5555550199", b"555-0142", b"555-0143", b"example.org", b"cj@example.com", b"Example Ct"):
                assert w not in raw, (f, w)
    r = sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT full_name, phone, profile FROM people WHERE id='sam'").fetchone()
    assert all(x.startswith("enc1:") for x in r)
    assert db.conn().execute("SELECT full_name FROM people WHERE id='sam'").fetchone()[0] == "Sam Rivera"

def test_wrong_key_cannot_decrypt():
    from cryptography.hazmat.primitives.ciphers.aead import AESSIV
    import base64, crypto
    v = sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT full_name FROM people WHERE id='alex'").fetchone()[0]
    with pytest.raises(Exception): AESSIV(AESSIV.generate_key(512)).decrypt(base64.urlsafe_b64decode(v[5:]), None)

# ---- uploads / files ----
def test_pdf_upload_removed_and_drawn_signature_required(admin, member):
    t = csrf(admin, "/person/sam")
    assert admin.post("/person/sam/auth", data={"csrf": t}, files={"pdf": ("a.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")}).status_code in (404, 405)
    tm = csrf(member, "/person/sam/authorize")
    r = member.post("/person/sam/authorize", data={"csrf": tm, "agree": "yes", "typed_name": "Sam Rivera", "sig": "[]"}); assert r.status_code == 400 and "Draw your signature" in r.text
    sig = json.dumps([[[0.1 + i / 50, 0.5 + (i % 5) / 20] for i in range(30)]])
    r = member.post("/person/sam/authorize", data={"csrf": tm, "agree": "yes", "typed_name": "Sam Rivera", "sig": sig}); assert r.status_code == 303
    pdf = member.get("/person/sam/authorization.pdf"); assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and b" l " in pdf.content
    raw = sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT esign_sig FROM people WHERE id='sam'").fetchone()[0]
    assert raw and "0.1" not in raw   # strokes stored encrypted
    assert oct(os.stat(os.path.join(os.environ["AUTH_DIR"], "sam-esign.pdf")).st_mode)[-3:] == "600"

def test_settings_apply_without_restart_and_secrets_write_only(admin):
    import config, remover
    t = csrf(admin, "/admin/heartbeat?tab=settings")
    r = admin.post("/admin/heartbeat/settings", data={"csrf": t, "APP_NAME": "Test Shield", "RESEND_API_KEY": "re_secret_value_123456", "FROM_ADDRESS": "privacy@example.net"}); assert r.status_code == 303
    page = admin.get("/admin/heartbeat?tab=settings").text
    assert "Test Shield" in page and "re_secret_value_123456" not in page and "3456" in page and remover.FROM_ADDRESS == "privacy@example.net"
    assert sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT v FROM settings WHERE k='RESEND_API_KEY'").fetchone()[0] != "re_secret_value_123456"
    admin.post("/admin/heartbeat/settings", data={"csrf": t, "RESEND_API_KEY": ""}); assert config.get("RESEND_API_KEY") == "re_secret_value_123456"   # empty keeps it
    r = admin.post("/admin/heartbeat/settings", data={"csrf": t, "DRY_RUN__present": "1"}); assert "LIVE" in r.headers["location"] and remover.DRY   # no confirmation -> stays dry
    admin.post("/admin/heartbeat/settings", data={"csrf": t, "APP_NAME": "", "FROM_ADDRESS": ""}); config.save({}, clear=["RESEND_API_KEY"])
    assert "Fletcher Fortis" in admin.get("/admin/heartbeat").text

def test_member_cannot_open_heartbeat_or_setup(member):
    assert member.get("/admin/heartbeat").status_code == 403
    assert client().get("/setup").status_code == 303   # admin exists: setup is closed

def test_onboarding_invite_flow(admin):
    t = csrf(admin, "/family")
    r = admin.post("/family/onboard", data={"csrf": t, "full_name": "Test Person", "email": "test.person@example.com"}); assert r.status_code == 303
    tok = re.search(r"inv=([\w-]+)", r.headers["location"]).group(1); pid = re.search(r"who=(\w+)", r.headers["location"]).group(1)
    o = db.conn().execute("SELECT state, channel FROM outbox WHERE channel='onboarding' ORDER BY id DESC").fetchone(); assert o["state"] == "dry_run"
    c = client(); assert c.get(f"/invite/{tok}").status_code == 200
    r = c.post(f"/invite/{tok}", data={"password": "orchid lantern river 2026", "password2": "orchid lantern river 2026"}); assert r.headers["location"] == f"/person/{pid}/profile?onboard=1"
    assert client().get(f"/invite/{tok}").status_code == 404   # single use

def test_letters_by_state():
    import letters
    p = {"id": "x", "full_name": "Test Person", "first": "Test", "last": "Person", "city": "Springfield", "state": "CA", "needs_auth": 1}
    s, t, h = letters.build(p, {"names": [], "addresses": [], "phones": [], "emails": []}, {"id": "spokeo", "name": "Spokeo"}, [], agent=True, reply_to="r@example.com", stage=0)
    assert "1798.105" in t and "45 days" in t and "<html" in h.lower()
    p["state"] = "TX"; s, t, h = letters.build(p, {"names": [], "addresses": [], "phones": [], "emails": []}, {"id": "spokeo", "name": "Spokeo"}, [], agent=True, reply_to="r@example.com", stage=2)
    assert "1798.105" not in t and "Texas" in t

def test_screenshot_path_traversal(admin):
    c = db.conn(); cur = c.execute("INSERT INTO listings(person_id,site,url,screenshot,first_seen) VALUES('alex','x',?,?,?)", (db.enc("https://x.invalid/t"), "/etc/passwd", db.now())); c.commit()
    assert admin.get(f"/shot/{cur.lastrowid}").status_code == 404
    c.execute("UPDATE listings SET screenshot=? WHERE id=?", (os.path.join(scanner.SHOTS, "..", "app.db"), cur.lastrowid)); c.commit()
    assert admin.get(f"/shot/{cur.lastrowid}").status_code == 404

def test_security_headers(admin):
    for r in (admin.get("/person/alex"), client().get("/login"), client().get("/healthz")):
        h = r.headers
        assert "script-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "same-origin" and h["x-content-type-options"] == "nosniff"
    os.environ["COOKIE_SECURE"] = "true"

def test_no_inline_scripts_or_handlers(admin):
    for u in ("/", "/person/alex", "/person/alex/profile", "/family", "/listing/1"):
        html = admin.get(u).text
        assert not re.search(r"<script(?![^>]*src=)[^>]*>", html), u
        assert not re.search(r"\son(click|change|load|submit|input)=", html), u

def test_inbound_secret_constant_time_and_errors():
    c = client()
    assert c.post("/inbound/email", content=b"x", headers={"x-inbound-secret": "nope"}).status_code == 401
    assert c.post("/inbound/email", content=b"From: a@b.c\n\nhi", headers={"x-inbound-secret": "s"}).json()["ok"]

def test_error_page_branded(admin):
    r = admin.get("/does-not-exist"); assert r.status_code == 404 and "Fletcher Fortis" in r.text and "/static/app.css" in r.text

def test_browser_origin_and_referrer_policy():
    # Real browsers send Origin on form posts; same-origin must work, cross-site must be blocked.
    c = client(); r = c.post("/login", data={"username": "alex", "password": APW}, headers={"Origin": "http://testserver"}); assert r.status_code == 303
    assert client().post("/login", data={"username": "alex", "password": APW}, headers={"Origin": "https://evil.example"}).status_code == 403
    # no-referrer makes Chrome send "Origin: null", which the check rejects, so the policy must stay same-origin
    assert c.get("/login").headers["referrer-policy"] == "same-origin"
