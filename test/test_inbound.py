"""Run: python -m pytest test/test_inbound.py  (no network: link clicks and Resend calls are stubbed)."""
import os, sys, tempfile, pathlib, importlib, json
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/t.db", PROFILES_DIR=str(ROOT / "test/profiles"), SCHEDULER="false", DRY_RUN="true",
                  DASHBOARD_PASSWORD="pw", INBOUND_SECRET="s3cret", SEND_PROVIDER="resend", FROM_ADDRESS="privacy@example.com",
                  BROKER_ALLOWLIST_FILE=str(ROOT / "optout/sites.yaml"))
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, remover, inbound, db
E = ROOT / "test/emails"
cli = TestClient(main.app)

def listing(pid, site):
    c = db.conn(); c.execute("INSERT OR IGNORE INTO listings(person_id,site,url,status,submitted_at) VALUES(?,?,?,?,?)", (pid, site, f"https://www.{site}.com/x/{pid}", "submitted", db.now())); c.commit()
    return c.execute("SELECT id FROM listings WHERE person_id=? AND site=?", (pid, site)).fetchone()[0]

def person(pid): return db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
def status(lid): return db.conn().execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()[0]
def raw(name, oid=0): return (E / name).read_text().replace("{OID}", str(oid)).encode()

def test_allowlist():
    assert "spokeo" in inbound.broker_site("spokeo.com") and "spokeo" in inbound.broker_site("mail.spokeo.com")
    assert inbound.broker_site("evilspokeo.com") is None and inbound.broker_site("spokeo.com.evil-login.ru") is None
    assert len(inbound.ALLOW) > 300

def test_outbox_dry_run_tag_and_replyto():
    c = db.conn(); lid = listing("alex", "spokeo")
    st = remover.send_email(c, person("alex"), "privacy@spokeo.com", "CCPA Data Deletion Request", "body", None, lid)
    o = c.execute("SELECT * FROM outbox ORDER BY id DESC").fetchone()
    assert st == "dry_run" and o["subject"].endswith(f"[FF-alex-{o['id']}]") and o["reply_to"] == "privacy+alex@example.com" and o["listing_id"] == lid

def test_resend_live_payload(monkeypatch):
    """With DRY_RUN off: From is the shared address, the auth PDF goes only with non-self (needs_auth) members."""
    sent = []; monkeypatch.setattr(remover, "DRY", False); monkeypatch.setattr(remover, "_resend", lambda *a: sent.append(a) or "sent:x")
    pdf = pathlib.Path(T) / "k.pdf"; pdf.write_bytes(b"%PDF-1.4 test")
    c = db.conn(); c.execute("UPDATE people SET auth_pdf=?, send_enabled=1 WHERE id IN ('sam','alex')", (str(pdf),)); c.commit()
    assert remover.send_email(c, person("sam"), "a@b.com", "S", "B", str(pdf)) == "sent"
    assert remover.send_email(c, person("alex"), "a@b.com", "S", "B", str(pdf)) == "sent"
    (frm, to, rto, subj, body, att, ref, _h), d = sent
    assert frm.endswith("<privacy@example.com>") and rto == "privacy+sam@example.com" and att == str(pdf) and f"[FF-sam-{ref}]" in subj
    assert d[2] == "privacy+alex@example.com" and d[5] is None
    c.execute("UPDATE people SET auth_pdf=NULL, send_enabled=0"); c.commit()

def test_webhook_secret():
    assert cli.post("/inbound/email", content=b"x").status_code == 401
    assert cli.post("/inbound/email", content=b"x", headers={"X-Inbound-Secret": "nope"}).status_code == 401

def test_verify_reply_dry_run_logs_only():
    c = db.conn(); lid = listing("alex", "spokeo"); oid = c.execute("SELECT id FROM outbox WHERE listing_id=?", (lid,)).fetchone()[0]
    r = cli.post("/inbound/email", content=raw("spokeo_verify.eml", oid), headers={"X-Inbound-Secret": "s3cret"}).json()
    assert r == {"ok": True, "kind": "verify", "matched": True}       # the webhook reveals nothing else
    row = db.conn().execute("SELECT * FROM inbox ORDER BY id DESC").fetchone()
    assert row["person_id"] == "alex" and row["listing_id"] == lid and row["outbox_id"] == oid
    clicked = json.loads(row["clicked"]); assert clicked and all(x.startswith("would-click") for x in clicked) and status(lid) == "pending"

def test_verify_reply_live_clicks_only_broker_links(monkeypatch):
    clicks = []; monkeypatch.setattr(inbound, "DRY", False)
    c = db.conn(); oid = c.execute("SELECT id FROM outbox WHERE person_id='alex' ORDER BY id LIMIT 1").fetchone()[0]
    r = inbound.handle(c, raw("spokeo_verify.eml", oid), clicker=lambda u: clicks.append(u) or "200 spokeo.com")
    assert clicks and all(inbound._host(u) == "spokeo.com" and "/optout/confirm" in u for u in clicks)
    clicks.clear(); r = inbound.handle(c, raw("spokeo_link_offsite.eml"), clicker=lambda u: clicks.append(u) or "200")
    assert r["kind"] == "verify" and clicks == []          # look-alike host and plain http are never clicked
    r = inbound.handle(c, raw("phish_lookalike.eml", oid), clicker=lambda u: clicks.append(u) or "200")
    assert r["site"] is None and clicks == []               # sender is not a broker -> nothing clicked

def test_removed_and_need_info():
    c = db.conn(); lk = listing("sam", "truthfinder")
    r = inbound.handle(c, raw("truthfinder_removed.eml"))
    assert r["person"] == "sam" and r["listing"] == lk and r["kind"] == "removed" and status(lk) == "pending"  # Removed only after re-scan
    ld = listing("alex", "spokeo"); r = inbound.handle(c, raw("unknown_needinfo.eml"))
    assert r["listing"] == ld and r["kind"] == "need_info" and status(ld) == "needs_human"
    assert c.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] >= 5

def test_click_guard_blocks_private_and_non_https():
    assert not inbound._public_https("http://www.spokeo.com/optout/confirm")       # not https
    assert not inbound._public_https("https://evil.example/optout/confirm")       # not a broker
    assert not inbound._public_https("https://localhost/optout")                   # not a broker / private
    import socket
    real = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("192.168.1.1", 443))]    # broker domain resolving to the LAN
    try: assert not inbound._public_https("https://www.spokeo.com/optout/confirm")
    finally: socket.getaddrinfo = real

def test_junk_triage_and_esign_gate():
    import esign, summary
    c = db.conn()
    raw = b"From: Deals <promo@shop-deals.example>\r\nTo: privacy@example.com\r\nSubject: Limited time 50% off\r\nList-Unsubscribe: <https://x.example/u>\r\n\r\nAct now https://shop-deals.example/claim\r\n"
    res = inbound.handle(c, raw, clicker=lambda u: (_ for _ in ()).throw(AssertionError("must not click")))
    assert res["kind"] == "junk" and not res["clicked"]
    k = person("sam"); c.execute("UPDATE people SET auth_pdf=NULL, esign_at=NULL WHERE id='sam'"); c.commit()
    assert not remover.has_auth(person("sam"))
    pdf = esign.render_pdf(esign.letter_lines(k, k["full_name"], "2026-10-13T11:00:00", "203.0.113.5", "sam"))
    assert pdf.startswith(b"%PDF-1.4") and b"%%EOF" in pdf[-16:] and b"Social Security" in pdf
    c.execute("UPDATE people SET esign_at='2026-10-13T11:00:00' WHERE id='sam'"); c.commit()
    assert remover.has_auth(person("sam")) and esign.name_matches(" sam  " + k["full_name"].split(" ", 1)[1], k["full_name"]) is (len(k["full_name"].split()) > 1)
    subj, html, text = summary.build(c, [person("sam")])
    assert "Requests sent" in text and "<table" in html and not summary.enabled()
    c.execute("UPDATE people SET esign_at=NULL WHERE id='sam'"); c.commit()
