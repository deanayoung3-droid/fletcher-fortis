"""Web forms for registered data brokers that do not take privacy requests by email.

Where the URL comes from: brokers/form_overrides.yaml, or a broker's own "Needs info" reply that points to a web form or
portal ("we don't accept requests by email, use https://..."). The URL is saved on the broker (broker_forms table) and every
person's request to that broker that has not had a form attempt yet becomes form_queued.

The automatic attempt runs on the scan worker (one Chromium at a time), live only (never in DRY_RUN), only for people who
signed their authorization and have sending enabled, paced FORM_PER_HOUR. Outcomes:
  submitted ............ request status 'sent' (channel form)
  CAPTCHA / unrecognised form / no success message ... 'needs_human' (Needs you), with the reason
  load error ........... retried on the next runs, then Needs you after MAX_TRIES"""
import os, re, json, asyncio, yaml
import config, remover, registries
from db import conn, now, event

HERE = os.path.dirname(os.path.abspath(__file__))
OVERRIDES = os.environ.get("FORM_OVERRIDES", os.path.join(HERE, "..", "brokers", "form_overrides.yaml"))
MAX_TRIES = 3
REMARK = ("queued", "failed", "sent", "needs_info", "dry_run")   # statuses a "use our form" broker's requests move from
URL = re.compile(r"https://[^\s<>\"')\]]+", re.I)
FORMISH = re.compile(r"privacy|request|webform|web-form|form|portal|dsr|dsar|rights|onetrust|trustarc|ketch|datagrail|transcend|consumer|opt.?out", re.I)
SKIP = re.compile(r"unsubscribe|fletcherfortis|mailto:|\.(png|jpe?g|gif|svg)(\?|$)|linkedin\.com|twitter\.com|facebook\.com|//(www\.)?x\.com/|google\.com/maps|"
                  r"/[\w-]*(policy|notice|terms)[\w-]*(\.html?)?/?$", re.I)   # policy / notice / terms pages are not request forms

def ensure(c):
    c.execute("CREATE TABLE IF NOT EXISTS broker_forms(broker TEXT PRIMARY KEY, url TEXT, source TEXT, inbox_id INT, updated TEXT)")
    for col in ("form_at TEXT", "form_tries INT DEFAULT 0", "form_detail TEXT"):
        try: c.execute("ALTER TABLE reg_requests ADD COLUMN " + col)
        except Exception: pass

def form_url(c, broker_id):
    ensure(c); r = c.execute("SELECT url FROM broker_forms WHERE broker=?", (broker_id,)).fetchone()
    return r["url"] if r else None

def extract(body, links=None):
    """The web form / portal URL a broker's reply points to (its own words only, not our quoted letter), or None.
    Prefers a deletion form when the reply lists several."""
    import inbound
    text = inbound.reply_text(body or "")
    urls = [u.rstrip(".,;:>") for u in URL.findall(text)]
    urls = [u for u in urls if not SKIP.search(u) and FORMISH.search(u)]
    if not urls:   # the link may only be in the HTML part ("Web Form: Privacy Center"): use the reply's own links
        urls = [u for u in (links or []) if u.lower().startswith("https://") and not SKIP.search(u) and FORMISH.search(u)][:1]
    if not urls: return None
    for u in urls:
        i = text.find(u); ctx = text[max(0, i - 160):i].lower()
        if "delet" in ctx and "opt-out" not in ctx[-60:]: return u
    return urls[0]

def save(c, broker_id, url, source, inbox_id=None):
    """Record the form URL on the broker and queue a form attempt for every request that has not had one. Returns rows queued."""
    ensure(c)
    c.execute("INSERT INTO broker_forms(broker,url,source,inbox_id,updated) VALUES(?,?,?,?,?) ON CONFLICT(broker) DO UPDATE SET url=excluded.url, source=excluded.source, inbox_id=COALESCE(excluded.inbox_id, broker_forms.inbox_id), updated=excluded.updated",
              (broker_id, url, source, inbox_id, now()))
    n = c.execute(f"UPDATE reg_requests SET status='form_queued', channel='form', target=?, updated=? WHERE broker=? AND form_at IS NULL AND status IN {REMARK}", (url, now(), broker_id)).rowcount  # nosec B608
    c.commit(); return n

def seed(c):
    """Apply brokers/form_overrides.yaml (idempotent)."""
    try: d = yaml.safe_load(open(OVERRIDES)) or {}
    except Exception: d = {}
    n = 0
    for bid, v in d.items():
        if v and v.get("url"): n += save(c, bid, v["url"], v.get("source") or "form_overrides.yaml")
    return n

def harvest(c):
    """Learn form URLs from 'Needs info' replies to registry requests. Returns {broker: rows queued} for new URLs."""
    ensure(c); out = {}
    reqs = {r["outbox_id"]: r for r in c.execute("SELECT broker, outbox_id FROM reg_requests WHERE outbox_id IS NOT NULL")}
    for i in c.execute("SELECT id, outbox_id, body, links FROM inbox WHERE kind='need_info' AND outbox_id IS NOT NULL ORDER BY id").fetchall():
        q = reqs.get(i["outbox_id"])
        if not q: continue
        have = c.execute("SELECT inbox_id FROM broker_forms WHERE broker=?", (q["broker"],)).fetchone()
        if have and (have["inbox_id"] is None or have["inbox_id"] >= i["id"]):   # set by hand / overrides, or already learned from this reply
            n = c.execute(f"UPDATE reg_requests SET status='form_queued', channel='form', updated=? WHERE broker=? AND form_at IS NULL AND status IN {REMARK}", (now(), q["broker"])).rowcount  # nosec B608
            if n: out[q["broker"]] = out.get(q["broker"], 0) + n
            continue
        try: links = json.loads(i["links"] or "[]")
        except Exception: links = []
        u = extract(i["body"], links)
        if u: out[q["broker"]] = save(c, q["broker"], u, "broker reply", i["id"])
    c.commit()
    if out: event(c, "registry_forms_learned", "; ".join(f"{b}: {n} request(s) queued for the web form" for b, n in out.items()))
    return out

def tick(c, limit=None):
    """Hourly: apply overrides, learn from replies, then queue a paced batch of live form attempts on the scan worker."""
    ensure(c); res = {"form_seeded": seed(c), "form_learned": sum(harvest(c).values()), "form_started": 0}
    if remover.DRY or config.get("REGISTRY_FORMS", "true") != "true": return res
    limit = limit or int(config.get("FORM_PER_HOUR") or 6)
    people = {p["id"]: p for p in c.execute("SELECT * FROM people WHERE enabled=1").fetchall()}
    per = {}
    for r in c.execute("SELECT id, person_id FROM reg_requests WHERE status='form_queued' ORDER BY id").fetchall():
        p = people.get(r["person_id"])
        if p and remover.can_send(p)[0]: per.setdefault(r["person_id"], []).append(r["id"])   # unsigned people stay locked
    import scanjobs; i = 0
    while res["form_started"] < limit and any(i < len(v) for v in per.values()):
        for v in per.values():
            if i < len(v) and res["form_started"] < limit and scanjobs.enqueue_regform(v[i]): res["form_started"] += 1
        i += 1
    return res

async def _fill(p, url, rid):
    import formfill, scanner, profiles, automation, chain
    from playwright.async_api import async_playwright
    st, detail = "error", ""
    for attempt in range(2):   # direct, then stealth after a short backoff
        try:
            async with async_playwright() as pw:
                b = await pw.chromium.launch(**scanner.launch_kw(proxy=attempt == 1))
                try:
                    ctx = await (scanner.new_context(b) if attempt == 0 else chain.stealth_context(b)); page = await ctx.new_page()
                    st, detail = await formfill.fill(page, url, formfill.values(p, profiles.get(p), ""), os.path.join(scanner.SHOTS, f"regform_{rid}.png"), dry=False, solver=automation.solver_on())
                finally: await b.close()
        except Exception as e: st, detail = "error", type(e).__name__
        if st != "error": break
        if attempt == 0: await asyncio.sleep(chain.backoff_ms(0) / 1000)
    return st, detail

def run(rid, filler=None):
    """One live form attempt for a registry request (called on the scan worker, under the BROWSER lock)."""
    c = conn(); ensure(c); r = c.execute("SELECT * FROM reg_requests WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] != "form_queued" or remover.DRY: return None
    p = c.execute("SELECT * FROM people WHERE id=?", (r["person_id"],)).fetchone()
    if not p or not remover.can_send(p)[0]: return None
    b = registries.by_id().get(r["broker"]) or {"id": r["broker"], "name": r["broker"]}
    url = form_url(c, r["broker"]) or r["target"] or b.get("optout_url")
    try: st, detail = (filler or (lambda p_, u, i: asyncio.run(_fill(p_, u, i))))(p, url, rid)
    except Exception as e: st, detail = "error", type(e).__name__
    tries = (r["form_tries"] or 0) + 1
    if st == "submitted": new = "sent"
    elif st == "error" and tries < MAX_TRIES: new = "form_queued"
    else: new = "needs_human"
    c.execute("UPDATE reg_requests SET status=?, channel='form', target=?, form_at=?, form_tries=?, form_detail=?, sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END, updated=? WHERE id=?",
              (new, url, now(), tries, (detail or "")[:300], new, now(), now(), rid))
    if new != "form_queued":
        remover._record(c, p, url, f"Registry broker web form: {b.get('name')}", detail or st, "submitted" if new == "sent" else "needs_human", None)
    event(c, "registry_form_" + ("submitted" if new == "sent" else "retry" if new == "form_queued" else "needs_human"), f"{b.get('name')}: {(detail or st)[:160]}", p["id"])
    c.commit(); return new
