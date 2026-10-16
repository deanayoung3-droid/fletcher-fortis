"""Optional automation for steps that used to land in "Needs you".
1. CAPTCHA solver (2Captcha or CapSolver): off by default. Needs CAPTCHA_PROVIDER + CAPTCHA_API_KEY in Heartbeat > Settings.
   Used only on opt-out forms for people who signed the authorization, never while DRY_RUN is on, and never past CAPTCHA_MONTHLY_CAP_USD.
2. Blocked-site retry: a site that blocked the automated search is retried from this server's own IP (no proxy) with
   exponential backoff (1, 2, 4, 8, 16 days). Only after every retry fails does it become a human task.
3. Auto re-check: sites a person marked "not listed" or "I submitted it" are searched again automatically after RECHECK_DAYS.
Truly human-only steps (ID upload, phone call, California DROP residency check, the person's own signature) are never automated."""
import datetime, json, re, time, urllib.request, urllib.parse, urllib.error
import config
from db import conn, now, event

BACKOFF = [1, 2, 4, 8, 16]                       # days between retries; 5 tries over ~31 days
PRICE = {"2captcha": 0.003, "capsolver": 0.001}  # approx. USD per reCAPTCHA v2 / Turnstile solve (check the provider's current pricing)
PROVIDERS = {"off": "Off", "2captcha": "2Captcha (~$3 per 1,000)", "capsolver": "CapSolver (~$1 per 1,000)"}
HUMAN_ONLY = {"id": "Asks for a photo ID", "phone": "Needs a phone call", "drop": "State verifies residency", "auth": "Person's own signature"}

def provider(): return (config.get("CAPTCHA_PROVIDER") or "off").lower()
def solver_on(): return provider() in PRICE and config.is_set("CAPTCHA_API_KEY")
def retry_on(): return config.get("RETRY_BLOCKED", "true") == "true"
def recheck_on(): return config.get("AUTO_RECHECK", "true") == "true"
def _cap(): return float(config.get("CAPTCHA_MONTHLY_CAP_USD") or 5)

def _ensure(c):
    c.execute("CREATE TABLE IF NOT EXISTS retry(listing_id INTEGER PRIMARY KEY, attempts INT DEFAULT 0, next_at TEXT, last TEXT, kind TEXT DEFAULT 'blocked')")

def spent_this_month(c=None):
    c = c or conn(); m = datetime.date.today().strftime("%Y-%m")
    return round(sum(json.loads(r["detail"]).get("usd", 0) for r in c.execute("SELECT detail FROM events WHERE kind='captcha_solved' AND ts LIKE ?", (m + "%",))), 3)

def estimate(c=None):
    """What the solver would cost per month for the CAPTCHA forms in the queue (plus re-files)."""
    c = c or conn(); n = c.execute("SELECT COUNT(*) FROM listings WHERE status='needs_human'").fetchone()[0]
    rate = PRICE.get(provider(), PRICE["2captcha"])
    return {"forms": n, "per_solve": rate, "monthly": round(n * rate * 1.5, 2), "provider": provider(), "spent": spent_this_month(c), "cap": _cap()}

SITEKEY = re.compile(r'data-sitekey=["\']([\w-]{20,})["\']')
def detect(html):
    k = SITEKEY.search(html or "")
    if not k: return None
    kind = "turnstile" if "turnstile" in html else "hcaptcha" if re.search(r"h-?captcha", html) else "recaptcha"
    return kind, k.group(1)

def _post(url, data, js=False, timeout=30):
    """POST to a solver endpoint. A 4xx/5xx with a JSON body (CapSolver answers errors that way) is returned as that body, so the
    provider's own reason (errorCode / errorDescription) reaches the user instead of a bare HTTPError."""
    body = json.dumps(data).encode() if js else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json" if js else "application/x-www-form-urlencoded"})
    try: return json.loads(urllib.request.urlopen(req, timeout=timeout).read())  # nosec B310 (fixed https endpoints)
    except urllib.error.HTTPError as e:
        try: d = json.loads(e.read() or b"{}")
        except Exception: d = {}
        d.setdefault("errorId", 1); d.setdefault("errorCode", f"HTTP {e.code}"); d.setdefault("errorDescription", e.reason or "")
        return d

def scrub(s):
    """Text safe to show or log: the API key is never included."""
    k = config.get("CAPTCHA_API_KEY") or ""
    s = str(s or "")
    return s.replace(k, "***") if len(k) >= 6 else s

def _why(r):
    return scrub(" ".join(x for x in (str(r.get("errorCode") or ""), str(r.get("errorDescription") or "")) if x).strip() or "unknown error")

SOLVER_ERR = "solver error"   # detail prefix for provider/network problems: temporary, retried with backoff

def solve(page_url, html, listing_id=None, dry=True):
    """Returns (token, detail). Never calls a provider while dry or over the monthly cap."""
    d = detect(html)
    if not d: return None, "no supported CAPTCHA found (only reCAPTCHA v2, hCaptcha, Turnstile)"
    kind, key = d; c = conn(); rate = PRICE[provider()]
    if dry: return None, f"DRY_RUN: would ask {provider()} to solve {kind} (about ${rate:.3f})"
    if spent_this_month(c) + rate > _cap(): return None, f"monthly CAPTCHA budget (${_cap():.2f}) reached"
    api = config.get("CAPTCHA_API_KEY")
    try:
        if provider() == "2captcha":
            m = {"recaptcha": "userrecaptcha", "hcaptcha": "hcaptcha", "turnstile": "turnstile"}[kind]
            r = _post("https://2captcha.com/in.php", {"key": api, "method": m, ("googlekey" if kind == "recaptcha" else "sitekey"): key, "pageurl": page_url, "json": 1})
            if r.get("status") != 1: return None, f"{SOLVER_ERR}: 2captcha: {scrub(r.get('request'))}"
            for _ in range(24):
                time.sleep(5); g = json.loads(urllib.request.urlopen(f"https://2captcha.com/res.php?key={urllib.parse.quote(api)}&action=get&id={r['request']}&json=1", timeout=30).read())  # nosec B310
                if g.get("status") == 1: tok = g["request"]; break
                if g.get("request") != "CAPCHA_NOT_READY": return None, f"{SOLVER_ERR}: 2captcha: {scrub(g.get('request'))}"
            else: return None, f"{SOLVER_ERR}: 2captcha: timed out"
        else:
            t = {"recaptcha": "ReCaptchaV2TaskProxyLess", "hcaptcha": "HCaptchaTaskProxyLess", "turnstile": "AntiTurnstileTaskProxyLess"}[kind]
            r = _post("https://api.capsolver.com/createTask", {"clientKey": api, "task": {"type": t, "websiteURL": page_url, "websiteKey": key}}, js=True)
            if r.get("errorId"): return None, f"{SOLVER_ERR}: capsolver: {_why(r)}"
            for _ in range(24):
                time.sleep(5); g = _post("https://api.capsolver.com/getTaskResult", {"clientKey": api, "taskId": r["taskId"]}, js=True)
                if g.get("status") == "ready": s = g["solution"]; tok = s.get("gRecaptchaResponse") or s.get("token"); break
                if g.get("errorId"): return None, f"{SOLVER_ERR}: capsolver: {_why(g)}"
            else: return None, f"{SOLVER_ERR}: capsolver: timed out"
    except Exception as e: return None, f"{SOLVER_ERR}: {provider()}: {type(e).__name__}: {scrub(e)[:120]}"
    event(c, "captcha_solved", json.dumps({"usd": rate, "kind": kind, "provider": provider()}), None, listing_id); c.commit()
    return (kind, tok), f"solved {kind} via {provider()}"

def check_balance(c=None, max_age=600, force=False):
    """Solver account status as the app sees it: {"provider", "ok", "balance", "status", "checked"}. Cached in app_meta for
    max_age seconds. Uses the saved key; the key itself is never returned, shown or logged."""
    c = c or conn(); prov = provider()
    try: cached = json.loads((c.execute("SELECT v FROM app_meta WHERE k='captcha_balance'").fetchone() or {}).get("v") or "{}")
    except Exception: cached = {}
    if not force and cached.get("provider") == prov and cached.get("checked") and (datetime.datetime.now() - datetime.datetime.fromisoformat(cached["checked"])).total_seconds() < max_age:
        return cached
    out = {"provider": prov, "ok": False, "balance": None, "status": "", "checked": now()}
    if prov not in PRICE: out["status"] = "solver off"
    elif not config.is_set("CAPTCHA_API_KEY"): out["status"] = "no API key saved"
    else:
        api = config.get("CAPTCHA_API_KEY")
        try:
            if prov == "capsolver":
                r = _post("https://api.capsolver.com/getBalance", {"clientKey": api}, js=True, timeout=15)
                if r.get("errorId"): out["status"] = _why(r)
                else: out.update(ok=True, balance=float(r.get("balance") or 0), status="ok")
            else:
                r = json.loads(urllib.request.urlopen(f"https://2captcha.com/res.php?key={urllib.parse.quote(api)}&action=getbalance&json=1", timeout=15).read())  # nosec B310
                if r.get("status") == 1: out.update(ok=True, balance=float(r.get("request") or 0), status="ok")
                else: out["status"] = scrub(r.get("request") or "unknown error")
        except Exception as e: out["status"] = f"could not reach {prov}: {type(e).__name__}"
        if out["ok"] and out["balance"] is not None and out["balance"] < max(PRICE[prov] * 20, 0.5): out["status"] = "low balance: top up"
    c.execute("INSERT INTO app_meta(k,v) VALUES('captcha_balance',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps(out),)); c.commit()
    return out

INJECT_JS = """([kind, tok]) => { const n = {recaptcha: 'g-recaptcha-response', hcaptcha: 'h-captcha-response', turnstile: 'cf-turnstile-response'}[kind];
  let els = document.querySelectorAll(`[name="${n}"], #${n}`);
  if (!els.length) { const f = document.querySelector('form'); const t = document.createElement('textarea'); t.name = n; t.style.display = 'none'; (f || document.body).appendChild(t); els = [t]; }
  els.forEach(e => e.value = tok); return els.length }"""

# ---- blocked-site retry + re-check -------------------------------------------------------------
def schedule(c, listing_id, kind="blocked"):
    """Called when a site blocks the search (or a human marks something done): first automatic try tomorrow."""
    _ensure(c)
    c.execute("INSERT INTO retry(listing_id, attempts, next_at, kind) VALUES(?,0,?,?) ON CONFLICT(listing_id) DO NOTHING",
              (listing_id, (datetime.datetime.now() + datetime.timedelta(days=BACKOFF[0] if kind == "blocked" else int(config.get("RECHECK_DAYS") or 7))).isoformat(timespec="seconds"), kind))

def pending(c=None):
    """listing_id -> (attempts, next_at) for listings the robot is still handling (hidden from Needs you)."""
    c = c or conn(); _ensure(c)
    if not retry_on(): return {}
    return {r["listing_id"]: (r["attempts"], r["next_at"]) for r in c.execute("SELECT * FROM retry WHERE kind='blocked' AND attempts < ?", (len(BACKOFF),))}

def due(c):
    _ensure(c); out = []
    if retry_on(): out += c.execute("SELECT r.*, l.site, l.person_id FROM retry r JOIN listings l ON l.id=r.listing_id WHERE r.kind='blocked' AND l.status='manual_check' AND r.attempts < ? AND r.next_at <= ?", (len(BACKOFF), now())).fetchall()
    if recheck_on(): out += c.execute("SELECT r.*, l.site, l.person_id FROM retry r JOIN listings l ON l.id=r.listing_id WHERE r.kind='recheck' AND r.next_at <= ?", (now(),)).fetchall()
    return out

def record(c, listing_id, ok, detail=""):
    """After a retry: ok = the site answered (listing found or confirmed absent). Otherwise back off; after the last try it's a human task."""
    _ensure(c); r = c.execute("SELECT * FROM retry WHERE listing_id=?", (listing_id,)).fetchone()
    if not r: return
    if ok or r["kind"] == "recheck": c.execute("DELETE FROM retry WHERE listing_id=?", (listing_id,)); return
    a = r["attempts"] + 1
    nxt = (datetime.datetime.now() + datetime.timedelta(days=BACKOFF[min(a, len(BACKOFF) - 1)])).isoformat(timespec="seconds")
    c.execute("UPDATE retry SET attempts=?, next_at=?, last=? WHERE listing_id=?", (a, nxt, detail[:200], listing_id))
    if a >= len(BACKOFF): event(c, "retry_exhausted", f"still blocked after {a} tries from this server: needs a person", None, listing_id)

async def run_due(dry=True):
    """Re-run the search for each due listing, from this server's IP (no proxy). Returns counts."""
    import scanner
    c = conn(); rows = due(c); out = {"retried": 0, "resolved": 0, "rechecked": 0}
    if not rows: return out
    from playwright.async_api import async_playwright
    kw = dict(scanner.launch_kw()); kw.pop("proxy", None)        # the host's own IP
    async with async_playwright() as pw:
        b = await pw.chromium.launch(**kw); ctx = await scanner.new_context(b); page = await ctx.new_page(); dpage = await ctx.new_page()
        for r in rows:
            site = next((s for s in scanner.SITES if s["id"] == r["site"]), None); p = c.execute("SELECT * FROM people WHERE id=?", (r["person_id"],)).fetchone()
            if not site or not p: c.execute("DELETE FROM retry WHERE listing_id=?", (r["listing_id"],)); continue
            before = c.execute("SELECT status FROM listings WHERE id=?", (r["listing_id"],)).fetchone()["status"]
            if r["kind"] == "recheck" and before == "checked_clear": c.execute("UPDATE listings SET status='manual_check' WHERE id=?", (r["listing_id"],))
            n = 0
            try: n = await scanner.scan_person(page, c, p, dict(site, health={"status": "works"}), dpage)
            except Exception as e: event(c, "retry_error", f"{site['id']}: {type(e).__name__}", p["id"], r["listing_id"])
            blocked = c.execute("SELECT 1 FROM events WHERE kind='scan_blocked' AND person_id=? AND detail LIKE ? AND ts >= ?", (p["id"], site["id"] + " %", r["next_at"])).fetchone()
            ok = not blocked
            if ok and before == "manual_check": c.execute("UPDATE listings SET status='checked_clear', last_checked=? WHERE id=? AND status='manual_check'", (now(), r["listing_id"])); out["resolved"] += 1
            elif r["kind"] == "recheck" and not ok: c.execute("UPDATE listings SET status=? WHERE id=?", (before, r["listing_id"]))
            event(c, "auto_retry" if r["kind"] == "blocked" else "auto_recheck", f"{site['id']}: {'answered, ' + str(n) + ' match(es)' if ok else 'still blocked'}", p["id"], r["listing_id"])
            record(c, r["listing_id"], ok, "blocked" if not ok else ""); out["retried" if r["kind"] == "blocked" else "rechecked"] += 1
            c.commit(); await page.wait_for_timeout(scanner.DELAY())
        await b.close()
    return out

def expected(c=None):
    """Manual-task forecast (an estimate, shown as a range). While retries run, blocked sites are not human tasks at all.
    After the retry window: sites with no automatable search stay manual; sites behind a Cloudflare-style challenge measured
    from a datacenter often answer from a home IP, so the low end assumes they clear and the high end assumes none do."""
    import lifecycle, remover, scanner
    c = c or conn(); S = {s["id"]: s for s in scanner.SITES}
    rows = c.execute("SELECT site FROM listings WHERE status='manual_check'").fetchall()
    blocked = len(rows); hard = sum(1 for r in rows if not (S.get(r["site"]) or {}).get("search_url"))
    captcha = c.execute("SELECT COUNT(*) FROM listings WHERE status='needs_human'").fetchone()[0]
    people = c.execute("SELECT * FROM people WHERE enabled=1").fetchall()
    human = sum(1 for p in people if lifecycle.drop_needed(p)) + sum(1 for p in people if p["needs_auth"] and not remover.has_auth(p))
    return {"blocked": blocked, "blocked_hard": hard, "captcha": captcha, "human_only": human, "now": blocked + captcha + human,
            "during_retry": captcha + human, "after_retry": (hard + captcha + human, blocked + captcha + human),
            "with_all": (hard + human, blocked + human)}
