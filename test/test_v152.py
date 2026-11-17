"""A blank name, a field the key cannot read, or a half-applied startup migration must not turn every page into ERROR 500."""
import os, sys, tempfile, pathlib, sqlite3, json
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", HEALTH_FILE=f"{T}/site_health.json",
                  PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
# A 1.4-shaped database: the people table has no circle / consent columns yet.
pre = sqlite3.connect(os.environ["DB_PATH"])
pre.execute("""CREATE TABLE people(id TEXT PRIMARY KEY, full_name TEXT, first TEXT, last TEXT, city TEXT, state TEXT,
  prior_cities TEXT, email TEXT, phone TEXT, age_min INT, age_max INT, enabled INT DEFAULT 1,
  needs_auth INT DEFAULT 0, auth_pdf TEXT, bulk_ccpa INT DEFAULT 0, notes TEXT)""")
pre.execute("CREATE TABLE app_meta(k TEXT PRIMARY KEY, v TEXT)")
pre.execute("INSERT INTO app_meta(k,v) VALUES('version','1.4.0')")
pre.execute("INSERT INTO people(id,full_name,first,last,city,state,enabled,needs_auth) VALUES('casey','Casey Sample','Casey','Sample','Oakland','CA',1,1)")
pre.commit(); pre.close()
sys.path.insert(0, str(ROOT / "optout"))
from fastapi.testclient import TestClient
import main, db
from crypto import enc

def client(): return TestClient(main.app, follow_redirects=False)
def login(c):
    r = c.post("/login", data={"username": "alex", "password": "admin correct horse battery"})
    assert r.status_code == 303, r.text[:200]
    return c

def test_upgrade_keeps_existing_people_in_the_family():
    row = db.conn().execute("SELECT circle, full_name FROM people WHERE id='casey'").fetchone()
    assert row["circle"] == "family" and row["full_name"] == "Casey Sample"
    assert db.has_col(db.conn(), "people", "consent_verified_at") and db.has_col(db.conn(), "people", "esign_ua")

def test_blank_name_and_bad_ciphertext_still_load():
    c = db.conn(); now = db.now()
    c.execute("INSERT INTO people(id,full_name,first,last,city,state,enabled,needs_auth,circle) VALUES(?,?,?,?,?,?,1,1,'family')",
              ("noname", enc(""), enc(""), None, enc("Oakland"), "CA"))
    c.execute("INSERT INTO listings(person_id,site,url,fields,status,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
              ("noname", "mockemail", enc("https://mock.invalid/n"), enc("{}"), "reappeared", now, now))
    c.commit()
    raw = sqlite3.connect(os.environ["DB_PATH"])
    raw.execute("INSERT INTO events(ts,person_id,kind,detail) VALUES(?,?,?,?)", (now, "noname", None, "enc1:not-valid"))
    raw.commit(); raw.close()
    json.dump({"mockemail": {"status": "mystery", "evidence": "odd", "checked": now}}, open(os.environ["HEALTH_FILE"], "w"))
    a = login(client())
    for path in ("/", "/family", "/needs-you", "/inbox", "/sites", "/person/noname", "/person/alex", "/admin/heartbeat", "/add", "/coverage"):
        r = a.get(path)
        assert r.status_code == 200, path
        assert "Something broke" not in r.text, path
    assert "Someone" in a.get("/").text   # the blank profile still has a label
    # The signed-in admin is not one of the family rows: the footer avatar used to crash on an empty last name.
    c2 = db.conn(); c2.execute("UPDATE users SET person_id=? WHERE username='alex'", ("nobody",)); c2.commit()
    r = a.get("/")
    assert r.status_code == 200 and "Something broke" not in r.text and "Overview" in r.text
