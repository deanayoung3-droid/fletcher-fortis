"""v1.3.10: web forms for registry brokers that only take requests that way (overrides + learned from replies), automatic attempts.
No network, no real email, no real browser."""
import os, sys, tempfile, pathlib, re, json
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver",
                  FROM_ADDRESS="privacy@example.org")
sys.path.insert(0, str(ROOT / "optout"))
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, db, regreq, registries, remover, inbound, regforms, scanjobs, score
APW = "admin correct horse battery"
BROKERS = [{"id": "leadpost", "name": "LeadPost LLC", "email": "privacy@leadpost.invalid", "category": "marketing", "registries": ["CA", "VT"], "channel": "email"},
           {"id": "acme", "name": "Acme Data Inc.", "email": "privacy@acme.invalid", "category": "marketing", "registries": ["CA", "VT"], "channel": "email"},
           {"id": "formonly", "name": "Form Only Co", "email": "", "optout_url": "https://formonly.invalid/privacy", "category": "marketing", "registries": ["VT"], "channel": "form"}]

@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(registries, "brokers", lambda: BROKERS); monkeypatch.setattr(registries, "by_id", lambda: {b["id"]: b for b in BROKERS})
    ov = tmp_path / "ov.yaml"; ov.write_text("leadpost:\n  url: https://app.leadpost.invalid/PrivacyRequest\n  source: test\n"); monkeypatch.setattr(regforms, "OVERRIDES", str(ov))
    monkeypatch.setattr(remover, "DRY", False)
    c = db.conn(); regreq._ensure(c)
    for t in ("listings", "outbox", "inbox", "reg_requests", "broker_forms"): c.execute(f"DELETE FROM {t}")
    c.execute("UPDATE people SET esign_at=NULL, auth_pdf=NULL, send_enabled=0"); c.commit()

def sign(*pids):
    c = db.conn()
    for p in pids: c.execute("UPDATE people SET esign_at=?, send_enabled=1 WHERE id=?", (db.now(), p))
    c.commit()
def out(pid, to, state="sent"):
    c = db.conn(); oid = c.execute("INSERT INTO outbox(ts,person_id,to_addr,subject,body,state,channel) VALUES(?,?,?,?,?,?,?)", (db.now(), pid, to, db.enc("s"), db.enc("b"), state, "registry_email")).lastrowid; c.commit(); return oid
def reg(pid, broker, status, oid=None):
    c = db.conn(); b = next(x for x in BROKERS if x["id"] == broker)
    c.execute("INSERT OR REPLACE INTO reg_requests(person_id,broker,category,status,channel,target,outbox_id,created,updated) VALUES(?,?,?,?,?,?,?,?,?)", (pid, broker, "marketing", status, b["channel"], b["email"] or b.get("optout_url"), oid, db.now(), db.now())); c.commit()
    return c.execute("SELECT id FROM reg_requests WHERE person_id=? AND broker=?", (pid, broker)).fetchone()["id"]
def status(rid): return db.conn().execute("SELECT * FROM reg_requests WHERE id=?", (rid,)).fetchone()

QUOTE = "\n\nOn Wed, Oct 14, 2026 at 3:16 PM Fletcher Fortis Privacy <privacy@example.org> wrote:\n> To Acme Data Inc. privacy team,\n> see https://privacy.example.org/request/123 and https://www.acme.invalid/webform/ours\n"
def test_extract_form_url_from_needs_info_replies():
    E = regforms.extract
    assert E("Example does not accept privacy requests via email. You can submit a privacy request via our interactive web form found here: https://example-privacy.my.onetrust.com/webform/a564/7e4e Information concerning your rights: https://www.example.invalid/data-privacy-notice/" + QUOTE) == "https://example-privacy.my.onetrust.com/webform/a564/7e4e"
    assert E("please use our online privacy request form at https://privacy.deepsync.invalid/. This is our designated method" + QUOTE) == "https://privacy.deepsync.invalid/"
    assert E("If you'd like to opt-out of disclosure, please click here<https://privacyportal.onetrust.com/webform/x/optout>. If you'd like to make a data deletion request directly to us, please click here<https://privacyportal.onetrust.com/webform/x/delete>. Privacy Policy at https://example.invalid/privacy-policy/") == "https://privacyportal.onetrust.com/webform/x/delete"
    assert E("Privacy requests must go through our web form at https://app.leadpost.invalid/PrivacyRequest.") == "https://app.leadpost.invalid/PrivacyRequest"
    assert E("please respond to this email with any social media handles to which you want your request to apply." + QUOTE) is None   # no form: stays Needs info
    assert E("Please submit your request through either our online portal at https://www.equifax.invalid/personal/my-privacy/ or by calling") == "https://www.equifax.invalid/personal/my-privacy/"
    assert E("Web Form: Example Privacy Center. We encourage you to use the Privacy Center web form.", ["https://example.invalid/privacy-center/"]) == "https://example.invalid/privacy-center/"
    assert E("Please respond with your social media handles. See https://www.example.invalid/privacy-and-terms") is None

def test_leadpost_override_queues_a_form_attempt_for_every_consented_person(monkeypatch):
    sign("alex", "sam")
    r1 = reg("alex", "leadpost", "needs_info", out("alex", "privacy@leadpost.invalid")); r2 = reg("sam", "leadpost", "queued"); r3 = reg("pat", "leadpost", "queued")
    started = []; monkeypatch.setattr(scanjobs, "enqueue_regform", lambda rid: started.append(rid) or True)
    res = regforms.tick(db.conn())
    assert res["form_seeded"] == 3 and regforms.form_url(db.conn(), "leadpost") == "https://app.leadpost.invalid/PrivacyRequest"
    assert [status(r)["status"] for r in (r1, r2, r3)] == ["form_queued"] * 3 and status(r2)["target"] == "https://app.leadpost.invalid/PrivacyRequest"
    assert sorted(started) == sorted([r1, r2])   # pat has not signed: stays locked
    monkeypatch.setattr(remover, "DRY", True); started.clear(); regforms.tick(db.conn()); assert started == []   # never in dry run

def test_form_attempt_outcomes():
    sign("alex"); regforms.seed(db.conn())
    a = reg("alex", "leadpost", "form_queued")
    assert regforms.run(a, filler=lambda p, u, i: ("submitted", "submitted (email); site said: thank you")) == "sent"
    r = status(a); assert (r["status"], r["channel"], r["form_tries"]) == ("sent", "form", 1) and r["sent_at"]
    assert db.conn().execute("SELECT state FROM outbox WHERE person_id='alex' AND channel='form'").fetchone()["state"] == "submitted"
    b = reg("alex", "acme", "form_queued")
    assert regforms.run(b, filler=lambda p, u, i: ("needs_human", "form has a CAPTCHA (solver off)")) == "needs_human"
    assert "CAPTCHA" in status(b)["form_detail"]
    f = reg("alex", "formonly", "form_queued"); boom = lambda p, u, i: ("error", "could not load form")
    assert [regforms.run(f, filler=boom) for _ in range(3)] == ["form_queued", "form_queued", "needs_human"]   # retried, then Needs you
    assert regforms.run(f, filler=boom) is None   # not form_queued any more: nothing runs

def test_needs_info_reply_with_a_form_link_is_learned_and_not_undone(monkeypatch):
    sign("alex"); oid = out("alex", "privacy@acme.invalid"); rid = reg("alex", "acme", "sent", oid); reg("sam", "acme", "queued")
    raw = (f"From: Privacy <privacy@acme.invalid>\r\nTo: privacy+alex@example.org\r\nSubject: Re: CCPA/CPRA deletion and opt-out request [FF-alex-{oid}]\r\n"
           "Content-Type: text/plain\r\n\r\nAcme does not accept privacy requests via email. Please submit your request through our portal at https://acme.my.onetrust.invalid/webform/abc\r\n")
    res = inbound.handle(db.conn(), raw.encode()); assert res["kind"] == "need_info"
    assert regforms.form_url(db.conn(), "acme") == "https://acme.my.onetrust.invalid/webform/abc"
    assert status(rid)["status"] == "form_queued" and db.conn().execute("SELECT status FROM reg_requests WHERE person_id='sam' AND broker='acme'").fetchone()["status"] == "form_queued"
    regreq.sync_replies(db.conn()); regforms.harvest(db.conn()); assert status(rid)["status"] == "form_queued"   # the old reply is not applied again
    regforms.run(rid, filler=lambda p, u, i: ("submitted", "ok")); regreq.sync_replies(db.conn()); regforms.harvest(db.conn())
    assert status(rid)["status"] == "sent"   # one attempt per request; no loop

def test_send_uses_the_form_path_for_form_brokers():
    sign("alex"); regforms.seed(db.conn()); c = db.conn(); p = c.execute("SELECT * FROM people WHERE id='alex'").fetchone()
    assert regreq.send(c, p, BROKERS[0], 0) == ("form_queued", None)
    assert regreq.send(c, p, BROKERS[2], 0) == ("form_queued", None)
    assert not db.conn().execute("SELECT 1 FROM outbox WHERE person_id='alex'").fetchone()   # no email, no manual record
    assert regreq.STATUS["form_queued"][0] == "Web form queued"
