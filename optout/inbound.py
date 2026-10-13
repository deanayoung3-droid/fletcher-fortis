"""Inbound broker replies. Cloudflare Email Routing -> Email Worker -> POST /inbound/email (raw RFC822, X-Inbound-Secret header).
Matches each reply to a person + request, classifies it, and auto-clicks verification links ONLY when both the sender
and the link host are known broker domains (allowlist built from sites.yaml). DRY_RUN=true: links are logged, never clicked."""
import email, email.policy, os, re, json, urllib.request, urllib.parse, urllib.error, html as _html
import yaml
from db import now, event
from crypto import enc
DRY = os.environ.get("DRY_RUN", "true").lower() != "false"
AUTOCLICK = os.environ.get("INBOUND_AUTOCLICK", "true").lower() == "true"
TAG = re.compile(r"\[FF-([a-z0-9_]+)-(\d+)\]", re.I)
PLUS = re.compile(r"[\w.-]+\+([a-z0-9_]+)@", re.I)
URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
VERIFY_URL = re.compile(r"confirm|verif|optout|opt-out|opt_out|remov|suppress|unlist|privacy|token|activate", re.I)
KINDS = [("removed", re.compile(r"(has|have) been (removed|deleted|suppressed|processed)|removal (is )?complete|request (has been |was )?(completed|fulfilled)|successfully (removed|opted)", re.I)),
         ("verify", re.compile(r"(confirm|verify) (your )?(request|email|opt.?out|removal)|click (the|this) link|verification link", re.I)),
         ("need_info", re.compile(r"verify your identity|additional information|unable to (locate|verify)|need more information|government.issued", re.I)),
         ("denied", re.compile(r"(cannot|can't|will not|unable to) (honor|process|comply)|request (is |was )?denied|not a california resident", re.I)),
         ("ack", re.compile(r"(we|have) received your|request (has been )?received|ticket|case number", re.I))]

JUNK_PHISH = re.compile(r"verify your account|password (?:expired|reset required)|unusual sign.?in|gift card|wire transfer|crypto|suspended|invoice attached", re.I)
JUNK_SPAM = re.compile(r"unsubscribe|limited time|act now|you(?:'| ha)ve won|free trial|% off|newsletter", re.I)

def _host(u):
    try: return (urllib.parse.urlsplit(u).hostname or "").lower().removeprefix("www.")
    except ValueError: return ""

def load_allowlist(path=None):
    path = path or os.environ.get("BROKER_ALLOWLIST_FILE") or os.path.join(os.path.dirname(__file__), "sites.yaml")
    d = yaml.safe_load(open(path)); doms = {}
    for s in d.get("sites") or []:
        oo = s.get("optout") or {}
        for h in (s.get("domain"), _host(s.get("search_url") or ""), _host(oo.get("url") or ""), (oo.get("to") or "@").split("@")[1]):
            h = (h or "").lower().removeprefix("www.")
            if h and "." in h: doms.setdefault(h, set()).add(s["id"])
    for name, p in (d.get("parents") or {}).items():
        h = _host((p or {}).get("optout") or "")
        if h: doms.setdefault(h, set()).add(name)
    return doms
ALLOW = load_allowlist()

def broker_site(host):
    """Return the site ids (set) if host is an allowlisted broker domain or a subdomain of one (no suffix tricks: 'evilspokeo.com' != 'spokeo.com')."""
    host = (host or "").lower().removeprefix("www.")
    parts = host.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in ALLOW: return ALLOW[".".join(parts[i:])]
    return None

def text_of(msg):
    plain, htm = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename(): continue
        try: t = part.get_content()
        except Exception: t = (part.get_payload(decode=True) or b"").decode(errors="ignore")
        if part.get_content_type() == "text/plain": plain.append(t)
        elif part.get_content_type() == "text/html": htm.append(t)
    links = []
    for h in htm: links += [_html.unescape(u) for u in re.findall(r'href=["\'](https?://[^"\']+)', h, re.I)]
    body = "\n".join(plain) or _html.unescape(re.sub(r"<[^>]+>", " ", "\n".join(htm)))
    links += URL.findall(body)
    return body, list(dict.fromkeys(l.rstrip(".,;") for l in links))

def classify(subject, body):
    for k, rx in KINDS:
        if rx.search(subject + "\n" + body[:8000]): return k
    return "other"

def _public_https(u):
    import socket, ipaddress
    p = urllib.parse.urlsplit(u)
    if p.scheme != "https" or not p.hostname or not broker_site(p.hostname): return False
    try: addrs = {a[4][0] for a in socket.getaddrinfo(p.hostname, 443, proto=socket.IPPROTO_TCP)}
    except OSError: return False
    return all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)

class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _public_https(newurl): raise urllib.error.HTTPError(newurl, code, "redirect to a non-broker or private host blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def _click(u):
    """GET a verification link. Every hop must be https, on an allowlisted broker domain and resolve to a public IP (no LAN/SSRF)."""
    if not _public_https(u): raise ValueError("blocked: not a public https broker URL")
    rq = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (Fletcher Fortis opt-out confirmation)"})
    with urllib.request.build_opener(_SafeRedirect).open(rq, timeout=20) as r:   # nosec B310: https-only, checked above
        return f"{r.status} {_host(r.geturl())}"

def handle(c, raw: bytes, clicker=_click):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    frm = str(msg.get("From", "")); subj = str(msg.get("Subject", ""))
    to = " ".join(str(msg.get(h, "")) for h in ("To", "Delivered-To", "X-Original-To", "X-Forwarded-To", "Cc"))
    sender_dom = (re.search(r"@([\w.-]+)", frm) or [None, ""])[1].lower()
    body, links = text_of(msg)
    # 1) person + request: subject tag [FF-alex-12] (brokers usually keep "Re: <subject>"), 2) plus address privacy+alex@, 3) sender domain
    pid = oid = lid = None
    m = TAG.search(subj) or TAG.search(body[:4000])
    if m:
        o = c.execute("SELECT * FROM outbox WHERE id=? AND person_id=?", (int(m.group(2)), m.group(1).lower())).fetchone()
        if o: pid, oid, lid = o["person_id"], o["id"], o["listing_id"]
    if not pid and (m := PLUS.search(to)):
        if c.execute("SELECT 1 FROM people WHERE id=?", (m.group(1).lower(),)).fetchone(): pid = m.group(1).lower()
    site = broker_site(sender_dom)
    if not lid and site:
        q = f"SELECT * FROM listings WHERE site IN ({','.join('?'*len(site))}) AND status IN ('submitted','pending','approved','needs_human')" + (" AND person_id=?" if pid else "") + " ORDER BY submitted_at DESC"  # nosec B608
        rows = c.execute(q, (*sorted(site), pid) if pid else tuple(sorted(site))).fetchall()
        if len(rows) == 1 or (pid and rows): lid, pid = rows[0]["id"], rows[0]["person_id"]
    if lid and not pid: pid = c.execute("SELECT person_id FROM listings WHERE id=?", (lid,)).fetchone()["person_id"]
    kind = classify(subj, body)
    if not site and not oid and not pid:   # not a broker and not a reply to one of our requests: junk / phishing triage (labels only, never deleted)
        if JUNK_PHISH.search(subj + " " + body[:3000]) or (sender_dom and any(sender_dom.endswith(x) for x in (".zip", ".mov", ".top", ".xyz"))): kind = "phishing"
        elif JUNK_SPAM.search(subj + " " + body[:3000]) or msg.get("List-Unsubscribe"): kind = "junk"
    # verification links: only https, only allowlisted broker hosts, only if the SENDER is a broker too
    cand = [u for u in links if VERIFY_URL.search(u) and u.lower().startswith("https://") and broker_site(_host(u))] if site else []
    clicked = []
    if kind == "verify" and cand and pid:
        for u in cand[:2]:
            if DRY or not AUTOCLICK: clicked.append(("would-click (DRY_RUN)" if DRY else "autoclick off") + " " + u); continue
            try: clicked.append(f"{clicker(u)} {u}")
            except Exception as e: clicked.append(f"error {e} {u}"[:300])
    new = {"verify": "pending", "removed": "pending", "ack": "pending", "need_info": "needs_human", "denied": "needs_human"}.get(kind)
    if lid and new:
        c.execute("UPDATE listings SET status=? WHERE id=? AND status NOT IN ('confirmed_removed','not_me')", (new, lid))
    c.execute("INSERT INTO inbox(ts,person_id,listing_id,outbox_id,from_addr,to_addr,subject,kind,links,clicked,body) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (now(), pid, lid, oid, enc(frm), enc(to.strip()), enc(subj), kind, enc(json.dumps(links[:20])), enc(json.dumps(clicked)), enc(body[:20000])))
    event(c, "broker_reply_" + kind, f"{frm} | {subj}" + (" | " + "; ".join(clicked) if clicked else ""), pid, lid)
    if kind == "removed" and lid: event(c, "broker_reports_removed", "will be marked Removed only after the next re-scan confirms it", pid, lid)
    c.commit()
    return {"person": pid, "listing": lid, "outbox": oid, "site": sorted(site) if site else None, "kind": kind, "clicked": clicked, "status": new if lid else None}
