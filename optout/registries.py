"""Official state data-broker registries -> one deduplicated, categorized broker list (request-based removal).
Sources (all public records):
  CA  California Privacy Protection Agency Data Broker Registry (CSV)            https://cppa.ca.gov/data_broker_registry/
  OR  Oregon DCBS / DFR Data Broker Registry: opt-out + data-collected reports (CSV)
  TX  Texas Secretary of State Data Broker Registry (Appian portal "Export to Excel", fetched with headless Chromium)
  VT  Vermont Secretary of State: the public search is behind reCAPTCHA and the bulk download needs a login, so it is
      imported from a file the admin downloads (Coverage > Import Vermont file, or /data/registries/vermont.csv|xlsx).
Run monthly by the scheduler (refresh) and on demand: python3 registries.py [--refresh]"""
import csv, datetime, io, json, os, re, sys, urllib.request
import yaml
HERE = os.path.dirname(__file__)
SEED = os.environ.get("REGISTRY_SEED", os.path.join(HERE, "..", "brokers", "registries.yaml"))
DIR = __import__("paths").registry_dir()
OUT = os.path.join(DIR, "registries.yaml")
CA_URL = "https://cppa.ca.gov/data_broker_registry/registry.csv"
OR_OPTOUT = "https://www4.cbs.state.or.us/ex/shared/downtemp/dfcs_db_optout.csv"
OR_COLLECTED = "https://www4.cbs.state.or.us/ex/shared/downtemp/dfcs_db_collected.csv"
TX_URL = "https://texas-sos.appianportalsgov.com/data-broker-registry"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 FletcherFortis-registry"
CATEGORIES = [("people-search", "People search"), ("marketing", "Marketing and lists"), ("location", "Location data"),
              ("risk", "Risk, fraud and identity"), ("financial", "Financial"), ("health", "Health"), ("other", "Other")]
LABEL = dict(CATEGORIES)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL = re.compile(r"https?://[^\s\"'<>\]\[)]+", re.I)
KW = [  # first match wins, checked against name + description fields
    ("people-search", r"people.?search|background.?check|public records|find (?:people|anyone)|people finder|person lookup|reverse (?:phone|address)|whitepages|truthfinder|spokeo|radaris|intelius"),
    ("location", r"precise geolocation|geolocation|location data|location intelligence|mobility|foot.?traffic|movement data|gps|visitation|mobile (?:device|advertising) id|\bmaid"),
    ("health", r"\bhealth|medical|patient|pharma|clinical|hipaa|wellness|prescription"),
    ("risk", r"fraud|identity (?:resolution|graph)|risk (?:management|scor|assess)|identity verification|authenticat|kyc|anti.?money|skip.?trac|collections?\b|screening|investigat|due diligence"),
    ("financial", r"credit|financial|mortgage|lending|loan|bank|insurance|fcra|glba|income|wealth|tax\b|debt"),
    ("marketing", r"marketing|advertis|audience|\blists?\b|lead(?:s| gen)|append|direct mail|segment|demographic|consumer (?:data|database|insight)|data cooperative|b2b|prospect|email (?:data|address)|intent data|sales intelligence|voter|\\bmedia\\b|contact data|\\bsales\\b|influencer|social|campaign|publisher|ad.?tech|programmatic|cookie|online identifier|browsing|interests?\\b|purchase"),
]
KWC = [(k, re.compile(rx, re.I)) for k, rx in KW]

def dom(u):
    u = (u or "").strip().lower()
    if not u or " " in u.strip(): u = (URL.search(u) or [None])[0] or (u.split()[0] if u else "")
    if not u: return ""
    if "@" in u and "/" not in u: u = u.split("@")[-1]
    u = u if "://" in u else "http://" + u
    try: h = (urllib.parse.urlsplit(u).hostname or "").lower()
    except ValueError: return ""
    h = h.strip(".;,")
    return h[4:] if h.startswith("www.") else h
import urllib.parse
def key(name): return re.sub(r"\b(inc|llc|ltd|corp|co|corporation|company|the|lp|llp|plc|dba|d b a)\b|[^a-z0-9]", "", (name or "").lower())
def _url(v):
    v = (v or "").strip()
    if not v or v.lower() in ("n/a", "na", "none", "agents", "no other methods"): return ""
    m = URL.search(v)
    if m: return m.group(0).rstrip(".,;")
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/\S*)?$", v, re.I): return "https://" + v
    return ""
def _email(v): m = EMAIL.search(v or ""); return m.group(0).lower().rstrip(".") if m else ""

def classify(text, flags=(), people_domains=(), domain="", name=""):
    """Score every category by distinct keyword hits (name counts triple), add registry checkbox hints, pick the best.
    Ties go to the more sensitive category (people-search > location > health > risk > financial > marketing)."""
    if domain and domain in people_domains: return "people-search"
    score = {}
    for k, rx in KWC:
        hits = {m.group(0).lower() for m in rx.finditer(text or "")}
        score[k] = len(hits) + 2 * len({m.group(0).lower() for m in rx.finditer(name or "")})
    for f in flags: score[f] = score.get(f, 0) + 0.5   # registry checkboxes only break ties
    order = [k for k, _ in KW]
    best = max(order, key=lambda k: (score.get(k, 0), -order.index(k)))
    return best if score.get(best, 0) > 0 else "other"

def _fetch(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    data = urllib.request.urlopen(req, timeout=90).read(60_000_000)  # nosec B310: fixed https URLs
    tmp = path + ".tmp"; open(tmp, "wb").write(data); os.replace(tmp, path); return path

# ---- parsers: each yields normalized dicts --------------------------------------------------------
def parse_ca(path):
    for r in csv.DictReader(open(path, encoding="utf-8-sig", errors="ignore")):
        g = lambda pre: next((v for k, v in r.items() if k and k.startswith(pre)), "") or ""
        yes = lambda pre: g(pre).strip().lower().startswith("yes")
        flags = [f for f, ok in (("location", yes("Data broker collects consumers' precise geolocation")), ("health", yes("The data broker or its subsidiaries are regulated by the HIPAA") or yes("The data broker or any of its subsidiaries is regulated by the California Confidentiality")),
                                 ("financial", yes("The data broker or any of its subsidiaries is regulated by the federal Fair Credit") or yes("The data broker or any of its subsidiaries is regulated by the Gramm")), ("risk", yes("Data broker collects consumers’ government‑issued identification"))) if ok]
        name = g("Data broker name").strip()
        if not name: continue
        yield dict(name=name, dba=g("Doing Business As").strip(), website=_url(g("Data broker primary website")) or g("Data broker primary website").strip(), email=_email(g("Data broker primary contact email")),
                   optout_url=_url(g("Data broker's primary website that contains")), phone=g("Data broker primary phone").strip(), flags=flags,
                   text=" ".join([name, g("Doing Business As"), g("If the data broker or its subsidiaries are regulated by the FCRA, describe the types"), g("Additional Context")]), minors=yes("Data broker collects personal information of minors"), registry="CA", reg_id="")

def parse_or(optout_path, collected_path=None):
    col = {}
    if collected_path and os.path.exists(collected_path):
        for r in csv.DictReader(open(collected_path, encoding="utf-8-sig", errors="ignore")): col[r.get("LICENSE NO.")] = r
    for r in csv.DictReader(open(optout_path, encoding="utf-8-sig", errors="ignore")):
        name = (r.get("FACILITY NAME") or "").strip()
        if not name: continue
        c = col.get(r.get("LICENSE NO."), {}); narr = r.get("OPT OUT NARRATIVE") or ""
        flags = ["risk"] if "Collect" in (c.get("DATA COLLECT - SSN/GOVERNMENT ID") or "") or "Collect" in (c.get("DATA COLLECT - BIOMETRIC INFORMATION") or "") else []
        yield dict(name=name, dba=(r.get("DOING BUSINESS AS") or "").strip(), website=_url(r.get("OPT OUT WEBSITE")) or _url(r.get("ADDITIONAL WEBSITE")), email=_email(r.get("OPT OUT EMAIL")) or _email(narr),
                   optout_url=_url(r.get("OPT OUT OTHER METHODS")) or _url(narr) or _url(r.get("OPT OUT WEBSITE")), phone=(r.get("OPT OUT PHONE") or "").strip(), flags=flags,
                   text=" ".join([name, r.get("DOING BUSINESS AS") or "", narr]), minors=False, registry="OR", reg_id=r.get("LICENSE NO.") or "")

def parse_tx(path):
    import openpyxl, warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore"); rows = list(openpyxl.load_workbook(path, read_only=True).active.iter_rows(values_only=True))
    hi = next(i for i, r in enumerate(rows) if r and r[0] == "Registration Number"); H = rows[hi]
    for r in rows[hi + 1:]:
        d = {H[i]: ("" if v is None else str(v)) for i, v in enumerate(r) if i < len(H) and H[i]}
        name = d.get("Full Legal Name", "").strip()
        if not name: continue
        yield dict(name=name, dba="", website=_url(d.get("Website")), email=_email(d.get("Data Broker Email Address")), optout_url=_url(d.get("Consumer Rights Link")),
                   phone=d.get("Telephone Number", "").strip(), flags=[], text=" ".join([name, d.get("Categories of Data Processed and Transferred", ""), d.get("Additional Information or Explanation", "")]),
                   minors=d.get("Data of a Known Child", "").lower().startswith("yes"), registry="TX", reg_id=d.get("Registration Number", ""))

def parse_vt(path):
    """Vermont bulk download (CSV or XLSX). Column names vary between exports, so match loosely."""
    if path.endswith(".xlsx"):
        import openpyxl; rows = list(openpyxl.load_workbook(path, read_only=True).active.iter_rows(values_only=True))
        hi = next(i for i, r in enumerate(rows) if r and any(str(x or "").lower().find("name") >= 0 for x in r)); H = [str(x or "") for x in rows[hi]]
        recs = [{H[i]: ("" if v is None else str(v)) for i, v in enumerate(r) if i < len(H)} for r in rows[hi + 1:]]
    else: recs = list(csv.DictReader(open(path, encoding="utf-8-sig", errors="ignore")))
    pick = lambda d, *ws: next((v for k, v in d.items() if k and all(w in k.lower() for w in ws) and v), "")
    for d in recs:
        name = (pick(d, "data broker", "name") or pick(d, "business", "name") or pick(d, "name")).strip()
        if not name or (pick(d, "status") and "regist" not in pick(d, "status").lower() and "active" not in pick(d, "status").lower()): continue
        blob = " ".join(str(v) for v in d.values())
        yield dict(name=name, dba="", website=_url(pick(d, "website") or pick(d, "url")), email=_email(pick(d, "email") or blob), optout_url=_url(pick(d, "opt") or ""),
                   phone=pick(d, "phone"), flags=[], text=blob[:4000], minors="minor" in blob.lower() and "yes" in blob.lower(), registry="VT", reg_id=pick(d, "record") or pick(d, "number"))

async def _tx_download(path):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"] if os.geteuid() == 0 else []); ctx = await b.new_context(accept_downloads=True, user_agent=UA); pg = await ctx.new_page()
        try:
            await pg.goto(TX_URL, wait_until="networkidle", timeout=90000)
            async with pg.expect_download(timeout=120000) as d: await pg.get_by_role("button", name=re.compile("EXPORT TO EXCEL", re.I)).click()
            await (await d.value).save_as(path)
        finally: await b.close()
    return path

# ---- merge -------------------------------------------------------------------------------------
def _people_domains():
    try:
        s = yaml.safe_load(open(os.environ.get("SITES_FILE") or os.path.join(HERE, "sites.yaml")))["sites"]
        return {x["domain"].lower().removeprefix("www.") for x in s if x.get("domain")}
    except Exception: return set()

def merge(recs, fetched=None):
    pd = _people_domains(); by_dom, by_key, out = {}, {}, []
    counts = {}
    for r in recs:
        counts[r["registry"]] = counts.get(r["registry"], 0) + 1
        d = dom(r["website"]) or dom(r["email"].split("@")[-1] if r["email"] and not re.search(r"@(gmail|aol|yahoo|outlook|hotmail|icloud)\.", r["email"]) else "") or dom(r["optout_url"])
        k = key(r["name"]); hit = by_dom.get(d) if d else None; hit = hit or by_key.get(k) or (by_key.get(key(r["dba"])) if r["dba"] else None)
        if hit:
            if r["registry"] not in hit["registries"]: hit["registries"].append(r["registry"])
            for f in ("email", "optout_url", "phone", "dba"):
                if not hit.get(f) and r.get(f): hit[f] = r[f]
            hit["_text"] += " " + r["text"]; hit["_flags"] |= set(r["flags"]); hit["minors"] = hit["minors"] or r["minors"]
            if r["reg_id"]: hit["reg_ids"][r["registry"]] = r["reg_id"]
            if d and not hit["domain"]: hit["domain"] = d; by_dom[d] = hit
            continue
        b = dict(id="", name=r["name"], dba=r["dba"], domain=d, email=r["email"], optout_url=r["optout_url"], phone=r["phone"], registries=[r["registry"]],
                 reg_ids={r["registry"]: r["reg_id"]} if r["reg_id"] else {}, minors=r["minors"], _text=r["text"], _flags=set(r["flags"]))
        out.append(b); by_key[k] = b
        if r["dba"]: by_key.setdefault(key(r["dba"]), b)
        if d: by_dom[d] = b
    ids = set()
    for b in out:
        b["category"] = classify(b.pop("_text"), sorted(b.pop("_flags")), pd, b["domain"], b["name"] + " " + (b["dba"] or ""))
        b["people_search_site"] = bool(b["domain"] and b["domain"] in pd)
        base = re.sub(r"[^a-z0-9]+", "-", (b["domain"].split(".")[0] if b["domain"] else key(b["name"]))[:40]).strip("-") or "broker"; i, n = base, 2
        while i in ids: i = f"{base}-{n}"; n += 1
        ids.add(i); b["id"] = i
        b["channel"] = "email" if b["email"] else ("form" if b["optout_url"] else "none")
        b["registries"].sort()
    cats = {}
    for b in out: cats[b["category"]] = cats.get(b["category"], 0) + 1
    return {"generated": datetime.datetime.now().isoformat(timespec="seconds"), "fetched": fetched or {}, "registry_counts": counts, "total": len(out),
            "category_counts": dict(sorted(cats.items(), key=lambda x: -x[1])), "brokers": sorted(out, key=lambda b: b["name"].lower())}

def refresh(download=True):
    """Download every registry we can, parse what we have (fresh or previous files), merge, write OUT. Returns the summary."""
    os.makedirs(DIR, exist_ok=True); fetched = {}; recs = []
    P = {k: os.path.join(DIR, f) for k, f in (("CA", "ca_registry.csv"), ("OR", "or_optout.csv"), ("ORc", "or_collected.csv"), ("TX", "tx_registry.xlsx"))}
    if download:
        for k, u in (("CA", CA_URL), ("OR", OR_OPTOUT), ("ORc", OR_COLLECTED)):
            try: _fetch(u, P[k]); fetched[k] = "ok"
            except Exception as e: fetched[k] = f"failed: {type(e).__name__}"
        try:
            import asyncio; asyncio.run(_tx_download(P["TX"] + ".tmp.xlsx")); os.replace(P["TX"] + ".tmp.xlsx", P["TX"]); fetched["TX"] = "ok"
        except Exception as e: fetched["TX"] = f"failed: {type(e).__name__}"
    seed_ca = os.path.join(HERE, "..", "brokers", "ca_registry.csv")
    for k, fn in (("CA", lambda: parse_ca(P["CA"] if os.path.exists(P["CA"]) else seed_ca)), ("OR", lambda: parse_or(P["OR"], P["ORc"])), ("TX", lambda: parse_tx(P["TX"]))):
        try: recs += list(fn())
        except FileNotFoundError: fetched.setdefault(k, "no file")
        except Exception as e: fetched[k] = f"parse failed: {type(e).__name__}: {e}"[:160]
    vt = next((os.path.join(DIR, f) for f in ("vermont.xlsx", "vermont.csv") if os.path.exists(os.path.join(DIR, f))), None)
    if vt:
        try: recs += list(parse_vt(vt)); fetched["VT"] = "imported file"
        except Exception as e: fetched["VT"] = f"parse failed: {type(e).__name__}"
    else: fetched["VT"] = "not imported (search is behind reCAPTCHA; bulk download needs a login)"
    if not recs: return load()
    d = merge(recs, fetched)
    if d["total"] < 0.6 * (load().get("total") or 0):   # a partial download must not shrink the list: keep the previous one
        return {"generated": d["generated"], "fetched": {**fetched, "kept": "previous list (new one was much smaller)"}, "registry_counts": d["registry_counts"], "total": load().get("total"), "category_counts": load().get("category_counts")}
    tmp = OUT + ".tmp"
    yaml.safe_dump(d, open(tmp, "w"), sort_keys=False, allow_unicode=True, width=200); os.replace(tmp, OUT); _cache.clear()
    return {k: d[k] for k in ("generated", "fetched", "registry_counts", "total", "category_counts")}

_cache = {}
def load():
    """The merged list: /data copy if present (monthly refresh), else the snapshot bundled with the release."""
    p = OUT if os.path.exists(OUT) else SEED
    try: m = os.path.getmtime(p)
    except OSError: return {"brokers": [], "total": 0, "registry_counts": {}, "category_counts": {}}
    if _cache.get("k") != (p, m): _cache.update(k=(p, m), d=yaml.safe_load(open(p)) or {"brokers": []})
    return _cache["d"]
def brokers(): return load().get("brokers") or []
def by_id(): return {b["id"]: b for b in brokers()}
def age_days():
    g = load().get("generated")
    try: return (datetime.datetime.now() - datetime.datetime.fromisoformat(g)).days
    except Exception: return 999

if __name__ == "__main__":
    print(json.dumps(refresh(download="--refresh" in sys.argv), indent=1))
