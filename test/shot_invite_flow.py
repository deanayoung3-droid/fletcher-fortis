"""Capture and audit every public (and related admin) state of the outside-person intake flow."""
from __future__ import annotations
import os, time, socket, threading, json, re
from pathlib import Path

WIDTHS = (375, 768, 1280)
# Extra width from the original iPhone report, used only for the filled intake card.
EXTRA = (390,)
HEIGHT = 900
SIG = json.dumps([[[round(i / 20, 4), 0.42] for i in range(12)]])

FAKE = {
    "name": "Jordan Rivera",
    "long_name": "Alexandria Maximiliana Christopher-Montgomery",
    "street": "9 Fake Street",
    "long_street": "1234 North West Extended Riverside Boulevard Suite 2100",
    "city": "Springfield",
    "state": "CA",
    "email": "jordan.rivera@example.com",
    "phone": "(555) 555-0100",
    "prev": "Jordan Lane",
}

OVERFLOW_JS = """() => {
  const docW = document.documentElement.clientWidth;
  const sw = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (r.right > docW + 1.5 && r.left < docW) {
      const name = (el.id && ('#' + el.id)) || (typeof el.className === 'string' && el.className.slice(0, 80)) || el.tagName;
      offenders.push({name, right: Math.round(r.right), w: Math.round(r.width), tag: el.tagName});
      if (offenders.length >= 8) break;
    }
  }
  return {pageOverflow: sw > docW + 1, scrollWidth: sw, clientWidth: docW, offenders};
}"""

UNLABELED_JS = """() => {
  const issues = [];
  const root = document.querySelector('.invite-flow') || document;
  const hidden = el => {
    if (el.type === 'hidden') return true;
    if (el.getAttribute('aria-hidden') === 'true') return true;
    if (el.hasAttribute('hidden')) return true;
    if (el.closest('template, [hidden], [aria-hidden="true"]')) return true;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return true;
    return false;
  };
  const text = el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
  for (const el of root.querySelectorAll('input, select, textarea, button')) {
    if (hidden(el)) continue;
    if (el.tagName === 'BUTTON' || el.type === 'submit' || el.type === 'button' || el.type === 'reset') {
      const name = (el.getAttribute('aria-label') || text(el) || el.getAttribute('title') || '').trim();
      if (!name) issues.push({kind: 'button', id: el.id, html: el.outerHTML.slice(0, 160)});
      continue;
    }
    const labeled = (el.labels && el.labels.length > 0)
      || !!el.closest('label')
      || !!el.getAttribute('aria-label')
      || !!el.getAttribute('aria-labelledby');
    const ph = (el.getAttribute('placeholder') || '').trim();
    if (!labeled && !ph) issues.push({kind: 'unlabeled', name: el.name, type: el.type, id: el.id});
  }
  return issues;
}"""

SMALL_INPUT_JS = """() => {
  const bad = [];
  const root = document.querySelector('.invite-flow') || document;
  for (const el of root.querySelectorAll('input:not([type=hidden]):not([type=radio]):not([type=checkbox]):not([type=file]), select, textarea')) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || el.closest('template, [hidden]')) continue;
    const px = parseFloat(st.fontSize) || 0;
    const box = el.getBoundingClientRect();
    if (px && px < 16) bad.push({id: el.id, name: el.name, font: px});
    if (box.height && box.height < 40 && el.type !== 'radio' && el.type !== 'checkbox')
      bad.push({id: el.id, name: el.name, height: Math.round(box.height)});
  }
  return bad;
}"""


def start_server(app, host="127.0.0.1"):
    import uvicorn
    sock = socket.socket(); sock.bind((host, 0)); port = sock.getsockname()[1]; sock.close()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True); t.start()
    deadline = time.time() + 15
    import urllib.request
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=0.4)
            return server, f"http://{host}:{port}"
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("test server did not start")


def stop_server(server):
    server.should_exit = True
    for _ in range(50):
        if not server.started:
            return
        time.sleep(0.05)


def set_viewport(page, width, height=HEIGHT):
    page.set_viewport_size({"width": width, "height": height})


def shot(page, out: Path, name: str, width: int):
    if out is None:
        return
    out.mkdir(parents=True, exist_ok=True)
    # Sticky headers repeat mid-page in full-page captures; pin them for the file only.
    page.evaluate("""() => { document.querySelectorAll('header.sticky,.sticky').forEach(e => { e.dataset._pos = e.style.position; e.style.position = 'static'; }); }""")
    path = out / f"{name}-{width}.png"
    page.screenshot(path=str(path), full_page=True)
    page.evaluate("""() => { document.querySelectorAll('header.sticky,.sticky').forEach(e => { e.style.position = e.dataset._pos || ''; }); }""")
    return path


def audit_page(page, label, width, check_inputs=True, check_labels=True):
    ov = page.evaluate(OVERFLOW_JS)
    issues = []
    if ov["pageOverflow"] or ov["offenders"]:
        issues.append(f"{label} @{width}px overflow: scroll {ov['scrollWidth']}>{ov['clientWidth']} offenders={ov['offenders']}")
    if check_labels:
        unlabeled = page.evaluate(UNLABELED_JS)
        if unlabeled:
            issues.append(f"{label} @{width}px unlabeled/empty controls: {unlabeled}")
    if check_inputs and width <= 390:
        small = page.evaluate(SMALL_INPUT_JS)
        if small:
            issues.append(f"{label} @{width}px inputs under 16px or under 40px tall: {small}")
    return issues


def draw_signature(page):
    loc = page.locator("#sigpad")
    loc.wait_for()
    box = loc.bounding_box()
    if not box:
        raise RuntimeError("signature pad has no box")
    x, y, w, h = box["x"], box["y"], box["width"], box["height"]
    page.mouse.move(x + w * 0.12, y + h * 0.55)
    page.mouse.down()
    for i in range(1, 16):
        page.mouse.move(x + w * (0.12 + i * 0.05), y + h * (0.55 - 0.18 * (1 if i % 2 == 0 else -1) * 0.4), steps=2)
    page.mouse.up()
    page.wait_for_timeout(80)


def fill_core(page, long=False):
    name = FAKE["long_name"] if long else FAKE["name"]
    street = FAKE["long_street"] if long else FAKE["street"]
    page.fill("#full_name", name)
    page.fill("#addr-street-0", street)
    page.fill("#addr-city-0", FAKE["city"])
    page.fill("#addr-state-0", FAKE["state"])
    page.fill("#email-0", FAKE["email"])
    page.fill("#tn", name)


def add_rows(page, list_name, n):
    btn = page.locator(f'[data-add-row="{list_name}"]')
    for _ in range(n):
        btn.click()


def cookies_from_client(client, base):
    out = []
    for k, v in client.cookies.items():
        out.append({"name": k, "value": v, "url": base})
    return out


def prepare_tokens(intake, db, admin, csrf):
    """Create the tokens and people needed to visit every state. Fake names only."""
    import crypto
    tok, csrf_tok, _ = intake.create(None, "outside")
    # a person + intake link from the admin add form (nickname only)
    r = admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": "Casey Sample", "needs_auth": "1", "relationship": "outside", "email": ""})
    assert r.status_code == 303, r.text[:400]
    loc = r.headers["location"]
    pid = re.search(r"/person/(\w+)", loc).group(1)
    person_url = loc
    person_page = admin.get(person_url)
    m = re.search(r'id="ink"[^>]*value="([^"]+)"', person_page.text)
    person_tok = m.group(1).rstrip("/").split("/")[-1] if m else None
    # family invite
    r2 = admin.post("/add", data={"csrf": csrf(admin, "/add"), "full_name": "Quinn Example", "city": "Springfield", "state": "CA", "needs_auth": "1", "email": "quinn@example.org"})
    fam_tok = re.search(r"inv=([\w-]+)", r2.headers["location"]).group(1)
    # expired
    exp, _, _ = intake.create(None, "outside")
    c = db.conn()
    c.execute("UPDATE intake_links SET expires=? WHERE token_hash=?", ("2000-01-01T00:00:00", crypto.token_hash(exp)))
    c.commit()
    # used (completed)
    used, used_csrf, _ = intake.create(None, "outside")
    body = {"csrf": used_csrf, "full_name": "Morgan Sample", "addr_city": "Springfield", "addr_state": "CA",
            "email": "morgan.sample@example.com", "typed_name": "Morgan Sample", "agree": "yes", "sig": SIG}
    done = __import__("fastapi").testclient.TestClient  # placeholder, caller posts
    return {
        "intake": (tok, csrf_tok),
        "person_id": pid,
        "person_url": person_url,
        "person_tok": person_tok,
        "family_invite": fam_tok,
        "expired": exp,
        "used_fresh": (used, used_csrf),
        "used_body": body,
    }


def run_audit_and_shots(page, context, base, client, admin, intake, db, csrf, out: Path | None, widths=WIDTHS):
    """Walk every state. Returns a list of issue strings (empty = clean)."""
    import intake as intake_mod
    issues = []
    data = prepare_tokens(intake, db, admin, csrf)
    tok, _ = data["intake"]
    form_url = f"{base}/invite/intake/{tok}"

    def visit(url, name, width, ready=None, check_inputs=True, check_labels=True):
        set_viewport(page, width)
        page.goto(url, wait_until="networkidle")
        if ready:
            ready()
        if out:
            shot(page, out, name, width)
        issues.extend(audit_page(page, name, width, check_inputs=check_inputs, check_labels=check_labels))

    # --- public intake, empty ---
    for w in widths:
        visit(form_url, "intake-empty", w)

    # --- filled with fake current + past address ---
    def fill_two():
        fill_core(page, long=False)
        page.fill("#phone-0", FAKE["phone"])
        page.fill("#prev-name-0", FAKE["prev"])
        page.click('[data-add-row="addrs"]')
        page.fill("#addr-street-1", "88 Former Ave")
        page.fill("#addr-city-1", "Shelbyville")
        page.fill("#addr-state-1", "IL")
        page.fill("#age_min", "25")
        page.fill("#age_max", "34")

    set_viewport(page, 375)
    page.goto(form_url, wait_until="networkidle")
    fill_two()
    for w in (*widths, *EXTRA):
        set_viewport(page, w)
        page.wait_for_timeout(50)
        if out:
            shot(page, out, "intake-filled", w)
        issues.extend(audit_page(page, "intake-filled", w))

    # --- long names, long addresses, many rows ---
    def fill_many():
        fill_core(page, long=True)
        add_rows(page, "prevs", 2)
        page.fill("#prev-name-0", "Alexandria Lane-Montgomery")
        page.fill("#prev-name-1", "A. M. Christopher")
        add_rows(page, "addrs", 3)
        for i in range(1, 4):
            page.fill(f"#addr-street-{i}", FAKE["long_street"])
            page.fill(f"#addr-city-{i}", "North Springfield-upon-River")
            page.fill(f"#addr-state-{i}", "CA")
        add_rows(page, "phones", 2)
        page.fill("#phone-0", FAKE["phone"])
        page.fill("#phone-1", "(555) 555-0199")
        add_rows(page, "emails", 1)
        page.fill("#email-1", "jordan.rivera.alt@example.com")
        page.fill("#age_min", "18")
        page.fill("#age_max", "120")

    set_viewport(page, 375)
    page.goto(form_url, wait_until="networkidle")
    fill_many()
    for w in widths:
        set_viewport(page, w)
        page.wait_for_timeout(50)
        if out:
            shot(page, out, "intake-long-many", w)
        issues.extend(audit_page(page, "intake-long-many", w))

    # --- HTML5 required validation (empty submit) ---
    for w in widths:
        set_viewport(page, w)
        page.goto(form_url, wait_until="networkidle")
        page.click("[data-sigsubmit]")
        if out:
            shot(page, out, "intake-html-validation", w)
        issues.extend(audit_page(page, "intake-html-validation", w))

    # --- server validation error ---
    def post_error():
        fill_core(page)
        page.fill("#tn", "Jordan River")  # mismatch: server rejects the typed name
        page.check("input[name=agree]")
        draw_signature(page)
        page.click("[data-sigsubmit]")
        page.wait_for_selector("[role=alert]")

    for w in widths:
        # need a fresh token so we don't consume the main one
        t, ctok, _ = intake.create(None, "outside")
        set_viewport(page, w)
        page.goto(f"{base}/invite/intake/{t}", wait_until="networkidle")
        post_error()
        if out:
            shot(page, out, "intake-server-error", w)
        issues.extend(audit_page(page, "intake-server-error", w))

    # --- e-sign pad + consent, then client-side "please draw" ---
    def fill_except_sig():
        fill_core(page)
        page.check("input[name=agree]")
        page.locator("#sigpad").scroll_into_view_if_needed()

    for w in widths:
        set_viewport(page, w)
        page.goto(form_url, wait_until="networkidle")
        fill_except_sig()
        if out:
            shot(page, out, "intake-esign", w)
        page.click("[data-sigsubmit]")
        if out:
            shot(page, out, "intake-esign-error", w)
        issues.extend(audit_page(page, "intake-esign", w))
        issues.extend(audit_page(page, "intake-esign-error", w))

    # --- drawn signature + disabled/signing button ---
    set_viewport(page, 375)
    page.goto(form_url, wait_until="networkidle")
    fill_except_sig()
    draw_signature(page)
    page.evaluate("""() => {
      const b = document.querySelector('[data-sigsubmit]');
      b.disabled = true; b.setAttribute('data-busy','1'); b.setAttribute('aria-busy','true');
    }""")
    page.locator("[data-sigsubmit]").scroll_into_view_if_needed()
    for w in widths:
        set_viewport(page, w)
        if out:
            shot(page, out, "intake-signing", w)
        issues.extend(audit_page(page, "intake-signing", w, check_inputs=False))

    # --- success ---
    used_tok, used_csrf = data["used_fresh"]
    from fastapi.testclient import TestClient
    # post via test client then open the used? We want the 200 thank-you HTML.
    # Easier: complete via Playwright on a fresh token (keeps the flow honest).
    fresh, _, _ = intake.create(None, "outside")
    set_viewport(page, 375)
    page.goto(f"{base}/invite/intake/{fresh}", wait_until="networkidle")
    fill_except_sig()
    draw_signature(page)
    page.click("[data-sigsubmit]")
    page.wait_for_selector("text=Your authorization is stored")
    for w in widths:
        set_viewport(page, w)
        if out:
            shot(page, out, "intake-done", w)
        issues.extend(audit_page(page, "intake-done", w, check_inputs=False))

    # used (the one we just submitted)
    for w in widths:
        visit(f"{base}/invite/intake/{fresh}", "intake-used", w, check_inputs=False)

    # expired
    for w in widths:
        visit(f"{base}/invite/intake/{data['expired']}", "intake-expired", w, check_inputs=False)

    # invalid / missing
    for w in widths:
        visit(f"{base}/invite/intake/not-a-real-token", "intake-invalid", w, check_inputs=False)

    # rate limited
    old = intake_mod.GET_LIMIT
    try:
        intake_mod.GET_LIMIT = 0
        tlim, _, _ = intake.create(None, "outside")
        for w in widths:
            visit(f"{base}/invite/intake/{tlim}", "intake-rate-limited", w, check_inputs=False)
    finally:
        intake_mod.GET_LIMIT = old

    # family invite password page
    for w in widths:
        visit(f"{base}/invite/{data['family_invite']}", "invite-password", w)

    # --- admin sides of the flow ---
    context.add_cookies(cookies_from_client(admin, base))

    def admin_ready():
        page.wait_for_selector("text=Add a person, text=Waiting on their intake form, text=Authorization, text=Paper authorization", timeout=8000)

    for w in widths:
        visit(f"{base}/add?relationship=outside", "admin-add-outside", w)

    for w in widths:
        visit(base + data["person_url"], "admin-person-intake-link", w, check_inputs=False, check_labels=False)
        # the link field is 16px now; table/other page chrome might be excluded from input check

    # paper upload + e-sign (admin viewing the outside person, cannot e-sign for them)
    for w in widths:
        visit(f"{base}/person/{data['person_id']}/authorize", "admin-esign-paper", w, check_inputs=False, check_labels=False)

    # an e-sign pad the person themselves can use: sign in as... we don't have a family login for Quinn easily.
    # Use alex (admin) own authorization page if they can sign, plus a cleared-pad state.
    # alex is typically the seeded admin person.
    r = admin.get("/person/alex/authorize")
    if r.status_code == 200 and "sigpad" in r.text:
        for w in widths:
            set_viewport(page, w)
            page.goto(f"{base}/person/alex/authorize", wait_until="networkidle")
            if out:
                shot(page, out, "admin-esign-pad", w)
            issues.extend(audit_page(page, "admin-esign-pad", w))
    elif r.status_code == 200:
        for w in widths:
            visit(f"{base}/person/alex/authorize", "admin-esign-pad", w, check_inputs=False)

    return issues
