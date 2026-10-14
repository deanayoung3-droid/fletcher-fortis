"""Fictional mail history for Inbox screenshots (Rivera demo family). Never sends anything.
Usage: DB_PATH=/tmp/x/optout.db FF_DATA_KEY=... PROFILES_DIR=test/profiles python3 test/demo/seed_inbox.py"""
import sys, json, datetime, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "optout"))
import db, profiles
from crypto import enc
c = db.init(); profiles.seed(c); T = datetime.datetime.now().replace(second=0, microsecond=0)
P = {r["id"]: r["full_name"] for r in c.execute("SELECT id, full_name FROM people")}
def ts(d, h=9, m=14): return (T - datetime.timedelta(days=d)).replace(hour=h, minute=m).isoformat()
def listing(pid, site, status, d):
    cur = c.execute("INSERT INTO listings(person_id,site,url,fields,status,score,first_seen,last_seen,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (pid, site, enc(f"https://www.{site}.com/find/{pid}"), enc(json.dumps({"name": P[pid], "city": "Springfield, CA"})), status, 90, ts(d + 2), ts(0), ts(d)))
    lid = cur.lastrowid; c.execute("INSERT INTO events(ts,person_id,listing_id,kind,detail) VALUES(?,?,?,?,?)", (ts(d + 2, 3, 12), pid, lid, "new_candidate", f"found on {site}"))
    c.execute("INSERT INTO events(ts,person_id,listing_id,kind,detail) VALUES(?,?,?,?,?)", (ts(d + 1, 20, 2), pid, lid, "approved", "approved for removal")); return lid
def out(pid, lid, site, d, stage=0, state="dry_run", h=9, channel="email", to=None, subj=None):
    name = P[pid]; subj = subj or (["CCPA deletion request", "Follow-up: CCPA deletion request", "Escalation: overdue CCPA deletion request"][stage] + f" for {name} [FF-{lid or 0}]")
    body = (f"Hello,\n\nUnder the California Consumer Privacy Act I request that you delete all personal information you hold about {name}, "
            f"including the listing at https://www.{site}.com/find/{pid}, and confirm in writing.\n\n"
            + ("This is a follow-up: 30 days have passed without a response.\n\n" if stage else "") + f"Thank you,\n{name}")
    cur = c.execute("INSERT INTO outbox(ts,person_id,to_addr,subject,body,state,listing_id,stage,channel) VALUES(?,?,?,?,?,?,?,?,?)",
                    (ts(d, h), pid, to or f"privacy@{site}.com", enc(subj), enc(body), state, lid, stage, channel)); return cur.lastrowid
def inn(pid, lid, oid, site, d, kind, subj, body, h=14):
    c.execute("INSERT INTO inbox(ts,person_id,listing_id,outbox_id,from_addr,to_addr,subject,kind,links,clicked,body) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (ts(d, h, 41), pid, lid, oid, enc(f"support@{site}.com"), enc(f"privacy+{pid}@example.invalid"), enc(subj), kind,
               enc(json.dumps([f"https://www.{site}.com/optout/verify?t=8f2c1"] if kind == "verify" else [])), enc("[]"), enc(body)))
    c.execute("INSERT INTO events(ts,person_id,listing_id,kind,detail) VALUES(?,?,?,?,?)", (ts(d, h, 41), pid, lid, "broker_reply_" + kind, subj))
L = [("alex", "spokeo", "confirmed_removed", 40, [("removed", 30, "Your opt-out request is complete", "We have removed the listing and suppressed future records.")]),
     ("alex", "radaris", "pending", 12, [("verify", 11, "Please verify your removal request", "Click the link below within 72 hours to confirm your request.")]),
     ("alex", "truepeoplesearch", "submitted", 3, []),
     ("sam", "nuwber", "needs_human", 9, [("need_info", 7, "Additional information required", "To process this request we need the last four digits of your phone number.")]),
     ("sam", "fastpeoplesearch", "confirmed_removed", 33, [("ack", 32, "We received your request", "Your request has been received and will be processed within 45 days."),
                                                           ("removed", 20, "Removal confirmation", "The record has been removed from our public site.")]),
     ("jordan", "peoplefinders", "submitted", 38, []),
     ("jordan", "thatsthem", "pending", 6, [("ack", 5, "Request received #48213", "Thanks, a privacy specialist will review your request.")]),
     ("riley", "usphonebook", "needs_human", 15, [("denied", 13, "Unable to process request", "We could not verify your identity. Please reply with an authorization letter.")])]
for pid, site, st, d, replies in L:
    lid = listing(pid, site, st, d); oid = out(pid, lid, site, d, state="sent" if d > 4 else "dry_run")
    if pid == "jordan" and site == "peoplefinders": out(pid, lid, site, 7, stage=1, state="dry_run", h=8)
    for kind, rd, subj, body in replies: inn(pid, lid, oid, site, rd, kind, subj, body)
out("alex", None, "intelius", 2, state="blocked: no privacy address on file", subj=f"CCPA deletion request for {P['alex']}", to="privacy@intelius.com")
for wk in (0, 7, 14):
    out("alex", None, "", wk, state="dry_run" if wk == 0 else "sent", h=7, channel="weekly", to="alex@example.invalid", subj="Your weekly privacy digest: 3 removed, 2 in progress")
c.commit(); print("seeded", c.execute("SELECT COUNT(*) n FROM outbox").fetchone()["n"], "sent,", c.execute("SELECT COUNT(*) n FROM inbox").fetchone()["n"], "received")
