"""Files removals. DRY_RUN=true (default): everything goes to outbox as 'dry_run', nothing leaves the server."""
import os, smtplib, ssl, datetime, yaml, subprocess, json, asyncio, threading  # nosec B404
import profiles
from crypto import enc
from email.message import EmailMessage
from db import conn, now, event
from scanner import SITES
from db import conn
DRY = os.environ.get("DRY_RUN", "true").lower() != "false"
DATA = os.path.dirname(os.environ.get("DB_PATH", "/data/optout.db"))
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _first(env, *paths): return os.environ.get(env) or next((x for x in paths if os.path.exists(x)), paths[0])
SKIP_FILE = _first("SKIP_FILE", "/app/skip_ids.txt", os.path.join(_HERE, "config", "skip_ids.txt"))
BROKERS_FILE = _first("BROKERS_FILE", "/app/brokers.yaml", os.path.join(_HERE, "eraser", "data", "brokers.yaml"))
def _skip_ids():
    try: return open(SKIP_FILE).read().split()
    except OSError: return []
SITE = {s["id"]: s for s in SITES}
try: PARENTS = yaml.safe_load(open(os.environ.get("SITES_FILE") or os.path.join(os.path.dirname(__file__), "sites.yaml"))).get("parents") or {}
except Exception: PARENTS = {}

def contact_email(p):
    """Where brokers should write back: the shared agent inbox (Resend) so verification links are auto-confirmed, else the person's own email."""
    return reply_to(p) if PROVIDER == "resend" else p["email"]

def ccpa_body(p, broker, url=None):
    ids = profiles.identifiers(profiles.get(p))
    def block(label, xs): return "".join(f"\n- {label}: {x}" for x in xs)
    auth = "\nAUTHORIZED AGENT: This request is submitted on the consumer's behalf with their signed authorization (attached), as permitted by Cal. Civ. Code 1798.135(e) and 11 CCR 7063.\n" if p["needs_auth"] else ""
    link = f"\n- Listing URL: {url}" if url else ""
    return f"""To Whom It May Concern at {broker},

I am a California resident exercising my rights under the CCPA/CPRA (Cal. Civ. Code 1798.105 and 1798.120). Please delete all personal information you hold about me, including every record matching any of the identifiers below, and do not sell or share it.

PERSONAL IDENTIFICATION:{block("Name", ids["names"][:1])}{block("Also known as", ids["names"][1:])}{block("Address", ids["addresses"])}{block("Phone", ids["phones"])}{block("Email", ids["emails"])}{block("Relative (for record matching only)", ids["relatives"][:6])}{link}
{auth}
Please confirm completion within 45 days as required by law. If you need to verify this request, reply to {contact_email(p)}.

Sincerely,
{p['full_name']}
{datetime.date.today():%B %d, %Y}
"""

def can_send(p):
    if not p["enabled"]: return False, "profile disabled"
    if not DRY and not p.get("send_enabled"): return False, "sending not enabled by the family admin yet"
    if p["needs_auth"] and not has_auth(p): return False, "the person has not e-signed the authorization yet"
    return True, ""

PROVIDER = os.environ.get("SEND_PROVIDER", "resend").lower()          # resend | smtp (legacy per-person Gmail)
FROM_ADDRESS = os.environ.get("FROM_ADDRESS") or "privacy@example.com"   # one shared sender; set in Heartbeat > Settings

def has_auth(p):
    """Authorization on file: in-app e-signature (preferred) or an uploaded signed PDF (fallback)."""
    return bool(p["esign_at"] or p["auth_pdf"])

def auth_file(p):
    """PDF attached to requests: the letter generated from the e-signature, else the uploaded fallback."""
    import esign
    f = esign.pdf_path(p["id"]) if p["esign_at"] else None
    return f if f and os.path.exists(f) else p["auth_pdf"]

def provider_ready():
    """True once a sending provider is configured (Resend key, or legacy SMTP app password). Real sends additionally need DRY_RUN=false."""
    if PROVIDER == "resend": return bool(os.environ.get("RESEND_API_KEY"))
    return any(k.startswith("GMAIL_APP_PASSWORD_") and v for k, v in os.environ.items())
FROM_NAME = os.environ.get("FROM_NAME") or "Fletcher Fortis Privacy"

def reply_to(p):
    """Per-person Reply-To on the same inbox via plus-addressing: privacy+alex@... (Cloudflare catch-all routes it to the Worker)."""
    if PROVIDER != "resend": return p["email"]
    if p["send_from"] and provider_ready(): return p["send_from"]          # per-person reply address (only once email is set up)
    local, dom = FROM_ADDRESS.split("@"); return f"{local}+{p['id']}@{dom}"

def tag(p, ref): return f"[FF-{p['id']}-{ref}]"   # subject tag: matches broker replies to person + request

def _resend(frm, to, rto, subject, body, attach, ref, html=None):
    import json, base64, urllib.request
    key = os.environ.get("RESEND_API_KEY")
    if not key: return "blocked: no RESEND_API_KEY"
    d = {"from": frm, "to": [to], "reply_to": [rto], "subject": subject, "text": body, "headers": {"X-FF-Ref": ref}}
    if html: d["html"] = html
    if attach and os.path.exists(attach):
        d["attachments"] = [{"filename": "signed-authorization.pdf", "content": base64.b64encode(open(attach, "rb").read()).decode()}]
    rq = urllib.request.Request("https://api.resend.com/emails", json.dumps(d).encode(), {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Idempotency-Key": ref})
    try:
        with urllib.request.urlopen(rq, timeout=30) as r: return "sent:" + json.load(r).get("id", "")  # nosec B310
    except Exception as e: return f"error: {e}"[:200]

def send_email(c, p, to, subject, body, attach=None, listing_id=None, html=None, stage=0, channel=None):
    """Every request is written to the outbox first (ref = outbox id), then sent only if DRY_RUN=false and the auth gate passes.
    attach (signed-authorization PDF) is only passed for people with needs_auth, i.e. non-self family members."""
    ok, why = can_send(p)
    cur = c.execute("INSERT INTO outbox(ts,person_id,to_addr,subject,body,state,listing_id,stage,channel,html) VALUES(?,?,?,?,?,?,?,?,?,?)", (now(), p["id"], to, enc(subject), enc(body), "queued", listing_id, stage, channel, enc(html) if html else None))
    ref = str(cur.lastrowid); subject = f"{subject} {tag(p, ref)}"; rto = reply_to(p)
    attach = attach if p["needs_auth"] else None
    if DRY: state = "dry_run"
    elif not ok: state = "blocked: " + why
    elif PROVIDER == "resend":
        state = _resend(f"{FROM_NAME} <{FROM_ADDRESS}>", to, rto, subject, body, attach, ref, html)
        state = "sent" if state.startswith("sent") else state
    else:
        pw = os.environ.get(f"GMAIL_APP_PASSWORD_{p['id'].upper()}")
        if not pw: state = "blocked: no app password in .env"
        else:
            m = EmailMessage(); m["From"] = p["email"]; m["To"] = to; m["Subject"] = subject; m["Reply-To"] = rto; m.set_content(body)
            if attach and os.path.exists(attach):
                m.add_attachment(open(attach, "rb").read(), maintype="application", subtype="pdf", filename="signed-authorization.pdf")
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
                s.login(p["email"], pw); s.send_message(m)
            state = "sent"
    c.execute("UPDATE outbox SET subject=?, state=?, reply_to=? WHERE id=?", (enc(subject), state, rto, ref)); c.commit()
    return state

def _record(c, p, to, subject, body, state, listing_id):
    c.execute("INSERT INTO outbox(ts,person_id,to_addr,subject,body,state,listing_id,reply_to,channel) VALUES(?,?,?,?,?,?,?,?,?)",
              (now(), p["id"], to, enc(subject), enc(body), state, listing_id, contact_email(p), "parent" if "parent" in subject else "form")); c.commit()

def _broker_emails():
    """Privacy-request addresses from the bundled broker databases (Eraser + merged registries), by domain."""
    out = {}
    def dom(u): return (u or "").lower().split("://")[-1].split("/")[0].removeprefix("www.")
    for f, key in ((BROKERS_FILE, "brokers"), (os.environ.get("BROKER_OUT", "/data/brokers_merged.yaml"), "brokers"), (os.path.join(_HERE, "data", "brokers_merged.yaml"), "brokers")):
        try:
            for b in yaml.safe_load(open(f)).get(key) or []:
                if b.get("email") and "@" in b["email"]: out.setdefault(dom(b.get("website") or b.get("domain")), b["email"].strip())
        except Exception: pass
    try:
        import csv
        for r in list(csv.reader(open(os.path.join(_HERE, "brokers", "ca_registry.csv"), encoding="utf-8", errors="ignore")))[1:]:
            if len(r) > 3 and "@" in r[3]: out.setdefault(dom(r[2]), r[3].strip()); CA_REGISTERED.add(dom(r[2]))
    except OSError: pass
    out.pop("", None); return out
CA_REGISTERED = set()   # domains in the California data broker registry: covered by one CA DROP request
BROKER_EMAIL = _broker_emails()

def channels(site):
    """Every automatic channel this site supports, best first: parent suppression, plain web form, privacy email. Human-only: captcha form."""
    oo = site.get("optout") or {}; t = oo.get("type"); out = []
    if t == "parent_suppression":
        par = (PARENTS.get(site.get("parent")) or {}).get("optout") or oo.get("url")
        if par: out.append(("parent", par))
    if t == "form" and oo.get("url") and not oo.get("captcha"): out.append(("form", oo["url"]))
    to = oo.get("to") or BROKER_EMAIL.get((site.get("domain") or "").lower())
    if to: out.append(("email", to))
    if t == "form" and oo.get("url") and oo.get("captcha"): out.append(("captcha_form", oo["url"]))
    return out

def channel_stats(c, site_id):
    """Outcome history per channel for this broker: {channel: (sent, confirmed)}."""
    rows = c.execute("SELECT o.channel ch, COUNT(*) n, SUM(CASE WHEN l.status='confirmed_removed' THEN 1 ELSE 0 END) ok FROM outbox o JOIN listings l ON l.id=o.listing_id WHERE l.site=? AND o.channel IS NOT NULL AND o.stage=0 GROUP BY o.channel", (site_id,)).fetchall()
    return {r["ch"]: (r["n"], r["ok"] or 0) for r in rows}

def method(site, c=None):
    """Best channel: email | form | parent | captcha_form | none, plus the target. Prefers the channel that has actually worked for this broker."""
    ch = channels(site)
    if not ch: return "none", (site.get("optout") or {}).get("url")
    if c is not None and len(ch) > 1:
        st = channel_stats(c, site.get("id"))
        def rate(x): n, ok = st.get(x[0], (0, 0)); return (ok / n) if n >= 3 else -1
        best = max(ch[:-1] if ch[-1][0] == "captcha_form" else ch, key=rate)
        if rate(best) > 0: return best
    return ch[0]

def _siblings(c, l, site):
    """Other open listings of the same person covered by the same removal: same parent company + same dedupe key, or any listing under the same parent when the parent suppresses all its sites."""
    par = site.get("parent")
    out = []
    for o in c.execute("SELECT * FROM listings WHERE person_id=? AND id<>? AND status IN ('candidate','approved')", (l["person_id"], l["id"])).fetchall():
        os_ = SITE.get(o["site"], {})
        if (l["dedupe"] and o["dedupe"] == l["dedupe"]) or (par and os_.get("parent") == par and method(os_)[0] == "parent"): out.append(o)
    return out

def _form_job(listing_id, url, captcha, solver=False):
    """Live form submission in a background thread (only reached with DRY_RUN=false and sending enabled)."""
    import formfill, scanner
    from playwright.async_api import async_playwright
    c = conn(); l = c.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone(); p = c.execute("SELECT * FROM people WHERE id=?", (l["person_id"],)).fetchone()
    async def run():
        async with async_playwright() as pw:
            b = await pw.chromium.launch(**scanner.launch_kw()); ctx = await scanner.new_context(b); page = await ctx.new_page()
            try: return await formfill.fill(page, url, formfill.values(p, profiles.get(p), l["url"]), os.path.join(scanner.SHOTS, f"form_{listing_id}.png"), dry=DRY, captcha_flag=captcha and not solver, solver=solver, listing_id=listing_id)
            finally: await b.close()
    try: st, detail = asyncio.run(run())
    except Exception as e: st, detail = "error", type(e).__name__
    new = {"submitted": "submitted", "dry_run_filled": "submitted"}.get(st, "needs_human")
    c.execute("UPDATE listings SET status=?, submitted_at=? WHERE id=?", (new, now(), listing_id))
    _record(c, p, url, "Opt-out web form", detail, st, listing_id); event(c, "form_" + st, detail, p["id"], listing_id)

def approve(listing_id):
    c = conn(); l = c.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    p = c.execute("SELECT * FROM people WHERE id=?", (l["person_id"],)).fetchone()
    site = SITE.get(l["site"], {"name": l["site"]})
    if p["needs_auth"] and not has_auth(p):
        c.execute("UPDATE listings SET status='blocked_auth' WHERE id=?", (listing_id,)); event(c, "blocked", "waiting for e-signed authorization", p["id"], listing_id); c.commit(); return "blocked"
    ok, why = can_send(p)
    if not ok:   # live mode but the admin has not enabled sending for this person: keep it approved and queued
        c.execute("UPDATE listings SET status='approved' WHERE id=?", (listing_id,)); event(c, "queued", why, p["id"], listing_id); c.commit(); return "queued: " + why
    m, target = method(site, c); new = "submitted"
    if m == "email":
        st = send_request(c, p, site, target, listing_id)
        new = "submitted" if st in ("sent", "dry_run") else "approved"
    elif m in ("form", "parent"):
        done = None
        if m == "parent":
            done = c.execute("SELECT l.id FROM listings l WHERE l.person_id=? AND l.id<>? AND l.status IN ('submitted','pending','confirmed_removed')", (p["id"], listing_id)).fetchall()
            done = next((x["id"] for x in done if SITE.get(c.execute("SELECT site FROM listings WHERE id=?", (x["id"],)).fetchone()["site"], {}).get("parent") == site.get("parent")), None)
        if done: st = f"covered by the {site.get('parent')} parent-company suppression already filed (listing #{done})"
        elif DRY:
            st = "dry_run"; _record(c, p, target, "Opt-out web form" + (" (parent suppression)" if m == "parent" else ""), f"DRY_RUN: would open {target}, fill it with this person's identifiers and submit it.", "dry_run", listing_id)
        else:
            st = "form autofill started"; new = "approved"
            threading.Thread(target=_form_job, args=(listing_id, target, False), daemon=True).start()
    elif m == "captcha_form":
        import automation
        if not automation.solver_on(): new = "needs_human"; st = f"CAPTCHA form, complete it yourself: {target}"
        elif DRY:
            st = "dry_run"; _record(c, p, target, "Opt-out web form (CAPTCHA solver)", f"DRY_RUN: would open {target}, ask {automation.provider()} to solve the CAPTCHA (about ${automation.PRICE[automation.provider()]:.3f}), fill and submit it.", "dry_run", listing_id)
        else:
            st = "form autofill with CAPTCHA solver started"; new = "approved"
            threading.Thread(target=_form_job, args=(listing_id, target, True, True), daemon=True).start()
    else:
        new = "needs_human"; st = "no automated opt-out method known for this site"
    c.execute("UPDATE listings SET status=?, submitted_at=? WHERE id=?", (new, now(), listing_id)); event(c, "approve", st, p["id"], listing_id)
    if new == "submitted":
        for o in _siblings(c, l, site):
            c.execute("UPDATE listings SET status='submitted', submitted_at=? WHERE id=?", (now(), o["id"])); event(c, "covered_by_sibling", f"same person on sister site {site['name']} (listing #{listing_id})", p["id"], o["id"])
    c.commit(); return st

def send_request(c, p, site, to, listing_id, stage=0, sent_at=None):
    """Tailored removal email (law by state, only the data points this broker showed, authorization attached when acting as agent)."""
    import letters
    ls = c.execute("SELECT * FROM listings WHERE person_id=? AND site=? AND status NOT IN ('not_me','checked_clear')", (p["id"], site.get("id"))).fetchall()
    subj, text, html = letters.build(p, profiles.get(p), site, ls, agent=bool(p["needs_auth"]), reply_to=reply_to(p), stage=stage, sent_at=sent_at)
    return send_email(c, p, to, subj, text, auth_file(p) if p["needs_auth"] else None, listing_id, html=html, stage=stage, channel="email")

def send_queued(pid):
    """Called when the admin enables sending: process this person's approved-but-queued listings."""
    c = conn(); return [approve(r["id"]) for r in c.execute("SELECT id FROM listings WHERE person_id=? AND status='approved'", (pid,)).fetchall()]

def bulk_ccpa(person_id):
    """Eraser-equivalent 746-broker run. Off by default; DRY_RUN just renders the drafts via the bundled eraser binary."""
    c = conn(); p = c.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    ok, why = can_send(p)
    if not p["bulk_ccpa"]: return "bulk CCPA disabled for this person (toggle it on first)"
    if not ok: return "blocked: " + why
    d = os.path.join(DATA, "bulk"); os.makedirs(d, mode=0o700, exist_ok=True); cfg = os.path.join(d, f"{person_id}.yaml")
    if PROVIDER == "resend":
        smtp = {"host": "smtp.resend.com", "port": 465, "username": "resend", "password": os.environ.get("RESEND_API_KEY", ""), "use_tls": True}; frm = FROM_ADDRESS
    else:
        smtp = {"host": "smtp.gmail.com", "port": 465, "username": p["email"], "password": os.environ.get(f"GMAIL_APP_PASSWORD_{person_id.upper()}", ""), "use_tls": True}; frm = p["email"]
    doc = {"profile": {"first_name": p["first"], "last_name": p["last"], "email": contact_email(p), "city": p["city"], "state": p["state"], "country": "USA", "phone": p["phone"]},
           "email": {"provider": "smtp", "from": frm, "smtp": smtp},
           "options": {"template": "ccpa", "rate_limit_ms": 5000, "dry_run": DRY, "regions": ["us", "global"], "excluded_brokers": _skip_ids()}}
    fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: yaml.safe_dump(doc, f)
    args = ["eraser", "send", "--config", cfg, "--brokers", BROKERS_FILE] + (["--dry-run"] if DRY else [])
    try: r = subprocess.run(args, capture_output=True, text=True, timeout=3600, env={"PATH": os.environ.get("PATH", ""), "HOME": os.path.join(DATA, "eraser-home", person_id)})  # nosec B603: fixed argv, no shell
    except FileNotFoundError: event(c, "bulk_ccpa", "eraser binary not installed", person_id); return "eraser binary not installed"
    finally:
        try: os.remove(cfg)   # the config holds an SMTP password: never leave it on disk
        except OSError: pass
    out = r.stdout[-400:]; event(c, "bulk_ccpa", ("DRY " if DRY else "") + f"exit {r.returncode}: " + out[-200:], person_id); return out
