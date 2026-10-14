"""California DROP (Delete Request and Opt-out Platform, privacy.ca.gov/drop): automate everything around the one step the
law reserves for the person.

What the law and the platform allow (CCPA regs §§ 7620-7621, DROP FAQ, Oct 2026):
- The consumer must verify California residency through the California Identity Gateway. Only they can do it; we never
  touch, fake or bypass it. DROP has no public consumer API, so there is no machine submission path.
- An authorized agent may help only AFTER residency is verified, and the agent's full name, email and trade name must be
  disclosed in the consumer's DROP account before the request is submitted. We prefill that disclosure from the signed
  authorization so the person pastes it in one step.
- DROP returns a DROP ID (8-10 characters) and per-broker statuses: deleted, opted-out, exempt, record not found, pending
  (up to 90 days). We ingest the confirmation and status emails (forwarded to the inbound address), store the DROP ID
  encrypted and masked, import the status list, and mark matching registry brokers as covered. A re-scan still decides
  whether a people-search listing is actually gone.
What stays human: the residency check (about 2 minutes) and pressing Submit in the person's own DROP account."""
import json, re
from db import conn, now, event
from crypto import enc, dec

URL = "https://privacy.ca.gov/drop/"
SENDERS = ("privacy.ca.gov", "cppa.ca.gov", "drop.privacy.ca.gov", "calprivacy.ca.gov")
DROP_ID = re.compile(r"DROP\s*ID\b[^A-Za-z0-9]{0,20}([A-Z0-9]{8,10})\b")
STATUSES = {"deleted": "deleted", "opted-out": "opted_out", "opted out": "opted_out", "exempt": "exempt", "record not found": "not_found", "not found": "not_found", "pending": "pending"}
STATE = {"not_started": ("Not started", "gray"), "verifying": ("Waiting for residency check", "amber"), "submitted": ("Submitted to DROP", "blue"),
         "processing": ("Brokers processing", "blue"), "results": ("Results in", "green")}

def ensure(c=None):
    c = c or conn()
    c.execute("CREATE TABLE IF NOT EXISTS drop_status(person_id TEXT PRIMARY KEY, state TEXT, drop_id TEXT, submitted_at TEXT, last_email TEXT, results TEXT, updated TEXT)")
    return c

def get(pid, c=None):
    c = ensure(c); r = c.execute("SELECT * FROM drop_status WHERE person_id=?", (pid,)).fetchone()
    p = c.execute("SELECT drop_at FROM people WHERE id=?", (pid,)).fetchone()
    d = dict(r) if r else {"person_id": pid, "state": "submitted" if p and p["drop_at"] else "not_started", "drop_id": None, "submitted_at": p["drop_at"] if p else None, "last_email": None, "results": None}
    did = dec(d["drop_id"]) if d.get("drop_id") else ""
    d["drop_id_masked"] = ("•" * (len(did) - 3) + did[-3:]) if did else ""
    try: d["results"] = json.loads(dec(d["results"])) if d.get("results") else {}
    except Exception: d["results"] = {}
    d["counts"] = {}
    for v in d["results"].values(): d["counts"][v] = d["counts"].get(v, 0) + 1
    d["label"], d["color"] = STATE.get(d["state"], STATE["not_started"]); return d

def _save(c, pid, **kw):
    ensure(c); cur = c.execute("SELECT * FROM drop_status WHERE person_id=?", (pid,)).fetchone()
    if not cur: c.execute("INSERT INTO drop_status(person_id,state,updated) VALUES(?,?,?)", (pid, "not_started", now()))
    for k, v in kw.items(): c.execute(f"UPDATE drop_status SET {k}=?, updated=? WHERE person_id=?", (v, now(), pid))   # nosec B608 (fixed keys)
    c.commit()

def needed(p): return (p["state"] or "").upper() == "CA" and get(p["id"])["state"] in ("not_started", "verifying")

def prefill(p, agent=None):
    """Exactly the fields DROP asks for, from the profile. No SSN, full birth date or ID numbers are stored, so none are offered."""
    import profiles
    prof = profiles.get(p)
    legal = next((n for n in prof["names"] if n.get("kind") == "legal"), (prof["names"] or [{}])[0])
    cur = next((a for a in prof["addresses"] if a.get("current")), (prof["addresses"] or [{}])[0])
    rows = [("First name", legal.get("first", "")), ("Middle name", legal.get("middle", "")), ("Last name", legal.get("last", ""))]
    rows += [(f"Other name {i + 1}", profiles.full(n)) for i, n in enumerate(n for n in prof["names"] if n is not legal)][:4]
    rows += [(f"Email {i + 1}", e) for i, e in enumerate(prof["emails"][:3])]
    rows += [(f"Phone {i + 1}", profiles.fmt_phone(x)) for i, x in enumerate(prof["phones"][:3])]
    rows += [("Street address", cur.get("street", "")), ("City", cur.get("city", "")), ("State", cur.get("state", "")), ("ZIP code", cur.get("zip", ""))]
    rows += [(f"Previous address {i + 1}", ", ".join(x for x in (a.get("street"), a.get("city"), a.get("state")) if x)) for i, a in enumerate(a for a in prof["addresses"] if a is not cur)][:3]
    rows = [(k, v) for k, v in rows if v]
    return {"fields": rows, "agent": agent}

def agent_disclosure(p):
    """Authorized-agent disclosure for the person's DROP account (§ 7621(b)). Only when they signed the authorization."""
    import remover, config
    if not remover.has_auth(p): return None
    c = conn(); a = c.execute("SELECT p.full_name FROM users u JOIN people p ON p.id=u.person_id WHERE u.role='admin' ORDER BY u.id LIMIT 1").fetchone()
    return {"Agent full name": (a or {}).get("full_name") or config.get("APP_NAME"), "Agent email": remover.FROM_ADDRESS, "Agent trade name": config.get("APP_NAME") or "Fletcher Fortis"}

def mark(c, pid, state, why):
    _save(c, pid, state=state); event(c, "drop_" + state, why, pid)
    if state == "submitted": c.execute("UPDATE people SET drop_at=COALESCE(drop_at, ?) WHERE id=?", (now(), pid)); c.commit()

def is_drop(sender_dom, subj, body):
    s = (sender_dom or "").lower()
    if any(s == d or s.endswith("." + d) for d in SENDERS): return True
    return bool(re.search(r"\bDROP\b", subj or "") and re.search(r"privacy\.ca\.gov|Delete Request and Opt-out Platform|CalPrivacy", body or "", re.I))   # forwarded by the person

def ingest(c, pid, subj, body):
    """A DROP email (direct or forwarded). Returns the kind: drop_id | status_update | verified | drop_other."""
    if not pid: return "drop_other"
    text = f"{subj}\n{body}"
    m = DROP_ID.search(text)
    if m:
        _save(c, pid, drop_id=enc(m.group(1)), last_email=now()); mark(c, pid, "submitted", "DROP confirmation email with a DROP ID"); return "drop_id"
    if re.search(r"status (update|has (been )?updated|changed)|data brokers? (have|has) (processed|updated)", text, re.I):
        _save(c, pid, last_email=now()); mark(c, pid, "processing", "DROP status update email"); return "status_update"
    if re.search(r"residen\w+ (has been |was )?verified|verification (is )?complete", text, re.I):
        _save(c, pid, last_email=now()); mark(c, pid, "verifying", "residency verified by California; submit the request in DROP"); return "verified"
    _save(c, pid, last_email=now()); return "drop_other"

def parse_results(text):
    """The per-broker status list copied from the DROP status page: one broker per line with its status."""
    out = {}
    for line in (text or "").splitlines()[:2000]:
        low = line.lower()
        st = next((v for k, v in sorted(STATUSES.items(), key=lambda kv: -len(kv[0])) if re.search(rf"\b{re.escape(k)}\b", low)), None)
        if not st: continue
        name = re.sub(rf"\b({'|'.join(re.escape(k) for k in STATUSES)})\b", "", line, flags=re.I).strip(" \t-–:|,")
        if 2 <= len(name) <= 120: out[name] = st
    return out

def _norm(s): return re.sub(r"\b(inc|llc|corp|corporation|co|ltd|lp|the|company)\b|[^a-z0-9]", "", (s or "").lower())

def apply_results(c, pid, results):
    """Mark registry brokers by DROP status; people-search sites get a chain attempt. Removal itself is only confirmed by a re-scan."""
    import registries, chain, scanner
    _save(c, pid, results=enc(json.dumps(results)), state="results"); n = 0
    by = {_norm(b["name"]): b for b in registries.brokers()}
    dom_site = {(s.get("domain") or "").lower(): s["id"] for s in scanner.SITES}
    for name, st in results.items():
        b = by.get(_norm(name))
        if not b: continue
        n += 1
        c.execute("UPDATE reg_requests SET status=?, reply_kind=?, updated=? WHERE person_id=? AND broker=?",
                  ("completed" if st in ("deleted", "opted_out", "not_found") else "covered_by_drop", "drop_" + st, now(), pid, b["id"]))
        sid = dom_site.get((b.get("domain") or "").lower())
        if sid: chain.record(c, pid, sid, "drop", "sent" if st != "not_found" else "not_found", f"DROP: {st.replace('_', ' ')} (a re-scan confirms removal)")
    c.commit(); event(c, "drop_results", f"{len(results)} broker statuses imported, {n} matched to registry brokers", pid); return n
