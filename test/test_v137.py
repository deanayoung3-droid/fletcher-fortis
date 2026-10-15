"""v1.3.7: honest status label, archived dry-run drafts, Resend daily-quota handling + fair queue, broker-reply matching and the
privacy score that recovers. No network, no real email."""
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
    assert "Unverified" not in t and "Removing" in t and "1 removal in progress" in t and "could not be scanned automatically" in t
    c = db.conn(); c.execute("UPDATE listings SET status='confirmed_removed' WHERE status='submitted'"); c.commit()
    t = a.get("/person/alex").text; assert "Partly checked" in t and "blocked the automated scan" in t

def test_archive_superseded_drafts_and_inbox_hides_them():
    clear(); sign("alex", "sam")
    d1 = out("alex", "privacy@acme.invalid", "dry_run_superseded"); out("alex", "privacy@acme.invalid", "sent")          # went out for real later
    d2 = out("alex", "optout@bolt.invalid", "dry_run_superseded"); reg("alex", "bolt", "queued")                         # back in the live queue
    lid = listing("sam", "approved"); d3 = out("sam", "https://form.invalid", "dry_run_superseded", "form", lid)          # live chain handles it
    d4 = out("sam", "nobody@else.invalid", "dry_run_superseded")                                                          # matches nothing: stays
    assert lifecycle.archive_drafts(db.conn()) == 3
    st = {r["id"]: r["state"] for r in db.conn().execute("SELECT id, state FROM outbox")}
    assert [st[x] for x in (d1, d2, d3, d4)] == ["dry_run_archived"] * 3 + ["dry_run_superseded"]
    assert mailbox.out_class("dry_run_archived") == "archived" and mailbox.mail_counts(db.conn())["total"]["archived"] == 3
    a = admin(); t = a.get("/inbox").text
    assert "Archived draft" not in t and "Show 3 archived drafts" in t
    assert "Archived draft" in a.get("/inbox?archived=1").text and a.get(f"/inbox/out/{d1}").status_code == 200
    assert not db.conn().execute("SELECT 1 FROM outbox WHERE state='sent' AND to_addr='optout@bolt.invalid'").fetchone()   # archiving never sends

def test_daily_quota_requeues_without_using_tries_and_pauses_batch(monkeypatch):
    clear(); sign("alex", "sam")
    q = "error: Resend 429: You have reached your daily email sending quota."
    r1 = reg("alex", "acme", "failed", out("alex", "privacy@acme.invalid", q), tries=5)
    assert remover.quota_hit(q) and not remover.quota_hit("error: Resend 429: Too many requests")
    reg("alex", "bolt", "queued"); reg("sam", "acme", "queued"); reg("sam", "bolt", "queued")
    monkeypatch.setattr(remover, "DRY", False); monkeypatch.setattr(remover, "can_send", lambda p: (True, ""))
    order = []
    def fake_send(c, p, b, stage, sent_at=None):
        order.append((p["id"], b["id"]))
        if len(order) == 2: return "failed", out(p["id"], b["email"], q, c=c)
        return "sent", out(p["id"], b["email"], "sent", c=c)
    monkeypatch.setattr(regreq, "send", fake_send)
    res = regreq.tick(limit=10)
    assert res["retried"] == 1 and db.conn().execute("SELECT tries FROM reg_requests WHERE id=?", (r1,)).fetchone()["tries"] == 5
    assert order[0][0] != order[1][0]   # round-robin across people
    assert len(order) == 2, order       # batch stopped at the quota
    assert db.conn().execute("SELECT COUNT(*) n FROM reg_requests WHERE status='queued' AND person_id IN ('alex','sam') AND broker IN ('acme','bolt')").fetchone()["n"] == 2

def test_fair_batch_round_robin():
    clear()
    for b in ("acme", "bolt", "cove"): reg("alex", b, "queued")
    reg("sam", "acme", "queued")
    got = [(r["person_id"], r["broker"]) for r in regreq.fair_batch(db.conn(), 3)]
    assert got == [("alex", "acme"), ("sam", "acme"), ("alex", "bolt")]

PLATFORM = """From: Privacy <noreply@platform.invalid>\r\nTo: privacy@example.org\r\nSubject: Confirmation: Your request has been completed\r\nContent-Type: text/plain\r\n\r\n
Dear Data Subject, We are writing to confirm that your request to exercise your right to erasure has been completed.

On Wed, Oct 14, 2026 Fletcher Fortis Privacy <privacy@example.org> wrote:
> To Cove Insights privacy team,
> Reply to privacy+{pid}@example.org
"""
def test_platform_reply_without_tag_is_matched_and_feeds_registry_status():
    clear(); sign("alex"); oid = out("alex", "dsr@cove.invalid", "sent"); rid = reg("alex", "cove", "sent", oid)
    res = inbound.handle(db.conn(), PLATFORM.replace("{pid}", "alex").encode())
    assert res["person"] == "alex" and res["outbox"] == oid and res["kind"] == "removed"
    assert regreq.sync_replies(db.conn()) == 1
    assert db.conn().execute("SELECT status, reply_kind FROM reg_requests WHERE id=?", (rid,)).fetchone() == {"status": "completed", "reply_kind": "removed"}
    c = db.conn(); c.execute("INSERT INTO inbox(ts,person_id,outbox_id,kind) VALUES(?,?,?,?)", (db.now(), "alex", oid, "ack")); c.commit()
    regreq.sync_replies(db.conn()); assert db.conn().execute("SELECT status FROM reg_requests WHERE id=?", (rid,)).fetchone()["status"] == "completed"   # no downgrade

def test_not_found_classification_and_relink_of_old_mail():
    for t in ("We searched our database and were unable to find a match.", "We did not find any records about you. So there is nothing for us to delete.",
              "Based on the information provided, we were unable to locate or verify any records associated with your request."):
        assert inbound.classify("Re: request", t) == "not_found"
    assert inbound.classify("Re", "The information provided has been added to our suppression lists.") == "removed"
    assert inbound.classify("Re", "Thank you for your recent privacy inquiry, which we will consider and process in accordance with applicable laws.") == "ack"
    clear(); sign("alex"); oid = out("alex", "dsr@cove.invalid", "sent"); reg("alex", "cove", "sent", oid)
    c = db.conn(); body = PLATFORM.split("\r\n\r\n", 1)[1].replace("{pid}", "alex")
    c.execute("INSERT INTO inbox(ts,person_id,outbox_id,to_addr,subject,kind,body) VALUES(?,?,?,?,?,?,?)", (db.now(), None, None, db.enc("privacy@example.org"), db.enc("Confirmation"), "other", db.enc(body))); c.commit()
    assert inbound.relink(db.conn()) == (1, 1)
    r = db.conn().execute("SELECT person_id, outbox_id, kind FROM inbox").fetchone(); assert (r["person_id"], r["outbox_id"], r["kind"]) == ("alex", oid, "removed")

def test_score_recovers_with_confirmations_and_page_explains_it():
    clear(); sign("alex"); c = db.conn()
    lids = [listing("alex", "submitted") for _ in range(3)]
    for b in ("acme", "bolt", "cove"): reg("alex", b, "queued")
    v0 = score.compute(c, "alex")["value"]
    c.execute("UPDATE reg_requests SET status='sent'"); c.commit(); v1 = score.compute(c, "alex")["value"]
    c.execute("INSERT INTO inbox(ts,person_id,listing_id,kind) VALUES(?,?,?,?)", (db.now(), "alex", lids[0], "removed"))
    c.execute("UPDATE listings SET status='pending' WHERE id=?", (lids[0],)); c.commit(); v2 = score.compute(c, "alex")["value"]
    c.execute("UPDATE listings SET status='confirmed_removed' WHERE id=?", (lids[0],)); c.commit(); v3 = score.compute(c, "alex")["value"]
    c.execute("UPDATE reg_requests SET status='completed', reply_kind='not_found'"); c.commit(); v4 = score.compute(c, "alex")["value"]
    c.execute("UPDATE listings SET status='confirmed_removed'"); c.commit(); v5 = score.compute(c, "alex")["value"]
    assert v0 < v1 < v2 < v3 < v4 < v5 == 100
    d = score.compute(c, "alex"); assert any("confirmed by re-scan" in p["label"] for p in d["parts"])
    c.execute("UPDATE listings SET status='submitted'"); c.commit()
    score.ensure(c); c.execute("INSERT OR REPLACE INTO score_history VALUES('alex','2026-01-01',10)"); c.commit()
    t = admin().get("/person/alex").text
    assert "Why this score" in t and "since 01/01" in t and "data-person-chart" in t and "Broker confirmed" in t and "Listings with a removal request sent" in t
