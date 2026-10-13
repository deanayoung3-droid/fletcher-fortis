"""Browser QA: every page × desktop/mobile × light/dark. Checks console errors, failed requests, horizontal overflow,
fonts (every visible text element must resolve to Geist / Geist Mono and the font files must be loaded), and basic interactions."""
import asyncio, json, sys, re, os
from playwright.async_api import async_playwright
B = os.environ.get("QA_BASE", "http://localhost:8091"); OUT = os.environ.get("QA_OUT", "/workspace/qa")
APW = "qa correct horse battery"; K1 = "first pass for k 123"; K2 = "my own passphrase 2026 k"
VIEWS = {"desktop": dict(viewport={"width": 1440, "height": 900}), "mobile": dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=2)}
FONTJS = """() => { const bad = new Set(); let n = 0;
 for (const el of document.querySelectorAll('body *')) { if (!el.offsetParent && getComputedStyle(el).position!=='fixed') continue;
   const t = [...el.childNodes].some(c => c.nodeType===3 && c.textContent.trim()); if (!t && !['INPUT','TEXTAREA','SELECT','BUTTON'].includes(el.tagName)) continue;
   n++; const f = getComputedStyle(el).fontFamily.split(',')[0].replace(/["']/g,'').trim(); if (f!=='Geist' && f!=='Geist Mono') bad.add(el.tagName+':'+f); }
 return {n, bad:[...bad], geist: document.fonts.check('14px Geist'), mono: document.fonts.check('14px "Geist Mono"'),
   loaded:[...document.fonts].filter(f=>f.status==='loaded').map(f=>f.family), overflow: document.documentElement.scrollWidth - innerWidth}; }"""
results = []
def rec(name, ok, detail=""): results.append((name, bool(ok), detail)); print(("PASS " if ok else "FAIL ") + name + (" — " + detail if detail else ""), flush=True)

async def login(pg, u, p):
    await pg.goto(B + "/login"); await pg.fill("input[name=username]", u); await pg.fill("input[name=password]", p); 
    async with pg.expect_navigation(): await pg.press("input[name=password]", "Enter")

async def audit(pg, label, url, shot=None):
    errs = []; pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None); pg.on("pageerror", lambda e: errs.append(str(e)))
    bad = []; pg.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 and not r.url.endswith("/nope") else None)
    r = await pg.goto(B + url); await pg.wait_for_load_state("networkidle"); await pg.evaluate("document.fonts.ready.then(()=>1)")
    f = await pg.evaluate(FONTJS)
    if url != "/nope" and not pg.url.split("?")[0].endswith(url) and url != "/": errs.append("redirected to " + pg.url)
    ok = not errs and not bad and not f["bad"] and f["geist"] and f["overflow"] <= 1
    rec(f"{label} {url}", ok, "" if ok else json.dumps({"console": errs[:3], "http": bad[:3], "fonts": f["bad"][:5], "geist": f["geist"], "overflow": f["overflow"]}))
    if shot: await pg.screenshot(path=f"{OUT}/{shot}", full_page=True)
    pg.remove_listener("console", pg.listeners("console")[0]) if hasattr(pg, "listeners") else None
    return r

async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch()
        for vname, vopt in VIEWS.items():
            for scheme in ("light", "dark"):
                L = f"[{vname}/{scheme}]"
                # logged out
                ctx = await br.new_context(color_scheme=scheme, **vopt); pg = await ctx.new_page()
                await audit(pg, L + " login", "/login", f"qa-{vname}-{scheme}-login.png")
                await audit(pg, L + " error page", "/nope", None)
                await pg.goto(B + "/login"); await pg.fill("input[name=username]", "alex"); await pg.fill("input[name=password]", "wrong password x")
                async with pg.expect_navigation(): await pg.press("input[name=password]", "Enter")
                rec(f"{L} wrong password shows error", "/login" in pg.url and await pg.locator("[role=alert]").count() > 0)
                await ctx.close()
                # admin
                ctx = await br.new_context(color_scheme=scheme, **vopt); pg = await ctx.new_page(); await login(pg, "alex", APW)
                rec(f"{L} admin login lands", "/login" not in pg.url, pg.url)
                for u in ["/", "/family", "/person/alex", "/person/alex/profile", "/person/sam", "/sites", "/calls", "/add", "/account/password"]:
                    await audit(pg, L + " admin", u, f"qa-{vname}-{scheme}-admin{u.replace('/', '_') or '_home'}.png")
                lid = await pg.evaluate("fetch('/person/alex').then(r=>r.text()).then(t=>(t.match(/\\/listing\\/(\\d+)/)||[])[1])")
                if lid: await audit(pg, L + " admin", f"/listing/{lid}", f"qa-{vname}-{scheme}-listing.png")
                # profile editor interaction: add a row, remove it, validation error on SSN
                await pg.goto(B + "/person/alex/profile"); n0 = await pg.locator("input[name=phone]").count()
                add = pg.locator("[data-add-row=phones]").first
                if await add.count(): await add.click(); rec(f"{L} profile add phone row", await pg.locator("input[name=phone]").count() == n0 + 1)
                else: rec(f"{L} profile add phone row", False, "no add button")
                await pg.locator("input[name=phone]").last.fill("123-45-6789"); 
                async with pg.expect_navigation(): await pg.locator("button:has-text('Save and re-scan')").last.click()
                rec(f"{L} profile rejects SSN with message", await pg.locator("text=/Social Security|SSN/i").count() > 0)
                if vname == "mobile":
                    await pg.goto(B + "/"); m = pg.locator("button[aria-label=Menu]")
                    if await m.count(): await m.first.click(); await pg.wait_for_timeout(400); rec(f"{L} mobile menu opens", await pg.locator("a[href='/family']").first.is_visible())
                await ctx.close()
                # member
                ctx = await br.new_context(color_scheme=scheme, **vopt); pg = await ctx.new_page()
                await login(pg, "sam", K2)
                if "/login" in pg.url: await login(pg, "sam", K1)   # first run: admin-set password, forced change below
                if "/account/password" in pg.url:
                    await audit(pg, L + " member forced password change", "/account/password", f"qa-{vname}-{scheme}-member-password.png")
                    await pg.fill("input[name=current]", K1); await pg.fill("input[name=password]", K2); await pg.fill("input[name=password2]", K2)
                    async with pg.expect_navigation(): await pg.press("input[name=password2]", "Enter"); await pg.wait_for_load_state()
                    rec(f"{L} member password change", "/account/password" not in pg.url, pg.url)
                for u in ["/", "/person/sam", "/person/sam/profile"]:
                    await audit(pg, L + " member", u, f"qa-{vname}-{scheme}-member{u.replace('/', '_') or '_home'}.png")
                for u in ["/person/alex", "/family", "/sites"]:
                    r = await pg.goto(B + u); rec(f"{L} member blocked {u}", r.status in (403, 404), str(r.status))
                rec(f"{L} member has no admin nav", await pg.locator("nav a[href='/family']").count() == 0)
                await ctx.close()
        await br.close()
    fails = [r for r in results if not r[1]]; print(f"\n{len(results)-len(fails)}/{len(results)} passed")
    json.dump(results, open(f"{OUT}/qa-results.json", "w"), indent=1); sys.exit(1 if fails else 0)
asyncio.run(main())
