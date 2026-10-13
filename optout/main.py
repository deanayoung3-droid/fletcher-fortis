import os, json, secrets, threading, time, asyncio, datetime, re, contextvars, logging
from urllib.parse import urlsplit, quote
from fastapi import FastAPI, Request, Form, UploadFile, File, Depends, HTTPException
from fastapi.responses import Response, HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup, escape
from starlette.exceptions import HTTPException as StarletteHTTPException
import db, profiles, remover, scanner, mailfilter, summary, calls, inbound, auth, crypto, esign, golive, charts, config, lifecycle, letters, mailer, automation
from db import now, event
from crypto import enc
import logsafe; logsafe.install()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
T = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
C = db.init(); profiles.seed(C); auth.bootstrap_admin(); config.load()
CUR = contextvars.ContextVar("user", default=None)
PID_RE = re.compile(r"^[a-z0-9_]{1,32}$")
STATUS = {"candidate": ("Needs review", "amber"), "not_me": ("Not me", "gray"), "approved": ("Approved · queued", "blue"), "submitted": ("Submitted", "blue"),
          "pending": ("Pending", "blue"), "needs_human": ("Needs you", "purple"), "confirmed_removed": ("Removed", "green"),
          "reappeared": ("Reappeared", "red"), "blocked_auth": ("Awaiting signed auth", "red"),
          "manual_check": ("Check manually (site blocked bots)", "purple"), "checked_clear": ("Checked · not listed", "gray")}
T.env.globals.update(has_auth=remover.has_auth, provider_ready=remover.provider_ready, FROM_ADDRESS=remover.FROM_ADDRESS, ESIGN_STATEMENT=esign.STATEMENT, STATUS=STATUS, DRY=remover.DRY, SITE=remover.SITE, KEY_SOURCE=crypto.KEY_SOURCE)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

def _user(): return CUR.get()
def _people():
    u = _user()
    if not u: return []
    if u["role"] == "admin": return C.execute("SELECT * FROM people ORDER BY COALESCE((SELECT 1 FROM users WHERE users.person_id=people.id AND users.role='admin'),0) DESC, rowid").fetchall()
    return C.execute("SELECT * FROM people WHERE id=?", (u["person_id"],)).fetchall()
def _queue():
    ids = [p["id"] for p in _people()]
    if not ids: return []
    auto = automation.pending(); return [l for l in C.execute(f"SELECT l.*, p.full_name, p.first FROM listings l JOIN people p ON p.id=l.person_id WHERE status IN ('needs_human','manual_check') AND l.person_id IN ({','.join('?'*len(ids))}) ORDER BY l.status, l.id", ids).fetchall() if l["id"] not in auto]  # nosec B608
def _csrf_field():
    u = _user(); return Markup(f'<input type="hidden" name="csrf" value="{escape(u["csrf"])}">') if u else Markup("")  # nosec B704
def _batch_needs(groups):
    out = []
    for pid, ls in groups:
        for st in ("needs_human", "manual_check"):
            sub = [l for l in ls if l["status"] == st]
            if sub: out.append(((pid, st), sub))
    return out
T.env.filters["batch_needs"] = _batch_needs
T.env.filters["site_name"] = lambda k: remover.SITE.get(k, {}).get("name", k)
def refresh_globals():
    """Branding + email flags for templates; called after Settings are saved (no restart)."""
    T.env.globals.update(APP=config.get("APP_NAME") or "Fletcher Fortis", BRAND_LOGO=config.get("LOGO_DATA") or "", BRAND=(config.get("BRAND_COLOR") if (config.get("BRAND_COLOR") or "").lower() not in ("", "#5559e3") else ""),
                         DRY=remover.DRY, FROM_ADDRESS=remover.FROM_ADDRESS, provider_ready=remover.provider_ready, AUTO_REFILE=config.get("AUTO_REFILE") == "true", drop_needed=lifecycle.drop_needed)
T.env.globals.update(csrf=_csrf_field, me=_user, profile_of=profiles.get, fmt_phone=profiles.fmt_phone, fullname=profiles.full)
OPEN = ("candidate", "approved", "submitted", "pending", "needs_human", "reappeared", "blocked_auth")
def _domain(l):
    m = re.match(r"https?://([^/:]+)", l["url"] or ""); return (m.group(1) if m else l["site"]).removeprefix("www.")
def stats(pid=None):
    q = "SELECT status, COUNT(*) n FROM listings" + (" WHERE person_id=?" if pid else "") + " GROUP BY status"  # nosec B608
    return {r["status"]: r["n"] for r in C.execute(q, (pid,) if pid else ())}
HIDE = ("not_me", "manual_check", "checked_clear")

# ---------- security middleware ----------
PUBLIC = ("/login", "/static/", "/healthz", "/inbound/email", "/twilio/", "/invite/", "/favicon.ico", "/setup")
_HAS_USERS = [False]
def has_users():
    if not _HAS_USERS[0]: _HAS_USERS[0] = bool(db.conn().execute("SELECT 1 FROM users LIMIT 1").fetchone())
    return _HAS_USERS[0]
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"
def _allowed_hosts(r):
    return {h for h in (r.headers.get("host", ""), urlsplit(os.environ.get("PUBLIC_URL", "")).netloc) if h}

@app.middleware("http")
async def security(r: Request, call_next):
    u = auth.session(r); r.state.user = u; tokv = CUR.set(u)
    try:
        path = r.url.path; public = any(path == p or (p.endswith("/") and path.startswith(p)) for p in PUBLIC)
        if not has_users() and not path.startswith(("/setup", "/static/", "/healthz", "/favicon.ico")):   # first run: setup wizard only
            return _headers(RedirectResponse("/setup", 303))
        if path == "/setup" and has_users(): return _headers(RedirectResponse("/login", 303))
        if r.method == "POST" and not path.startswith(("/inbound/", "/twilio/")):
            o = r.headers.get("origin") or r.headers.get("referer")
            if o and urlsplit(o).netloc not in _allowed_hosts(r): resp = Response("Cross-site request blocked", 403); return _headers(resp)
        if not public and not u:
            if r.method == "GET" and "application/json" not in r.headers.get("accept", ""):
                return _headers(RedirectResponse("/login" + (f"?next={quote(path)}" if path not in ("/", "/logout") else ""), 303))
            return _headers(JSONResponse({"detail": "login required"}, 401))
        if u and u["must_change"] and not public and path not in ("/account/password", "/logout"):
            return _headers(RedirectResponse("/account/password", 303))
        return _headers(await call_next(r))
    finally: CUR.reset(tokv)

def _headers(resp):
    # Referrer-Policy is same-origin, not no-referrer: with no-referrer Chrome sends "Origin: null" on form posts and the Origin check above rejects them.
    h = resp.headers
    if "frame-ancestors 'self'" in h.get("content-security-policy", ""): h["X-Frame-Options"] = "SAMEORIGIN"   # email preview iframe only (no scripts)
    else: h["Content-Security-Policy"] = CSP; h["X-Frame-Options"] = "DENY"
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "same-origin"; h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"; h["Cross-Origin-Resource-Policy"] = "same-origin"
    if auth.SECURE: h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    if "cache-control" not in h: h["Cache-Control"] = "no-store"
    return resp

async def csrf(r: Request):
    """Dependency on every state-changing route: the per-session token must come back in the form or the X-CSRF-Token header."""
    sent = r.headers.get("x-csrf-token")
    if not sent:
        try: sent = (await r.form()).get("csrf")
        except Exception: sent = None
    if not auth.csrf_ok(r, sent): raise HTTPException(403, "Your session expired or the form was stale. Reload the page and try again.")

@app.exception_handler(StarletteHTTPException)
async def http_err(r: Request, e: StarletteHTTPException):
    if "text/html" in r.headers.get("accept", "") or r.method == "GET":
        msg = {404: "We couldn't find that page, or it belongs to someone else.", 403: e.detail if isinstance(e.detail, str) and e.detail != "Forbidden" else "You don't have access to that.",
               401: "Please sign in.", 413: "That file is too large."}.get(e.status_code, e.detail if isinstance(e.detail, str) else "Something went wrong.")
        return _headers(T.TemplateResponse(r, "error.html", {"code": e.status_code, "msg": msg}, status_code=e.status_code))
    return _headers(JSONResponse({"detail": e.detail}, status_code=e.status_code))

@app.exception_handler(Exception)
async def crash(r: Request, e: Exception):
    logging.getLogger("ff").error("unhandled %s on %s", type(e).__name__, r.url.path)
    return _headers(T.TemplateResponse(r, "error.html", {"code": 500, "msg": "Something went wrong on our side. It has been logged."}, status_code=500))

if os.environ.get("TWILIO_ENABLED", "false") == "true": app.include_router(calls.router)

def _listing_for(r, lid):
    l = C.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
    if not l: raise HTTPException(404)
    auth.require_person(r, l["person_id"]); return l
def _person_for(r, pid):
    if not PID_RE.match(pid or ""): raise HTTPException(404)
    auth.require_person(r, pid)
    p = C.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not p: raise HTTPException(404)
    return p
def _rescan(pid, why):
    if os.environ.get("SCAN_ON_EDIT", "true") != "true": return
    threading.Thread(target=lambda: asyncio.run(scanner.run_all(pid)), daemon=True).start(); event(C, "scan_started", why, pid)

# ---------- login / accounts ----------
def _safe_next(n): return n if n and n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"

@app.get("/login", response_class=HTMLResponse)
def login_page(r: Request, next: str = ""):
    if r.state.user: return RedirectResponse(_safe_next(next), 303)
    return T.TemplateResponse(r, "login.html", {"next": _safe_next(next), "error": None})

@app.post("/login")
def login(r: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    ip = auth.client_ip(r); username = username.strip().lower()[:64]
    if auth.throttled(username, ip):
        return T.TemplateResponse(r, "login.html", {"next": _safe_next(next), "error": "Too many attempts. Wait 15 minutes and try again."}, status_code=429)
    u = auth.authenticate(username, password)
    if not u:
        auth.record_fail(username, ip); time.sleep(0.3)
        return T.TemplateResponse(r, "login.html", {"next": _safe_next(next), "error": "That username and password don't match."}, status_code=401)
    auth.clear_fails(username); tok = auth.create_session(u["id"], ip)
    resp = RedirectResponse(_safe_next(next) if not u["must_change"] else "/account/password", 303); auth.set_cookie(resp, tok); return resp

@app.post("/logout")
def logout(r: Request, _=Depends(csrf)):
    auth.logout(r); resp = RedirectResponse("/login", 303); resp.delete_cookie(auth.COOKIE, path="/"); return resp

@app.get("/invite/{tok}", response_class=HTMLResponse)
def invite_page(r: Request, tok: str):
    u = auth.invite_user(tok)
    if not u: raise HTTPException(404)
    return T.TemplateResponse(r, "password.html", {"invite": tok, "uname": u["username"], "error": None, "first": True})

@app.post("/invite/{tok}")
def invite_set(r: Request, tok: str, password: str = Form(""), password2: str = Form("")):
    u = auth.invite_user(tok)
    if not u: raise HTTPException(404)
    err = "The two passwords don't match." if password != password2 else auth.pw_problem(password, u["username"])
    if err: return T.TemplateResponse(r, "password.html", {"invite": tok, "uname": u["username"], "error": err, "first": True}, status_code=400)
    c = db.conn(); c.execute("UPDATE users SET pw_hash=?, must_change=0, invite_hash=NULL, invite_expires=NULL WHERE id=?", (auth.hash_pw(password), u["id"])); c.commit()
    auth.end_user_sessions(u["id"]); tok2 = auth.create_session(u["id"], auth.client_ip(r))
    nxt = f"/person/{u['person_id']}/profile?onboard=1" if not u["onboarded"] else "/"
    resp = RedirectResponse(nxt, 303); auth.set_cookie(resp, tok2); return resp

@app.get("/account/password", response_class=HTMLResponse)
def pw_page(r: Request):
    return T.TemplateResponse(r, "password.html", {"invite": None, "uname": r.state.user["username"], "error": None, "first": bool(r.state.user["must_change"])})

@app.post("/account/password")
def pw_change(r: Request, current: str = Form(""), password: str = Form(""), password2: str = Form(""), _=Depends(csrf)):
    u = r.state.user; first = bool(u["must_change"])
    err = None
    if not auth.check_pw(current, db.conn().execute("SELECT pw_hash FROM users WHERE id=?", (u["id"],)).fetchone()["pw_hash"]): err = "Your current password is wrong."
    elif password != password2: err = "The two new passwords don't match."
    elif password == current: err = "Pick a new password that's different from the current one."
    else: err = auth.pw_problem(password, u["username"])
    if err: return T.TemplateResponse(r, "password.html", {"invite": None, "uname": u["username"], "error": err, "first": first}, status_code=400)
    c = db.conn(); c.execute("UPDATE users SET pw_hash=?, must_change=0 WHERE id=?", (auth.hash_pw(password), u["id"])); c.commit()
    auth.end_user_sessions(u["id"], keep=r.cookies.get(auth.COOKIE)); event(C, "password_changed", "", u["person_id"])
    return RedirectResponse("/?pw=1", 303)

# ---------- family admin ----------
@app.get("/family", response_class=HTMLResponse)
def family(r: Request, inv: str = "", who: str = "", err: str = "", sent: str = ""):
    auth.require_admin(r)
    users = {u["person_id"]: u for u in db.conn().execute("SELECT * FROM users").fetchall()}
    people = C.execute("SELECT * FROM people ORDER BY COALESCE((SELECT 1 FROM users WHERE users.person_id=people.id AND users.role='admin'),0) DESC, rowid").fetchall()
    link = f"{(config.get('PUBLIC_URL') or str(r.base_url)).rstrip('/')}/invite/{inv}" if inv and re.fullmatch(r"[\w-]{20,64}", inv) else None
    return T.TemplateResponse(r, "family.html", {"people": people, "users": users, "stats": {p["id"]: stats(p["id"]) for p in people}, "invite_link": link, "who": who, "err": err[:200], "generated": None, "sent": sent[:120]})

def _fam_err(pid, msg): return RedirectResponse(f"/family?who={pid}&err={quote(msg)}", 303)

@app.post("/family/{pid}/account")
def family_account(r: Request, pid: str, action: str = Form(...), username: str = Form(""), password: str = Form(""), _=Depends(csrf)):
    auth.require_admin(r); _person_for(r, pid); c = db.conn()
    u = c.execute("SELECT * FROM users WHERE person_id=?", (pid,)).fetchone()
    if action in ("invite", "password"):
        if not u:
            username = (username or pid).strip().lower()
            if not re.fullmatch(r"[a-z0-9._-]{3,32}", username): return _fam_err(pid, "Username: 3–32 lowercase letters, digits, dot, dash or underscore.")
            if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone(): return _fam_err(pid, "That username is taken.")
            c.execute("INSERT INTO users(username,role,person_id,must_change,created) VALUES(?,?,?,1,?)", (username, "member", pid, now())); c.commit()
            u = c.execute("SELECT * FROM users WHERE person_id=?", (pid,)).fetchone()
        if action == "invite":
            tok = auth.make_invite(u["id"]); event(C, "invite_created", "", pid); return RedirectResponse(f"/family?inv={tok}&who={pid}", 303)
        err = auth.pw_problem(password, u["username"])
        if err: return _fam_err(pid, err)
        c.execute("UPDATE users SET pw_hash=?, must_change=1 WHERE id=?", (auth.hash_pw(password), u["id"])); c.commit(); auth.end_user_sessions(u["id"])
        event(C, "initial_password_set", "", pid)
    elif action == "remove" and u and u["role"] != "admin":
        auth.end_user_sessions(u["id"]); c.execute("DELETE FROM users WHERE id=?", (u["id"],)); c.commit(); event(C, "account_removed", "", pid)
    return RedirectResponse("/family", 303)

@app.post("/person/{pid}/toggle/{field}")
def toggle(r: Request, pid: str, field: str, _=Depends(csrf)):
    auth.require_admin(r); _person_for(r, pid)
    if field not in ("enabled", "bulk_ccpa", "send_enabled"): raise HTTPException(400)
    C.execute(f"UPDATE people SET {field}=1-COALESCE({field},0) WHERE id=?", (pid,)); C.commit()  # nosec B608: field is allow-listed above
    p = C.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone(); event(C, f"{field}_{'on' if p[field] else 'off'}", "", pid)
    if field == "send_enabled" and p[field] and not remover.DRY: threading.Thread(target=remover.send_queued, args=(pid,), daemon=True).start()
    return RedirectResponse("/family" if (r.headers.get("referer") or "").endswith("/family") else f"/person/{pid}", 303)

def _heat(pid=None, days=84):
    """Exposure/scan activity per day for the heatmap: listings seen + removal events."""
    start = datetime.date.today() - datetime.timedelta(days=days - 1); cnt = {}
    q = "SELECT substr(ts,1,10) d, COUNT(*) n FROM events WHERE ts>=?" + (" AND person_id=?" if pid else "") + " GROUP BY d"  # nosec B608
    for r in C.execute(q, (start.isoformat(), pid) if pid else (start.isoformat(),)): cnt[r["d"]] = r["n"]
    return [((start + datetime.timedelta(i)).isoformat(), cnt.get((start + datetime.timedelta(i)).isoformat(), 0)) for i in range(days)]
def _domain(l):
    m = re.match(r"https?://([^/:]+)", l["url"] or ""); return (m.group(1) if m else l["site"]).removeprefix("www.")
OPEN = ("candidate", "approved", "submitted", "pending", "needs_human", "reappeared", "blocked_auth")
def _score(pid=None):
    if pid is None:
        ps = [_score(r["id"]) for r in _people()]
        if not ps: return {"value": 100, "grade": "A", "open": 0, "removed": 0}
        v = round(sum(x["value"] for x in ps) / len(ps))
        return {"value": v, "grade": "ABCDEFG"[min(6, (100 - v) // 13)], "open": sum(x["open"] for x in ps), "removed": sum(x["removed"] for x in ps)}
    s = stats(pid); n = sum(s.get(k, 0) for k in ("candidate", "blocked_auth", "needs_human")); prog = s.get("submitted", 0) + s.get("pending", 0) + s.get("approved", 0)
    v = max(0, min(100, 100 - 7 * n - 3 * prog - 15 * s.get("reappeared", 0) - min(20, s.get("manual_check", 0))))  # unchecked sites are unknown risk
    return {"value": v, "grade": "ABCDEFG"[min(6, (100 - v) // 13)], "open": n + prog + s.get("reappeared", 0), "removed": s.get("confirmed_removed", 0)}
def _months(pid=None, n=8):
    """Exposures found per month (lollipop chart)."""
    t = datetime.date.today().replace(day=1); ms = []
    for i in range(n - 1, -1, -1):
        y, m = divmod(t.year * 12 + t.month - 1 - i, 12); ms.append(f"{y}-{m+1:02d}")
    q = "SELECT substr(first_seen,1,7) m, COUNT(*) n FROM listings WHERE status NOT IN ('not_me','manual_check','checked_clear')" + (" AND person_id=?" if pid else "") + " GROUP BY m"  # nosec B608
    c = {r["m"]: r["n"] for r in C.execute(q, (pid,) if pid else ())}
    q = "SELECT substr(confirmed_at,1,7) m, COUNT(*) n FROM listings WHERE confirmed_at IS NOT NULL" + (" AND person_id=?" if pid else "") + " GROUP BY m"  # nosec B608
    rm = {r["m"]: r["n"] for r in C.execute(q, (pid,) if pid else ())}
    return [(m, datetime.date(int(m[:4]), int(m[5:]), 1).strftime("%b"), c.get(m, 0), rm.get(m, 0)) for m in ms]
def _weeks(pid=None, n=12):
    """New exposures found vs confirmed removed per week (Monday start)."""
    t = datetime.date.today(); t -= datetime.timedelta(t.weekday()); ws = [t - datetime.timedelta(weeks=i) for i in range(n - 1, -1, -1)]
    def bucket(col, extra=""):
        out = [0] * n
        for r in C.execute(f"SELECT substr({col},1,10) d FROM listings WHERE {col} IS NOT NULL {extra}" + (" AND person_id=?" if pid else ""), (pid,) if pid else ()):  # nosec B608
            try: d = datetime.date.fromisoformat(r["d"])
            except Exception: continue
            k = (d - ws[0]).days // 7
            if 0 <= k < n: out[k] += 1
        return out
    return [w.strftime("%b %-d") for w in ws], bucket("first_seen", "AND status NOT IN ('not_me','manual_check','checked_clear')"), bucket("confirmed_at")
T.env.globals.update(chart=charts.chart, weeks=_weeks)
T.env.globals.update(score=_score, months=_months, OPEN=OPEN, today=lambda: datetime.date.today())
T.env.globals.update(fam_list=_people, hq=_queue, heat=_heat, domain=_domain, pstats=lambda pid=None: stats(pid))
import math; T.env.filters.update(ord=ord, cos=math.cos, sin=math.sin); T.env.tests["startingwith"] = lambda s, p: str(s).startswith(p)
T.env.filters["j"] = lambda s: json.loads(s or "{}")



HEALTH_LABEL = {"works": ("Works", "green"), "blocked": ("Blocked · manual queue", "purple"), "unreachable_dc": ("Retry from home IP", "amber"),
                "dead": ("Dead / merged", "gray"), "no_search": ("No public search", "gray"), "broken": ("Definition broken", "red"), "unchecked": ("Not probed yet", "gray")}
def _coverage():
    """Per-site health = sites.yaml health (last full probe) overlaid with live results from scans (HEALTH_FILE)."""
    try: live = json.load(open(scanner.HEALTH_FILE))
    except Exception: live = {}
    rows = []
    for s_ in scanner.SITES:
        h = dict(s_.get("health") or {}); h.update(live.get(s_["id"]) or {})
        rows.append({"id": s_["id"], "name": s_.get("name", s_["id"]), "domain": s_.get("domain", ""), "parent": s_.get("parent"), "status": h.get("status", "unchecked"),
                     "evidence": h.get("evidence", ""), "checked": (h.get("checked") or "")[:10], "search": bool(s_.get("search_url")), "optout": s_.get("optout") or {}, "sources": s_.get("sources", [])})
    order = list(HEALTH_LABEL); rows.sort(key=lambda r_: (order.index(r_["status"]), r_["parent"] or "~", r_["name"].lower()))
    cnt = {k: sum(1 for r_ in rows if r_["status"] == k) for k in HEALTH_LABEL}
    return {"rows": rows, "count": cnt, "total": len(rows), "parents": sorted({r_["parent"] for r_ in rows if r_["parent"]})}
T.env.globals.update(coverage=_coverage, HEALTH_LABEL=HEALTH_LABEL)



# ---------- pages ----------
def _brokers():
    try:
        import yaml; d = yaml.safe_load(open(os.environ.get("BROKER_OUT", "/data/brokers_merged.yaml"))); return {"total": d["total"], "src": d["source_counts"], "ts": d["generated"]}
    except Exception: return None

@app.get("/", response_class=HTMLResponse)
def home(r: Request):
    u = r.state.user
    if u["role"] != "admin": return RedirectResponse(f"/person/{u['person_id']}" + ("?pw=1" if r.query_params.get("pw") else ""), 303)
    people = C.execute("SELECT * FROM people ORDER BY COALESCE((SELECT 1 FROM users WHERE users.person_id=people.id AND users.role='admin'),0) DESC, rowid").fetchall()
    return T.TemplateResponse(r, "home.html", {"people": people, "stats": {p["id"]: stats(p["id"]) for p in people}, "all": stats(),
        "brokers": _brokers(), "events": C.execute("SELECT * FROM events WHERE kind NOT IN ('scan_finished','scan_result') ORDER BY ts DESC, id DESC LIMIT 12").fetchall(),
        "alerts": C.execute("SELECT l.*, p.full_name FROM listings l JOIN people p ON p.id=l.person_id WHERE status='reappeared'").fetchall()})

@app.get("/person/{pid}", response_class=HTMLResponse)
def person(r: Request, pid: str, f: str = "", saved: str = "", signed: str = "", welcome: str = ""):
    p = _person_for(r, pid)
    if f and f not in STATUS: f = ""
    q = "SELECT * FROM listings WHERE person_id=?" + (" AND status=?" if f else " AND status NOT IN ('not_me','checked_clear')") + " ORDER BY CASE status WHEN 'reappeared' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, COALESCE(score,0) DESC, last_seen DESC"  # nosec B608
    return T.TemplateResponse(r, "person.html", {"p": p, "prof": profiles.get(p), "stats": stats(pid), "f": f, "listings": C.execute(q, (pid, f) if f else (pid,)).fetchall(),
        "outbox": C.execute("SELECT * FROM outbox WHERE person_id=? AND COALESCE(channel,'') NOT IN ('onboarding','weekly_test') ORDER BY id DESC LIMIT 8", (pid,)).fetchall(), "account": db.conn().execute("SELECT username, role, last_login FROM users WHERE person_id=?", (pid,)).fetchone(), "saved": bool(saved), "signed": bool(signed), "welcome": bool(welcome), "drop_needed": lifecycle.drop_needed(p)})

@app.get("/person/{pid}/profile", response_class=HTMLResponse)
def profile_page(r: Request, pid: str, saved: str = "", onboard: str = ""):
    p = _person_for(r, pid)
    prof = profiles.get(p); suggested = False
    if not prof["relatives"]:   # default: the other family members, still editable before saving
        prof["relatives"] = [x["full_name"] for x in C.execute("SELECT full_name FROM people WHERE id<>? ORDER BY rowid", (pid,)).fetchall() if x["full_name"] and len(x["full_name"].split()) >= 2][:20]; suggested = bool(prof["relatives"])
    return T.TemplateResponse(r, "profile.html", {"p": p, "prof": prof, "errors": [], "saved": bool(saved), "suggested": suggested, "onboard": bool(onboard)})

@app.post("/person/{pid}/profile")
async def profile_save(r: Request, pid: str, _=Depends(csrf)):
    p = _person_for(r, pid); form = await r.form()
    data = {k: form.getlist(k) for k in form.keys() if k != "csrf"}
    if sum(len(v) for v in data.values()) > 200 or any(len(str(x)) > 200 for v in data.values() for x in v): raise HTTPException(400, "Too much data in the form.")
    prof, errors = profiles.validate(data)
    if errors:
        return T.TemplateResponse(r, "profile.html", {"p": p, "prof": prof if prof["names"] else profiles.get(p), "errors": errors, "saved": False, "suggested": False, "onboard": bool(form.get("onboard"))}, status_code=400)
    profiles.save(C, pid, prof); event(C, "profile_updated", f"{len(prof['names'])} names, {len(prof['addresses'])} addresses, {len(prof['phones'])} phones", pid)
    _rescan(pid, "profile edited")
    _pp = C.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if form.get("onboard") and _pp["needs_auth"] and not remover.has_auth(_pp) and _can_sign(r, pid):
        return RedirectResponse(f"/person/{pid}/authorize?onboard=1", 303)
    if form.get("onboard"): _mark_onboarded(r); return RedirectResponse(f"/person/{pid}?welcome=1", 303)
    return RedirectResponse(f"/person/{pid}?saved=1", 303)

@app.get("/listing/{lid}", response_class=HTMLResponse)
def listing(r: Request, lid: int):
    l = _listing_for(r, lid)
    sib = C.execute("SELECT id, site, status FROM listings WHERE person_id=? AND dedupe=? AND id<>?", (l["person_id"], l["dedupe"], lid)).fetchall() if l["dedupe"] else []
    return T.TemplateResponse(r, "listing.html", {"l": l, "p": C.execute("SELECT * FROM people WHERE id=?", (l["person_id"],)).fetchone(), "siblings": sib,
        "method": remover.method(remover.SITE.get(l["site"], {}))[0], "events": C.execute("SELECT * FROM events WHERE listing_id=? ORDER BY id DESC LIMIT 20", (lid,)).fetchall()})

@app.post("/listing/{lid}/{action}")
def act(r: Request, lid: int, action: str, next: str = Form(""), _=Depends(csrf)):
    l = _listing_for(r, lid)
    if action == "approve": remover.approve(lid)
    elif action == "notme": C.execute("UPDATE listings SET status='not_me' WHERE id=?", (lid,)); event(C, "not_me", "", l["person_id"], lid)
    elif action == "clear": C.execute("UPDATE listings SET status='checked_clear', last_checked=? WHERE id=?", (now(), lid)); event(C, "manual_checked_clear", "", l["person_id"], lid); C.execute("DELETE FROM retry WHERE listing_id=?", (lid,)); automation.schedule(C, lid, "recheck")
    elif action == "done": C.execute("UPDATE listings SET status='submitted', submitted_at=? WHERE id=?", (now(), lid)); event(C, "form_done_by_human", "", l["person_id"], lid)
    elif action == "undo" and l["status"] == "not_me": C.execute("UPDATE listings SET status='candidate' WHERE id=?", (lid,)); event(C, "not_me_undone", "", l["person_id"], lid)
    else: raise HTTPException(400)
    C.commit(); return RedirectResponse(_safe_next(next) if next else f"/person/{l['person_id']}", 303)

@app.post("/person/{pid}/bulk")
def bulk(r: Request, pid: str, _=Depends(csrf)):
    auth.require_admin(r); _person_for(r, pid); event(C, "bulk_ccpa_started", "dry run" if remover.DRY else "", pid)
    threading.Thread(target=remover.bulk_ccpa, args=(pid,), daemon=True).start()   # can run for an hour (rate-limited); never block the request
    return RedirectResponse(f"/person/{pid}", 303)

@app.post("/person/{pid}/scan")
def scan_now(r: Request, pid: str, _=Depends(csrf)):
    _person_for(r, pid)
    threading.Thread(target=lambda: asyncio.run(scanner.run_all(pid)), daemon=True).start(); event(C, "scan_started", "manual", pid)
    return RedirectResponse(f"/person/{pid}", 303)

@app.post("/person/{pid}/add-listing")
def add_listing(r: Request, pid: str, site: str = Form(...), url: str = Form(...), _=Depends(csrf)):
    _person_for(r, pid)
    if site not in remover.SITE or not re.match(r"^https?://[^\s]{4,500}$", url): raise HTTPException(400, "Pick a site and paste the full listing URL (https://…).")
    C.execute("INSERT OR IGNORE INTO listings(person_id,site,url,fields,first_seen,last_seen,last_checked) VALUES(?,?,?,?,?,?,?)", (pid, site, enc(url), enc('{"snippet":"added manually"}'), now(), now(), now()))
    C.commit(); return RedirectResponse(f"/person/{pid}", 303)

@app.get("/person/{pid}/proof.zip")
def proof(r: Request, pid: str):
    """Proof pack: screenshots, every removal request in the outbox, the authorization PDF and a CSV log. Built locally, nothing is sent."""
    import zipfile, io, csv
    p = _person_for(r, pid)
    buf = io.BytesIO(); z = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED); log = io.StringIO(); w = csv.writer(log)
    w.writerow(["id", "site", "url", "status", "first_seen", "submitted_at", "confirmed_at"])
    for l in C.execute("SELECT * FROM listings WHERE person_id=?", (pid,)):
        w.writerow([l["id"], l["site"], l["url"], l["status"], l["first_seen"], l["submitted_at"], l["confirmed_at"]])
        sp = _safe_shot(l["screenshot"])
        if sp: z.write(sp, f"screenshots/{l['id']}-{l['site']}.png")
    for o in C.execute("SELECT * FROM outbox WHERE person_id=? AND COALESCE(channel,'') NOT IN ('onboarding','weekly_test')", (pid,)):
        z.writestr(f"requests/{o['id']}-{o['state'][:20]}.txt", f"To: {o['to_addr']}\nDate: {o['ts']}\nState: {o['state']}\nSubject: {o['subject']}\n\n{o['body']}")
    af = remover.auth_file(p)
    if af and os.path.exists(af): z.write(af, "authorization.pdf")
    z.writestr("listings.csv", log.getvalue()); z.close()
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{pid}-proof-pack.zip"'})

@app.get("/person/{pid}/reminders.ics")
def reminders(r: Request, pid: str):
    p = _person_for(r, pid)
    ev = [(_next_scan(), "Weekly re-scan for " + p["full_name"])]
    for l in C.execute("SELECT * FROM listings WHERE person_id=? AND status IN ('submitted','pending') AND submitted_at IS NOT NULL", (pid,)):
        ev.append((datetime.date.fromisoformat(l["submitted_at"][:10]) + datetime.timedelta(45), f"CCPA 45-day deadline: {SITE_NAME(l['site'])}"))
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Fletcher Fortis//EN"]
    for i, (d, t) in enumerate(ev):
        out += ["BEGIN:VEVENT", f"UID:{pid}-{i}-{d:%Y%m%d}@optout", f"DTSTAMP:{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}", f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"SUMMARY:{t}", "END:VEVENT"]
    return Response("\r\n".join(out + ["END:VCALENDAR"]), media_type="text/calendar", headers={"Content-Disposition": f'attachment; filename="{pid}-reminders.ics"'})

def SITE_NAME(k): return remover.SITE.get(k, {}).get("name", k)
def _next_scan():
    d = datetime.date.today(); wd = int(config.get("SCAN_DAY") or 6); return d + datetime.timedelta((wd - d.weekday()) % 7 or 7)
T.env.globals.update(next_scan=_next_scan, pevents=lambda pid, n=14: C.execute("SELECT * FROM events WHERE person_id=? AND kind NOT IN ('scan_finished','scan_result') ORDER BY ts DESC, id DESC LIMIT ?", (pid, n)).fetchall())

@app.get("/add", response_class=HTMLResponse)
def add_form(r: Request): auth.require_admin(r); return T.TemplateResponse(r, "add.html", {"errors": []})

@app.post("/add")
def add(r: Request, full_name: str = Form(...), city: str = Form(...), state: str = Form("CA"), needs_auth: str = Form("1"), _=Depends(csrf)):
    auth.require_admin(r); parts = full_name.split()
    prof, errors = profiles.validate({"name_kind": ["legal"], "name_first": [" ".join(parts[:-1])], "name_middle": [""], "name_last": [parts[-1] if parts else ""],
                                      "addr_street": [""], "addr_city": [city], "addr_state": [state], "addr_current": ["0"]})
    if len(parts) < 2: errors.insert(0, "Enter a first and last name.")
    if errors: return T.TemplateResponse(r, "add.html", {"errors": errors}, status_code=400)
    base = re.sub(r"[^a-z0-9]", "", parts[0].lower())[:24] or "member"; pid = base; i = 2
    while C.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone(): pid = f"{base}{i}"; i += 1
    profiles.new_person(C, pid, prof, needs_auth == "1"); event(C, "person_added", "", pid)
    return RedirectResponse(f"/person/{pid}/profile", 303)

def _safe_shot(path):
    """Only files inside SHOT_DIR, resolved (no ../ or symlink escapes)."""
    if not path: return None
    root = os.path.realpath(scanner.SHOTS); real = os.path.realpath(path)
    return real if real.startswith(root + os.sep) and os.path.isfile(real) else None

@app.get("/shot/{lid}")
def shot(r: Request, lid: int):
    c = db.conn(); l = c.execute("SELECT person_id, screenshot FROM listings WHERE id=?", (lid,)).fetchone(); c.close()
    if not l: raise HTTPException(404)
    auth.require_person(r, l["person_id"]); sp = _safe_shot(l["screenshot"])
    if not sp: raise HTTPException(404)
    return FileResponse(sp, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

@app.get("/calls", response_class=HTMLResponse)
def calls_page(r: Request):
    auth.require_admin(r)
    inbox = C.execute("SELECT i.*, p.full_name FROM inbox i LEFT JOIN people p ON p.id=i.person_id ORDER BY i.id DESC LIMIT 100").fetchall()
    return T.TemplateResponse(r, "calls.html", {"calls": C.execute("SELECT * FROM calls ORDER BY id DESC LIMIT 50").fetchall(),
        "replies": [m for m in inbox if m["kind"] not in ("junk", "phishing")], "junk": [m for m in inbox if m["kind"] in ("junk", "phishing")],
        "twilio": os.environ.get("TWILIO_ENABLED", "false") == "true", "inbound_on": bool(os.environ.get("INBOUND_SECRET")), "autoclick": inbound.AUTOCLICK,
        "mail": C.execute("SELECT * FROM events WHERE kind LIKE 'mail_%' OR kind LIKE 'broker_reply_%' ORDER BY id DESC LIMIT 50").fetchall()})

@app.get("/sites", response_class=HTMLResponse)
def sites_page(r: Request, status: str = "", q: str = ""):
    auth.require_admin(r); return T.TemplateResponse(r, "sites.html", {"cov": _coverage(), "fstatus": status, "fq": q})

@app.post("/inbound/email")
async def inbound_email(r: Request):
    """Raw RFC822 message from the Cloudflare Email Worker. Disabled (404) unless INBOUND_SECRET is set; the header is compared in constant time."""
    want = os.environ.get("INBOUND_SECRET", "")
    if not want: raise HTTPException(404)
    if not secrets.compare_digest(r.headers.get("x-inbound-secret", "").encode(), want.encode()): raise HTTPException(401)
    if int(r.headers.get("content-length") or 0) > 10_000_000: raise HTTPException(413)
    raw = await r.body()
    if not raw or len(raw) > 10_000_000: raise HTTPException(413 if raw else 400)
    res = inbound.handle(db.conn(), raw); return {"ok": True, "kind": res["kind"], "matched": bool(res["person"])}

# ---------- e-sign authorization ----------
def _can_sign(r, pid): u = r.state.user; return bool(u) and u["person_id"] == pid   # only the person themself, never the admin for someone else

@app.get("/person/{pid}/authorize", response_class=HTMLResponse)
def esign_page(r: Request, pid: str, onboard: str = ""):
    p = _person_for(r, pid)
    return T.TemplateResponse(r, "esign.html", {"p": p, "can_sign": _can_sign(r, pid), "error": None, "onboard": bool(onboard)})

def _mark_onboarded(r):
    u = r.state.user
    if u and not u["onboarded"]: c = db.conn(); c.execute("UPDATE users SET onboarded=1 WHERE id=?", (u["id"],)); c.commit()

@app.post("/person/{pid}/authorize")
def esign_submit(r: Request, pid: str, typed_name: str = Form(""), agree: str = Form(""), sig: str = Form(""), onboard: str = Form(""), _=Depends(csrf)):
    p = _person_for(r, pid)
    if not _can_sign(r, pid): raise HTTPException(403, f"Only {p['first'] or p['full_name']} can sign their own authorization, from their own login.")
    err = None
    if agree != "yes": err = "Tick the box to confirm the authorization."
    elif not esign.name_matches(typed_name[:120], p["full_name"]): err = f"Type your full legal name exactly as on your profile: {p['full_name']}."
    strokes = esign.parse_strokes(sig)
    if not err and not strokes: err = "Draw your signature in the box (finger, pen or mouse)."
    if err: return T.TemplateResponse(r, "esign.html", {"p": p, "can_sign": True, "error": err, "onboard": bool(onboard)}, status_code=400)
    ts = now(); ip = auth.client_ip(r); u = r.state.user
    esign.write_pdf(p, typed_name.strip(), ts, ip, u["username"], strokes)
    C.execute("UPDATE people SET esign_name=?, esign_at=?, esign_ip=?, esign_user=?, esign_sig=? WHERE id=?", (enc(typed_name.strip()), ts, enc(ip), u["username"], enc(json.dumps(strokes)), pid))
    C.execute("UPDATE listings SET status='candidate' WHERE person_id=? AND status='blocked_auth'", (pid,)); C.commit()
    event(C, "auth_esigned", f"@{u['username']}", pid)
    if onboard: _mark_onboarded(r); return RedirectResponse(f"/person/{pid}?welcome=1", 303)
    return RedirectResponse(f"/person/{pid}?signed=1", 303)

@app.post("/person/{pid}/drop-done")
def drop_done(r: Request, pid: str, next: str = Form(""), _=Depends(csrf)):
    _person_for(r, pid); C.execute("UPDATE people SET drop_at=? WHERE id=?", (now(), pid)); C.commit(); event(C, "drop_submitted", "CA DROP request submitted by the person", pid)
    return RedirectResponse(_safe_next(next) if next else f"/person/{pid}", 303)

# ---------- Needs you ----------
def _needs(show_snoozed=False):
    """Every task that only a human can do, grouped by person, with the reason in plain language."""
    sn = {r["k"]: r["until"] for r in C.execute("SELECT * FROM snooze WHERE until > ?", (now(),))}
    admin = _user() and _user()["role"] == "admin"; groups = []; hidden = 0
    auto = automation.pending(); solver = automation.solver_on(); robot = {"retrying": 0, "next": ""}
    for p in _people():
        first = p["first"] or p["full_name"].split()[0]; tasks = []
        def add(key, kind, title, why, actions, site=None):
            nonlocal hidden
            if key in sn and not show_snoozed: hidden += 1; return
            tasks.append({"key": key, "kind": kind, "title": title, "why": why, "actions": actions, "site": site, "snoozed": sn.get(key, "")[:10]})
        ls = C.execute("SELECT * FROM listings WHERE person_id=? AND status IN ('needs_human','manual_check','reappeared') ORDER BY status, site", (p["id"],)).fetchall()
        for l in ls:
            st = remover.SITE.get(l["site"], {}); name = st.get("name", l["site"]); key = f"l:{l['id']}"
            if l["status"] == "manual_check" and l["id"] in auto:
                robot["retrying"] += 1; robot["next"] = min(robot["next"] or auto[l["id"]][1], auto[l["id"]][1]); continue
            oo = st.get("optout") if isinstance(st.get("optout"), dict) else {}; req = oo.get("requires")
            if l["status"] == "needs_human" and req in ("id", "phone"):
                add(key, req, f"{name}: {'upload a photo ID' if req == 'id' else 'call to opt out'} for {first}", ("This broker only accepts removal requests with a photo ID. We never store or send ID documents, so the person has to do it on the broker's site." if req == "id" else "This broker only takes opt-outs by phone. A person has to make the call; we'll re-check that the listing is gone."),
                    [("link", "Open opt-out page", oo.get("url") or l["url"]), ("post", "Done", f"/listing/{l['id']}/done")], name); continue
            if l["status"] == "needs_human" and solver and not remover.DRY: continue   # the solver picks it up on the next hourly run
            if l["status"] == "needs_human":
                add(key, "captcha", f"Fill in {name}'s opt-out form", f"{name}'s removal form has a CAPTCHA. The optional CAPTCHA solver is off (Heartbeat > Settings > Automation), so a person has to submit it. It usually takes about 2 minutes. When you're done, mark it done and we'll keep re-checking that the listing disappears.",
                    [("link", "Open opt-out form", oo.get("url") or l["url"]), ("post", "I submitted it", f"/listing/{l['id']}/done")], name)
            elif l["status"] == "manual_check":
                add(key, "search", f"Check {name} for {first}", f"{name} kept blocking our search, including {len(automation.BACKOFF)} automatic retries from this server, so we can't tell whether {first} is listed there. Search the site yourself. If {first} isn't there, mark it not listed. If they are, open the task and we'll file the removal.",
                    [("link", "Search the site", l["url"] or ("https://" + st.get("domain", ""))), ("post", "Not listed", f"/listing/{l['id']}/clear"), ("go", "Found a listing", f"/listing/{l['id']}")], name)
            elif config.get("AUTO_REFILE") != "true":
                add(key, "reappeared", f"{name} listed {first} again", "This listing was removed and has come back. Approve it to send a new removal request (or turn on automatic re-filing in Heartbeat > Settings).",
                    [("post", "File again", f"/listing/{l['id']}/approve"), ("go", "View listing", f"/listing/{l['id']}")], name)
        if p["needs_auth"] and not remover.has_auth(p) and admin:
            add(f"auth:{p['id']}", "auth", f"Get {first}'s signature", f"Brokers only accept requests from an agent the person authorized themselves, so {first} has to draw their own signature. Until then their removals are paused. Send them an invite, or have them sign in and open Authorization.",
                [("go", "Invite or resend", f"/family?who={p['id']}")])
        if lifecycle.drop_needed(p):
            add(f"drop:{p['id']}", "drop", f"California DROP request for {first}", f"One request on the state's Delete Request and Opt-out Platform reaches 500+ registered data brokers at once. The state requires {first} to verify California residency personally, so we can't do it for them. It takes about 5 minutes.",
                [("link", "Open DROP", "https://privacy.ca.gov/drop/"), ("post", "Done", f"/person/{p['id']}/drop-done")])
        GW = {"search": f"These sites kept blocking our search after every automatic retry from this server, so we can't tell whether {first} is listed. Search each one yourself: mark it not listed, or open it if you find {first} and we'll file the removal.",
              "captcha": "These removal forms have a CAPTCHA and the optional solver is off (Heartbeat > Settings > Automation), so a person has to submit them (about 2 minutes each). Mark each one done and we'll keep re-checking that the listing disappears."}
        GT = {"search": "Check {n} blocked sites for " + first, "captcha": "Fill in {n} opt-out forms with a CAPTCHA"}
        merged = []
        for t in tasks:
            if t["kind"] in GW and merged and merged[-1]["kind"] == t["kind"]:
                g = merged[-1]
                if "batch" not in g: g.update(batch=[dict(g)], title=None, why=GW[t["kind"]], key=None)
                g["batch"].append(t); g["title"] = GT[t["kind"]].format(n=len(g["batch"]))
            else: merged.append(dict(t))
        if merged: groups.append((p, merged))
    _needs.robot = robot
    return groups, hidden

@app.get("/needs-you", response_class=HTMLResponse)
def needs_you(r: Request, snoozed: str = ""):
    groups, hidden = _needs(bool(snoozed))
    return T.TemplateResponse(r, "needs.html", {"groups": groups, "hidden": hidden, "show_snoozed": bool(snoozed), "total": sum(len(t) for _, t in groups), "robot": _needs.robot, "solver": automation.solver_on(), "fc": automation.expected(C)})

@app.post("/needs-you/snooze")
def needs_snooze(r: Request, key: str = Form(...), days: int = Form(7), _=Depends(csrf)):
    kind, _x, ref = key.partition(":")
    if kind == "l": _listing_for(r, int(ref) if ref.isdigit() else -1)
    elif kind in ("auth", "drop"): _person_for(r, ref)
    else: raise HTTPException(400)
    if days <= 0: C.execute("DELETE FROM snooze WHERE k=?", (key,))
    else: C.execute("INSERT INTO snooze(k,until) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET until=excluded.until", (key, (datetime.datetime.now() + datetime.timedelta(days=min(days, 90))).isoformat(timespec="seconds")))
    C.commit(); return RedirectResponse("/needs-you", 303)
T.env.globals.update(auto_estimate=lambda: automation.estimate(C), HUMAN_ONLY=automation.HUMAN_ONLY, needs_count=lambda: sum(len(t) for _, t in _needs()[0]))

@app.get("/person/{pid}/authorization.pdf")
def esign_pdf(r: Request, pid: str):
    p = _person_for(r, pid); f = remover.auth_file(p)
    if not f or not os.path.exists(f): raise HTTPException(404)
    return FileResponse(f, media_type="application/pdf", filename=f"{pid}-authorization.pdf", headers={"Cache-Control": "private, no-store"})

@app.post("/person/{pid}/sender")
def set_sender(r: Request, pid: str, send_from: str = Form(""), _=Depends(csrf)):
    auth.require_admin(r); _person_for(r, pid)
    if not remover.provider_ready(): raise HTTPException(400, "Available once email is set up (a sending provider must be configured).")
    v = send_from.strip().lower()
    if v and not profiles.EMAIL_OK.match(v): return _fam_err(pid, "That send-from / reply email isn't valid.")
    C.execute("UPDATE people SET send_from=? WHERE id=?", (enc(v) if v else None, pid)); C.commit(); event(C, "sender_set", v or "default", pid)
    return RedirectResponse(f"/family?who={pid}", 303)

# ---------- admin: logins, weekly email preview, go-live ----------
@app.post("/family/generate-logins", response_class=HTMLResponse)
def gen_logins(r: Request, _=Depends(csrf)):
    auth.require_admin(r)
    rows = auth.generate_family_logins(db.conn(), C.execute("SELECT * FROM people ORDER BY rowid").fetchall())
    event(C, "logins_generated", f"{len(rows)} accounts")
    resp = family(r); ctx = {k: v for k, v in resp.context.items() if k != "request"}; ctx["generated"] = rows   # shown once, never stored; page is Cache-Control: no-store
    return T.TemplateResponse(r, "family.html", ctx)

@app.get("/admin/go-live")
def go_live_old(): return RedirectResponse("/admin/heartbeat", 301)

@app.get("/admin/heartbeat", response_class=HTMLResponse)
def heartbeat(r: Request, check: str = "", saved: str = "", tab: str = "status", msg: str = ""):
    auth.require_admin(r); import heartbeat as hb
    people = C.execute("SELECT * FROM people ORDER BY rowid").fetchall()
    return T.TemplateResponse(r, "heartbeat.html", {"status": hb.status(C), "items": golive.checklist(C), "dns": golive.dns_checks() if check else None, "domain": golive.domain(),
        "fields": config.FIELDS, "groups": config.groups(), "cfg": config, "saved": saved, "tab": tab, "people": people, "msg": msg[:200], "lifecycle": lifecycle.LIFECYCLE})

@app.post("/admin/heartbeat/settings")
async def heartbeat_save(r: Request, _=Depends(csrf)):
    auth.require_admin(r); form = await r.form(); vals = {}; errs = []
    for k, (label, group, kind, d) in config.FIELDS.items():
        if kind == "bool": vals[k] = "true" if form.get(k) == "true" else ("false" if f"{k}__present" in form else None)
        elif kind == "image":
            f = form.get(k)
            if getattr(f, "filename", ""):
                data = await f.read(200_001); ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml"}.get(f.filename.rsplit(".", 1)[-1].lower())
                if len(data) > 200_000 or not ctype: errs.append("Logo must be PNG, JPEG or SVG under 200 KB.")
                elif ctype == "image/svg+xml" and re.search(rb"<script|on\w+=|javascript:", data, re.I): errs.append("That SVG contains scripts.")
                else: import base64; vals[k] = f"data:{ctype};base64," + base64.b64encode(data).decode()
            elif form.get(k + "__clear"): vals[k] = ""; config.save({}, clear=[k])
        elif k in form: vals[k] = str(form.get(k))[:500]
    vals = {k: v for k, v in vals.items() if v is not None}
    if vals.get("DRY_RUN") == "false" and remover.DRY and form.get("confirm_live") != "LIVE": vals.pop("DRY_RUN"); errs.append("To turn dry run off, type LIVE in the confirmation box.")
    for k, v in vals.items():
        kind = config.FIELDS[k][2]
        if v and kind == "int" and not v.lstrip("-").isdigit(): errs.append(f"{config.FIELDS[k][0]}: a number."); 
        if v and kind.startswith("select:") and v not in [o.split("=", 1)[0] for o in kind[7:].split("|")]: errs.append(f"{config.FIELDS[k][0]}: pick one of the options.")
        if v and kind == "email" and not profiles.EMAIL_OK.match(v): errs.append(f"{config.FIELDS[k][0]}: not a valid email.")
        if v and kind == "url" and not re.match(r"^https?://[\w.-]+(:\d+)?(/.*)?$", v): errs.append(f"{config.FIELDS[k][0]}: must start with https://")
        if v and kind == "color" and not re.fullmatch(r"#[0-9a-fA-F]{6}", v): errs.append("Accent color: #RRGGBB.")
    if errs: return RedirectResponse(f"/admin/heartbeat?tab=settings&msg={quote(' '.join(errs))}", 303)
    changed = config.save(vals); event(C, "settings_changed", ", ".join(k if k not in config.SECRET else k + " (secret)" for k in changed))
    return RedirectResponse(f"/admin/heartbeat?tab=settings&saved={len(changed)}", 303)

@app.get("/admin/heartbeat/weekly")
def weekly_raw(r: Request, pid: str = ""):
    auth.require_admin(r); people = C.execute("SELECT * FROM people ORDER BY rowid").fetchall(); sel = [p for p in people if p["id"] == pid]
    h = summary.build(C, sel or people, family=not sel)[1] if people else "<p style='font-family:sans-serif;padding:24px'>Add a family member to preview the weekly email.</p>"
    return HTMLResponse(h, headers={"Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; frame-ancestors 'self'"})

@app.get("/admin/heartbeat/broker-email")
def broker_email_raw(r: Request, pid: str = "", site: str = "spokeo", stage: int = 0):
    """Preview of a tailored broker request (sample data if the person has no listing there)."""
    auth.require_admin(r); p = C.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone() or C.execute("SELECT * FROM people ORDER BY rowid").fetchone()
    if not p: raise HTTPException(404)
    st = remover.SITE.get(site) or next(iter(remover.SITE.values())); ls = C.execute("SELECT * FROM listings WHERE person_id=? AND site=?", (p["id"], st["id"])).fetchall()
    subj, text, h = letters.build(p, profiles.get(p), st, ls, agent=bool(p["needs_auth"]), reply_to=remover.reply_to(p), stage=max(0, min(2, stage)))
    to = dict(remover.channels(st)).get("email", "privacy@" + (st.get("domain") or "broker.example"))
    head = f'<div style="font-family:-apple-system,Segoe UI,sans-serif;font-size:12.5px;color:#57534e;padding:14px 20px;border-bottom:1px solid #e7e5e4;background:#fafaf9"><b>From:</b> {escape(remover.FROM_NAME)} &lt;{escape(remover.FROM_ADDRESS)}&gt;<br><b>To:</b> {escape(to)}<br><b>Reply-To:</b> {escape(remover.reply_to(p))}<br><b>Subject:</b> {escape(subj)}' + ("<br><b>Attachment:</b> authorization.pdf" if p["needs_auth"] else "") + "</div>"
    return HTMLResponse(h.replace('<body style="margin:0;padding:20px;background:#ffffff">', '<body style="margin:0;background:#ffffff">' + head + '<div style="padding:20px">', 1).replace("</body>", "</div></body>"),
                        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'"})

@app.post("/admin/heartbeat/test-weekly")
def weekly_test(r: Request, _=Depends(csrf)):
    """Send-test: goes to the admin's own email through the normal pipeline (DRY_RUN = recorded in the outbox only)."""
    auth.require_admin(r); u = r.state.user; p = C.execute("SELECT * FROM people WHERE id=?", (u["person_id"],)).fetchone()
    to = (p and p["email"]) or u["email"]
    if not to: return RedirectResponse("/admin/heartbeat?tab=settings&msg=" + quote("Add your email to your profile first."), 303)
    people = C.execute("SELECT * FROM people ORDER BY rowid").fetchall(); subj, h, t = summary.build(C, people, family=True)
    st = mailer.send(to, "[Test] " + subj, t, h, kind="weekly_test")
    return RedirectResponse("/admin/heartbeat?tab=settings&msg=" + quote(f"Test weekly email to {to}: {st}."), 303)

@app.post("/admin/heartbeat/run")
def heartbeat_run(r: Request, _=Depends(csrf)):
    auth.require_admin(r); res = lifecycle.tick(); event(C, "lifecycle_run", json.dumps(res))
    return RedirectResponse("/admin/heartbeat?msg=" + quote("Automation pass done: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in res.items())), 303)

# ---------- first-run setup ----------
@app.get("/setup", response_class=HTMLResponse)
def setup_page(r: Request): return T.TemplateResponse(r, "setup.html", {"error": None, "v": {}})

@app.post("/setup")
def setup_submit(r: Request, app_name: str = Form("Fletcher Fortis"), public_url: str = Form(""), username: str = Form(""), email: str = Form(""), full_name: str = Form(""),
                 city: str = Form(""), state: str = Form(""), password: str = Form(""), password2: str = Form(""), resend: str = Form("")):
    if has_users(): raise HTTPException(404)
    v = {"app_name": app_name, "public_url": public_url, "username": username, "email": email, "full_name": full_name, "city": city, "state": state}
    username = username.strip().lower(); err = None
    if not re.fullmatch(r"[a-z0-9._-]{3,32}", username): err = "Username: 3–32 lowercase letters, digits, dot, dash or underscore."
    elif len(full_name.split()) < 2: err = "Enter your first and last name."
    elif email and not profiles.EMAIL_OK.match(email.strip()): err = "That email isn't valid."
    elif public_url and not re.match(r"^https?://[\w.-]+(:\d+)?/?$", public_url.strip()): err = "Public URL looks like https://privacy.example.com"
    elif password != password2: err = "The two passwords don't match."
    else: err = auth.pw_problem(password, username)
    if err: return T.TemplateResponse(r, "setup.html", {"error": err, "v": v}, status_code=400)
    parts = full_name.split(); pid = re.sub(r"[^a-z0-9]", "", parts[0].lower())[:24] or "me"
    prof = {"names": [{"kind": "legal", "first": " ".join(parts[:-1]), "middle": "", "last": parts[-1]}], "addresses": [{"street": "", "city": city.strip(), "state": (state.strip().upper() or "CA")[:2], "current": True}] if city.strip() else [],
            "phones": [], "emails": [email.strip().lower()] if email.strip() else [], "relatives": [], "birth_year": None, "age_min": None, "age_max": None}
    if not C.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone(): profiles.new_person(C, pid, prof, False)
    c = db.conn(); c.execute("INSERT INTO users(username,pw_hash,role,person_id,must_change,created,email,onboarded) VALUES(?,?,?,?,0,?,?,1)", (username, auth.hash_pw(password), "admin", pid, now(), enc(email.strip().lower()) if email else None)); c.commit()
    _HAS_USERS[0] = True
    dom = urlsplit(public_url.strip()).hostname if public_url.strip() else ""
    config.save({"APP_NAME": app_name.strip()[:60] or "Fletcher Fortis", "PUBLIC_URL": public_url.strip().rstrip("/"), "MAIL_DOMAIN": ".".join(dom.split(".")[-2:]) if dom else "", "RESEND_API_KEY": resend.strip()})
    event(C, "setup_completed", app_name[:60]); u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    resp = RedirectResponse(f"/person/{pid}/profile?onboard=1", 303); auth.set_cookie(resp, auth.create_session(u["id"], auth.client_ip(r))); return resp

# ---------- onboarding invites ----------
@app.post("/family/onboard")
def onboard_invite(r: Request, full_name: str = Form(""), email: str = Form(""), _=Depends(csrf)):
    """Admin-only: create a member from just name + email and send (or show) a one-time onboarding link. No public signup."""
    auth.require_admin(r); email = email.strip().lower(); parts = full_name.split()
    if len(parts) < 2 or not all(profiles.NAME_OK.match(x) for x in parts): return RedirectResponse("/family?err=" + quote("Enter their first and last name."), 303)
    if not profiles.EMAIL_OK.match(email): return RedirectResponse("/family?err=" + quote("Enter a valid email."), 303)
    base = re.sub(r"[^a-z0-9]", "", parts[0].lower())[:24] or "member"; pid = base; i = 2
    while C.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone() or db.conn().execute("SELECT 1 FROM users WHERE username=?", (pid,)).fetchone(): pid = f"{base}{i}"; i += 1
    prof = {"names": [{"kind": "legal", "first": " ".join(parts[:-1]), "middle": "", "last": parts[-1]}], "addresses": [], "phones": [], "emails": [email], "relatives": [], "birth_year": None, "age_min": None, "age_max": None}
    profiles.new_person(C, pid, prof, True); event(C, "person_added", "onboarding invite", pid)
    c = db.conn(); c.execute("INSERT INTO users(username,role,person_id,must_change,created,email,onboarded) VALUES(?,?,?,1,?,?,0)", (pid, "member", pid, now(), enc(email))); c.commit()
    u = c.execute("SELECT * FROM users WHERE username=?", (pid,)).fetchone(); tok = auth.make_invite(u["id"])
    link = f"{(config.get('PUBLIC_URL') or str(r.base_url)).rstrip('/')}/invite/{tok}"
    subj, text, h = mailer.onboarding(parts[0], r.state.user["username"], link)
    st = mailer.send(email, subj, text, h, kind="onboarding", person_id=pid); event(C, "invite_created", f"onboarding email: {st}", pid)
    return RedirectResponse(f"/family?inv={tok}&who={pid}&sent={quote(st)}", 303)

@app.get("/admin/heartbeat/onboarding-email")
def onboarding_raw(r: Request):
    auth.require_admin(r); subj, text, h = mailer.onboarding("Alex", r.state.user["username"], (config.get("PUBLIC_URL") or "https://privacy.example.com") + "/invite/EXAMPLE-ONE-TIME-TOKEN")
    return HTMLResponse(h, headers={"Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; frame-ancestors 'self'"})

@app.get("/favicon.ico", include_in_schema=False)
def favicon(): return RedirectResponse("/static/favicon.svg", 301)

@app.get("/healthz")
def health(): return {"ok": True, "dry_run": remover.DRY}

SCHED = {"tick": None, "lifecycle": None}
def scheduler():
    """Single-container scheduler: weekly scan+recheck (SCAN_DAY/SCAN_HOUR), mail filter every 15 min, summary weekly."""
    last = {}
    while True:
        n = datetime.datetime.now(); key = n.strftime("%Y-%W"); SCHED["tick"] = n.isoformat(timespec="seconds")
        try:
            if time.time() - last.get("life", 0) > 3600:
                last["life"] = time.time(); res = lifecycle.tick(); SCHED["lifecycle"] = n.isoformat(timespec="seconds")
                if any(res.values()): event(db.conn(), "lifecycle_run", json.dumps(res))
                ar = asyncio.run(automation.run_due())
                if any(ar.values()): event(db.conn(), "automation_run", json.dumps(ar))
            if config.get("SCAN_ENABLED") == "true" and n.weekday() == int(config.get("SCAN_DAY")) and n.hour == int(config.get("SCAN_HOUR")) and last.get("scan") != key:
                last["scan"] = key; asyncio.run(scanner.run_all())
            if os.environ.get("MAIL_FILTER_ENABLED", "false") == "true" and time.time() - last.get("mail", 0) > 900:
                last["mail"] = time.time(); mailfilter.run_all()
            if n.weekday() == 5 and n.hour == 2 and last.get("brokers") != key:
                last["brokers"] = key; import brokerdb; c, t = brokerdb.merge(refresh=True); bad = brokerdb.check_contacts(); event(db.conn(), "broker_refresh", f"{c} -> {t} merged, {bad} unresolvable")
            if n.weekday() == 0 and n.hour == int(config.get("SUMMARY_HOUR")) and last.get("sum") != key and summary.enabled():   # Monday 08:00 (TZ=America/Los_Angeles)
                last["sum"] = key; summary.send_all()
        except Exception as e: event(db.conn(), "scheduler_error", str(e))
        time.sleep(60)
if os.environ.get("SCHEDULER", "true") == "true": threading.Thread(target=scheduler, daemon=True).start()

config.apply(); refresh_globals()
