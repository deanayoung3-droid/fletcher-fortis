"""Request-based removal for registered data brokers that have no public listing to scan (marketing lists, location data,
risk/identity, financial, health ...). Same lifecycle as listings, minus the scan:
  queued -> sent (DRY_RUN: dry_run) -> acknowledged / needs_info / denied / completed (from broker replies, matched by the
  [FF-person-ref] subject tag) -> reminder on FOLLOWUP_DAYS -> escalation on ESCALATE_DAYS.
Gates: REGISTRY_REQUESTS (Heartbeat toggle), DRY_RUN, the person's own e-signed authorization (family members), can_send.
California residents: brokers registered only in California are reached by the state's DROP request. Once the person has
done DROP they are marked covered_by_drop instead of being emailed again."""
import datetime
import config, remover, registries
from db import conn, now, event

ACTIVE = ("sent", "dry_run", "acknowledged")
DONE = ("completed", "covered_by_drop", "people_search")
STATUS = {"queued": ("Queued", "gray"), "blocked_auth": ("Waiting for signature", "amber"), "sent": ("Request sent", "blue"), "dry_run": ("Written (dry run)", "gray"),
          "acknowledged": ("Acknowledged", "blue"), "needs_info": ("Broker asked for more", "amber"), "denied": ("Denied", "red"), "completed": ("Completed", "green"),
          "covered_by_drop": ("Covered by CA DROP", "green"), "people_search": ("Scanned as people-search", "green"), "no_channel": ("No contact listed", "gray"), "failed": ("Send failed", "red")}

MAX_TRIES = 5   # temporary send errors are retried on the next hourly runs, then left as failed

def enabled(): return config.get("REGISTRY_REQUESTS", "true") == "true"
def _ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS reg_requests(id INTEGER PRIMARY KEY, person_id TEXT, broker TEXT, category TEXT, status TEXT, channel TEXT, target TEXT,
                 outbox_id INT, stage INT DEFAULT 0, created TEXT, sent_at TEXT, updated TEXT, reply_kind TEXT, tries INT DEFAULT 0, UNIQUE(person_id, broker))""")
    try: c.execute("ALTER TABLE reg_requests ADD COLUMN tries INT DEFAULT 0")
    except Exception: pass

def _site(b):
    """A broker record shaped like a sites.yaml entry, so letters/remover can use it."""
    return {"id": "reg:" + b["id"], "name": b["name"], "domain": b.get("domain"), "request_category": b.get("category"), "registries": b.get("registries"),
            "optout": {"type": "email" if b.get("email") else "form", "to": b.get("email"), "url": b.get("optout_url")}}

def plan(c, p):
    """Make sure every registry broker has a row for this person; returns how many were added."""
    _ensure(c); have = {r["broker"] for r in c.execute("SELECT broker FROM reg_requests WHERE person_id=?", (p["id"],))}; n = 0
    ca = (p["state"] or "").upper() == "CA"
    for b in registries.brokers():
        if b["id"] in have: continue
        st = ("people_search" if b.get("people_search_site") else "covered_by_drop" if ca and p["drop_at"] and b.get("registries") == ["CA"]
              else "no_channel" if b.get("channel") == "none" else "blocked_auth" if p["needs_auth"] and not remover.has_auth(p) else "queued")
        c.execute("INSERT OR IGNORE INTO reg_requests(person_id,broker,category,status,channel,target,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                  (p["id"], b["id"], b.get("category"), st, b.get("channel"), b.get("email") or b.get("optout_url"), now(), now())); n += 1
    return n

def tick(limit=None):
    """Plan, send a paced batch, sync broker replies, follow up. Runs hourly from lifecycle.tick()."""
    out = {"planned": 0, "sent": 0, "followups": 0, "replies": 0}
    if not enabled(): return out
    c = conn(); _ensure(c); B = registries.by_id(); limit = limit or int(config.get("REGISTRY_PER_HOUR") or 25)
    people = c.execute("SELECT * FROM people WHERE enabled=1").fetchall()
    for p in people:
        out["planned"] += plan(c, p)
        if remover.has_auth(p) or not p["needs_auth"]: c.execute("UPDATE reg_requests SET status='queued', updated=? WHERE person_id=? AND status='blocked_auth'", (now(), p["id"]))
        if (p["state"] or "").upper() == "CA" and p["drop_at"]:
            for r in c.execute("SELECT id, broker FROM reg_requests WHERE person_id=? AND status='queued'", (p["id"],)).fetchall():
                if (B.get(r["broker"]) or {}).get("registries") == ["CA"]: c.execute("UPDATE reg_requests SET status='covered_by_drop', updated=? WHERE id=?", (now(), r["id"]))
    P = {p["id"]: p for p in people}
    # sends that failed for a temporary reason (Resend 429 rate limit, 5xx, network) go back in the queue, up to MAX_TRIES.
    # Resend's daily quota is not the request's fault: those go back without using a try.
    if not remover.DRY:
        for r in c.execute("SELECT q.id, q.tries, o.state FROM reg_requests q JOIN outbox o ON o.id=q.outbox_id WHERE q.status='failed'").fetchall():
            if remover.quota_hit(r["state"]):
                c.execute("UPDATE reg_requests SET status='queued', updated=? WHERE id=?", (now(), r["id"])); out["retried"] = out.get("retried", 0) + 1
            elif remover.retryable(r["state"]) and (r["tries"] or 0) < MAX_TRIES:
                c.execute("UPDATE reg_requests SET status='queued', tries=COALESCE(tries,0)+1, updated=? WHERE id=?", (now(), r["id"])); out["retried"] = out.get("retried", 0) + 1
        if out.get("retried"): event(c, "registry_retry", f"{out['retried']} requests re-queued after a temporary send error (rate limit, daily quota or outage)")
    for r in fair_batch(c, limit):
        b = B.get(r["broker"]); p = P.get(r["person_id"])
        if not b or not p: continue
        ok, why = remover.can_send(p)
        if not ok and not remover.DRY: c.execute("UPDATE reg_requests SET status=?, updated=? WHERE id=?", ("blocked_auth" if "e-signed" in why else "queued", now(), r["id"])); continue
        st, oid = send(c, p, b, 0)
        c.execute("UPDATE reg_requests SET status=?, outbox_id=?, sent_at=?, updated=? WHERE id=?", (st, oid, now(), now(), r["id"])); out["sent"] += 1
        if st == "failed" and oid and remover.quota_hit((c.execute("SELECT state FROM outbox WHERE id=?", (oid,)).fetchone() or {}).get("state")):
            event(c, "registry_paused", "Resend daily sending quota reached; the rest of the queue waits for the next hourly run"); break
    out["replies"] = sync_replies(c)
    fu, es = int(config.get("FOLLOWUP_DAYS") or 20), int(config.get("ESCALATE_DAYS") or 45)
    for r in c.execute("SELECT * FROM reg_requests WHERE status IN ('sent','dry_run','acknowledged') AND sent_at IS NOT NULL AND (status<>'dry_run' OR ?)", (1 if remover.DRY else 0,)).fetchall():
        d = (datetime.datetime.now() - datetime.datetime.fromisoformat(r["sent_at"][:19])).days
        stage = 2 if d >= es and r["stage"] < 2 else 1 if fu <= d < es and r["stage"] < 1 else 0
        if not stage or r["broker"] not in B or r["person_id"] not in P: continue
        send(c, P[r["person_id"]], B[r["broker"]], stage, r["sent_at"]); c.execute("UPDATE reg_requests SET stage=?, updated=? WHERE id=?", (stage, now(), r["id"])); out["followups"] += 1
    c.commit(); return out

def fair_batch(c, limit):
    """Next queued requests, taken in turn from each person (round-robin by id), so one person's long queue never holds up the others."""
    per = {}
    for r in c.execute("SELECT * FROM reg_requests WHERE status='queued' ORDER BY id").fetchall(): per.setdefault(r["person_id"], []).append(r)
    out, i = [], 0
    while len(out) < limit and any(i < len(v) for v in per.values()):
        for v in per.values():
            if i < len(v) and len(out) < limit: out.append(v[i])
        i += 1
    return out

# broker reply kind -> request status, and how far along each status is (a later, weaker reply never downgrades a request)
REPLY = {"removed": "completed", "not_found": "completed", "ack": "acknowledged", "verify": "acknowledged", "need_info": "needs_info", "denied": "denied"}
RANK = {"queued": 0, "failed": 0, "sent": 1, "acknowledged": 2, "needs_info": 3, "denied": 3, "completed": 4}
def sync_replies(c):
    """Apply broker replies (inbound.py links them to the outbox row they answer) to the registry requests, oldest first.
    Replies to a follow-up count too: any registry email to the request's address for that person maps to the request."""
    reqs = c.execute("SELECT id, person_id, target, outbox_id, status, reply_kind FROM reg_requests WHERE outbox_id IS NOT NULL").fetchall()
    by_oid = {r["outbox_id"]: r for r in reqs}; by_addr = {}
    for r in reqs: by_addr.setdefault((r["person_id"], (r["target"] or "").lower()), []).append(r)
    n = 0; state = {r["id"]: (r["status"], r["reply_kind"]) for r in reqs}
    for i in c.execute("SELECT i.id, i.kind, i.outbox_id, o.person_id, o.to_addr FROM inbox i JOIN outbox o ON o.id=i.outbox_id WHERE i.kind IN (%s) ORDER BY i.id" % ",".join("?" * len(REPLY)), tuple(REPLY)).fetchall():
        q = by_oid.get(i["outbox_id"])
        if not q:
            cand = by_addr.get((i["person_id"], (i["to_addr"] or "").lower())) or []
            q = cand[0] if len(cand) == 1 else None
        if not q: continue
        st, rk = state[q["id"]]; new = REPLY[i["kind"]]
        if RANK.get(new, 0) < RANK.get(st, 0) or (new, i["kind"]) == (st, rk): continue
        state[q["id"]] = (new, i["kind"]); c.execute("UPDATE reg_requests SET status=?, reply_kind=?, updated=? WHERE id=?", (new, i["kind"], now(), q["id"])); n += 1
    c.commit(); return n

def send(c, p, b, stage, sent_at=None):
    """Email when the registry lists an address; otherwise record the web form for the form filler (DRY_RUN: recorded only)."""
    import letters, profiles
    site = _site(b)
    if b.get("email"):
        subj, text, html = letters.build(p, profiles.get(p), site, [], agent=bool(p["needs_auth"]), reply_to=remover.reply_to(p), stage=stage, sent_at=sent_at, category=b.get("category"))
        st = remover.send_email(c, p, b["email"], subj, text, remover.auth_file(p) if p["needs_auth"] else None, None, html=html, stage=stage, channel="registry_email")
        oid = c.execute("SELECT MAX(id) FROM outbox WHERE person_id=?", (p["id"],)).fetchone()[0]
        event(c, "registry_request" if not stage else ("registry_followup" if stage == 1 else "registry_escalation"), f"{b['name']} ({b.get('category')}) -> {b['email']}: {st}", p["id"])
        return ("dry_run" if st == "dry_run" else "sent" if st == "sent" else "queued" if st.startswith("blocked") else "failed"), oid
    remover._record(c, p, b.get("optout_url"), f"Registry broker web form: {b['name']}", f"{'DRY_RUN: would open' if remover.DRY else 'Open'} {b.get('optout_url')} and submit a deletion + opt-out request", "dry_run" if remover.DRY else "needs_human", None)
    return ("dry_run" if remover.DRY else "sent"), None

def coverage(person_ids):
    """{category: {"brokers": n, "by_status": {status: n}, per person: {pid: {status: n}}}} for the Coverage view."""
    c = conn(); _ensure(c); cats = {k: {"label": v, "brokers": 0, "status": {}, "people": {}} for k, v in registries.CATEGORIES}
    for b in registries.brokers(): cats.setdefault(b.get("category") or "other", {"label": "Other", "brokers": 0, "status": {}, "people": {}})["brokers"] += 1
    if person_ids:
        q = f"SELECT person_id, category, status, COUNT(*) n FROM reg_requests WHERE person_id IN ({','.join('?' * len(person_ids))}) GROUP BY 1,2,3"  # nosec B608
        for r in c.execute(q, list(person_ids)):
            k = cats.setdefault(r["category"] or "other", {"label": "Other", "brokers": 0, "status": {}, "people": {}})
            k["status"][r["status"]] = k["status"].get(r["status"], 0) + r["n"]
            k["people"].setdefault(r["person_id"], {})[r["status"]] = r["n"]
    return cats
