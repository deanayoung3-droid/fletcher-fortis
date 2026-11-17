"""v1.5.4: adding an outside person needs only a name or nickname; intake collects the rest; scans wait until the form is signed."""
import os, sys, tempfile, pathlib, json, re
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
os.environ.pop("RESEND_API_KEY", None)
os.environ.pop("SCAN_ON_CREATE", None)
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, db, intake, profiles, scanner, scanjobs, remover
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
@pytest.fixture
def q(monkeypatch):
    calls = []; monkeypatch.setattr(scanjobs, "enqueue", lambda pid, why="manual": calls.append((pid, why)) or True); return calls

def test_version():
    assert open(ROOT / "VERSION").read().strip() == "1.5.4"

def test_family_add_still_needs_name_city_email(admin):
    t = csrf(admin, "/add")
    r = admin.post("/add", data={"csrf": t, "full_name": "Pat Example", "city": "Springfield", "state": "CA", "needs_auth": "1", "relationship": "family", "email": ""})
    assert r.status_code == 400 and "email" in r.text.lower()
    r = admin.post("/add", data={"csrf": t, "full_name": "Pat Example", "city": "", "state": "CA", "needs_auth": "1", "relationship": "family", "email": "pat.example@example.com"})
    assert r.status_code == 400 and "city" in r.text.lower()
    r = admin.post("/add", data={"csrf": t, "full_name": "Pat", "city": "Springfield", "state": "CA", "needs_auth": "1", "relationship": "family", "email": "pat.example@example.com"})
    assert r.status_code == 400 and "first and last" in r.text.lower()

def test_add_outside_name_only_waits_and_does_not_scan(admin, q):
    page = admin.get("/add?relationship=outside")
    assert page.status_code == 200 and "a nickname is enough" in page.text and "name=\"city\" required" not in page.text
    r = admin.post("/add", data={"csrf": csrf(admin, "/add?relationship=outside"), "full_name": "Jordan", "needs_auth": "1", "relationship": "outside"})
    assert r.status_code == 303 and "/person/" in r.headers["location"] and "ink=" in r.headers["location"]
    pid = re.search(r"/person/(\w+)", r.headers["location"]).group(1)
    p = db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    assert p["full_name"] == "Jordan" and p["circle"] == "outside" and not p["city"] and not p["email"]
    assert not p["esign_at"] and not profiles.complete(p) and not profiles.scan_ready(p) and profiles.waiting_intake(p)
    assert q == []
    person = admin.get(r.headers["location"])
    assert person.status_code == 200 and "Waiting on their intake form" in person.text
    assert "/invite/intake/" in person.text and "Scan now" not in person.text
    home = admin.get("/").text
    assert "Jordan" in home.split('id="outside"', 1)[1] and "Waiting on their intake form" in home
    assert admin.post(f"/person/{pid}/scan", data={"csrf": csrf(admin, f"/person/{pid}")}).status_code == 403
    assert q == []
    # weekly / everyone scan would also skip them
    site = next(s for s in scanner.SITES if s.get("search_url"))
    assert scanner.queries(site, profiles.get(p)) == []

def test_intake_requires_city_and_email_then_person_becomes_scannable(admin, q):
    r = admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": "Friend from school", "needs_auth": "1", "relationship": "outside"})
    pid = re.search(r"/person/(\w+)", r.headers["location"]).group(1)
    tok = re.search(r"ink=([\w-]+)", r.headers["location"]).group(1)
    assert q == [] and not profiles.scan_ready(db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone())
    pub = client()
    page = pub.get(f"/invite/intake/{tok}")
    assert page.status_code == 200 and "Current city and state are required" in page.text
    link_csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    base = {"csrf": link_csrf, "full_name": "Morgan Sample", "typed_name": "Morgan Sample", "agree": "yes", "sig": SIG}
    miss = pub.post(f"/invite/intake/{tok}", data=base)
    assert miss.status_code == 400 and ("city" in miss.text.lower() or "email" in miss.text.lower())
    miss = pub.post(f"/invite/intake/{tok}", data={**base, "addr_city": "Springfield", "addr_state": "CA"})
    assert miss.status_code == 400 and "email" in miss.text.lower()
    ok = pub.post(f"/invite/intake/{tok}", data={**base, "addr_city": "Springfield", "addr_state": "CA", "addr_street": "9 Fake St", "addr_current": "0",
                                                 "email": "morgan.sample@example.com", "phone": "5555550100", "prev_name": "Morgan Example",
                                                 "age_min": "30", "age_max": "40"}, headers={"User-Agent": "TestBrowser/1.5"})
    assert ok.status_code == 200 and "Signed" in ok.text
    p = db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    prof = profiles.get(p)
    assert p["full_name"] == "Morgan Sample" and p["city"] == "Springfield" and p["state"] == "CA" and p["email"] == "morgan.sample@example.com"
    assert p["esign_at"] and remover.has_auth(p) and profiles.complete(p) and profiles.scan_ready(p) and not profiles.waiting_intake(p)
    assert prof["addresses"][0]["street"] == "9 Fake St" and prof["phones"] == ["5555550100"] and prof["names"][1]["last"] == "Example" and prof["age_min"] == 30
    assert q == [(pid, "intake signed")]
    site = next(s for s in scanner.SITES if "{city" in (s.get("search_url") or ""))
    assert scanner.queries(site, prof)
    person = admin.get(f"/person/{pid}").text
    assert "Waiting on their intake form" not in person and "Springfield" in person and "Scan now" in person

def test_run_all_skips_people_without_location_or_consent(admin, q):
    r = admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": "Casey Pending", "needs_auth": "1", "relationship": "outside"})
    pid = re.search(r"/person/(\w+)", r.headers["location"]).group(1)
    c = db.conn()
    # even a forged signature without a city is not scannable
    c.execute("UPDATE people SET esign_at=?, consent_verified_at=? WHERE id=?", ("2026-11-17T00:00:00", "2026-11-17T00:00:00", pid)); c.commit()
    p = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    assert remover.has_auth(p) and not profiles.complete(p) and not profiles.scan_ready(p)
    assert scanner.queries(next(s for s in scanner.SITES if s.get("search_url")), profiles.get(p)) == []
    assert admin.post(f"/person/{pid}/scan", data={"csrf": csrf(admin, f"/person/{pid}")}).status_code == 403
    assert q == []
    # once a city is on file, they become scannable
    prof = profiles.get(p)
    prof["names"] = [{"kind": "legal", "first": "Casey", "middle": "", "last": "Pending"}]
    prof["addresses"] = [{"street": "", "city": "Springfield", "state": "CA", "current": True}]
    profiles.save(c, pid, prof)
    p = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    assert profiles.scan_ready(p)
    assert scanner.queries(next(s for s in scanner.SITES if "{city" in (s.get("search_url") or "")), profiles.get(p))
