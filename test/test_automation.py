"""Needs-you automation: blocked-site backoff, re-check scheduling, CAPTCHA solver gating (no network, no real broker)."""
import os, sys, tempfile, pathlib, datetime, urllib.request
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/a.db", FF_DATA_KEY="automation-test-key-0123456789-abcdef", SITES_FILE=str(ROOT / "test/sites.test.yaml"), PROFILES_DIR=str(ROOT / "test/profiles"), SCHEDULER="false", DRY_RUN="true")
sys.path.insert(0, str(ROOT / "optout"))
import db, config, automation
c = db.conn(); c.executescript(db.SCHEMA); config.load()
c.execute("INSERT INTO people(id, full_name, first, last, city, state) VALUES('p1','Test Person','Test','Person','Springfield','IL')")
c.execute("INSERT INTO listings(id, person_id, site, url, status) VALUES(1,'p1','mocksite','u','manual_check')"); c.commit()

def test_backoff_then_human():
    automation.schedule(c, 1); assert 1 in automation.pending(c)
    for i in range(len(automation.BACKOFF)):
        assert 1 in automation.pending(c); automation.record(c, 1, ok=False, detail="blocked")
    assert 1 not in automation.pending(c)                     # all retries used: now a human task
    r = c.execute("SELECT * FROM retry WHERE listing_id=1").fetchone(); assert r["attempts"] == 5
    assert c.execute("SELECT 1 FROM events WHERE kind='retry_exhausted'").fetchone(); c.commit()

def test_backoff_intervals_double():
    c.execute("DELETE FROM retry"); automation.schedule(c, 1)
    for want in (2, 4, 8):
        automation.record(c, 1, ok=False); n = c.execute("SELECT next_at FROM retry WHERE listing_id=1").fetchone()["next_at"]
        assert abs((datetime.datetime.fromisoformat(n) - datetime.datetime.now()).days - want) <= 1
    c.commit()

def test_retry_off_shows_everything():
    config.save({"RETRY_BLOCKED": "false"}); assert automation.pending(c) == {}; config.save({"RETRY_BLOCKED": "true"})

def test_solver_off_by_default_and_never_calls_in_dry_run(monkeypatch):
    assert automation.provider() == "off" and not automation.solver_on()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network call")))
    config.save({"CAPTCHA_PROVIDER": "2captcha", "CAPTCHA_API_KEY": "test-key-not-real-123"}); assert automation.solver_on()
    tok, why = automation.solve("http://x.invalid/form", '<div class="g-recaptcha" data-sitekey="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"></div>', dry=True)
    assert tok is None and why.startswith("DRY_RUN")
    est = automation.estimate(c); assert est["per_solve"] == 0.003 and est["cap"] == 5
    config.save({"CAPTCHA_PROVIDER": "off"}, clear=("CAPTCHA_API_KEY",))

def test_budget_cap_blocks_live_solve(monkeypatch):
    config.save({"CAPTCHA_PROVIDER": "capsolver", "CAPTCHA_API_KEY": "test-key-not-real-123", "CAPTCHA_MONTHLY_CAP_USD": "0"})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network call")))
    tok, why = automation.solve("http://x.invalid/form", '<div class="cf-turnstile" data-sitekey="0x4AAAAAAAAAAAAAAAAAAAAAAAAAAA"></div>', dry=False)
    assert tok is None and "budget" in why
    config.save({"CAPTCHA_PROVIDER": "off", "CAPTCHA_MONTHLY_CAP_USD": "5"}, clear=("CAPTCHA_API_KEY",))

def test_detect_kinds():
    assert automation.detect('<div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001"></div>')[0] == "hcaptcha"
    assert automation.detect("<form>no captcha</form>") is None
