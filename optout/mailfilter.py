"""IMAP filter per person (Gmail app password). Labels only; never deletes. DRY_RUN logs decisions without moving mail."""
import imaplib, email, os, re
from email.header import decode_header, make_header
from db import conn, now, event
DRY = os.environ.get("DRY_RUN", "true").lower() != "false"
BROKER_DOMAINS = set()
try:
    import yaml; BROKER_DOMAINS = {b["email"].split("@")[1].lower() for b in yaml.safe_load(open("/app/brokers.yaml"))["brokers"] if b.get("email")}
except Exception: pass
PHISH = re.compile(r"verify your account|password (?:expired|reset required)|unusual sign.?in|gift card|wire transfer|crypto|suspended", re.I)
SPAM = re.compile(r"unsubscribe|limited time|act now|you(?:'| ha)ve won|free trial|% off", re.I)
CONFIRM = re.compile(r"https?://\S*(?:confirm|verify|optout|opt-out|removal|suppress)\S*", re.I)

def classify(frm, subj, body):
    dom = (re.search(r"@([\w.-]+)", frm or "") or [None, ""])[1].lower()
    if any(dom.endswith(d) for d in BROKER_DOMAINS) or re.search(r"opt.?out|removal request|ccpa|data deletion", subj or "", re.I):
        return "OptOut/Broker-Reply", CONFIRM.findall(body or "")[:3]
    if PHISH.search((subj or "") + " " + (body or "")[:3000]): return "Filter/Phishing", []
    if SPAM.search((subj or "") + " " + (body or "")[:3000]) and "List-Unsubscribe" not in (body or ""): return "Filter/Marketing", []
    return None, []

def run_person(p):
    pw = os.environ.get(f"GMAIL_APP_PASSWORD_{p['id'].upper()}")
    if not pw: return f"{p['id']}: no app password, skipped"
    c = conn(); m = imaplib.IMAP4_SSL("imap.gmail.com"); m.login(p["email"], pw); m.select("INBOX")
    _, ids = m.search(None, "UNSEEN SINCE", (__import__("datetime").date.today() - __import__("datetime").timedelta(days=2)).strftime("%d-%b-%Y"))
    n = 0
    for i in ids[0].split()[-200:]:
        _, d = m.fetch(i, "(BODY.PEEK[])"); msg = email.message_from_bytes(d[0][1])
        subj = str(make_header(decode_header(msg.get("Subject", "")))); frm = msg.get("From", "")
        body = next((part.get_payload(decode=True).decode(errors="ignore") for part in msg.walk() if part.get_content_type() == "text/plain" and part.get_payload(decode=True)), "")
        label, links = classify(frm, subj, body)
        if not label: continue
        n += 1; event(c, "mail_" + label.split("/")[1], f"{frm} | {subj} | {' '.join(links)}", p["id"])
        if not DRY:
            m.create(label); m.copy(i, label)
            if label.startswith("Filter/"): m.store(i, "-X-GM-LABELS", "\\Inbox")  # archive, not delete
    m.logout(); return f"{p['id']}: {n} classified"

def run_all():
    c = conn(); return [run_person(p) for p in c.execute("SELECT * FROM people WHERE enabled=1 AND email<>''")]
