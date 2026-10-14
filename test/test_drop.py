"""v1.3.1: California DROP assistance (prefill, agent disclosure, email ingest, status import) and ID/phone assist pages."""
import os, sys, tempfile, pathlib, re
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
sys.path.insert(0, str(ROOT / "optout"))
from fastapi.testclient import TestClient
import main, db, drop, inbound, scanner, remover
def admin():
    c = TestClient(main.app, follow_redirects=False); c.post("/login", data={"username": "alex", "password": "admin correct horse battery"}); return c
def csrf(c, url): return re.search(r'name="csrf" value="([^"]+)"', c.get(url).text).group(1)
def setup_ca():
    c = db.conn(); c.execute("UPDATE people SET state='CA', drop_at=NULL, esign_at=? WHERE id='alex'", (db.now(),)); c.execute("DELETE FROM drop_status") if c.execute("SELECT name FROM sqlite_master WHERE name='drop_status'").fetchone() else None; c.commit()

def test_needs_you_single_verify_task_and_prefill_page():
    setup_ca(); a = admin()
    page = a.get("/needs-you").text; assert "Verify with California (2 min)" in page and "/person/alex/drop" in page
    d = a.get("/person/alex/drop").text
    assert "Prefilled for DROP" in d and "data-copy" in d and "Authorized-agent disclosure" in d and "privacy.ca.gov" in d
    assert "SSN" not in d and "Social Security" not in d
    r = a.post("/person/alex/drop/started", data={"csrf": csrf(a, "/person/alex/drop")}); assert r.headers["location"] == drop.URL
    assert drop.get("alex")["state"] == "verifying" and "Verify with California" in a.get("/needs-you").text   # still the person's step until submitted

def test_confirmation_email_ingested_and_task_cleared():
    setup_ca()
    raw = (b"From: DROP <no-reply@privacy.ca.gov>\r\nTo: privacy+alex@example.org\r\nSubject: Your DROP request was submitted\r\n\r\n"
           b"Thank you. Your DROP ID: AB12CD34XY. Keep it safe.")
    out = inbound.handle(db.conn(), raw); assert out["kind"] == "drop_id" and out["person"] == "alex"
    d = drop.get("alex"); assert d["state"] == "submitted" and d["drop_id_masked"].endswith("4XY") and "AB12CD34XY" not in str(d["drop_id_masked"])
    assert "Verify with California (2 min) for Alex" not in admin().get("/needs-you").text

def test_status_import_marks_registry_brokers():
    setup_ca(); import registries, regreq
    bs = registries.brokers()[:2]
    if not bs: return
    c = db.conn(); regreq.plan(c, c.execute("SELECT * FROM people WHERE id='alex'").fetchone()); c.commit()
    text = f"{bs[0]['name']}    Deleted\n{bs[1]['name']} - Record not found\nnoise line"
    assert drop.parse_results(text) == {bs[0]["name"]: "deleted", bs[1]["name"]: "not_found"}
    a = admin(); r = a.post("/person/alex/drop/results", data={"csrf": csrf(a, "/person/alex/drop"), "text": text}); assert r.status_code == 303 and "Imported%202" in r.headers["location"]
    st = db.conn().execute("SELECT reply_kind FROM reg_requests WHERE person_id='alex' AND broker=?", (bs[0]["id"],)).fetchone()["reply_kind"]
    assert st == "drop_deleted" and drop.get("alex")["state"] == "results"

def test_id_and_phone_assist_pages():
    c = db.conn(); c.execute("UPDATE people SET esign_at=? WHERE id='alex'", (db.now(),)); c.commit()
    s = next(x for x in scanner.SITES if x["id"] == "mockemail"); s["optout"].update(requires="phone", phone="800-555-0100")
    try:
        lid = c.execute("INSERT INTO listings(person_id,site,url,first_seen,status) VALUES(?,?,?,?,?)", ("alex", "mockemail", db.enc("https://x.invalid/p"), db.now(), "needs_human")).lastrowid; c.commit()
        a = admin(); n = a.get("/needs-you").text; assert f"/listing/{lid}/assist" in n and "(5 min)" in n
        pg = a.get(f"/listing/{lid}/assist").text; assert "tel:800-555-0100" in pg and "California Consumer Privacy Act" in pg and "data-copy" in pg
        s["optout"]["requires"] = "id"; pg = a.get(f"/listing/{lid}/assist").text; assert "never store or send ID documents" in pg
    finally: s["optout"].pop("requires"); s["optout"].pop("phone")
