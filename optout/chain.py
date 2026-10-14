"""Per-broker method chain ("try everything"). Official channels only, consent first, DRY_RUN gates every broker send,
and only a re-scan confirms a removal.

Discovery (finding out whether the person is listed), in order, stopping at the first that gets through:
  scan      direct Playwright search
  solver    the same page with the CAPTCHA solved (reCAPTCHA, hCaptcha, Turnstile; needs CAPTCHA_PROVIDER + key, budget-capped)
  stealth   a fresh stealth browser profile after a backoff
  proxy     the residential proxy in PROXY_URL (off unless set)
Removal, in order, falling through on block, CAPTCHA or failure:
  form      the broker's official opt-out form (parent-company suppression first), CAPTCHA solver when a challenge appears
  email     the broker's official privacy address, with the tailored legal letter (CCPA/CPRA etc.)
  registry  the request contact from the state data-broker registries (CA, OR, TX, VT)
  blind     for sites that block every search: the same official request without a confirmed listing (BLIND_REQUESTS setting)
Only ID uploads, phone-only opt-outs and the CA DROP residency check go to "Needs you".
Every attempt is stored in `attempts` and shown on the listing and in Heartbeat > Coverage."""
import asyncio, os, random, re
import config
from db import conn, now, event

LABEL = {"scan": "Direct scan", "solver": "CAPTCHA solver", "stealth": "Stealth retry", "proxy": "Residential proxy",
         "form": "Web form", "email": "Privacy email", "registry": "State registry contact", "blind": "Blind opt-out", "human": "Needs you"}
DISCOVERY = ("scan", "solver", "stealth", "proxy"); REMOVAL = ("form", "email", "registry", "blind")
OK = ("found", "not_found", "captcha_solved", "sent", "submitted", "dry_run", "confirmed")
BLOCKED = ("blocked", "captcha")

def ensure(c=None):
    c = c or conn()
    c.execute("CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, person_id TEXT, site TEXT, listing_id INT, method TEXT, result TEXT, ts TEXT, error TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS attempts_ps ON attempts(person_id, site)"); return c

def record(c, pid, site, method, result, error="", listing_id=None):
    ensure(c).execute("INSERT INTO attempts(person_id,site,listing_id,method,result,ts,error) VALUES(?,?,?,?,?,?,?)",
                      (pid, site, listing_id, method, result, now(), (error or "")[:300])); c.commit()

def for_listing(lid, c=None):
    c = ensure(c); l = c.execute("SELECT person_id, site FROM listings WHERE id=?", (lid,)).fetchone()
    if not l: return []
    return [dict(r) for r in c.execute("SELECT * FROM attempts WHERE person_id=? AND site=? AND (listing_id IS NULL OR listing_id=?) ORDER BY id DESC LIMIT 40", (l["person_id"], l["site"], lid))]

def stealth_on(): return config.get("STEALTH_RETRY", "true") == "true"
def backoff_ms(attempt):
    base = int(os.environ.get("CHAIN_BACKOFF_MS", "20000")); return min(base * (2 ** attempt), 120000) + random.randint(0, 1500)   # nosec B311 (jitter)

STEALTH_JS = """() => {
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
  Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
  window.chrome = window.chrome || {runtime: {}};
  const q = navigator.permissions && navigator.permissions.query;
  if (q) navigator.permissions.query = p => p && p.name === 'notifications' ? Promise.resolve({state: Notification.permission}) : q(p);
  const gp = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (x) { return x === 37445 ? 'Intel Inc.' : x === 37446 ? 'Intel Iris OpenGL Engine' : gp.call(this, x); };
}"""
UAS = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"]

async def stealth_context(b):
    vp = random.choice([(1440, 900), (1536, 864), (1366, 768)])   # nosec B311
    ctx = await b.new_context(user_agent=random.choice(UAS), locale="en-US", timezone_id="America/Los_Angeles", viewport={"width": vp[0], "height": vp[1]},   # nosec B311
                              extra_http_headers={"Accept-Language": "en-US,en;q=0.9", "Upgrade-Insecure-Requests": "1"})
    await ctx.add_init_script(f"({STEALTH_JS})()"); return ctx

async def _solve(page, html):
    """Solve the challenge on the current page and submit it. Scans send nothing to the broker, so this runs in dry run too (budget-capped)."""
    import automation
    tok, why = await asyncio.to_thread(automation.solve, page.url, html, None, False)
    if not tok: return False, why
    try:
        await page.evaluate(automation.INJECT_JS, list(tok))
        btn = await page.query_selector("form button[type=submit], form input[type=submit]")
        if btn: await btn.click()
        else: await page.evaluate("() => { const f = document.querySelector('form'); if (f) f.submit(); }")
        await page.wait_for_timeout(4000)
    except Exception as e: return False, f"{why}; submit failed: {type(e).__name__}"
    return True, why

async def discover(pw, b, page, dpage, c, p, site):
    """Run the discovery chain for one site. Returns the number of matches. A site still blocked at the end gets a
    manual_check row, which the removal side of the chain (blind) and Coverage pick up; it is not sent to Needs you."""
    import scanner, automation
    n = await scanner.scan_person(page, c, p, site, dpage, chain=True); r = dict(scanner.LAST)
    if r.get("result") == "skipped": return n
    record(c, p["id"], site["id"], "scan", r["result"], r.get("detail", ""))
    if r["result"] not in BLOCKED: return n
    # 1. CAPTCHA solver on the challenge page
    if r["result"] == "captcha" and automation.solver_on() and automation.detect(r.get("html")):
        ok, why = await _solve(page, r["html"])
        if ok:
            n = await scanner.scan_person(page, c, p, site, dpage, chain=True); r = dict(scanner.LAST)
            record(c, p["id"], site["id"], "solver", "captcha_solved" if r["result"] not in BLOCKED else "captcha_failed", why)
            if r["result"] not in BLOCKED: return n
        else: record(c, p["id"], site["id"], "solver", "captcha_failed", why)
    elif r["result"] == "captcha": record(c, p["id"], site["id"], "solver", "skipped", "CAPTCHA solver is off" if not automation.solver_on() else "no solvable widget on the page")
    # 2. stealth profile after a backoff
    if stealth_on():
        await page.wait_for_timeout(backoff_ms(0))
        ctx = await stealth_context(b)
        try:
            sp = await ctx.new_page(); n = await scanner.scan_person(sp, c, p, site, None, chain=True); r = dict(scanner.LAST)
            if r["result"] == "captcha" and automation.solver_on() and automation.detect(r.get("html")):
                ok, why = await _solve(sp, r["html"])
                if ok: n = await scanner.scan_person(sp, c, p, site, None, chain=True); r = dict(scanner.LAST)
            record(c, p["id"], site["id"], "stealth", r["result"], r.get("detail", ""))
        except Exception as e: record(c, p["id"], site["id"], "stealth", "error", type(e).__name__); r = {"result": "error"}
        finally: await ctx.close()
        if r["result"] not in BLOCKED + ("error",): return n
    # 3. residential proxy (only if configured)
    if scanner.proxy_opts():
        await page.wait_for_timeout(backoff_ms(1))
        try:
            pb = await pw.chromium.launch(**scanner.launch_kw(proxy=True))
            try:
                ctx = await stealth_context(pb); sp = await ctx.new_page()
                n = await scanner.scan_person(sp, c, p, site, None, chain=True); r = dict(scanner.LAST)
            finally: await pb.close()
            record(c, p["id"], site["id"], "proxy", r["result"], r.get("detail", ""))
        except Exception as e: record(c, p["id"], site["id"], "proxy", "error", type(e).__name__); r = {"result": "error"}
        if r["result"] not in BLOCKED + ("error",): return n
    else: record(c, p["id"], site["id"], "proxy", "skipped", "PROXY_URL not set")
    scanner.manual_check(c, p, site, r.get("url") or f"https://{site.get('domain', '')}/", r.get("detail") or "blocked"); c.commit()
    return n

# ---------- removal ----------
def registry_contact(site):
    """(email, url) for this broker from the state data-broker registries, matched by domain."""
    d = (site.get("domain") or "").lower().removeprefix("www.")
    if not d: return None, None
    try:
        import registries
        for b in registries.brokers():
            if (b.get("domain") or "").lower().removeprefix("www.") == d: return b.get("email"), b.get("optout_url")
    except Exception: pass
    return None, None

def plan(site):
    """Every official removal channel for this site, in chain order: [(method, kind, target)]. kind: form|captcha_form|parent|email|registry_email|registry_form."""
    import remover
    out, seen = [], set()
    for m, t in remover.channels(site):
        meth = "email" if m == "email" else "form"
        out.append((meth, m, t)); seen.add(t)
    out.sort(key=lambda x: REMOVAL.index(x[0]))
    re_, ru = registry_contact(site)
    if re_ and re_ not in seen: out.append(("registry", "registry_email", re_))
    elif ru and ru not in seen: out.append(("registry", "registry_form", ru))
    return out

def human_only(site):
    oo = site.get("optout") if isinstance(site.get("optout"), dict) else {}
    return oo.get("requires") if oo.get("requires") in ("id", "phone") else None

def run_dry(c, p, l, site, method_hint=None):
    """DRY_RUN: walk the chain and stop at the first channel that would go out; nothing leaves the server.
    Returns (state, detail, method)."""
    import remover, automation
    for meth, kind, target in plan(site):
        if kind == "captcha_form" and not automation.solver_on():
            record(c, p["id"], site["id"], meth, "skipped", "CAPTCHA form and the solver is off", l["id"]); continue
        if kind in ("email", "registry_email"):
            st = remover.send_request(c, p, site, target, l["id"]); res = "dry_run" if st == "dry_run" else ("sent" if st == "sent" else "failed")
            record(c, p["id"], site["id"], meth, res, "" if res != "failed" else st, l["id"])
            if res != "failed": return res, f"{LABEL[meth]}: {target}", meth
            continue
        label = {"parent": "Opt-out web form (parent suppression)", "captcha_form": "Opt-out web form (CAPTCHA solver)", "registry_form": "Registry opt-out form"}.get(kind, "Opt-out web form")
        remover._record(c, p, target, label, f"DRY_RUN: would open {target}, fill it with this person's identifiers{' and solve the CAPTCHA' if kind == 'captcha_form' else ''} and submit it.", "dry_run", l["id"])
        record(c, p["id"], site["id"], meth, "dry_run", "", l["id"]); return "dry_run", f"{label}: {target}", meth
    return None, "no official channel left", None

async def _live(p, l, site, steps):
    """Live chain (DRY_RUN off): forms are filled and submitted for real; falls through to the next channel on failure."""
    import remover, formfill, scanner, profiles, automation
    from playwright.async_api import async_playwright
    c = conn()
    for meth, kind, target in steps:
        if kind in ("email", "registry_email"):
            st = remover.send_request(c, p, site, target, l["id"])
            ok = st in ("sent", "dry_run"); record(c, p["id"], site["id"], meth, "sent" if ok else "failed", "" if ok else st, l["id"])
            if ok: return "submitted", meth
            continue
        if kind == "captcha_form" and not automation.solver_on():
            record(c, p["id"], site["id"], meth, "skipped", "CAPTCHA form and the solver is off", l["id"]); continue
        st = "error"; detail = ""
        for attempt in range(2):   # direct, then stealth after a backoff (plus proxy when set)
            try:
                async with async_playwright() as pw:
                    b = await pw.chromium.launch(**scanner.launch_kw(proxy=attempt == 1))
                    try:
                        ctx = await (scanner.new_context(b) if attempt == 0 else stealth_context(b)); page = await ctx.new_page()
                        st, detail = await formfill.fill(page, target, formfill.values(p, profiles.get(p), l["url"]), os.path.join(scanner.SHOTS, f"form_{l['id']}.png"),
                                                         dry=False, solver=automation.solver_on(), listing_id=l["id"])
                    finally: await b.close()
            except Exception as e: st, detail = "error", type(e).__name__
            record(c, p["id"], site["id"], meth, "submitted" if st == "submitted" else ("captcha" if "CAPTCHA" in (detail or "") else "failed"), "" if st == "submitted" else detail, l["id"])
            if st == "submitted": remover._record(c, p, target, "Opt-out web form", detail, "submitted", l["id"]); return "submitted", meth
            if attempt == 0: await asyncio.sleep(backoff_ms(0) / 1000)
    return "needs_human", None

def run_live(listing_id):
    import remover
    c = conn(); l = c.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone(); p = c.execute("SELECT * FROM people WHERE id=?", (l["person_id"],)).fetchone()
    site = remover.SITE.get(l["site"], {"id": l["site"], "name": l["site"]})
    try: new, meth = asyncio.run(_live(p, l, site, plan(site)))
    except Exception as e: new, meth = "approved", None; event(c, "chain_error", type(e).__name__, p["id"], listing_id)
    if new == "needs_human" and not human_only(site):
        new = "approved"; event(c, "chain_exhausted", "every official channel failed; retried on the next run", p["id"], listing_id)   # robots keep trying; only real human-only steps reach Needs you
    c.execute("UPDATE listings SET status=?, submitted_at=? WHERE id=?", (new, now(), listing_id)); event(c, "chain_" + new, LABEL.get(meth, "") if meth else "", p["id"], listing_id); c.commit()

def blind(c, p, l, site):
    """Blocked site: official opt-out without a confirmed listing. Governed by BLIND_REQUESTS (off by default)."""
    import remover
    if config.get("BLIND_REQUESTS", "false") != "true":
        last = ensure(c).execute("SELECT ts FROM attempts WHERE person_id=? AND site=? AND method='blind' ORDER BY id DESC LIMIT 1", (p["id"], site["id"])).fetchone()
        if not last or last["ts"][:10] != now()[:10]: record(c, p["id"], site["id"], "blind", "skipped", "blind opt-outs are off (Heartbeat > Settings > Removals)", l["id"])
        return None
    if not remover.can_send(p)[0]: return None
    steps = plan(site)
    if not steps: record(c, p["id"], site["id"], "blind", "failed", "no official channel", l["id"]); return None
    if remover.DRY:
        st, detail, meth = run_dry(c, p, l, site)
    else:
        meth, kind, target = next(((m, k, t) for m, k, t in steps if k in ("email", "registry_email")), steps[0])
        if kind in ("email", "registry_email"): s = remover.send_request(c, p, site, target, l["id"]); st = "sent" if s in ("sent", "dry_run") else None; detail = target
        else: st = None; detail = "form"
        if st is None:
            import threading; threading.Thread(target=run_live, args=(l["id"],), daemon=True).start(); st = "started"
    if st:
        record(c, p["id"], site["id"], "blind", st if st != "started" else "sent", detail, l["id"])
        c.execute("UPDATE listings SET status='submitted', submitted_at=? WHERE id=?", (now(), l["id"]))
        event(c, "blind_request", f"site blocks scans; {detail} ({st})", p["id"], l["id"]); c.commit()
    return st

# ---------- coverage ----------
RANK = {"confirmed": 9, "sent": 8, "submitted": 8, "dry_run": 7, "found": 6, "not_found": 6, "captcha_solved": 6, "skipped": 1, "captcha": 2, "blocked": 2, "error": 1, "failed": 1, "captcha_failed": 2}

def coverage(person_ids=None, c=None):
    """Per broker in the roster: best method reached and its result; plus totals. Read-only."""
    import scanner
    c = ensure(c); people = person_ids or [r["id"] for r in c.execute("SELECT id FROM people WHERE enabled=1 AND (esign_at IS NOT NULL OR auth_pdf IS NOT NULL)")]
    rows = []
    q = ",".join("?" * len(people)) or "''"
    att = {}
    for a in c.execute(f"SELECT * FROM attempts WHERE person_id IN ({q}) ORDER BY id", people):   # nosec B608 (placeholders)
        att.setdefault(a["site"], []).append(a)
    lst = {}
    for l in c.execute(f"SELECT site, status FROM listings WHERE person_id IN ({q})", people):   # nosec B608
        lst.setdefault(l["site"], []).append(l["status"])
    for s in scanner.SITES:
        if s.get("disabled") or not s.get("search_url") and not (s.get("optout") or {}): continue
        a = att.get(s["id"], []); st = lst.get(s["id"], [])
        best = max(a, key=lambda x: (RANK.get(x["result"], 0), x["id"]), default=None)
        method, result = (best["method"], best["result"]) if best else (None, "not_tried")
        if "confirmed_removed" in st: method, result = method or "scan", "confirmed"
        solved = any(x["method"] == "solver" and x["result"] == "captcha_solved" for x in a)
        direct_ok = any(x["method"] == "scan" and x["result"] in ("found", "not_found") for x in a)
        blocked = bool(a) and not any(x["method"] in DISCOVERY and x["result"] in ("found", "not_found", "captcha_solved") for x in a)
        email_only = blocked and any(k in ("email", "registry_email") for _, k, _ in plan(s))
        rows.append({"site": s["id"], "name": s.get("name", s["id"]), "method": method, "label": LABEL.get(method, "Not tried yet"), "result": result,
                     "covered": result in OK, "direct": direct_ok, "solved": solved, "blocked": blocked, "email_only": email_only,
                     "captcha": any(x["result"] in ("captcha", "captcha_failed") for x in a), "tries": len(a),
                     "last": a[-1]["ts"] if a else "", "error": next((x["error"] for x in reversed(a) if x["error"]), "")})
    n = len(rows) or 1
    tot = {"brokers": len(rows), "covered": sum(r["covered"] for r in rows), "working": sum(r["direct"] for r in rows),
           "blocked": sum(r["blocked"] for r in rows), "captcha_solved": sum(r["solved"] for r in rows), "email_only": sum(r["email_only"] for r in rows),
           "captcha_seen": sum(r["captcha"] for r in rows), "not_tried": sum(r["result"] == "not_tried" for r in rows)}
    tot["pct"] = round(100 * tot["covered"] / n)
    return rows, tot

if __name__ == "__main__":   # python3 chain.py report [person_id]: totals only, no names or addresses
    import sys, json
    pid = sys.argv[2] if len(sys.argv) > 2 else None
    rows, tot = coverage([pid] if pid else None)
    best = {}
    for r in rows: best[r["label"]] = best.get(r["label"], 0) + 1
    print(json.dumps({"totals": tot, "best_method": best, "results": {k: sum(r["result"] == k for r in rows) for k in sorted({r["result"] for r in rows})}}, indent=1))
