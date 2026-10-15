"""v1.3.8: Removed counts broker-confirmed deletions (shared counts for the home tile, family cards, person page and score),
immediate refresh on inbound replies, and classifier fixes. No network, no real email."""
import os, sys, tempfile, pathlib, re, json
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver",
                  FROM_ADDRESS="privacy@example.org")
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, db, mailbox, regreq, registries, remover, lifecycle, inbound, score
APW = "admin correct horse battery"
BROKERS = [{"id": "acme", "name": "Acme Data Inc.", "email": "privacy@acme.invalid", "category": "marketing", "registries": ["CA", "VT"], "channel": "email"},
           {"id": "bolt", "name": "Bolt Lists LLC", "email": "optout@bolt.invalid", "category": "marketing", "registries": ["CA", "VT"], "channel": "email"},
           {"id": "cove", "name": "Cove Insights", "email": "dsr@cove.invalid", "category": "risk", "registries": ["CA", "VT"], "channel": "email"}]

@pytest.fixture(autouse=True)
def brokers(monkeypatch):
    monkeypatch.setattr(registries, "brokers", lambda: BROKERS); monkeypatch.setattr(registries, "by_id", lambda: {b["id"]: b for b in BROKERS})

def admin():
    c = TestClient(main.app, follow_redirects=False); c.post("/login", data={"username": "alex", "password": APW}); return c
def sign(*pids):
    c = db.conn()
    for p in pids: c.execute("UPDATE people SET esign_at=?, send_enabled=1 WHERE id=?", (db.now(), p))
    c.commit()
def listing(pid, status, site="mockemail"):
    c = db.conn(); lid = c.execute("INSERT INTO listings(person_id,site,url,first_seen,status) VALUES(?,?,?,?,?)", (pid, site, db.enc(f"https://x.invalid/{os.urandom(4).hex()}"), db.now(), status)).lastrowid; c.commit(); return lid
def out(pid, to, state, channel="registry_email", listing_id=None, stage=0, c=None):
    c = c or db.conn(); oid = c.execute("INSERT INTO outbox(ts,person_id,to_addr,subject,body,state,channel,listing_id,stage) VALUES(?,?,?,?,?,?,?,?,?)", (db.now(), pid, to, db.enc("s"), db.enc("b"), state, channel, listing_id, stage)).lastrowid; c.commit(); return oid
def reg(pid, broker, status, oid=None, tries=0):
    c = db.conn(); regreq._ensure(c); b = next(x for x in BROKERS if x["id"] == broker)
    c.execute("INSERT OR REPLACE INTO reg_requests(person_id,broker,category,status,channel,target,outbox_id,created,updated,tries) VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, broker, b["category"], status, "email", b["email"], oid, db.now(), db.now(), tries)); c.commit()
    return c.execute("SELECT id FROM reg_requests WHERE person_id=? AND broker=?", (pid, broker)).fetchone()["id"]
def clear():
    c = db.conn(); regreq._ensure(c)
    for t in ("listings", "outbox", "inbox", "reg_requests"): c.execute(f"DELETE FROM {t}")
    c.commit()

def test_status_label_says_what_it_means():
    clear(); sign("alex"); a = admin()
    listing("alex", "submitted"); listing("alex", "manual_check", "mockdeep")
    t = a.get("/person/alex").text

QUOTE = "\n\nOn Wed, Oct 14, 2026 at 3:16 PM Fletcher Fortis Privacy <privacy@example.org> wrote:\n> To Acme Data Inc. privacy team,\n> - Delete all personal information you hold about me; it has been removed elsewhere\n"
CASES = {  # broker reply wording seen in production (no personal data), with our letter quoted below
    "Please note that for privacy and security reasons, Example Co does not accept privacy requests via email. You can submit a privacy request via our interactive web form found here:": "need_info",
    "To submit a request to opt out, delete, or access personal information, please use our online privacy request form at https://privacy.example.invalid/.": "need_info",
    "We searched our database for the information you provided and were unable to find\na match. As such, we cannot fulfill your request because we do not have your data.": "not_found",
    "If you wish to exercise your privacy rights and choices, please use the self-service options below. NOTE: Privacy requests must originate via the self-service options below.": "need_info",
    "IF YOU REQUIRE ACTION, please utilize the appropriate link provided below to access our submission portal for the type of request you would like to make.": "need_info",
    "If you would like to exercise your rights, please submit your request here <https://example.invalid/privacy-center> . Because we are a B2B company": "need_info",
    "We searched the records and systems under our control using the identifying information you provided and did not identify any personal data associated with you.": "not_found",
    "Example, Inc. is in receipt of your request. Please submit your request through either our online portal at https://example.invalid/my-privacy/ or by calling": "need_info",
    "Dear Data Subject, We are writing to confirm that your request to exercise your right to erasure has been completed.": "removed",
    "We have received your request and will respond within 45 days. Your case number is 123.": "ack",
    "We have received your email. To submit a data rights request, please complete the form which can be found here: https://x.invalid.": "need_info",
    "We received your request and are working to resolve your issue. Your ticket reference is HS-1. If you have any additional information that you think will help us, simply reply.": "ack",
    "Your request will be processed unless we contact you for additional information. To help us expedite your request please fill out the form found via https://x.invalid": "need_info",
    "We have submitted your information into the platform we use to process data deletion requests. Once the deletion request is complete, you will receive a notification. We are closing this ticket.": "ack",
    "Based on the information provided, we were unable to locate or verify any records associated with your request.": "not_found",
}
OUTLOOK = "\n________________________________\nFrom: Fletcher Fortis Privacy <privacy@example.org>\nSent: Thursday\nTo: Privacy\nSubject: x\nTo Acme Data Inc. privacy team, it has been removed"
def test_classifier_reads_the_reply_not_our_quoted_letter():
    for body, want in CASES.items(): assert inbound.classify("Re: CCPA/CPRA deletion and opt-out request [FF-alex-1]", body + QUOTE) == want, body[:60]
    assert inbound.classify("Re: request", "Thanks for your message." + QUOTE) == "other"   # 'has been removed' only appears in our own quoted letter
    assert inbound.classify("Re: request", "Thanks for your message." + OUTLOOK) == "other"

def test_latest_reply_wins_but_completed_is_final():
    clear(); sign("alex"); oid = out("alex", "privacy@acme.invalid", "sent"); rid = reg("alex", "acme", "sent", oid); c = db.conn()
    c.execute("INSERT INTO inbox(ts,person_id,outbox_id,kind) VALUES(?,?,?,?)", ("2026-10-14T10:00:00", "alex", oid, "need_info"))
    c.execute("INSERT INTO inbox(ts,person_id,outbox_id,kind) VALUES(?,?,?,?)", ("2026-10-14T12:00:00", "alex", oid, "ack")); c.commit()
    regreq.sync_replies(db.conn()); assert db.conn().execute("SELECT status FROM reg_requests WHERE id=?", (rid,)).fetchone()["status"] == "acknowledged"
    c = db.conn(); c.execute("INSERT INTO inbox(ts,person_id,outbox_id,kind) VALUES(?,?,?,?)", ("2026-10-15T10:00:00", "alex", oid, "removed"))
    c.execute("INSERT INTO inbox(ts,person_id,outbox_id,kind) VALUES(?,?,?,?)", ("2026-10-15T12:00:00", "alex", oid, "need_info")); c.commit()
    regreq.sync_replies(db.conn()); assert db.conn().execute("SELECT status FROM reg_requests WHERE id=?", (rid,)).fetchone()["status"] == "completed"

def seed():
    """alex: 1 listing verified by re-scan, 1 the broker says is removed, 1 the broker says it has no records, 1 still sent;
    registry: acme confirmed deletion (two emails), bolt no records, cove sent."""
    clear(); sign("alex")
    l1 = listing("alex", "confirmed_removed"); l2 = listing("alex", "pending"); l3 = listing("alex", "pending"); listing("alex", "submitted")
    c = db.conn(); c.execute("UPDATE listings SET confirmed_at='2026-10-10T10:00:00' WHERE id=?", (l1,))
    c.execute("INSERT INTO inbox(ts,person_id,listing_id,kind) VALUES('2026-10-11T09:00:00','alex',?,'removed')", (l2,))
    c.execute("INSERT INTO inbox(ts,person_id,listing_id,kind) VALUES('2026-10-11T09:00:00','alex',?,'not_found')", (l3,)); c.commit()
    oa = out("alex", "privacy@acme.invalid", "sent"); ob = out("alex", "optout@bolt.invalid", "sent"); oc = out("alex", "dsr@cove.invalid", "sent")
    reg("alex", "acme", "sent", oa); reg("alex", "bolt", "sent", ob); reg("alex", "cove", "sent", oc)
    c = db.conn()
    for ts, oid, k in (("2026-10-12T08:00:00", oa, "removed"), ("2026-10-12T08:00:01", oa, "removed"), ("2026-10-13T08:00:00", ob, "not_found")):
        c.execute("INSERT INTO inbox(ts,person_id,outbox_id,kind) VALUES(?,?,?,?)", (ts, "alex", oid, k))
    c.commit(); regreq.sync_replies(db.conn()); return oc

def test_removed_counts_broker_confirmations_and_everything_agrees(monkeypatch):
    seed(); monkeypatch.setattr(score, "now", lambda: "2026-10-15T12:00:00")
    r = score.removals(db.conn(), "alex")
    assert (r["removed"], r["verified"], r["broker"], r["broker_listings"], r["broker_registry"], r["clear"]) == (3, 1, 2, 1, 1, 2)
    assert (r["found"], r["contacted"], r["denom"]) == (4, 3, 6) and r["pct"] == 50   # found 4 + contacted 3 - 1 broker with no records
    assert r["days"][-1] == "2026-10-15" and r["series"][-6:] == [1, 2, 3, 3, 3, 3] and r["series"][0] == 0   # backfilled from re-scan and reply dates
    assert score.removals(db.conn())["removed"] == 3                                  # family total = sum of people
    d = score.compute(db.conn(), "alex"); assert d["removed"] == r["removed"] and d["clear"] == r["clear"]   # score uses the same counts
    a = admin(); home = a.get("/").text
    assert "1 verified by re-scan · 2 confirmed by broker" in home and "50% of 6 found or contacted · 2 no records (clear)" in home and "data-removed-spark" in home
    fam = a.get("/family").text; assert "1 verified by re-scan · 2 confirmed by broker · 2 no records" in fam
    per = a.get("/person/alex").text; assert "1 verified by re-scan · 2 confirmed by broker · 2 no records (clear)" in per and "3 removed" in per
    assert db.conn().execute("SELECT status FROM listings WHERE id IN (SELECT listing_id FROM inbox WHERE kind='removed' AND listing_id IS NOT NULL)").fetchone()["status"] == "pending"   # only a re-scan marks Removed

REPLY = """From: Privacy <privacy@cove.invalid>\r\nTo: privacy+alex@example.org\r\nSubject: Re: CCPA/CPRA deletion and opt-out request [FF-alex-{oid}]\r\nContent-Type: text/plain\r\n\r\nHello, your data has been deleted from our systems.\r\n"""
def test_new_confirmation_updates_everything_immediately():
    oc = seed(); before = score.removals(db.conn(), "alex"); v0 = score.compute(db.conn(), "alex")["value"]
    res = inbound.handle(db.conn(), REPLY.replace("{oid}", str(oc)).encode())
    assert res["kind"] == "removed" and res["outbox"] == oc
    c = db.conn()   # no tick / nightly job ran
    assert c.execute("SELECT status, reply_kind FROM reg_requests WHERE outbox_id=?", (oc,)).fetchone() == {"status": "completed", "reply_kind": "removed"}
    after = score.removals(c, "alex"); assert after["removed"] == before["removed"] + 1 and after["broker_registry"] == 2
    v1 = score.compute(c, "alex")["value"]; assert v1 > v0
    assert c.execute("SELECT value FROM score_history WHERE person_id='alex' AND day=?", (db.now()[:10],)).fetchone()["value"] == v1
    assert f"{after['broker']} confirmed by broker" in admin().get("/").text

def test_inbox_shows_no_records_pill():
    seed(); assert mailbox.IN_PILL["not_found"][0] == "No records found"
