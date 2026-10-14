"""Heartbeat 'Email DNS' check: legacy (amazonses MX/TXT) and newer Resend (send CNAME to forge.rmta.net) setups. No network."""
import os, sys, tempfile, pathlib
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", FROM_ADDRESS="privacy@example.org")
sys.path.insert(0, str(ROOT / "optout"))
import golive

def run(monkeypatch, z):
    monkeypatch.setattr(golive, "domain", lambda: "example.org")
    monkeypatch.setattr(golive, "doh_records", lambda n, t: z.get((n, t), []))
    return {x["key"]: x for x in golive.dns_checks()}

ROOT_SPF = [("TXT", "v=spf1 include:_spf.mx.cloudflare.net ~all")]
DKIM = [("TXT", "p=MIGfMA0")]

def test_new_resend_cname_style(monkeypatch):
    r = run(monkeypatch, {("send.example.org", "TXT"): [("CNAME", "send.forge.rmta.net."), ("TXT", "v=spf1 ip4:1.2.3.0/24 ~all")],
                          ("send.example.org", "MX"): [("CNAME", "send.forge.rmta.net."), ("MX", "10 feedback.forge.rmta.net.")],
                          ("example.org", "TXT"): ROOT_SPF, ("resend._domainkey.example.org", "TXT"): DKIM,
                          ("_dmarc.example.org", "TXT"): [("TXT", "v=DMARC1; p=none;")]})
    assert r["spf"]["ok"] and r["mx_send"]["ok"] and r["dkim"]["ok"] and r["dmarc"]["ok"]
    assert r["spf"]["detail"] == "v=spf1 ip4:1.2.3.0/24 ~all (via CNAME send.forge.rmta.net)"
    assert "_spf.mx.cloudflare.net" not in r["spf"]["detail"] and r["spf_root"]["ok"]
    assert r["mx_send"]["detail"] == "10 feedback.forge.rmta.net. (via CNAME send.forge.rmta.net)"

def test_cname_only_mx(monkeypatch):
    r = run(monkeypatch, {("send.example.org", "MX"): [("CNAME", "send.forge.rmta.net.")]})
    assert r["mx_send"]["ok"] and not r["spf"]["ok"]

def test_legacy_amazonses(monkeypatch):
    r = run(monkeypatch, {("send.example.org", "TXT"): [("TXT", "v=spf1 include:amazonses.com ~all")],
                          ("send.example.org", "MX"): [("MX", "10 feedback-smtp.us-east-1.amazonses.com.")],
                          ("_dmarc.example.org", "TXT"): [("TXT", "v=DMARC1; p=none; rua=mailto:d@example.org")]})
    assert r["spf"]["ok"] and r["mx_send"]["ok"] and r["dmarc"]["ok"] and "spf_root" not in r

def test_missing_and_wrong(monkeypatch):
    r = run(monkeypatch, {("send.example.org", "MX"): [("MX", "10 mx.other.net.")], ("example.org", "TXT"): ROOT_SPF})
    assert not r["spf"]["ok"] and not r["mx_send"]["ok"] and not r["dmarc"]["ok"] and not r["dkim"]["ok"]
    assert r["spf"]["detail"] == "no SPF record found"
