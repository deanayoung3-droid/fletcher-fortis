"""Inbox: one list of every email the app sent or received (broker requests, follow-ups, replies, verification mail,
weekly digests, invites). Built on the existing outbox + inbox tables; the only new state is 'handled' (mail_handled).
Status buckets: pending | progress | done | needs."""
import json, re
from db import now
import remover

STAGE = {0: "Request", 1: "Follow-up", 2: "Escalation"}
DIGEST = ("weekly", "weekly_test", "summary", "onboarding", "mail", "setup_test")
IN_PILL = {"removed": ("Says removed", "green", "done"), "verify": ("Verification", "blue", "progress"), "ack": ("Acknowledged", "blue", "progress"),
           "need_info": ("Needs info", "purple", "needs"), "denied": ("Denied", "red", "needs"), "other": ("Reply", "gray", "pending")}
BUCKETS = [("all", "All"), ("needs", "Needs you"), ("pending", "Pending"), ("progress", "In progress"), ("done", "Completed")]
LISTING_DONE = ("confirmed_removed", "checked_clear", "not_me")
LISTING_NEEDS = ("needs_human", "manual_check", "blocked_auth", "reappeared")
REG_DONE = ("completed", "covered_by_drop")
REG_NEEDS = ("needs_info", "denied")

def ensure(c):
    c.execute("CREATE TABLE IF NOT EXISTS mail_handled(k TEXT PRIMARY KEY, ts TEXT, by_user TEXT)"); c.commit()

_REG = {}
def _site_name(sid):
    if not sid: return ""
    if sid.startswith("reg:"):
        try:
            import registries; d = registries.load()
            if _REG.get("d") is not d: _REG.update(d=d, m={b["id"]: b.get("name") for b in d.get("brokers") or []})
            return _REG["m"].get(sid[4:]) or sid[4:]
        except Exception: return sid[4:]
    return remover.SITE.get(sid, {}).get("name", sid)

def _reg_by_outbox(c):
    try: return {r["outbox_id"]: r for r in c.execute("SELECT * FROM reg_requests WHERE outbox_id IS NOT NULL")}
    except Exception: return {}

def _broker_from_addr(addr):
    m = re.search(r"@([\w.-]+)", addr or ""); return m.group(1).removeprefix("mail.").removeprefix("privacy.") if m else ""

# One definition of "sent", shared by Inbox, Family & accounts and anything else that counts mail.
#   sent     : delivered to the provider for real (outbox.state == 'sent')
#   drafted  : written in dry run, never left the server ('dry_run', and 'dry_run_superseded' once going live re-queued it)
#   not_sent : failed or held (error / blocked / not sent)
#   queued   : waiting to go out
#   manual   : a web form recorded for a person to submit (not an email)
OUT_CLASSES = (("sent", "Sent"), ("drafted", "Drafted (dry run)"), ("not_sent", "Not sent"), ("queued", "Queued"), ("manual", "Web form for a person"))
def out_class(state):
    st = state or ""
    if st == "sent": return "sent"
    if st.startswith("dry_run"): return "drafted"
    if st == "queued": return "queued"
    if st.startswith(("blocked", "error", "not sent")): return "not_sent"
    if st in ("needs_human",): return "manual"
    return "not_sent"   # unknown states are never counted as delivered

def mail_counts(c, person_ids=None):
    """{"total": {...}, "people": {pid: {...}}} with keys sent/drafted/not_sent/queued/manual/received. Every outbox and (non-junk)
    inbox row is counted exactly once, under its person_id (None for mail not linked to a person), so per-person counts add up to the total."""
    keys = [k for k, _ in OUT_CLASSES] + ["received"]
    tot = dict.fromkeys(keys, 0); per = {}
    def add(pid, k):
        if person_ids is not None and pid not in person_ids: return
        tot[k] += 1; per.setdefault(pid, dict.fromkeys(keys, 0))[k] += 1
    for r in c.execute("SELECT person_id, state FROM outbox"): add(r["person_id"], out_class(r["state"]))
    for r in c.execute("SELECT person_id FROM inbox WHERE COALESCE(kind,'') NOT IN ('junk','phishing')"): add(r["person_id"], "received")
    return {"total": tot, "people": per}

def _out_status(o, lst, reg, handled):
    st = (o["state"] or ""); cls = out_class(st)
    if cls == "drafted": return ("Draft (dry run, not sent)", "gray", "done" if handled or st != "dry_run" else "pending")
    if cls == "queued": return ("Queued", "amber", "pending")
    if cls == "manual": return ("Web form for a person", "purple", "done" if handled else "needs")
    if cls == "not_sent": return ("Not sent", "red", "done" if handled else "needs")
    if handled: return ("Handled", "green", "done")
    if (o["channel"] or "") in DIGEST: return ("Sent", "green", "done")
    if lst:
        if lst in LISTING_DONE: return ("Removed" if lst == "confirmed_removed" else "Closed", "green", "done")
        if lst in LISTING_NEEDS: return ("Needs you", "purple", "needs")
    if reg:
        if reg["status"] in REG_DONE: return ("Completed", "green", "done")
        if reg["status"] in REG_NEEDS: return ("Needs you", "purple", "needs")
    return ("Sent · awaiting reply", "blue", "progress")

def messages(c, person_ids=None):
    """Newest first. Each item: dict(key, dir, id, ts, person_id, person, broker, site, subject, label, color, bucket, kind, listing_id, reg_id, handled)."""
    ensure(c); handled = {r["k"] for r in c.execute("SELECT k FROM mail_handled")}
    people = {r["id"]: r["full_name"] for r in c.execute("SELECT id, full_name FROM people")}
    listings = {r["id"]: r for r in c.execute("SELECT id, site, status, person_id FROM listings")}
    regs = _reg_by_outbox(c); out = []
    for o in c.execute("SELECT id, ts, person_id, to_addr, subject, state, listing_id, stage, channel FROM outbox"):
        l = listings.get(o["listing_id"]); reg = regs.get(o["id"]); k = f"out-{o['id']}"
        site = (l["site"] if l else ("reg:" + reg["broker"] if reg else ""))
        ch = o["channel"] or ""
        broker = _site_name(site) if site else ("Weekly digest" if ch.startswith(("weekly", "summary")) else ("Invite" if ch == "onboarding" else _broker_from_addr(o["to_addr"]) or "Email"))
        label, color, bucket = _out_status(o, l["status"] if l else None, reg, k in handled)
        kind = "digest" if ch in DIGEST else (STAGE.get(o["stage"] or 0, "Request").lower().replace("-", "_"))
        out.append(dict(key=k, dir="out", id=o["id"], ts=o["ts"] or "", person_id=o["person_id"], person=people.get(o["person_id"], ""), broker=broker, site=site,
                        subject=o["subject"] or "(no subject)", label=label, color=color, bucket=bucket, kind=kind, stage=STAGE.get(o["stage"] or 0, "Request") if kind != "digest" else ch.replace("_", " ").title(),
                        listing_id=o["listing_id"], reg_id=reg["id"] if reg else None, handled=k in handled, addr=o["to_addr"] or ""))
    for m in c.execute("SELECT id, ts, person_id, listing_id, outbox_id, from_addr, subject, kind FROM inbox WHERE COALESCE(kind,'') NOT IN ('junk','phishing')"):
        l = listings.get(m["listing_id"]); reg = regs.get(m["outbox_id"]); k = f"in-{m['id']}"
        site = (l["site"] if l else ("reg:" + reg["broker"] if reg else ""))
        label, color, bucket = IN_PILL.get(m["kind"] or "other", IN_PILL["other"])
        if k in handled: label, color, bucket = "Handled", "green", "done"
        out.append(dict(key=k, dir="in", id=m["id"], ts=m["ts"] or "", person_id=m["person_id"], person=people.get(m["person_id"], ""), broker=_site_name(site) if site else (_broker_from_addr(m["from_addr"]) or "Unknown sender"),
                        site=site, subject=m["subject"] or "(no subject)", label=label, color=color, bucket=bucket, kind=m["kind"] or "other", stage="Reply",
                        listing_id=m["listing_id"], reg_id=reg["id"] if reg else None, handled=k in handled, addr=m["from_addr"] or ""))
    if person_ids is not None: out = [x for x in out if x["person_id"] in person_ids]
    out.sort(key=lambda x: (x["ts"], x["dir"] == "in", x["id"]), reverse=True); return out

def filtered(items, tab="all", person="", broker="", direction="", q=""):
    q = (q or "").strip().lower()
    return [x for x in items if (tab in ("", "all") or x["bucket"] == tab) and (not person or x["person_id"] == person) and (not broker or x["site"] == broker or x["broker"] == broker)
            and (not direction or x["dir"] == direction) and (not q or q in " ".join((x["subject"], x["broker"], x["person"], x["addr"], x["label"])).lower())]

def counts(items):
    d = {k: 0 for k, _ in BUCKETS}; d["all"] = len(items)
    for x in items: d[x["bucket"]] += 1
    return d

def get(c, direction, mid):
    row = c.execute(f"SELECT * FROM {'outbox' if direction == 'out' else 'inbox'} WHERE id=?", (mid,)).fetchone()  # nosec B608: fixed table names
    if not row: return None, None
    item = next((x for x in messages(c) if x["key"] == f"{direction}-{mid}"), None); return row, item

def thread(c, item):
    """Everything on the same request: messages with the same listing or registry request (or person+site), plus listing events."""
    items = messages(c); same = []
    for x in items:
        if x["key"] == item["key"]: same.append(x); continue
        if item["listing_id"] and x["listing_id"] == item["listing_id"]: same.append(x)
        elif item["reg_id"] and x["reg_id"] == item["reg_id"]: same.append(x)
        elif item["site"] and x["site"] == item["site"] and x["person_id"] == item["person_id"] and not item["listing_id"] and not item["reg_id"]: same.append(x)
    ev = []
    if item["listing_id"]: ev = c.execute("SELECT ts, kind, detail FROM events WHERE listing_id=? ORDER BY ts", (item["listing_id"],)).fetchall()
    tl = [dict(ts=x["ts"], what=("Sent " if x["dir"] == "out" else "Received ") + x["stage"].lower(), sub=x["subject"], key=x["key"], dir=x["dir"], label=x["label"], color=x["color"]) for x in same]
    tl += [dict(ts=e["ts"], what=e["kind"].replace("_", " ").capitalize(), sub=(e["detail"] or "")[:140], key=None, dir="event", label="", color="gray") for e in ev if not e["kind"].startswith(("mail_", "broker_reply_"))]
    return sorted(tl, key=lambda t: t["ts"])

def mark(c, key, handled=True, user=""):
    ensure(c)
    if handled: c.execute("INSERT OR REPLACE INTO mail_handled(k, ts, by_user) VALUES(?,?,?)", (key, now(), user))
    else: c.execute("DELETE FROM mail_handled WHERE k=?", (key,))
    c.commit()

def links(row):
    try: return json.loads(row["links"] or "[]")
    except (ValueError, KeyError, TypeError): return []
