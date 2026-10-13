"""READ-ONLY health probe of every site in sites.yaml for ONE person (default alex).
Per site: <=3 page loads (homepage, search, maybe 1 URL-pattern guess), >=4 s apart, 6 different sites in parallel.
No clicks, no form submits (search templates are read from GET <form> markup and loaded as plain URLs), no CAPTCHA solving, no emails.
Groups: works | blocked | unreachable_dc | dead | no_search | broken. Writes /tmp/probe_all/results.json (+ screenshots)."""
import asyncio, os, sys, json, re, time, datetime
from urllib.parse import urlparse, urlencode, urljoin
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "optout"))
os.environ.setdefault("DB_PATH", "/tmp/probe_all/probe.db"); os.environ.setdefault("HEALTH_FILE", "/tmp/probe_all/site_health.json")
import db, profiles, scanner
from playwright.async_api import async_playwright
OUT = "/tmp/probe_all"; os.makedirs(OUT, exist_ok=True)
PID = sys.argv[1] if len(sys.argv) > 1 else "alex"; ONLY = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else None
PAR = int(os.environ.get("PROBE_PARALLEL", "6")); GAP = float(os.environ.get("PROBE_GAP_S", "4"))
PARKED = re.compile(r"domain (?:is )?for sale|buy this domain|parked|this domain|hugedomains|dan\.com|sedo|godaddy|account suspended|site not found|default web page|coming soon|under construction|court order", re.I)
UNREACH = ("ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_REFUSED", "ERR_TIMED_OUT", "Timeout", "ERR_EMPTY_RESPONSE", "ERR_HTTP2", "ERR_SSL", "ERR_CERT", "ERR_ADDRESS_UNREACHABLE", "ERR_TUNNEL", "ERR_QUIC")
FIELD = [(re.compile(r"^(?:fn|first|fname|first_?name|firstname|f)$", re.I), "{first_q}"), (re.compile(r"^(?:ln|last|lname|last_?name|lastname|l)$", re.I), "{last_q}"),
         (re.compile(r"citystatezip|location|where|loc|csz", re.I), "{city_q}%2C%20{state}"), (re.compile(r"^city$", re.I), "{city_q}"), (re.compile(r"^(?:state|st)$", re.I), "{state}"),
         (re.compile(r"^(?:q|name|full_?name|fullname|search|query|who|keyword|s|term|n)$", re.I), "{first_q}%20{last_q}")]
FORMS_JS = """() => [...document.forms].map(f => ({action: f.getAttribute('action') || '', method: (f.getAttribute('method') || 'get').toLowerCase(),
  inputs: [...f.querySelectorAll('input,select')].map(i => ({name: i.name, type: (i.type || 'text').toLowerCase(), value: i.value, ph: i.placeholder || '', tag: i.tagName}))}))"""
FUNNEL = {"truthfinder": "peopleconnect", "instantcheckmate": "peopleconnect", "intelius": "peopleconnect", "beenverified": "beenverified", "peoplelooker": "beenverified", "infotracer": "infotracer", "peoplefinders": "peoplefinders", "spokeo": "spokeo", "peoplewhiz": "peoplewhiz"}
PATTERNS = ["/name/{first}-{last}", "/people/{first}-{last}", "/{first}-{last}", "/search?name={first_q}%20{last_q}"]
def mk(s, p, tmpl): return scanner.search_url({"search_url": tmpl}, p)
def template_from_forms(base, forms):
    best = None
    for f in forms:
        if f["method"] != "get": continue
        q, hasname = [], False
        for i in f["inputs"]:
            if not i["name"] or i["type"] in ("submit", "button", "checkbox", "radio", "password", "email"): continue
            v = None
            for rx, ph in FIELD:
                if rx.search(i["name"]) or (i["type"] == "text" and rx.search(re.sub(r"\W", "", i["ph"].lower()) or "_")): v = ph; break
            if v: hasname |= "first" in v
            elif i["type"] == "hidden": v = re.sub(r"[{}]", "", i["value"] or "")
            else: continue
            q.append(f"{i['name']}={v}")
        if hasname and any("last" in x for x in q) or (hasname and any("%20{last_q}" in x for x in q)):
            act = urljoin(base, f["action"] or base).split("#")[0]
            # keep only one field per placeholder kind
            t = act + ("&" if "?" in act else "?") + "&".join(dict.fromkeys(q))
            if best is None or len(q) > best[1]: best = (t, len(q))
    return best[0] if best else None
async def load(page, url):
    resp = {}
    def on(r):
        if r.request.is_navigation_request() and r.frame == page.main_frame: resp["s"] = r.status
    page.on("response", on)
    try:
        r = await page.goto(url, timeout=40000, wait_until="domcontentloaded"); await page.wait_for_timeout(2500)
        return (r.status if r else resp.get("s", 0)), None
    except Exception as e: return resp.get("s", 0), str(e).splitlines()[0][:160]
    finally: page.remove_listener("response", on)
async def info(page):
    try: t = (await page.title())[:90]
    except Exception: t = ""
    try: body = (await page.inner_text("body"))[:30000]
    except Exception: body = ""
    return t, body
def blocked(st, t, body):
    tl = t.lower(); bl = body[:3000].lower()
    return st in (401, 403, 429, 503) or any(k in tl for k in scanner.CHALLENGE) or any(k in bl for k in ("verify you are human", "press & hold", "checking your browser", "enable javascript and cookies to continue", "access denied", "are you a robot", "unusual traffic"))
async def probe(b, s, p, sem, known_hosts):
    async with sem:
        r = {"site": s["id"], "name": s.get("name"), "domain": s.get("domain"), "parent": s.get("parent"), "when": datetime.datetime.now().isoformat(timespec="seconds"), "loads": 0}
        ctx = await scanner.new_context(b); page = await ctx.new_page(); first = p["first"].split()[0]
        try:
            home = f"https://{s['domain']}/"
            st, err = await load(page, home); r["loads"] += 1; t, body = await info(page); fh = urlparse(page.url).netloc.lower().removeprefix("www.")
            r.update(home_status=st, home_title=t, final_host=fh)
            if err:
                if "NAME_NOT_RESOLVED" in err: r.update(group="dead", evidence=f"DNS: {err}"); return r
                if any(k in err for k in UNREACH) and not s.get("search_url"): r.update(group="unreachable_dc", evidence=err); return r
                r["home_err"] = err
            elif fh and not fh.endswith(s["domain"]) and not s["domain"].endswith(fh):
                other = known_hosts.get(fh)
                if other or not s.get("search_url"):
                    r.update(group="dead", evidence=f"redirects to {fh}" + (f" (= site '{other}', covered there)" if other else ""), redirect=fh); return r
            elif PARKED.search(t) or (st and st >= 500 and not blocked(st, t, body)) or st == 404 or (len(body.strip()) < 200 and not blocked(st, t, body) and not s.get("search_url")):
                if not blocked(st, t, body): r.update(group="dead", evidence=f"home HTTP {st} '{t[:60]}' {body.strip()[:80]!r}"); return r
            homeblocked = (not err) and blocked(st, t, body)
            tmpl = None if os.environ.get("PROBE_REDISCOVER") else s.get("search_url")
            if tmpl and re.search(r"\{(?!(?:first|last|city|state)(?:_q)?\})\w+\}", tmpl): r.update(group="no_search", evidence=f"search template needs {re.findall(r'{(\w+)}', tmpl)} (phone/email lookup, not a name search)"); return r
            if not tmpl and not err and not homeblocked:
                try: forms = await page.evaluate(FORMS_JS)
                except Exception: forms = []
                tmpl = template_from_forms(page.url, forms)
                if tmpl: r["discovered"] = tmpl.replace(f"https://www.{s['domain']}", "https://www." + s["domain"])
            if not tmpl:
                if homeblocked: r.update(group="blocked", evidence=f"home HTTP {st} '{t[:60]}'"); return r
                if r.get("home_err"): r.update(group="unreachable_dc", evidence=r["home_err"]); return r
                # one URL-pattern guess (most common people-search layout)
                await asyncio.sleep(GAP); g = f"https://{fh or s['domain']}" + PATTERNS[0]
                st2, err2 = await load(page, mk(s, p, g)); r["loads"] += 1; t2, body2 = await info(page)
                hits = await scanner.generic_cards(page, first, p["last"])
                if not err2 and st2 == 200 and hits: tmpl = g; r["discovered"] = g; st, err, t, body = st2, err2, t2, body2
                else: r.update(group="no_search", evidence=f"home HTTP {st} reachable; no GET search form; {PATTERNS[0]} -> HTTP {st2} {len(hits)} hits"); return r
            else:
                await asyncio.sleep(GAP); url = mk(s, p, tmpl); r["search"] = url
                st, err = await load(page, url); r["loads"] += 1; t, body = await info(page)
            r.update(search_status=st, search_title=t)
            sh = urlparse(page.url).netloc.lower().removeprefix("www.")
            if not err and sh and not sh.endswith(s["domain"].split(".", 1)[-1] if s["domain"].count(".") > 1 else s["domain"]):
                fam = next((v for k, v in FUNNEL.items() if k in sh), None)
                r.update(group="no_search", funnel=sh, funnel_parent=fam, evidence=f"search hands off to {sh} (affiliate funnel{', ' + fam if fam else ''}); no own public profiles"); return r
            if err:
                r.update(group="unreachable_dc" if any(k in err for k in UNREACH) else "blocked" if "TOO_MANY_REDIRECTS" in err else "broken", evidence=err); return r
            card_sel = (s.get("extractor") or {}).get("card") if isinstance(s.get("extractor"), dict) else s.get("card")
            cards = await page.query_selector_all(card_sel) if card_sel else []
            hits = await scanner.generic_cards(page, first, p["last"])
            r["css_cards"] = len(cards); r["generic_hits"] = len(hits)
            if not cards and not hits and blocked(st, t, body): r.update(group="blocked", evidence=f"search HTTP {st} '{t[:60]}'"); return r
            nf = bool(scanner.NOT_FOUND.search(body) or scanner.NOT_FOUND.search(t))
            parsed = []
            for h in hits[:40]:
                ok, age = scanner.plausible(scanner.profiles.get(p), h["text"]); line = re.sub(r"\s+", " ", h["text"])[:110]
                parsed.append({"head": h["head"][:40], "age": age, "plausible": ok, "text": line})
            r["parsed"] = parsed[:6]; r["plausible_matches"] = sum(1 for x in parsed if x["plausible"])
            if cards or hits: r.update(group="works", evidence=f"search HTTP {st}: {len(cards)} css cards, {len(hits)} generic hits, {r['plausible_matches']} plausible for {p['full_name']} ({p['city']})")
            elif nf: r.update(group="works", evidence=f"search HTTP {st}: not-found signal ('{(scanner.NOT_FOUND.search(body) or scanner.NOT_FOUND.search(t) or [str(st)])[0]}')")
            elif st == 404: r.update(group="broken", evidence=f"search template returns HTTP 404 '{t[:50]}' (stale URL pattern)")
            elif s.get("parent") == "infotracer" or "arrests" in s["id"]: r.update(group="no_search", evidence=f"search HTTP {st} '{t[:40]}': results page renders no public records (paywall funnel); opt out via parent")
            elif r.get("discovered"): r.update(group="no_search", evidence=f"GET form found ({tmpl[:60]}) but results are not public / script-rendered: HTTP {st} '{t[:40]}'"); r.pop("discovered")
            else: r.update(group="broken", evidence=f"search HTTP {st} '{t[:50]}': no cards, no generic hits, no not-found text")
            return r
        except Exception as e:
            r.update(group="broken", evidence=f"probe error {str(e)[:120]}"); return r
        finally:
            try: await page.screenshot(path=f"{OUT}/{s['id']}.png")
            except Exception: pass
            await ctx.close(); print(json.dumps({k: r.get(k) for k in ("site", "group", "evidence")}), flush=True)
async def main():
    os.environ.setdefault("DB_PATH", f"{OUT}/probe.db")
    c = db.init(); profiles.seed(c); p = dict(c.execute("SELECT * FROM people WHERE id=?", (PID,)).fetchone())
    sites = [s for s in scanner.SITES if not ONLY or s["id"] in ONLY]
    known = {s["domain"]: s["id"] for s in scanner.SITES if s.get("domain")}
    sem = asyncio.Semaphore(PAR)
    async with async_playwright() as pw:
        b = await pw.chromium.launch(**scanner.launch_kw())
        res = await asyncio.gather(*[probe(b, s, p, sem, known) for s in sites]); await b.close()
    json.dump(res, open(os.environ.get("PROBE_OUT") or f"{OUT}/results.json", "w"), indent=1)
    import collections; print("SUMMARY", dict(collections.Counter(r["group"] for r in res)))
asyncio.run(main())
