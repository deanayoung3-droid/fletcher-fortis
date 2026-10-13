"""Generic opt-out form automation (Playwright). Only for forms WITHOUT a CAPTCHA.
dry=True (DRY_RUN on): loads the form, fills it and screenshots the filled form, NEVER clicks submit.
dry=False: fills, submits once, and looks for a success / check-your-email message. Anything unclear goes to "Needs you"."""
import re, os, json
CAPTCHA = re.compile(r"recaptcha|hcaptcha|turnstile|captcha|cf-chl|arkose|funcaptcha", re.I)
SUCCESS = re.compile(r"(request|submission) (has been |was )?(received|submitted|processed)|check your (e-?mail|inbox)|we(?:'ve| have) (sent|emailed)|confirmation (e-?mail|link)|thank you|success", re.I)
# (field-key, regex over name/id/placeholder/aria-label/label text)
FIELDS = [("first", r"first.?name|fname|given"), ("last", r"last.?name|lname|surname|family.?name"), ("full", r"^name$|full.?name|your.?name"),
          ("email", r"e-?mail"), ("phone", r"phone|mobile|tel"), ("street", r"street|address(?!.*e-?mail)|addr1"), ("city", r"city|town"),
          ("state", r"state|region|province"), ("url", r"url|link|listing|profile|record"), ("middle", r"middle")]
DESCRIBE_JS = r"""() => [...document.querySelectorAll('input,select,textarea')].filter(e => e.offsetParent !== null || e.type==='hidden').map((e, i) => {
  e.setAttribute('data-ff-i', i); const lab = (e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`)) || e.closest('label');
  return {i, tag: e.tagName.toLowerCase(), type: (e.type||'').toLowerCase(), key: [e.name, e.id, e.placeholder, e.getAttribute('aria-label'), lab ? lab.innerText : ''].join(' ').toLowerCase()} })"""

def values(p, prof, listing_url):
    import profiles
    legal = next((n for n in prof["names"] if n["kind"] == "legal"), prof["names"][0])
    cur = next((a for a in prof["addresses"] if a.get("current")), (prof["addresses"] or [{}])[0])
    from remover import contact_email
    return {"first": legal["first"], "middle": legal.get("middle", ""), "last": legal["last"], "full": profiles.full(legal), "email": contact_email(p),
            "phone": profiles.fmt_phone((prof["phones"] or [""])[0]), "street": cur.get("street", ""), "city": cur.get("city", ""), "state": cur.get("state", ""), "url": listing_url or ""}

async def fill(page, url, vals, shot_path, dry=True, captcha_flag=False, solver=False, listing_id=None):
    """Returns (state, detail). state: dry_run_filled | submitted | needs_human | error"""
    try: await page.goto(url, timeout=45000, wait_until="domcontentloaded"); await page.wait_for_timeout(2000)
    except Exception as e: return "error", f"could not load form: {str(e).splitlines()[0][:120]}"
    html = await page.content()
    if captcha_flag or (CAPTCHA.search(html) and not solver): return "needs_human", "form has a CAPTCHA (solver off)"
    tok = None
    if solver and CAPTCHA.search(html):   # optional, admin-enabled; automation.solve never calls a provider in dry run or over budget
        import automation, asyncio
        tok, why = await asyncio.to_thread(automation.solve, page.url, html, listing_id, dry)
        if not tok: return ("dry_run_filled" if dry else "needs_human"), why
    fields = await page.evaluate(DESCRIBE_JS); filled = []
    for f in fields:
        if f["type"] in ("hidden", "submit", "button", "file", "password"): continue
        if f["type"] == "checkbox":
            if re.search(r"agree|confirm|certify|consent|i am|resident|terms", f["key"]): await page.check(f'[data-ff-i="{f["i"]}"]'); filled.append("checkbox")
            continue
        for k, rx in FIELDS:
            if re.search(rx, f["key"]) and vals.get(k):
                sel = f'[data-ff-i="{f["i"]}"]'
                try:
                    if f["tag"] == "select":
                        try: await page.select_option(sel, value=vals[k])
                        except Exception: await page.select_option(sel, label=vals[k])
                    else: await page.fill(sel, vals[k])
                    filled.append(k)
                except Exception: pass
                break
    if not any(k in filled for k in ("email", "url", "first", "full")): return "needs_human", f"could not recognise the form fields (filled: {filled or 'none'})"
    os.makedirs(os.path.dirname(shot_path), exist_ok=True); await page.screenshot(path=shot_path, full_page=True)
    if tok:
        import automation; await page.evaluate(automation.INJECT_JS, list(tok))
    if dry: return "dry_run_filled", f"filled {', '.join(filled)}; not submitted (DRY_RUN)"
    btn = await page.query_selector("button[type=submit], input[type=submit], form button:not([type=button])")
    if not btn: return "needs_human", "no submit button found"
    await btn.click(); await page.wait_for_timeout(4000)
    body = await page.inner_text("body")
    if SUCCESS.search(body): return "submitted", f"submitted ({', '.join(filled)}); site said: {SUCCESS.search(body).group(0)}"
    return "needs_human", "submitted but no success message was recognised; please check the form"
