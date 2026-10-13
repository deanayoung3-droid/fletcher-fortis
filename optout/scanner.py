"""Headless scan of people-search sites. Respects 'not_me', applies age/city filters, screenshots evidence."""
import re, os, json, yaml, asyncio, datetime
from db import conn, now, event
from crypto import enc
import profiles
SITES = yaml.safe_load(open(os.environ.get("SITES_FILE") or os.path.join(os.path.dirname(__file__), "sites.yaml")))["sites"]
HEALTH_FILE = os.environ.get("HEALTH_FILE", "/data/site_health.json")
NOT_FOUND = re.compile(r"no (?:results|records|matches|people|one) (?:found|were found|matched)|we (?:could not|couldn.t) find|0 (?:results|records|people) found|did not match any|no exact matches|nothing found|page you requested could not be found|no one by that name", re.I)
SUMMARY = re.compile(r"\b(?:records?|results?|matches|people|profiles?) (?:for|named|found)\b|we (?:found|uncovered)|search results|searching for|sample report|showing \d", re.I)
STATE_RE = re.compile(r",\s?[A-Z]{2}\b|\b(?:CA|California)\b")

def proxy_opts():
    """Optional bring-your-own residential proxy (off by default). PROXY_URL=http://user:pass@host:port"""
    u = os.environ.get("PROXY_URL", "").strip()
    if not u: return None
    from urllib.parse import urlparse
    x = urlparse(u); o = {"server": f"{x.scheme}://{x.hostname}:{x.port}"}
    if x.username: o.update(username=x.username, password=x.password or "")
    return o
def launch_kw():
    kw = {"headless": True, "args": LAUNCH_ARGS}
    if proxy_opts(): kw["proxy"] = proxy_opts()
    return kw

GENERIC_JS = r"""([first, last]) => {
  const wr = w => new RegExp('\\b' + w.replace(/[^A-Za-z' -]/g, '') + '\\b', 'i');
  const F = wr(first), L = wr(last), AGE = /\b(?:age|aged)\s*:?\s*\d{1,3}\b|\b\d{1,3}\s*(?:years old|yrs)\b/i, LOC = /,\s?[A-Z]{2}\b/;
  const out = [], seen = new Set();
  const all = [...document.body.querySelectorAll('h1,h2,h3,h4,h5,a,strong,b,span,div,p,td,li')];
  for (const el of all) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 80 || !(F.test(t) && L.test(t))) continue;
    if ([...el.children].some(c => F.test(c.innerText || '') && L.test(c.innerText || ''))) continue;  // innermost name node
    let card = el;
    for (let i = 0; i < 8 && card.parentElement; i++) {
      const tt = card.innerText || '';
      if ((AGE.test(tt) || LOC.test(tt)) && tt.length > 25) break;
      if ((card.parentElement.innerText || '').length > 1500) break;
      card = card.parentElement;
    }
    if (seen.has(card)) continue; seen.add(card);
    const txt = (card.innerText || '').slice(0, 1200);
    if (!(AGE.test(txt) || LOC.test(txt))) continue;
    const a = (el.closest('a[href]') || card.querySelector('a[href]'));
    out.push({text: txt, head: t, href: a ? a.href : null});
    if (out.length >= 40) break;
  }
  return out;
}"""
async def generic_cards(page, first, last):
    """Site-independent fallback: innermost elements naming the person, widened to a card that shows an age or 'City, ST'."""
    try: raw = await page.evaluate(GENERIC_JS, [first, last])
    except Exception: return []
    out = []
    for h in raw:
        head = SUBJECT_CUT.split(h["head"].strip(), 1)[0].strip()
        # the subject must START with the first name ("Gregory Alex Smith" is not Alex Smith) and the card must not be a page summary
        if not re.match(rf"{re.escape(first)}\b", head, re.I) or not name_in(first, last, head) or len(head) > 40: continue
        if NOT_FOUND.search(h["text"]) or SUMMARY.search(h["text"][:200]): continue
        if re.fullmatch(rf"\s*{re.escape(first)}\b.*?\b{re.escape(last)}\s+(?:in|near|from)\s+[^\n]{{0,40}}", h["text"].split("\n")[0], re.I) and len(h["text"]) < 60: continue  # the site echoing our own query
        h["head"] = head; out.append(h)
    return out

def record_health(site_id, status, evidence):
    """Per-site health check: works | blocked | unreachable_dc | dead | broken. A definition that loads but yields neither cards nor a not-found signal is 'broken'."""
    try: h = json.load(open(HEALTH_FILE))
    except Exception: h = {}
    h[site_id] = {"status": status, "evidence": evidence[:200], "checked": now()}
    try:
        os.makedirs(os.path.dirname(HEALTH_FILE), exist_ok=True); json.dump(h, open(HEALTH_FILE, "w"), indent=1)
    except Exception: pass
SHOTS = os.environ.get("SHOT_DIR", "/data/shots")
AGE_RE = re.compile(r"\b(?:Age|age)[:\s]*(\d{1,3})\b")

def slug(s): return re.sub(r"\s+", "-", (s or "").strip())
from urllib.parse import quote
UA = os.environ.get("SCAN_UA") or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
CHALLENGE = ("just a moment", "attention required", "checking your browser", "verify you are human", "are you a robot", "access denied", "press & hold", "security check")

async def new_context(b):
    """A normal desktop browser profile: real UA, locale, timezone, viewport. No CAPTCHA solving, no fingerprint spoofing beyond hiding the automation flag."""
    ctx = await b.new_context(user_agent=UA, locale="en-US", timezone_id="America/Los_Angeles", viewport={"width": 1366, "height": 900},
                              extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
    await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return ctx
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

def search_url(site, first, last, city, state):
    return site["search_url"].format(first=slug(first), last=slug(last), city=slug(city), state=state,
                                     first_q=quote(first), last_q=quote(last), city_q=quote(city))

SUBJECT_CUT = re.compile(r"\s(?:age|aged|in|lives|living|relatives|related|used|also|phone|address)\b|[·|,(:]", re.I)
def name_in(first, last, head):
    h = head.lower(); return bool(re.search(rf"\b{re.escape(first.lower())}\b", h) and re.search(rf"\b{re.escape(last.lower())}\b", h))

def manual_check(c, p, site, url, why):
    """Hard-blocked site: queue a human check with the exact search URL. Re-asks at most every 28 days."""
    row = c.execute("SELECT * FROM listings WHERE person_id=? AND site=? AND url=?", (p["id"], site["id"], enc(url))).fetchone()
    snip = f"{site['name']} blocked the automated search ({why}). Open the search, look for {p['full_name']} in {p['city']}, and add the listing URL if you find one."
    if not row:
        c.execute("INSERT INTO listings(person_id,site,url,fields,status,first_seen,last_seen,last_checked) VALUES(?,?,?,?,?,?,?,?)",
                  (p["id"], site["id"], enc(url), enc(json.dumps({"snippet": snip, "manual_search": True})), "manual_check", now(), now(), now()))
        import automation; automation.schedule(c, c.execute("SELECT last_insert_rowid()").fetchone()[0])
    elif row["status"] == "checked_clear" and (row["last_checked"] or "") < (datetime.datetime.now() - datetime.timedelta(days=28)).isoformat():
        c.execute("UPDATE listings SET status='manual_check', last_checked=? WHERE id=?", (now(), row["id"]))
        import automation; automation.schedule(c, row["id"])
PHONE_RE = re.compile(r"\(?\b([2-9]\d{2})\)?[-. ]?(\d{3})[-. ]?(\d{4})\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
REL_HDR = re.compile(r"(?:relatives?|related to|family members?|associates?|may be related|possible relatives?)\s*:?", re.I)
NEXT_JS = r"""() => { const h = location.host;
  const a = document.querySelector('a[rel=next]') || [...document.querySelectorAll('a[href]')].find(a => /^\s*(next|next page|›|»|>)\s*$/i.test(a.innerText||'') || /next/i.test(a.getAttribute('aria-label')||''));
  try { return a && new URL(a.href).host === h ? a.href : null } catch(e) { return null } }"""
def MAXQ(): return int(os.environ.get("SCAN_MAX_QUERIES_PER_SITE", "6"))
def MAXP(): return int(os.environ.get("SCAN_MAX_PAGES", "2"))
def MAXD(): return int(os.environ.get("SCAN_MAX_DETAILS", "3"))
def DELAY(): return int(os.environ.get("SCAN_DELAY_MS", "8000"))

def queries(site, prof):
    """Every name variant x every current/past city, de-duplicated (templates without {city} collapse), capped per site."""
    out = []
    for f, l in profiles.name_pairs(prof):
        for city, st in (profiles.places(prof) or [("", "CA")]):
            try: u = search_url(site, f, l, city, st)
            except (KeyError, IndexError): return []   # phone/email-only template: not a name search
            if u not in [q[0] for q in out]: out.append((u, f, l))
    return out[:MAXQ()]

def plausible(prof, text):
    """Keep a hit only if it mentions a current/past city (state must agree when shown) and an age in range (if any age is shown)."""
    def city_hit(cn, state):
        for m in re.finditer(rf"\b{re.escape(cn.strip())}\b,?\s*([A-Z]{{2}}\b|[A-Z][a-z]+)?", text, re.I):
            stt = m.group(1)
            if not stt or len(stt) != 2 or not stt.isupper() or stt == (state or "").upper(): return True
        return False
    if not any(city_hit(cn, st) for cn, st in profiles.places(prof) if cn.strip()): return False, None
    ages = [int(a) for a in AGE_RE.findall(text)]; lo, hi = profiles.age_range(prof)
    if ages and lo is not None and hi is not None and not any(lo <= a <= hi for a in ages): return False, ages[0]
    return True, (ages[0] if ages else None)

def evidence(prof, text):
    """Which of the person's identifiers appear on the listing (card + detail page)."""
    phones = {"".join(m) for m in PHONE_RE.findall(text)}; emails = {e.lower() for e in EMAIL_RE.findall(text)}; low = text.lower()
    ev = {"phones": [profiles.fmt_phone(p) for p in prof["phones"] if p in phones], "emails": [e for e in prof["emails"] if e.lower() in emails],
          "relatives": [r for r in prof["relatives"] if re.search(r"\b" + r"\b.*?\b".join(map(re.escape, r.lower().split())) + r"\b", low)],
          "streets": [a["street"] for a in prof["addresses"] if a.get("street") and re.sub(r"\s+", " ", a["street"].lower()) in re.sub(r"\s+", " ", low)],
          "cities": [cn for cn, _ in profiles.places(prof) if re.search(rf"\b{re.escape(cn.lower())}\b", low)]}
    m = REL_HDR.search(text)
    if m: ev["listed_relatives"] = [x.strip() for x in re.split(r"[,\n•|]", text[m.end():m.end() + 300]) if 3 < len(x.strip()) < 40][:8]
    return ev

def score(ev, age):
    return min(100, 40 + 10 * min(2, len(ev["cities"])) + (10 if age is not None else 0) + 15 * len(ev["phones"]) + 15 * len(ev["emails"]) + 8 * len(ev["relatives"]) + 15 * len(ev["streets"]))

def dedupe_key(site, head, age, ev):
    """Same person across sister sites (same parent company): parent + normalized name + age + first matched city."""
    import hashlib
    k = "|".join([site.get("parent") or site["id"], re.sub(r"[^a-z ]", "", head.lower()).strip(), str(age or ""), (ev["cities"] or [""])[0].lower()])
    return hashlib.sha256(k.encode()).hexdigest()[:16]

async def _goto(page, url):
    last = None
    for attempt in (1, 2):
        try:
            r = await page.goto(url, timeout=45000, wait_until="domcontentloaded"); await page.wait_for_timeout(min(2500, DELAY())); return r, None
        except Exception as e:
            last = str(e).splitlines()[0][:160]
            if attempt == 1: await page.wait_for_timeout(min(6000, DELAY()))
    return None, last

def _abs(page_url, href):
    from urllib.parse import urljoin
    return urljoin(page_url, href) if href else None

async def _cards(page, site, pairs):
    """Result cards on the current page: (head, text, href, element-or-None)."""
    ex = site.get("extractor") or "generic"; card_sel = site.get("card") or (ex.get("card") if isinstance(ex, dict) else None)
    name_sel = site.get("name_sel") or (ex.get("name_sel") if isinstance(ex, dict) else None); out = []
    if card_sel:
        for card in await page.query_selector_all(card_sel):
            text = (await card.inner_text())[:2000]
            head = SUBJECT_CUT.split(next((x for x in text.split("\n") if x.strip()), ""), 1)[0]
            if name_sel:
                h = await card.query_selector(name_sel); head = (await h.inner_text()) if h else head
            href = None
            for a in await card.query_selector_all("a[href]"):   # the subject's profile link, not a relative's
                t = (await a.inner_text()).lower()
                if any(name_in(f, l, t) for f, l in pairs) or "detail" in t or "view" in t: href = _abs(page.url, await a.get_attribute("href")); break
            out.append((head, text, href, card))
        return out, True
    seen = set()
    for f, l in pairs:
        for g in await generic_cards(page, f, l):
            if g["text"] in seen: continue
            seen.add(g["text"]); out.append((g["head"], g["text"], g["href"], None))
    return out, False

async def scan_person(page, c, p, site, dpage=None):
    """Deep scan of one site for one person. Returns the number of matches; logs a scan_result event."""
    prof = profiles.get(p); pairs = profiles.name_pairs(prof)
    hs = (site.get("health") or {}).get("status")
    if not site.get("search_url") and hs == "blocked" and site.get("domain") and not site.get("disabled"):
        manual_check(c, p, site, f"https://{site['domain']}/", "home page bot check"); c.commit(); return 0
    if not site.get("search_url") or site.get("disabled") or hs == "dead" or not pairs: return 0
    qs = queries(site, prof)
    if not qs: return 0
    stat = {"queries": 0, "pages": 0, "cards": 0, "name": 0, "city_reject": 0, "age_reject": [], "match": 0, "details": 0}
    n = 0; seen_urls = set(); details = 0
    for qi, (url, qf, ql) in enumerate(qs):
        if qi: await page.wait_for_timeout(DELAY())
        resp, err = await _goto(page, url)
        if err:
            if qi == 0:
                record_health(site["id"], "dead" if "NAME_NOT_RESOLVED" in err else "unreachable_dc", err)
                event(c, "scan_error", f"{site['id']}: {err}", p["id"]); c.commit(); return 0
            continue
        stat["queries"] += 1
        for pg in range(MAXP()):
            if pg:
                nxt = await page.evaluate(NEXT_JS)
                if not nxt: break
                await page.wait_for_timeout(DELAY()); resp, err = await _goto(page, nxt)
                if err: break
            stat["pages"] += 1
            status = resp.status if resp else 0; title = (await page.title()).lower()
            cards, css = await _cards(page, site, pairs)
            if not cards and (status in (401, 403, 429, 503) or any(k in title for k in CHALLENGE)):
                record_health(site["id"], "blocked", f"HTTP {status} {title[:60]}")
                event(c, "scan_blocked", f"{site['id']} HTTP {status} '{title[:40]}' -> needs human", p["id"])
                manual_check(c, p, site, page.url, f"HTTP {status}, {title[:30] or 'bot check'}"); c.commit(); return n
            if not cards and qi == 0 and pg == 0:
                body = (await page.inner_text("body"))[:20000] if status else ""
                if NOT_FOUND.search(body) or NOT_FOUND.search(title) or status == 404: record_health(site["id"], "works", f"HTTP {status} not-found signal")
                else: record_health(site["id"], "broken", f"HTTP {status}: no cards and no not-found signal ('{title[:50]}')")
            elif cards and qi == 0 and pg == 0: record_health(site["id"], "works", f"HTTP {status}, {len(cards)} {'cards' if css else 'generic hits'}")
            stat["cards"] += len(cards)
            for head, text, href, el in cards:
                hit = next(((f, l) for f, l in pairs if name_in(f, l, head) and re.match(rf"\s*{re.escape(f)}\b", head, re.I)), None)
                if not hit: continue
                stat["name"] += 1
                ok, age = plausible(prof, text)
                if not ok:
                    if age is not None: stat["age_reject"].append(age)
                    else: stat["city_reject"] += 1
                    continue
                lurl = href or page.url
                if lurl in seen_urls: continue
                seen_urls.add(lurl)
                row = c.execute("SELECT * FROM listings WHERE person_id=? AND site=? AND url=?", (p["id"], site["id"], enc(lurl))).fetchone()
                if row and row["status"] == "not_me": continue
                full_text = text; dtext = ""
                if href and dpage is not None and details < MAXD() and href != page.url and same_site(href, page.url, site):   # follow into the profile detail page (same site only)
                    details += 1; await page.wait_for_timeout(min(DELAY(), 4000)); r2, e2 = await _goto(dpage, href)
                    if not e2 and r2 and r2.status < 400:
                        dtext = (await dpage.inner_text("body"))[:30000]; full_text += "\n" + dtext; stat["details"] += 1
                        ok2, age2 = plausible(prof, dtext)
                        if age2 is not None and not ok2 and AGE_RE.search(dtext):
                            lo, hi = profiles.age_range(prof)
                            if lo is not None and not (lo <= age2 <= hi): stat["age_reject"].append(age2); continue
                        age = age if age is not None else age2
                ev = evidence(prof, full_text); sc = score(ev, age); dk = dedupe_key(site, head, age, ev)
                os.makedirs(SHOTS, exist_ok=True); shot = os.path.join(SHOTS, f"{p['id']}_{site['id']}_{hashlib_short(lurl)}.png")
                if el is not None:
                    await el.evaluate("e => { e.style.outline = '3px solid #ef4444'; e.style.outlineOffset = '3px'; }")
                    box = await el.evaluate("e => { const r = e.getBoundingClientRect(); return {y: r.y + window.scrollY, h: r.height, H: document.documentElement.scrollHeight, W: document.documentElement.scrollWidth}; }")
                    y = max(0, box["y"] - 80); h = max(200, min(520, box["h"] + 160)); h = min(h, max(1, box["H"] - y))
                    await page.screenshot(path=shot, full_page=True, clip={"x": 0, "y": y, "width": min(1100, box["W"]), "height": h})
                else: await page.screenshot(path=shot)
                fields = enc(json.dumps({"age": age, "snippet": text[:400], "name": head, "query": f"{qf} {ql}", "evidence": ev, "detail": bool(dtext), "extractor": "css" if css else "generic"}))
                if row:
                    st = row["status"]
                    if st == "confirmed_removed": st = "reappeared"; event(c, "ALERT_reappeared", f"{site['name']} {lurl}", p["id"], row["id"])
                    c.execute("UPDATE listings SET last_seen=?,last_checked=?,screenshot=?,fields=?,status=?,dedupe=?,score=? WHERE id=?", (now(), now(), shot, fields, st, dk, sc, row["id"]))
                else:
                    c.execute("INSERT INTO listings(person_id,site,url,fields,screenshot,first_seen,last_seen,last_checked,dedupe,score) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (p["id"], site["id"], enc(lurl), fields, shot, now(), now(), now(), dk, sc))
                    event(c, "new_candidate", f"{site['name']} (match score {sc})", p["id"])
                n += 1
    stat["match"] = n
    event(c, "scan_result", f"{site['id']}: {stat['queries']} searches, {stat['pages']} pages, {stat['details']} detail pages, {stat['cards']} cards, {stat['name']} name hits, {n} match, out-of-range ages {stat['age_reject'][:8]}, wrong city {stat['city_reject']}", p["id"])
    c.commit(); return n

def same_site(href, page_url, site):
    """Only follow http(s) links on the broker's own host/domain: a hostile page must not steer the scanner to LAN or other hosts."""
    from urllib.parse import urlsplit
    h = urlsplit(href); base = (urlsplit(page_url).hostname or "").removeprefix("www."); dom = (site.get("domain") or base).removeprefix("www.")
    host = (h.hostname or "").removeprefix("www.")
    return h.scheme in ("http", "https") and bool(host) and (host == base or host == dom or host.endswith("." + dom))

def hashlib_short(s):
    import hashlib; return hashlib.sha256(s.encode()).hexdigest()[:16]

async def recheck(page, c):
    """Re-scan submitted/pending listings; mark confirmed_removed only when the page no longer shows the person."""
    for l in c.execute("SELECT l.*, p.first, p.last FROM listings l JOIN people p ON p.id=l.person_id WHERE status IN ('submitted','pending')").fetchall():
        try:
            r = await page.goto(l["url"], timeout=45000); txt = (await page.content()).lower()
        except Exception as e:
            event(c, "recheck_error", str(e), l["person_id"], l["id"]); continue
        ttl = (await page.title()).lower()
        if any(k in ttl for k in CHALLENGE) or (r and r.status in (403, 429, 503)): continue  # blocked: unknown, keep status
        gone = (r and r.status in (404, 410)) or not (l["first"].split()[0].lower() in txt and l["last"].lower() in txt)
        c.execute("UPDATE listings SET last_checked=? WHERE id=?", (now(), l["id"]))
        if gone:
            c.execute("UPDATE listings SET status='confirmed_removed', confirmed_at=? WHERE id=?", (now(), l["id"]))
            event(c, "confirmed_removed", l["url"], l["person_id"], l["id"])
    c.commit()

async def run_all(only_person=None):
    from playwright.async_api import async_playwright
    c = conn()
    q = "SELECT * FROM people WHERE enabled=1" + (" AND id=?" if only_person else "")   # nosec B608
    people = c.execute(q, ((only_person,) if only_person else ())).fetchall()
    async with async_playwright() as pw:
        b = await pw.chromium.launch(**launch_kw()); ctx = await new_context(b); page = await ctx.new_page(); dpage = await ctx.new_page()
        for p in people:
            for s in SITES:
                try: await scan_person(page, c, p, s, dpage)
                except Exception as e: event(c, "scan_error", f"{s['id']}: {type(e).__name__}", p["id"])
                await page.wait_for_timeout(DELAY())
        await recheck(page, c); await b.close()
    event(c, "scan_complete", f"{len(people)} people")
if __name__ == "__main__": asyncio.run(run_all())
