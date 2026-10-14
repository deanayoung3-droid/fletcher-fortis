"""Listing screenshots: a full-page capture plus a crop of the matched record, 1280 px wide, stored as small WebP files in SHOT_DIR.
Kinds: before (at discovery), after (at the re-scan that confirms removal), retake (on demand). Failures are recorded with the reason
so the UI can show a fallback with a Retake button. Files are only served through the app (auth + consent checked)."""
import os, io, json, hashlib, datetime
from db import conn, now, event
from crypto import enc

WIDTH = 1280; MAX_H = 7000; Q_FULL = 62; Q_CROP = 78

def root():
    import scanner; return scanner.SHOTS

def ensure(c=None):
    c = c or conn()
    c.execute("""CREATE TABLE IF NOT EXISTS shots(id INTEGER PRIMARY KEY, listing_id INT, person_id TEXT, kind TEXT, ts TEXT, url TEXT,
                 full TEXT, crop TEXT, ok INT DEFAULT 0, error TEXT, bytes INT DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS shots_listing ON shots(listing_id)"); c.commit(); return c

def _webp(png, quality, max_h=MAX_H):
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if im.width > WIDTH: im = im.resize((WIDTH, round(im.height * WIDTH / im.width)))
    if im.height > max_h: im = im.crop((0, 0, im.width, max_h))
    out = io.BytesIO(); im.save(out, "WEBP", quality=quality, method=6); return out.getvalue()

FIND_JS = r"""([first, last]) => {
  const F = new RegExp('\\b' + first + '\\b', 'i'), L = new RegExp('\\b' + last + '\\b', 'i'); let best = null;
  for (const e of document.querySelectorAll('article,li,tr,section,div,a')) {
    const t = e.innerText || ''; if (!(F.test(t) && L.test(t))) continue;
    const r = e.getBoundingClientRect(); if (r.height < 40 || r.height > 900 || r.width < 200) continue;
    if (!best || r.height * r.width < best.a) best = {a: r.height * r.width, x: r.x + scrollX, y: r.y + scrollY, w: r.width, h: r.height};
  } return best }"""

async def capture(page, listing, kind, first="", last="", el=None, c=None):
    """Capture the page that is currently open. Returns the shots row id (ok or failed)."""
    import re
    c = ensure(c); d = root(); os.makedirs(d, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S"); base = f"{listing['person_id']}_{listing['id']}_{kind}_{stamp}"
    full = crop = None; err = None; size = 0
    try:
        try: await page.set_viewport_size({"width": WIDTH, "height": 900})
        except Exception: pass
        png = await page.screenshot(full_page=True, type="png", timeout=30000)
        data = _webp(png, Q_FULL); full = os.path.join(d, base + ".webp"); open(full, "wb").write(data); size += len(data)
        box = None
        if el is not None:
            try: box = await el.evaluate("e => { const r = e.getBoundingClientRect(); return {x: r.x + scrollX, y: r.y + scrollY, w: r.width, h: r.height}; }")
            except Exception: box = None
        if not box and first and last:
            try: box = await page.evaluate(FIND_JS, [re.escape(first.split()[0]), re.escape(last)])
            except Exception: box = None
        if box:
            y = max(0, box["y"] - 60); h = min(1200, box["h"] + 120)
            cp = await page.screenshot(full_page=True, type="png", clip={"x": 0, "y": y, "width": WIDTH, "height": max(120, h)}, timeout=30000)
            data = _webp(cp, Q_CROP, 1400); crop = os.path.join(d, base + "_crop.webp"); open(crop, "wb").write(data); size += len(data)
    except Exception as e:
        err = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}" if str(e) else type(e).__name__
    cur = c.execute("INSERT INTO shots(listing_id,person_id,kind,ts,url,full,crop,ok,error,bytes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (listing["id"], listing["person_id"], kind, now(), enc(page.url if page else ""), full, crop, int(bool(full)), err, size))
    if full: c.execute("UPDATE listings SET screenshot=? WHERE id=?", (crop or full, listing["id"]))
    c.commit()
    if err: event(c, "shot_failed", f"{kind}: {err}"[:200], listing["person_id"], listing["id"])
    return cur.lastrowid

async def retake(lid, kind="retake"):
    """Open the listing URL in a fresh browser and capture it (used by the Retake button, through the scan worker)."""
    import scanner
    from playwright.async_api import async_playwright
    c = ensure(); l = c.execute("SELECT l.*, p.first, p.last FROM listings l JOIN people p ON p.id=l.person_id WHERE l.id=?", (lid,)).fetchone()
    if not l: return None
    async with async_playwright() as pw:
        b = await pw.chromium.launch(**scanner.launch_kw())
        try:
            ctx = await scanner.new_context(b); page = await ctx.new_page()
            try: await page.goto(l["url"], timeout=45000, wait_until="domcontentloaded"); await page.wait_for_timeout(1500)
            except Exception as e:
                c.execute("INSERT INTO shots(listing_id,person_id,kind,ts,url,ok,error) VALUES(?,?,?,?,?,0,?)", (lid, l["person_id"], kind, now(), enc(l["url"]), f"page did not load: {type(e).__name__}")); c.commit(); return None
            return await capture(page, l, kind, l["first"] or "", l["last"] or "", c=c)
        finally: await b.close()

def for_listing(c, lid):
    ensure(c); return c.execute("SELECT * FROM shots WHERE listing_id=? ORDER BY id", (lid,)).fetchall()

def proof(c, lid):
    """(before, after, latest_failed) rows for the before/after view."""
    rows = for_listing(c, lid); ok = [r for r in rows if r["ok"]]
    before = next((r for r in ok if r["kind"] in ("before", "retake")), None)
    after = next((r for r in reversed(ok) if r["kind"] == "after"), None)
    failed = rows[-1] if rows and not rows[-1]["ok"] else None
    return before, after, failed

def thumbs(c, lids):
    """listing_id -> best thumbnail shot row (latest ok)."""
    ensure(c); out = {}
    if not lids: return out
    q = ",".join("?" * len(lids))
    for r in c.execute(f"SELECT * FROM shots WHERE ok=1 AND listing_id IN ({q}) ORDER BY kind='after' DESC, id", list(lids)).fetchall(): out[r["listing_id"]] = r  # nosec B608: latest non-'after' wins
    return out

def path(row, variant):
    p = row["crop"] if variant == "crop" and row["crop"] else row["full"]
    if not p: return None
    real = os.path.realpath(p); r = os.path.realpath(root())
    return real if real.startswith(r + os.sep) and os.path.isfile(real) else None
