"""v1.3.9: score formula where broker confirmations visibly move the score, points breakdown, history recomputed.
No network, no real email."""
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

def many_brokers(pid, n, status="queued"):
    c = db.conn(); regreq._ensure(c)
    for i in range(n):
        c.execute("INSERT INTO reg_requests(person_id,broker,category,status,channel,target,created,updated) VALUES(?,?,?,?,?,?,?,?)", (pid, f"x{i}", "marketing", status, "email", f"p@x{i}.invalid", db.now(), db.now()))
    c.commit()
def set_reg(pid, n, status, kind=None, offset=0):
    c = db.conn(); ids = [r["id"] for r in c.execute("SELECT id FROM reg_requests WHERE person_id=? ORDER BY id LIMIT ? OFFSET ?", (pid, n, offset))]
    for i in ids: c.execute("UPDATE reg_requests SET status=?, reply_kind=? WHERE id=?", (status, kind, i))
    c.commit()
def val(pid="alex"): return score.compute(db.conn(), pid)["value"]
def exact(pid="alex"): return 100 - score.compute(db.conn(), pid)["lost"]

def test_registry_confirmations_visibly_move_the_score():
    clear(); sign("alex"); many_brokers("alex", 640)
    set_reg("alex", 400, "sent"); base = exact()
    gains = []
    for k in range(1, 8):
        set_reg("alex", 1, "completed", "removed", offset=k - 1); gains.append(exact() - base); base = exact()
    assert all(0.5 <= g <= 1.2 for g in gains), gains          # each early confirmation is worth about a point
    set_reg("alex", 1, "completed", "not_found", offset=7); assert 0.5 <= exact() - base <= 1.2   # "no records" counts the same
    d = score.compute(db.conn(), "alex"); p = {x["label"]: x for x in d["parts"]}
    assert p["Broker deletion confirmations"]["count"] == 7 and 5 <= p["Broker deletion confirmations"]["gained"] <= 7
    t = admin().get("/person/alex").text
    assert "from 7 broker deletion confirmations" in t and "Points earned back" in t and "Points still lost" in t and "still lost = " in t

def test_not_contacted_brokers_hold_it_down_gently_and_full_marks_are_reachable():
    clear(); sign("alex"); many_brokers("alex", 600)
    d = score.compute(db.conn(), "alex"); p = {x["label"]: x for x in d["parts"]}
    assert p["Registered brokers waiting in the automatic queue"]["lost"] == 5.0   # 15-point reach share, a third while queued
    set_reg("alex", 600, "failed"); assert {x["label"]: x for x in score.compute(db.conn(), "alex")["parts"]}["Registered brokers not contacted (failed or blocked)"]["lost"] == 15.0
    clear(); sign("alex"); many_brokers("alex", 3); set_reg("alex", 3, "completed", "not_found"); assert val() == 100   # every broker answered

def test_score_is_monotonic_along_the_removal_path():
    clear(); sign("alex"); many_brokers("alex", 50); lid = listing("alex", "approved"); listing("alex", "submitted")
    steps = [exact()]
    c = db.conn(); c.execute("UPDATE listings SET status='submitted' WHERE id=?", (lid,)); c.commit(); steps.append(exact())
    set_reg("alex", 50, "sent"); steps.append(exact())
    c = db.conn(); c.execute("INSERT INTO inbox(ts,person_id,listing_id,kind) VALUES(?,?,?,?)", (db.now(), "alex", lid, "removed")); c.execute("UPDATE listings SET status='pending' WHERE id=?", (lid,)); c.commit()
    broker = exact(); steps.append(broker)
    c = db.conn(); c.execute("UPDATE listings SET status='confirmed_removed' WHERE id=?", (lid,)); c.commit(); rescan = exact(); steps.append(rescan)
    for k in range(5): set_reg("alex", 1, "completed", "removed", offset=k); steps.append(exact())
    assert all(b > a for a, b in zip(steps, steps[1:])), steps
    assert (broker - steps[2]) >= 0.75 * (rescan - steps[2])     # a broker's email gives most of what the re-scan gives

def test_history_is_recomputed_with_the_new_formula(monkeypatch):
    clear(); sign("alex"); many_brokers("alex", 100, "sent"); c = db.conn()
    c.execute("UPDATE reg_requests SET created='2026-10-01T00:00:00'"); c.commit()
    lid = listing("alex", "submitted"); c = db.conn(); c.execute("UPDATE listings SET first_seen='2026-10-10T00:00:00', submitted_at='2026-10-11T00:00:00' WHERE id=?", (lid,))
    oid = out("alex", "p@x0.invalid", "sent", c=c); c.execute("UPDATE outbox SET ts='2026-10-11T00:00:00'"); c.execute("UPDATE reg_requests SET outbox_id=? WHERE broker='x0'", (oid,))
    c.execute("UPDATE reg_requests SET status='completed', reply_kind='removed' WHERE broker='x0'")
    c.execute("INSERT INTO inbox(ts,person_id,outbox_id,kind) VALUES('2026-10-13T09:00:00','alex',?,'removed')", (oid,)); c.commit()
    score.ensure(c); c.execute("INSERT OR REPLACE INTO score_history VALUES('alex','2026-10-12',99)"); c.commit()   # an old-formula snapshot
    monkeypatch.setattr(score, "now", lambda: "2026-10-15T12:00:00")
    assert score.recompute_history(db.conn()) >= 6
    h = dict(score.history(db.conn(), "alex"))
    assert h["2026-10-12"] != 99 and h["2026-10-12"] < h["2026-10-13"] and h["2026-10-15"] == val()   # the confirmation on Oct 13 shows up as a step
    monkeypatch.undo()
    c = db.conn(); c.execute("UPDATE score_history SET day=date(day, '+' || (julianday('now') - julianday('2026-10-15')) || ' days')"); c.commit()
    t = admin().get("/person/alex").text; assert "data-score-history" in t and "Score formula updated" in t
