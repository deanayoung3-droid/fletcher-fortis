"""Go-live readiness: what is configured vs missing. Read-only. Never sends an email.
DNS checks (only when the admin presses 'Test email setup') use DNS-over-HTTPS lookups for the sending domain."""
import os, json, urllib.request, urllib.parse
import remover, esign

def domain(): return remover.FROM_ADDRESS.split("@")[-1].lower()

def doh_records(name, rtype):
    """Public DNS lookup via Cloudflare DoH. Returns [(type_name, data)] including any CNAME hops (None on error)."""
    u = "https://cloudflare-dns.com/dns-query?" + urllib.parse.urlencode({"name": name, "type": rtype})
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers={"Accept": "application/dns-json"}), timeout=6) as r:  # nosec B310: fixed https host
            ans = json.load(r).get("Answer", []) or []
    except Exception: return None
    tn = {1: "A", 5: "CNAME", 15: "MX", 16: "TXT", 28: "AAAA"}
    return [(tn.get(a.get("type"), str(a.get("type"))), a.get("data", "").strip().strip('"').replace('" "', "")) for a in ans]

def doh(name, rtype):
    """Record strings of the requested type only (CNAME hops dropped). None on error."""
    rr = doh_records(name, rtype)
    return None if rr is None else [d for t, d in rr if t == rtype]

def cname_of(rr): return next((d.rstrip(".").lower() for t, d in rr or [] if t == "CNAME"), "")

RESEND_HOSTS = ("rmta.net", "amazonses.com")
def _resend_host(h): h = h.rstrip(".").lower(); return any(h == x or h.endswith("." + x) for x in RESEND_HOSTS)
def _mx_host(v): return v.split()[-1].rstrip(".").lower() if v.strip() else ""
def bounce_mx_ok(mxs, cname=""):
    for v in mxs:
        h = _mx_host(v)
        if (h.startswith("feedback-smtp.") and h.endswith(".amazonses.com")) or (h.startswith("feedback.") and h.endswith(".rmta.net")): return True
    return bool(cname) and cname.endswith(".rmta.net") and "forge" in cname
def spf_ok(txts): return any(x.lower().startswith("v=spf1") for x in txts)
def dmarc_ok(txts): return any(x.replace(" ", "").upper().startswith("V=DMARC1;") or x.strip().upper() == "V=DMARC1" for x in txts)
def _via(cname): return f" (via CNAME {cname})" if cname else ""

def dns_checks():
    d = domain(); out = []
    def add(key, label, ok, detail, fix): out.append({"key": key, "label": label, "ok": ok, "detail": detail, "fix": fix})
    rr = doh_records(f"send.{d}", "TXT") or []; cn = cname_of(rr); spf = [x for t, x in rr if t == "TXT" and x.lower().startswith("v=spf1")]
    add("spf", f"SPF for Resend (send.{d})", spf_ok(spf), ("; ".join(spf)[:160] + _via(cn)) if spf else (f"CNAME {cn}, but no SPF behind it" if cn else "no SPF record found"),
        "Resend > Domains > add the SPF record (TXT, or the CNAME in Resend's newer setup) for send." + d)
    root = [x for x in (doh(d, "TXT") or []) if x.lower().startswith("v=spf1")]
    if root: add("spf_root", f"Root SPF ({d})", True, "; ".join(root)[:160], "")
    dk = doh_records(f"resend._domainkey.{d}", "TXT") or []; dkc = cname_of(dk)
    add("dkim", f"DKIM (resend._domainkey.{d})", any(t == "TXT" and "p=" in x for t, x in dk), ("found" + _via(dkc)) if dk else "missing", "Resend > Domains > copy the DKIM record into Cloudflare DNS")
    mrr = doh_records(f"send.{d}", "MX") or []; mcn = cname_of(mrr) or cn; mxs = [x for t, x in mrr if t == "MX"]
    add("mx_send", f"Bounce MX (send.{d})", bounce_mx_ok(mxs, mcn), (", ".join(mxs) + _via(mcn)) if mxs else (f"CNAME {mcn}" if mcn else "missing"),
        "Resend > Domains > add the MX record (or the send CNAME) for send." + d)
    mx = doh(d, "MX") or []
    add("mx_in", f"Inbound MX ({d})", any("mx.cloudflare.net" in x for x in mx), ", ".join(mx) or "missing", "Cloudflare > Email > Email Routing > Enable (adds route1/2/3.mx.cloudflare.net)")
    dm = [x for x in (doh(f"_dmarc.{d}", "TXT") or []) if x.upper().startswith("V=DMARC1")]
    add("dmarc", f"DMARC (_dmarc.{d})", dmarc_ok(dm), "; ".join(dm)[:120] or "missing", "Add TXT _dmarc: v=DMARC1; p=none; (rua=mailto:dmarc@" + d + " is optional)")
    return out

def checklist(c):
    """Static readiness (no network)."""
    import config; env = lambda k, d=None: config.get(k, d) or (d if d is not None else None); items = []
    def add(group, label, ok, detail, fix=""): items.append({"group": group, "label": label, "ok": ok, "detail": detail, "fix": fix})
    add("Sending", "Sending provider", remover.PROVIDER == "resend", remover.PROVIDER, "SEND_PROVIDER=resend")
    add("Sending", "Resend API key", bool(env("RESEND_API_KEY")), "set" if env("RESEND_API_KEY") else "missing", "Resend > API keys > Create (sending access, your domain only), then paste it in Settings > Email")
    add("Sending", "Sender address", not remover.FROM_ADDRESS.endswith("@example.com"), f"{remover.FROM_NAME} <{remover.FROM_ADDRESS}>", "Settings > Email > From address (e.g. privacy@yourdomain.com)")
    add("Sending", "Public URL (links in emails)", env("PUBLIC_URL", "").startswith("https://"), env("PUBLIC_URL") or "missing", "Settings > Domain > Public URL (e.g. https://privacy.yourdomain.com)")
    add("Inbound", "Inbound webhook secret", len(env("INBOUND_SECRET", "")) >= 32, "set" if env("INBOUND_SECRET") else "missing (endpoint returns 404)", "Settings > Inbound > secret (32+ chars), same value as the Cloudflare Worker secret")
    add("Inbound", "Verification-link auto-click", True, "on" if os.environ.get("Settings > Inbound", "true") == "true" else "off (links only logged)", "Settings > Inbound")
    last = c.execute("SELECT ts FROM inbox ORDER BY id DESC LIMIT 1").fetchone()
    add("Inbound", "Worker delivering mail", bool(last), f"last message {last['ts'][:16]}" if last else "no message received yet", f"Send a test mail to {remover.FROM_ADDRESS} after setting up the Worker (deploy/cloudflare-email-worker.js)")
    people = c.execute("SELECT * FROM people ORDER BY rowid").fetchall()
    for p in people:
        if p["needs_auth"]: add("Authorization", f"{p['full_name']} e-signed", bool(p["esign_at"] or p["auth_pdf"]), (f"e-signed {p['esign_at'][:10]}" if p["esign_at"] else "PDF on file" if p["auth_pdf"] else "not yet"), "Invite them (Family > Invite member); they sign during onboarding")
        else: add("Authorization", f"{p['full_name']}", True, "self (no agent authorization needed)")
        add("Accounts", f"{p['full_name']} login", bool(c.execute("SELECT 1 FROM users WHERE person_id=? AND pw_hash IS NOT NULL", (p["id"],)).fetchone()), "", "Family > Invite member")
        add("Sending", f"Sending enabled for {p['first'] or p['full_name']}", bool(p["send_enabled"]), "admin toggle", "Family & accounts > Sending")
    if not people: add("Authorization", "Family members", False, "nobody added yet", "Add family member")
    add("Safety", "DRY_RUN", not remover.DRY, "ON: nothing leaves the server" if remover.DRY else "off: live", "Settings > Safety, only when everything above is green")
    add("Safety", "Encryption key in env", os.environ.get("FF_DATA_KEY") is not None or os.path.exists(os.environ.get("FF_DATA_KEY_FILE", "/run/secrets/ff_data_key")), "", "FF_DATA_KEY env var or secret file (back it up!)")
    add("Safety", "Secure cookies (HTTPS)", os.environ.get("COOKIE_SECURE", "true") != "false", "", "COOKIE_SECURE=true")
    return items

def ready_to_send():
    """Real email is possible only when ALL of these hold; each person additionally needs the admin 'Sending' toggle and an authorization."""
    return remover.provider_ready() and not remover.DRY
