"""Per-listing lifecycle, fully automatic except true human-only steps:
scan -> match (score) -> approve (person/admin, or auto when score >= AUTO_APPROVE_MIN) -> best channel (parent > form > email, learned from outcomes)
-> submit (DRY_RUN-gated) -> verification emails auto-handled by inbound.py -> reminder (FOLLOWUP_DAYS) -> escalation (ESCALATE_DAYS)
-> weekly re-scan confirms -> Removed -> weekly re-check -> Reappeared -> auto re-file.
Blocked sites can get a blind email request when the broker has a privacy address (BLIND_REQUESTS, off by default). CA residents get one DROP item (one request reaches 500+ registered brokers).
tick() runs hourly from the scheduler (and on demand from Heartbeat)."""
import datetime, os
import config, remover
from db import conn, now, event

def _days(ts):
    try: return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts[:19])).days
    except Exception: return 0

def tick():
    c = conn(); out = {"auto_approved": 0, "refiled": 0, "blind": 0, "reminders": 0, "escalations": 0}
    auto = config.get("AUTO_APPROVE", "false") == "true"; amin = int(config.get("AUTO_APPROVE_MIN", "85") or 85)
    if auto:
        for l in c.execute("SELECT id FROM listings WHERE status='candidate' AND COALESCE(score,0)>=?", (amin,)).fetchall():
            remover.approve(l["id"]); event(c, "auto_approved", f"match score >= {amin}", None, l["id"]); out["auto_approved"] += 1
    if config.get("AUTO_REFILE", "true") == "true":
        for l in c.execute("SELECT id, person_id FROM listings WHERE status='reappeared'").fetchall():
            remover.approve(l["id"]); event(c, "auto_refiled", "listing came back after removal: new request filed", l["person_id"], l["id"]); out["refiled"] += 1
    # blocked sites: if the broker has a privacy email, send a blind request instead of asking a human to check
    # off by default: no blind opt-outs unless the admin turns BLIND_REQUESTS on in Heartbeat > Settings
    for l in (c.execute("SELECT * FROM listings WHERE status='manual_check'").fetchall() if config.get("BLIND_REQUESTS", "false") == "true" else []):
        site = remover.SITE.get(l["site"], {}); ch = dict(remover.channels(site))
        p = c.execute("SELECT * FROM people WHERE id=?", (l["person_id"],)).fetchone()
        if "email" in ch and p and remover.can_send(p)[0]:
            st = remover.send_request(c, p, site, ch["email"], l["id"])
            c.execute("UPDATE listings SET status='submitted', submitted_at=? WHERE id=?", (now(), l["id"])); event(c, "blind_request", f"site blocks scans; emailed {ch['email']} ({st})", p["id"], l["id"]); out["blind"] += 1
    import automation
    if automation.solver_on() and not remover.DRY:   # optional CAPTCHA solver: re-try forms that were waiting on a person
        for l in c.execute("SELECT id FROM listings WHERE status='needs_human'").fetchall():
            if remover.method(remover.SITE.get(c.execute("SELECT site FROM listings WHERE id=?", (l["id"],)).fetchone()["site"], {}))[0] == "captcha_form":
                remover.approve(l["id"]); out["captcha_solver"] = out.get("captcha_solver", 0) + 1
    fu, es = int(config.get("FOLLOWUP_DAYS") or 20), int(config.get("ESCALATE_DAYS") or 45)
    for o in c.execute("""SELECT o.*, l.status lst, l.site FROM outbox o JOIN listings l ON l.id=o.listing_id WHERE o.stage=0 AND o.channel='email'
                          AND o.state IN ('sent','dry_run') AND l.status IN ('submitted','pending')""").fetchall():
        d = _days(o["ts"]); have = {r["stage"] for r in c.execute("SELECT stage FROM outbox WHERE listing_id=? AND stage>0", (o["listing_id"],))}
        stage = 2 if d >= es and 2 not in have else 1 if fu <= d < es and 1 not in have else 0
        if not stage: continue
        p = c.execute("SELECT * FROM people WHERE id=?", (o["person_id"],)).fetchone(); site = remover.SITE.get(o["site"], {"id": o["site"], "name": o["site"]})
        remover.send_request(c, p, site, o["to_addr"], o["listing_id"], stage=stage, sent_at=o["ts"])
        event(c, "followup_sent" if stage == 1 else "escalation_sent", f"day {d}: {o['to_addr']}", p["id"], o["listing_id"]); out["reminders" if stage == 1 else "escalations"] += 1
    c.commit(); return out

def drop_needed(p):
    """California residents: one DROP request (residency must be verified by the person on the state site, so it stays a human step)."""
    return (p["state"] or "").upper() == "CA" and not p["drop_at"]

LIFECYCLE = [("Scan", "Weekly, plus after every profile edit"), ("Match", "Name, age range, city and relatives scored 0-100"),
             ("Approve", "You approve, or auto-approve high-confidence matches (Heartbeat > Settings)"), ("Channel", "Parent suppression, web form, then privacy email; learns which works per broker"),
             ("Submit", "Sent automatically (dry run until live)"), ("Verify", "Broker confirmation emails and links handled automatically"),
             ("Follow up", "Reminder on day 20, escalation citing the regulator on day 45"), ("Confirm", "Re-scan shows the page is gone: Removed"),
             ("Watch", "Weekly re-check; if it reappears a new request is filed automatically")]
