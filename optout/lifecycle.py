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
    if not remover.DRY: out["requeued_drafts"] = requeue_dry(c)
    auto = config.get("AUTO_APPROVE", "false") == "true"; amin = int(config.get("AUTO_APPROVE_MIN", "85") or 85)
    if auto:
        for l in c.execute("SELECT id FROM listings WHERE status='candidate' AND COALESCE(score,0)>=?", (amin,)).fetchall():
            remover.approve(l["id"]); event(c, "auto_approved", f"match score >= {amin}", None, l["id"]); out["auto_approved"] += 1
    if config.get("AUTO_REFILE", "true") == "true":
        for l in c.execute("SELECT id, person_id FROM listings WHERE status='reappeared'").fetchall():
            remover.approve(l["id"]); event(c, "auto_refiled", "listing came back after removal: new request filed", l["person_id"], l["id"]); out["refiled"] += 1
    # blocked sites: if the broker has a privacy email, send a blind request instead of asking a human to check
    # off by default: no blind opt-outs unless the admin turns BLIND_REQUESTS on in Heartbeat > Settings
    import chain   # blind opt-outs are one step of the method chain; BLIND_REQUESTS still decides (off by default)
    for l in c.execute("SELECT * FROM listings WHERE status='manual_check'").fetchall():
        site = remover.SITE.get(l["site"], {}); p = c.execute("SELECT * FROM people WHERE id=?", (l["person_id"],)).fetchone()
        if site and p and remover.has_auth(p) and chain.blind(c, p, l, site): out["blind"] += 1
    if not remover.DRY:   # live: approved listings go through the method chain on the single worker, CHAIN_PER_HOUR per run
        out["chain_started"] = start_chains(c)
    import automation
    if automation.solver_on() and not remover.DRY:   # optional CAPTCHA solver: re-try forms that were waiting on a person
        for l in c.execute("SELECT id FROM listings WHERE status='needs_human'").fetchall():
            if remover.method(remover.SITE.get(c.execute("SELECT site FROM listings WHERE id=?", (l["id"],)).fetchone()["site"], {}))[0] == "captcha_form":
                remover.approve(l["id"]); out["captcha_solver"] = out.get("captcha_solver", 0) + 1
    fu, es = int(config.get("FOLLOWUP_DAYS") or 20), int(config.get("ESCALATE_DAYS") or 45)
    for o in c.execute("""SELECT o.*, l.status lst, l.site FROM outbox o JOIN listings l ON l.id=o.listing_id WHERE o.stage=0 AND o.channel='email'
                          AND o.state IN ('sent','dry_run') AND (o.state='sent' OR ?) AND l.status IN ('submitted','pending')""", (1 if remover.DRY else 0,)).fetchall():
        d = _days(o["ts"]); have = {r["stage"] for r in c.execute("SELECT stage FROM outbox WHERE listing_id=? AND stage>0", (o["listing_id"],))}
        stage = 2 if d >= es and 2 not in have else 1 if fu <= d < es and 1 not in have else 0
        if not stage: continue
        p = c.execute("SELECT * FROM people WHERE id=?", (o["person_id"],)).fetchone(); site = remover.SITE.get(o["site"], {"id": o["site"], "name": o["site"]})
        remover.send_request(c, p, site, o["to_addr"], o["listing_id"], stage=stage, sent_at=o["ts"])
        event(c, "followup_sent" if stage == 1 else "escalation_sent", f"day {d}: {o['to_addr']}", p["id"], o["listing_id"]); out["reminders" if stage == 1 else "escalations"] += 1
    try:
        import regreq; out.update({"registry_" + k: v for k, v in regreq.tick().items()})
    except Exception as e: event(c, "registry_error", f"{type(e).__name__}: {e}"[:200])
    c.commit()
    try:
        import invites; out["invites"] = invites.flush_pending()
    except Exception as e: event(c, "invite_flush_failed", type(e).__name__)
    return out

def drop_needed(p):
    """California residents: one DROP request (residency must be verified by the person on the state site, so it stays a human step)."""
    import drop; return drop.needed(p)

LIFECYCLE = [("Scan", "Weekly, plus after every profile edit"), ("Match", "Name, age range, city and relatives scored 0-100"),
             ("Approve", "You approve, or auto-approve high-confidence matches (Heartbeat > Settings)"), ("Channel", "Parent suppression, web form, then privacy email; learns which works per broker"),
             ("Submit", "Sent automatically (dry run until live)"), ("Verify", "Broker confirmation emails and links handled automatically"),
             ("Follow up", "Reminder on day 20, escalation citing the regulator on day 45"), ("Confirm", "Re-scan shows the page is gone: Removed"),
             ("Watch", "Weekly re-check; if it reappears a new request is filed automatically")]


def start_chains(c, limit=None):
    """Queue up to CHAIN_PER_HOUR approved listings whose chain has not run in the last day. Gated on chain_at (set when a chain
    actually starts on the worker), not submitted_at: approving or a lost in-memory job (restart) no longer hides a listing for a day.
    Chains still run one at a time on the scan worker under the browser lock (1.3.3 memory safety)."""
    import scanjobs
    limit = limit if limit is not None else int(config.get("CHAIN_PER_HOUR") or 10)
    busy = {k for k, j in list(scanjobs.JOBS.items()) if k.startswith("chain:") and (j or {}).get("state") in ("queued", "running")}
    room = max(0, limit - len(busy)); n = 0
    if not room: return 0
    for l in c.execute("SELECT id FROM listings WHERE status='approved' AND (chain_at IS NULL OR chain_at < ?) ORDER BY COALESCE(chain_at,''), id",
                       ((datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec="seconds"),)).fetchall():
        if f"chain:{l['id']}" in busy: continue
        if str(remover.approve(l["id"])).startswith("removal chain started"): n += 1   # consent/auth gates may keep it waiting
        if n >= room: break
    return n

def requeue_dry(c):
    """Going live: dry-run drafts were never sent. Put them back in the queue so the real request goes out first (rate limits
    and consent still apply), instead of following up on letters the broker never received. Idempotent."""
    n = 0
    for l in c.execute("""SELECT l.id FROM listings l WHERE l.status IN ('submitted','pending') AND EXISTS (SELECT 1 FROM outbox o WHERE o.listing_id=l.id AND o.state='dry_run')
                          AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.listing_id=l.id AND o.state IN ('sent','submitted'))""").fetchall():
        c.execute("UPDATE listings SET status='approved', submitted_at=NULL WHERE id=?", (l["id"],)); n += 1
    try:
        n += c.execute("UPDATE reg_requests SET status='queued', stage=0, sent_at=NULL, updated=? WHERE status='dry_run'", (now(),)).rowcount
    except Exception: pass
    c.execute("UPDATE outbox SET state='dry_run_superseded' WHERE state='dry_run'")
    if n: event(c, "golive_requeued", f"{n} dry-run drafts queued to be sent for real")
    c.commit(); return n
