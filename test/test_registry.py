"""Registry import (parse/classify/dedup), request-based removal in DRY_RUN, version compare + pre-migration backup. No network."""
import os, sys, tempfile, pathlib, csv, sqlite3
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/a.db", DATA_DIR=T, REGISTRY_DIR=f"{T}/reg", FF_DATA_KEY="registry-test-key-0123456789-abcdef", SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  PROFILES_DIR=str(ROOT / "test/profiles"), SCHEDULER="false", DRY_RUN="true", UPDATE_CHECK="false")
sys.path.insert(0, str(ROOT / "optout"))
import db, config, registries, regreq, updates
c = db.init(); config.load()

def _ca(path):
    H = ["Data broker name", "Doing Business As", "Data broker primary website", "Data broker primary contact email", "Data broker's primary website that contains opt-out", "Data broker collects consumers' precise geolocation", "Additional Context"]
    rows = [["Acme Location Signals Inc", "", "https://www.acmegeo.example", "privacy@acmegeo.example", "", "Yes", "mobile location data from SDKs, foot traffic"],
            ["Bright Lists LLC", "", "brightlists.example;", "optout@brightlists.example", "", "No", "consumer marketing lists, direct mail, audience segments"],
            ["Careful Health Data Co", "", "https://carefulhealth.example", "", "https://carefulhealth.example/optout", "No", "health and wellness, pharmacy, patient insights"]]
    w = csv.writer(open(path, "w", newline="")); w.writerow(H); w.writerows(rows)

def _or(path):
    H = ["LICENSE NO.", "FACILITY NAME", "DOING BUSINESS AS", "OPT OUT WEBSITE", "OPT OUT EMAIL", "OPT OUT NARRATIVE", "OPT OUT PHONE", "OPT OUT OTHER METHODS", "ADDITIONAL WEBSITE"]
    w = csv.writer(open(path, "w", newline="")); w.writerow(H)
    w.writerow(["DB-1", "Bright Lists, L.L.C.", "", "https://brightlists.example", "", "email optout@brightlists.example", "", "", ""])     # duplicate of a CA broker
    w.writerow(["DB-2", "Ledger Credit Insights", "", "https://ledgercredit.example", "privacy@ledgercredit.example", "credit and lending, consumer reports", "", "", ""])

def test_parse_classify_dedup():
    _ca(f"{T}/ca.csv"); _or(f"{T}/or.csv")
    recs = list(registries.parse_ca(f"{T}/ca.csv")) + list(registries.parse_or(f"{T}/or.csv"))
    assert len(recs) == 5
    d = registries.merge(recs)
    assert d["total"] == 4 and d["registry_counts"] == {"CA": 3, "OR": 2}
    B = {b["name"]: b for b in d["brokers"]}
    assert B["Bright Lists LLC"]["registries"] == ["CA", "OR"] and B["Bright Lists LLC"]["domain"] == "brightlists.example"
    assert B["Acme Location Signals Inc"]["category"] == "location"
    assert B["Bright Lists LLC"]["category"] == "marketing"
    assert B["Careful Health Data Co"]["category"] == "health" and B["Careful Health Data Co"]["channel"] == "form"
    assert B["Ledger Credit Insights"]["category"] == "financial"
    assert len({b["id"] for b in d["brokers"]}) == 4

def test_classify_other_and_people_search():
    assert registries.classify("we do things", name="Generic Holdings") == "other"
    assert registries.classify("anything", people_domains={"findpeople.example"}, domain="findpeople.example") == "people-search"

def test_requests_dry_run_and_gates(monkeypatch):
    d = registries.merge(list(registries.parse_ca(f"{T}/ca.csv")) + list(registries.parse_or(f"{T}/or.csv")))
    monkeypatch.setattr(registries, "brokers", lambda: d["brokers"]); monkeypatch.setattr(registries, "by_id", lambda: {b["id"]: b for b in d["brokers"]})
    c.execute("INSERT INTO people(id, full_name, first, last, city, state, enabled, email) VALUES('p1', ?, 'Test', 'Person', ?, 'IL', 1, ?)", (db.enc("Test Person"), db.enc("Springfield"), db.enc("test@example.org"))); c.commit()
    out = regreq.tick(limit=10)
    assert out["planned"] == 4 and out["sent"] == 4
    st = {r[0]: r[1] for r in c.execute("SELECT broker, status FROM reg_requests WHERE person_id='p1'")}
    assert set(st.values()) == {"dry_run"}
    assert c.execute("SELECT COUNT(*) FROM outbox WHERE person_id='p1' AND channel='registry_email'").fetchone()[0] == 3   # 3 email + 1 form
    cov = regreq.coverage(["p1"]); assert cov["marketing"]["people"]["p1"] == {"dry_run": 1}
    config.save({"REGISTRY_REQUESTS": "false"}); assert regreq.tick() == {"planned": 0, "sent": 0, "followups": 0, "replies": 0}; config.save({"REGISTRY_REQUESTS": "true"})

def test_semver():
    assert updates.newer("1.0.1", "1.0.0") and updates.newer("1.10.0", "1.9.9") and updates.newer("v2.0.0", "1.99.99")
    assert updates.newer("1.0.0", "1.0.0-rc1") and not updates.newer("1.0.0", "1.0.0") and not updates.newer("garbage", "1.0.0")
    assert updates.current() == open(ROOT / "VERSION").read().strip()

def test_watchtower_not_configured_and_auto_off():
    assert updates.trigger() == "Watchtower sidecar not configured"
    assert config.get("AUTO_UPDATE") == "false" and updates.auto_tick() is None

def test_backup_before_migration_on_version_change():
    c.execute("UPDATE app_meta SET v='0.9.0' WHERE k='version'"); c.commit()
    db.init(); bk = os.listdir(f"{T}/backups")
    assert len(bk) == 1 and "0.9.0-to-" in bk[0] and oct(os.stat(f"{T}/backups/{bk[0]}").st_mode)[-3:] == "600"
    assert (b := sqlite3.connect(f"{T}/backups/{bk[0]}")).execute("SELECT COUNT(*) FROM people").fetchone()[0] == c.execute("SELECT COUNT(*) FROM people").fetchone()[0] > 0; b.close()
    db.init(); assert len([f for f in os.listdir(f"{T}/backups") if f.endswith(".db")]) == 1                       # same version: no new backup

def test_temporary_send_errors_are_requeued(monkeypatch):
    """v1.3.5: a registry request that failed with Resend 429/5xx goes back in the queue (up to MAX_TRIES); 403 stays failed."""
    import remover
    d = registries.merge(list(registries.parse_ca(f"{T}/ca.csv")) + list(registries.parse_or(f"{T}/or.csv")))
    monkeypatch.setattr(registries, "brokers", lambda: d["brokers"]); monkeypatch.setattr(registries, "by_id", lambda: {b["id"]: b for b in d["brokers"]})
    c.execute("INSERT OR IGNORE INTO people(id, full_name, first, last, city, state, enabled, email, send_enabled) VALUES('p2', ?, 'Test', 'Two', ?, 'IL', 1, ?, 1)", (db.enc("Test Two"), db.enc("Springfield"), db.enc("two@example.org"))); c.commit()
    regreq.plan(c, c.execute("SELECT * FROM people WHERE id='p2'").fetchone()); c.commit()
    rows = [r["id"] for r in c.execute("SELECT id FROM reg_requests WHERE person_id='p2' AND channel='email' ORDER BY id")][:2]
    for rid, st in zip(rows, ("error: Resend 429: Too many requests", "error: Resend 403: domain is not verified")):
        oid = c.execute("INSERT INTO outbox(ts, person_id, to_addr, state, channel) VALUES (?,?,?,?,?)", (db.now(), "p2", "x@example.org", st, "registry_email")).lastrowid
        c.execute("UPDATE reg_requests SET status='failed', outbox_id=? WHERE id=?", (oid, rid))
    c.execute("UPDATE reg_requests SET status='done_for_test' WHERE person_id<>'p2' AND status='queued'"); c.commit()
    monkeypatch.setattr(remover, "DRY", False); monkeypatch.setattr(remover, "can_send", lambda p: (True, ""))
    monkeypatch.setattr(regreq, "send", lambda c, p, b, stage, sent_at=None: ("sent", None))
    out = regreq.tick(limit=50)
    assert out.get("retried") == 1
    st = {r["id"]: (r["status"], r["tries"]) for r in c.execute("SELECT id, status, tries FROM reg_requests WHERE id IN (?,?)", rows)}
    assert st[rows[0]] == ("sent", 1) and st[rows[1]] == ("failed", 0)
