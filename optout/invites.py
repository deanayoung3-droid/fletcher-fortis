"""Family invites. Every family member added by the admin gets one: set a password, review, consent (button + drawn signature).
Invites are real emails to family, never to brokers, so they are sent as soon as email is configured, even in dry run.
Without a working email setup they wait as 'Invite pending: email not set up' and the admin can copy the link.
Links are single use and expire after 7 days; resending makes a new link. No removal is filed until the person has signed
(remover.can_send checks the signature)."""
import re, datetime
import auth, config, mailer, remover, db
from db import conn, now, event
from crypto import enc

DAYS = 7
DRY_NOTE = "Invites go to your family, not to brokers, so they are sent as soon as email is set up, even while dry run is on."

def email_ready(): return remover.provider_ready() and not (config.get("FROM_ADDRESS") or remover.FROM_ADDRESS or "").endswith("@example.com")

def ensure_user(pid, email):
    c = conn(); u = c.execute("SELECT * FROM users WHERE person_id=?", (pid,)).fetchone()
    if not u:
        name = pid; i = 2
        while c.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone(): name = f"{pid}{i}"; i += 1
        c.execute("INSERT INTO users(username,role,person_id,must_change,created,email,onboarded) VALUES(?,?,?,1,?,?,0)", (name, "member", pid, now(), enc(email)))
    elif email: c.execute("UPDATE users SET email=? WHERE id=?", (enc(email), u["id"]))
    c.commit(); return c.execute("SELECT * FROM users WHERE person_id=?", (pid,)).fetchone()

def link_for(tok, base=""): return f"{(config.get('PUBLIC_URL') or base or '').rstrip('/')}/invite/{tok}"

def send(pid, email, first, admin, base=""):
    """Create a fresh link (old one stops working) and email it if email is set up. Returns (token, state) with state 'sent' or 'pending'."""
    u = ensure_user(pid, email); tok = auth.make_invite(u["id"], days=DAYS); link = link_for(tok, base)
    c = conn(); c.execute("UPDATE users SET invite_sent_at=NULL, invite_opened_at=NULL, invite_state='pending', invite_error=NULL WHERE id=?", (u["id"],)); c.commit()
    err = None
    if not email: state = "pending"
    elif email_ready():
        subj, text, h = mailer.onboarding(first, admin, link)
        st = mailer.send(email, subj, text, h, kind="onboarding", person_id=pid, family=True)
        state = "sent" if st == "sent" else "failed"; err = None if st == "sent" else st[:240]
    else: state = "pending"
    c.execute("UPDATE users SET invite_state=?, invite_sent_at=?, invite_error=? WHERE id=?", (state, now() if state == "sent" else None, err, u["id"])); c.commit()
    event(db.conn(), {"sent": "invite_sent", "failed": "invite_failed"}.get(state, "invite_pending"), (err or "")[:240], pid); return tok, state

def opened(u):
    if u and not u["invite_opened_at"]:
        c = conn(); c.execute("UPDATE users SET invite_opened_at=? WHERE id=?", (now(), u["id"])); c.commit()

def status(p, u):
    """(label, color, detail) for the person page."""
    if p and remover.has_auth(p) and u and u["pw_hash"]: return ("Accepted", "green", "Signed. Removals can be filed.")
    if not u or not u["invite_expires"] and not u["invite_state"]: return ("Not sent", "gray", "No invite yet.")
    expired = bool(u["invite_expires"]) and u["invite_expires"] <= now() and not u["pw_hash"]
    if u["pw_hash"] and not remover.has_auth(p): return ("Opened", "blue", "Account set up, waiting for their signature.")
    if expired: return ("Expired", "amber", "The link expired. Resend to make a new one.")
    if u["invite_opened_at"]: return ("Opened", "blue", f"Opened {u['invite_opened_at'][:16].replace('T', ' ')}. Waiting for them to finish.")
    if u["invite_state"] == "sent": return ("Sent", "blue", f"Emailed {(u['invite_sent_at'] or '')[:16].replace('T', ' ')}. Link expires {u['invite_expires'][:10]}.")
    if u["invite_state"] == "failed": return ("Not sent: " + _short(u["invite_error"]), "red", "Sending failed. Fix it in Heartbeat → Email, then Resend. Or copy the link and send it yourself.")
    return ("Invite pending: email not set up", "amber", "Copy the link and send it yourself, or set up email and resend.")

def _short(err):
    e = (err or "unknown error").removeprefix("error: ")
    return e if len(e) <= 90 else e[:87] + "…"

def flush_pending():
    """Once email works, send invites that were waiting (new link each, as the old one was only shown to the admin)."""
    if not email_ready(): return 0
    c = conn(); n = 0
    for u in c.execute("SELECT u.*, p.full_name, p.first FROM users u JOIN people p ON p.id=u.person_id WHERE u.invite_state IN ('pending','failed') AND u.pw_hash IS NULL AND u.role='member'").fetchall():
        if u["email"]: send(u["person_id"], u["email"], u["first"] or (u["full_name"] or "").split(" ")[0], "admin"); n += 1
    return n
