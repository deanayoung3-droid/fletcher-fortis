"""READ-ONLY probe of the real people-search sites for ONE person. One page load per site, slow pacing,
no clicks, no form submits, no emails. Writes listings into a separate DB (PROBE_DB) for dashboard screenshots."""
import asyncio, os, sys, json, time
os.environ.setdefault("SCAN_DELAY_MS", "9000")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "optout"))
import db, profiles, scanner
from playwright.async_api import async_playwright
PID = sys.argv[1] if len(sys.argv) > 1 else "alex"; ONLY = sys.argv[2].split(",") if len(sys.argv) > 2 else None
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
async def main():
    c = db.init(); profiles.seed(c); p = c.execute("SELECT * FROM people WHERE id=?", (PID,)).fetchone(); out = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=scanner.LAUNCH_ARGS); ctx = await scanner.new_context(b)
        for s in scanner.SITES:
            if ONLY and s["id"] not in ONLY: continue
            r = {"site": s["id"], "url": None, "status": None, "title": "", "blocked": False, "cards": 0, "name_hits": 0, "matches": 0, "ages_seen": [], "note": ""}
            if not s.get("search_url"): r["note"] = "no search_url (manual)"; out.append(r); print(json.dumps(r)); continue
            page = await ctx.new_page(); resp = {}
            page.on("response", lambda x, resp=resp, page=page: resp.setdefault("s", x.status) if x.request.is_navigation_request() and x.frame == page.main_frame else None)
            before = c.execute("SELECT COUNT(*) FROM listings WHERE person_id=? AND site=?", (PID, s["id"])).fetchone()[0]
            t = time.time()
            try: await scanner.scan_person(page, c, p, s)
            except Exception as e: r["note"] = f"error {str(e)[:120]}"
            r["status"] = resp.get("s"); r["url"] = page.url; r["secs"] = round(time.time() - t, 1)
            try:
                r["title"] = (await page.title())[:80]; html = (await page.content()).lower()
                r["blocked"] = any(k in html for k in ("captcha", "cf-challenge", "verify you are human", "access denied", "just a moment", "press & hold", "are you a robot"))
                cards = await page.query_selector_all(s["card"]) if s.get("card") else []; r["cards"] = len(cards)
                r["parsed"] = []
                for cd in cards[:40]:
                    tx = (await cd.inner_text())
                    hd = scanner.SUBJECT_CUT.split(next((x for x in tx.split("\n") if x.strip()), ""), 1)[0].strip()
                    ag = scanner.AGE_RE.findall(tx); city = next((x for x in tx.split("\n") if "lives in" in x.lower() or " in " in x.lower()), "")[:60]
                    if len(r["parsed"]) < 4: r["parsed"].append(f"{hd} | age {ag[0] if ag else '?'} | {city}")
                    if "rivera" in tx.lower() and "alex" in tx.lower(): r["name_hits"] += 1; r["ages_seen"] += [int(a) for a in scanner.AGE_RE.findall(tx)][:1]
                await page.screenshot(path=f"/tmp/probe/{s['id']}.png")
            except Exception as e: r["note"] += f" post:{str(e)[:80]}"
            r["matches"] = c.execute("SELECT COUNT(*) FROM listings WHERE person_id=? AND site=?", (PID, s["id"])).fetchone()[0] - before
            ev = c.execute("SELECT kind, detail FROM events WHERE person_id=? ORDER BY id DESC LIMIT 1", (PID,)).fetchone()
            if ev and s["id"] in (ev["detail"] or ""): r["note"] += f" {ev['kind']}: {ev['detail'][:220]}"
            out.append(r); print(json.dumps(r), flush=True); await page.close()
            await asyncio.sleep(int(os.environ["SCAN_DELAY_MS"]) / 1000)
        await b.close()
    json.dump(out, open("/tmp/probe/results.json", "w"), indent=1)
asyncio.run(main())
