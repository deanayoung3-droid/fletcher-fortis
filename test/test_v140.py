"""v1.4.0: DATA_DIR/DB_DIR split, encrypted backup export/import (CLI, Heartbeat, first-run restore), env DRY_RUN wins,
optional Cloudflare Access check. Dummy data only. The 'old install' runs in a subprocess (modules read env at import)."""
import os, sys, json, re, time, base64, hashlib, sqlite3, subprocess, tempfile, pathlib, textwrap
ROOT = pathlib.Path(__file__).resolve().parents[1]; T = pathlib.Path(tempfile.mkdtemp())
KEY = "test-key-" + "k" * 40; OTHER = "other-key-" + "o" * 40; PW = "a long and quiet harbor 2026"
NEW = T / "new"
for k in ("ADMIN_PASSWORD", "DASHBOARD_PASSWORD", "PUBLIC_URL", "RESEND_API_KEY", "INBOUND_SECRET", "CAPTCHA_API_KEY", "DB_PATH", "SHOT_DIR", "AUTH_DIR", "HEALTH_FILE", "STATE_DIR", "FORCE_DRY_RUN", "CF_ACCESS_MODE"): os.environ.pop(k, None)
os.environ.update(FF_DATA_KEY=KEY, DATA_DIR=str(NEW / "data"), DB_DIR=str(NEW / "state"), PROFILES_DIR=str(T / "no-profiles"),
                  SITES_FILE=str(ROOT / "test/sites.test.yaml"), SCHEDULER="false", DRY_RUN="true", COOKIE_SECURE="false",
                  SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", UPDATE_CHECK="false")
sys.path.insert(0, str(ROOT / "optout"))

OLD_SCRIPT = textwrap.dedent('''
    import os, sys; sys.path.insert(0, os.environ["OPTOUT"])
    import main, db, config, backup, paths
    c = db.conn()
    c.execute("INSERT INTO people(id, full_name, first, last, email) VALUES(?,?,?,?,?)", ("sam_test", db.enc("Sam Testperson"), db.enc("Sam"), db.enc("Testperson"), db.enc("sam@example.com")))
    c.commit()
    config.save({"RESEND_API_KEY": "re_dummy_resend_key_123456", "INBOUND_SECRET": "dummy-inbound-secret-abcdef", "CAPTCHA_API_KEY": "CAP-dummy-solver-key-999", "DRY_RUN": "false", "APP_NAME": "Test Fortress"})
    for d, f, b in ((paths.shot_dir(), "sam_test_1.webp", b"shot" * 1000), (paths.auth_dir(), "sam_test.pdf", b"%PDF-1.4 dummy")):
        os.makedirs(d, exist_ok=True); open(os.path.join(d, f), "wb").write(b)
    sys.exit(backup.main(["export", sys.argv[1]]))
''')

def make_old_backup(out, key, name):
    d = T / name; env = {k: v for k, v in os.environ.items() if k not in ("DB_DIR",)}
    env.update(FF_DATA_KEY=key, DATA_DIR=str(d), ADMIN_USERNAME="olda", ADMIN_PASSWORD=PW, OPTOUT=str(ROOT / "optout")); env.pop("DRY_RUN", None)   # live via the app setting (pinned in the DB)
    p = subprocess.run([sys.executable, "-c", OLD_SCRIPT, str(out)], env=env, capture_output=True, text=True, cwd=str(ROOT / "optout"))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "exported" in p.stdout and "re_dummy" not in p.stdout + p.stderr
    return d

GOOD = T / "good.ffb"; BAD = T / "otherkey.ffb"
OLD_DIR = make_old_backup(GOOD, KEY, "old"); make_old_backup(BAD, OTHER, "old2")

import pytest
from fastapi.testclient import TestClient
import main, db, onboarding, config, remover, backup, paths, cfaccess

def client(): return TestClient(main.app, follow_redirects=False)
def setup_client():
    c = client(); assert c.get(f"/setup?token={onboarding._TOKEN[0]}").status_code == 303; return c

def test_paths_defaults_unchanged(monkeypatch):
    for k in ("DATA_DIR", "DB_DIR", "STATE_DIR", "DB_PATH", "SHOT_DIR", "AUTH_DIR", "HEALTH_FILE", "BROKER_OUT", "BROKER_SRC", "REGISTRY_DIR"): monkeypatch.delenv(k, raising=False)
    assert (paths.db_path(), paths.shot_dir(), paths.auth_dir(), paths.health_file(), paths.key_file()) == \
        ("/data/optout.db", "/data/shots", "/data/auth", "/data/site_health.json", "/data/secrets/ff_data_key")
    monkeypatch.setenv("DATA_DIR", "/data"); monkeypatch.setenv("STATE_DIR", "/state")
    assert paths.db_path() == "/state/optout.db" and paths.shot_dir() == "/data/shots" and paths.health_file() == "/state/site_health.json"
    monkeypatch.setenv("DB_PATH", "/x/y.db"); assert paths.db_path() == "/x/y.db"         # explicit variables still win

def test_new_install_uses_split_dirs():
    assert db.DB == str(NEW / "state" / "optout.db") and os.path.exists(db.DB)
    assert not (NEW / "data" / "optout.db").exists()

def test_backup_file_is_encrypted_and_has_no_plaintext():
    raw = GOOD.read_bytes(); assert raw.startswith(backup.MAGIC)
    for s in (b"re_dummy_resend_key", b"Sam Testperson", b"sam@example.com", b"%PDF", b"SQLite format"): assert s not in raw

def test_wizard_offers_restore_and_log_line_mentions_it(capsys):
    onboarding.init_token(); out = capsys.readouterr().out
    assert "Setup URL: " in out and "/setup?token=" in out and "restore from a backup" in out
    p = setup_client().get("/setup"); assert p.status_code == 200 and 'action="/setup/restore"' in p.text and "Restore from a backup" in p.text

def test_restore_refuses_other_key_and_changes_nothing():
    c = setup_client()
    r = c.post("/setup/restore", files={"backup": ("x.ffb", BAD.read_bytes(), "application/octet-stream")})
    assert r.status_code == 400 and "different FF_DATA_KEY" in r.text
    r = c.post("/setup/restore", files={"backup": ("x.ffb", b"not a backup at all", "application/octet-stream")})
    assert r.status_code == 400 and "not a Fletcher Fortis backup" in r.text
    t = GOOD.read_bytes(); r = c.post("/setup/restore", files={"backup": ("x.ffb", t[:-200], "application/octet-stream")})
    assert r.status_code == 400 and ("incomplete" in r.text or "damaged" in r.text)
    assert not db.conn().execute("SELECT 1 FROM users").fetchone() and not db.conn().execute("SELECT 1 FROM people").fetchone()
    assert not (NEW / "data" / "shots" / "sam_test_1.webp").exists()
    assert client().post("/setup/restore", files={"backup": ("x.ffb", t, "application/octet-stream")}).status_code == 403   # needs the setup token

def test_restore_from_wizard_then_login_with_restored_admin():
    c = setup_client()
    r = c.post("/setup/restore", files={"backup": ("ff-backup.ffb", GOOD.read_bytes(), "application/octet-stream")})
    assert r.status_code == 303 and r.headers["location"] == "/login?restored=1"
    assert onboarding._TOKEN[0] is None and client().get("/setup").headers["location"] == "/login"
    assert "Backup restored" in client().get("/login?restored=1").text
    l = client(); assert l.post("/login", data={"username": "olda", "password": PW, "next": "/"}).status_code == 303
    assert l.get("/admin/heartbeat").status_code == 200
    p = db.conn().execute("SELECT * FROM people WHERE id='sam_test'").fetchone(); assert p["full_name"] == "Sam Testperson"
    assert (NEW / "data" / "shots" / "sam_test_1.webp").read_bytes() == b"shot" * 1000 and (NEW / "data" / "auth" / "sam_test.pdf").exists()
    assert not (NEW / "data" / "optout.db").exists()        # the database stays in DB_DIR

def test_restore_brings_every_db_setting():
    assert config.get("RESEND_API_KEY") == "re_dummy_resend_key_123456" and config.get("INBOUND_SECRET") == "dummy-inbound-secret-abcdef"
    assert config.get("CAPTCHA_API_KEY") == "CAP-dummy-solver-key-999" and config.get("APP_NAME") == "Test Fortress"
    assert os.environ.get("RESEND_API_KEY") == "re_dummy_resend_key_123456"   # applied to the running app, no restart

def test_env_dry_run_wins_over_restored_setting():
    assert config._cache.get("DRY_RUN") == "false"          # the old install was live...
    assert config.get("DRY_RUN") == "true" and remover.DRY is True and config.source("DRY_RUN") == "env"
    assert client().get("/healthz").json()["dry_run"] is True
    l = client(); l.post("/login", data={"username": "olda", "password": PW, "next": "/"})
    tok = re.search(r'name="csrf" value="([^"]+)"', l.get("/admin/heartbeat?tab=settings").text).group(1)
    r = l.post("/admin/heartbeat/settings", data={"csrf": tok, "DRY_RUN": "false", "DRY_RUN__present": "1", "confirm_live": "LIVE"})
    assert "locked" in r.headers["location"] and remover.DRY is True

def test_force_dry_run_and_cautious_direction(monkeypatch):
    monkeypatch.setitem(config._ENV0, "DRY_RUN", "false"); monkeypatch.setitem(config._cache, "DRY_RUN", "false")
    assert config.get("DRY_RUN") == "false"
    monkeypatch.setenv("FORCE_DRY_RUN", "true"); assert config.get("DRY_RUN") == "true"
    monkeypatch.delenv("FORCE_DRY_RUN"); monkeypatch.setitem(config._cache, "DRY_RUN", "true")
    assert config.get("DRY_RUN") == "true"                  # the app can still switch dry run on when env says false

def test_heartbeat_export_import_round_trip():
    l = client(); l.post("/login", data={"username": "olda", "password": PW, "next": "/"})
    page = l.get("/admin/heartbeat").text; assert "Export full backup" in page and "Import backup" in page
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert l.post("/admin/backup/export", data={}).status_code == 403                     # CSRF
    assert client().post("/admin/backup/export", data={"csrf": tok}).status_code in (303, 401, 403)   # admin only
    r = l.post("/admin/backup/export", data={"csrf": tok}); assert r.status_code == 200 and r.content.startswith(backup.MAGIC)
    assert not [f for f in os.listdir(backup.tmp_dir()) if f.endswith(".ffbackup")]       # temp file removed after sending
    c = db.conn(); c.execute("DELETE FROM people WHERE id='sam_test'"); c.commit(); os.remove(NEW / "data" / "shots" / "sam_test_1.webp")
    r = l.post("/admin/backup/import", data={"csrf": tok, "confirm": "nope"}, files={"backup": ("b.ffb", r.content, "application/octet-stream")})
    assert "RESTORE" in r.headers["location"] and not db.conn().execute("SELECT 1 FROM people").fetchone()
    exp = l.post("/admin/backup/export", data={"csrf": tok}).content   # (the now-empty state, to prove import replaces it)
    r2 = l.post("/admin/backup/import", data={"csrf": tok, "confirm": "RESTORE"}, files={"backup": ("b.ffb", GOOD.read_bytes(), "application/octet-stream")})
    assert r2.status_code == 303 and r2.headers["location"] == "/login?restored=1"
    assert db.conn().execute("SELECT 1 FROM people WHERE id='sam_test'").fetchone() and (NEW / "data" / "shots" / "sam_test_1.webp").exists()
    assert [f for f in os.listdir(NEW / "state" / "backups") if f.startswith("pre-import-")]   # safety copy kept
    assert exp.startswith(backup.MAGIC)

def test_upload_of_60mb_backup_is_accepted(monkeypatch):
    big = NEW / "data" / "shots" / "big.bin"; big.write_bytes(os.urandom(60_000_000))
    path, s = backup.export(str(T / "big.ffb")); assert os.path.getsize(path) > 60_000_000
    big.unlink()
    l = client(); l.post("/login", data={"username": "olda", "password": PW, "next": "/"})
    tok = re.search(r'name="csrf" value="([^"]+)"', l.get("/admin/heartbeat").text).group(1)
    with open(path, "rb") as fh:
        r = l.post("/admin/backup/import", data={"csrf": tok, "confirm": "RESTORE"}, files={"backup": ("big.ffb", fh, "application/octet-stream")})
    assert r.status_code == 303 and r.headers["location"] == "/login?restored=1" and big.stat().st_size == 60_000_000
    big.unlink()

def test_cli_check_export_and_wrong_key():
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    p = subprocess.run([sys.executable, "-m", "optout.backup", "check", str(GOOD)], env=env, capture_output=True, text=True, cwd="/")
    assert p.returncode == 0 and "opens with this FF_DATA_KEY" in p.stdout, p.stderr
    p = subprocess.run([sys.executable, "-m", "optout.backup", "check", str(BAD)], env=env, capture_output=True, text=True, cwd="/")
    assert p.returncode == 1 and "different FF_DATA_KEY" in p.stderr
    out = T / "cli.ffb"; p = subprocess.run([sys.executable, "-m", "optout.backup", "export", str(out)], env=env, capture_output=True, text=True, cwd="/")
    assert p.returncode == 0 and out.read_bytes().startswith(backup.MAGIC), p.stderr
    p = subprocess.run([sys.executable, "-m", "optout.backup"], env=env, capture_output=True, text=True, cwd="/"); assert p.returncode == 2

def test_newer_backup_is_refused(monkeypatch):
    monkeypatch.setattr(db, "app_version", lambda: "1.0.0")
    with open(GOOD, "rb") as f, pytest.raises(ValueError, match="newer version"): backup.import_(f)

def _jwt(key, claims, kid="k1"):
    from cryptography.hazmat.primitives import hashes; from cryptography.hazmat.primitives.asymmetric import padding
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    h, p = b({"alg": "RS256", "kid": kid}), b(claims)
    sig = key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

def test_cloudflare_access_check(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048); n = k.public_key().public_numbers()
    i2b = lambda i: base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
    jwks = json.dumps({"keys": [{"kid": "k1", "kty": "RSA", "e": i2b(n.e), "n": i2b(n.n)}]}).encode()
    assert cfaccess.mode() == "off" and cfaccess.check("/", {}) == (True, None)        # off by default
    monkeypatch.setenv("PLATFORM_CF_TEAM_DOMAIN", "team.example.com"); monkeypatch.setenv("PLATFORM_CF_AUD", "aud123")
    monkeypatch.setattr(cfaccess, "_JWKS", {"at": time.time(), "keys": {}}); cfaccess._keys(force=True, fetch=lambda u: jwks)
    now = time.time(); good = {"iss": "https://team.example.com", "aud": ["aud123"], "exp": now + 600, "email": "pat@example.com"}
    assert cfaccess.identity(cfaccess.verify(_jwt(k, good))) == "pat@example.com"
    assert cfaccess.identity(cfaccess.verify(_jwt(k, {**good, "email": None, "common_name": "worker"}))) == "service:worker"
    for bad, why in (({**good, "aud": ["x"]}, "audience"), ({**good, "iss": "https://evil"}, "issuer"), ({**good, "exp": now - 600}, "expired")):
        with pytest.raises(ValueError, match=why): cfaccess.verify(_jwt(k, bad))
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ValueError, match="signature"): cfaccess.verify(_jwt(other, good))
    monkeypatch.setenv("CF_ACCESS_MODE", "require")
    assert client().get("/login").status_code == 403 and client().get("/healthz").status_code == 200
    r = client().get("/login", headers={"Cf-Access-Jwt-Assertion": _jwt(k, good)}); assert r.status_code in (200, 303)
    assert client().get("/admin/heartbeat", headers={"Cf-Access-Jwt-Assertion": _jwt(k, good)}).status_code in (303, 401)   # a valid token never logs anyone in
    monkeypatch.setenv("CF_ACCESS_MODE", "log"); assert client().get("/login").status_code in (200, 303)
