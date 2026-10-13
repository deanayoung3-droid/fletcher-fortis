"""App login: server-side sessions (random token, only its SHA-256 stored), bcrypt password hashes, per-session CSRF token,
rate-limited login, admin-issued invite links (single use, 72 h) or an initial password that must be changed on first login."""
import os, secrets, time, datetime, threading, hmac
import bcrypt
from fastapi import Request, HTTPException
from db import conn, now
from crypto import token_hash
COOKIE = "ff_session"
SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
TTL_H = int(os.environ.get("SESSION_HOURS", "168"))
MIN_PW = 12
_fail = {}; _lock = threading.Lock()
WINDOW, MAX_FAILS, MAX_IP_FAILS = 900, 5, 20

def hash_pw(pw): return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(12)).decode()
def check_pw(pw, h):
    try: return bool(h) and bcrypt.checkpw(pw.encode()[:72], h.encode())
    except ValueError: return False
_DUMMY = hash_pw(secrets.token_hex(8))

def pw_problem(pw, username=""):
    if len(pw) < MIN_PW: return f"Use at least {MIN_PW} characters (a short sentence works well)."
    if len(pw) > 200: return "Password too long."
    if username and username.lower() in pw.lower(): return "Don't include your username in the password."
    if len(set(pw)) < 5: return "Password is too repetitive."
    return None

def client_ip(r: Request):
    if os.environ.get("TRUST_CF_HEADER", "true") == "true" and r.headers.get("cf-connecting-ip"): return r.headers["cf-connecting-ip"]
    return r.client.host if r.client else "?"

def throttled(username, ip):
    t = time.time()
    with _lock:
        for k in list(_fail): _fail[k] = [x for x in _fail[k] if t - x < WINDOW]
        return len(_fail.get("u:" + username.lower(), [])) >= MAX_FAILS or len(_fail.get("ip:" + ip, [])) >= MAX_IP_FAILS
def record_fail(username, ip):
    with _lock:
        for k in ("u:" + username.lower(), "ip:" + ip): _fail.setdefault(k, []).append(time.time())
def clear_fails(username):
    with _lock: _fail.pop("u:" + username.lower(), None)

def authenticate(username, pw):
    c = conn(); u = c.execute("SELECT * FROM users WHERE username=?", (username.strip().lower(),)).fetchone()
    ok = check_pw(pw, u["pw_hash"] if u else _DUMMY) and bool(u)   # constant work whether or not the user exists
    return u if ok else None

def create_session(user_id, ip):
    tok = secrets.token_urlsafe(32); c = conn()
    c.execute("DELETE FROM sessions WHERE expires < ?", (now(),))
    c.execute("INSERT INTO sessions(token_hash,user_id,csrf,created,expires,ip) VALUES(?,?,?,?,?,?)",
              (token_hash(tok), user_id, secrets.token_urlsafe(24), now(), (datetime.datetime.now() + datetime.timedelta(hours=TTL_H)).isoformat(timespec="seconds"), ip))
    c.execute("UPDATE users SET last_login=? WHERE id=?", (now(), user_id)); c.commit(); return tok

def set_cookie(resp, tok):
    resp.set_cookie(COOKIE, tok, max_age=TTL_H * 3600, httponly=True, secure=SECURE, samesite="lax", path="/")

def session(r: Request):
    tok = r.cookies.get(COOKIE)
    if not tok: return None
    c = conn(); s = c.execute("SELECT s.csrf, s.expires, u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (token_hash(tok),)).fetchone()
    if not s or s["expires"] < now(): return None
    return s

def logout(r: Request):
    tok = r.cookies.get(COOKIE)
    if tok: c = conn(); c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(tok),)); c.commit()

def end_user_sessions(user_id, keep=None):
    c = conn(); c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user_id, token_hash(keep) if keep else "")); c.commit()

def csrf_ok(r: Request, sent):
    u = getattr(r.state, "user", None)
    return bool(u and sent and hmac.compare_digest(str(sent), u["csrf"]))

def can_view(u, pid): return bool(u) and (u["role"] == "admin" or u["person_id"] == pid)
def require_person(r: Request, pid):
    if not can_view(r.state.user, pid): raise HTTPException(404)
def require_admin(r: Request):
    if not r.state.user or r.state.user["role"] != "admin": raise HTTPException(403, "Only the family admin can do that.")

def make_invite(user_id):
    tok = secrets.token_urlsafe(24); c = conn()
    c.execute("UPDATE users SET invite_hash=?, invite_expires=? WHERE id=?", (token_hash(tok), (datetime.datetime.now() + datetime.timedelta(hours=72)).isoformat(timespec="seconds"), user_id)); c.commit()
    return tok
def invite_user(tok):
    c = conn(); u = c.execute("SELECT * FROM users WHERE invite_hash=?", (token_hash(tok or "x"),)).fetchone()
    return u if u and u["invite_expires"] and u["invite_expires"] > now() else None

def bootstrap_admin():
    """First boot: create the admin from ADMIN_USERNAME / ADMIN_PASSWORD (falls back to DASHBOARD_USER / DASHBOARD_PASSWORD). Later changes happen in the app."""
    c = conn()
    if c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone(): return
    name = (os.environ.get("ADMIN_USERNAME") or os.environ.get("DASHBOARD_USER") or "admin").lower()
    pw = os.environ.get("ADMIN_PASSWORD") or os.environ.get("DASHBOARD_PASSWORD") or ""
    if not pw: return
    pid = os.environ.get("ADMIN_PERSON_ID", name)
    c.execute("INSERT INTO users(username,pw_hash,role,person_id,must_change,created) VALUES(?,?,?,?,?,?)",
              (name, hash_pw(pw), "admin", pid, 1 if pw_problem(pw, name) else 0, now())); c.commit()

WORDS = "amber birch cedar delta ember fjord grove harbor island juniper kestrel lumen maple nimbus orchid pebble quartz river sierra timber umber velvet willow yarrow zephyr".split()
def gen_password():
    """Strong, readable one-time password, e.g. 'cedar-Quartz-river-4821-kestrel' (~70 bits). Must be changed on first login."""
    w = [secrets.choice(WORDS) for _ in range(4)]; w[1] = w[1].capitalize()
    return "-".join(w[:3] + [f"{secrets.randbelow(9000) + 1000}", w[3]])

def generate_family_logins(c, people, reset_unused=True):
    """Create a member account (username = person id) for every person without one, with a random password and must-change-on-first-login.
    Existing member accounts that never signed in get a fresh password; admins and accounts in use are left alone.
    Returns [(person_id, full_name, username, password)]: shown ONCE, never stored in plain text."""
    out = []
    for p in people:
        u = c.execute("SELECT * FROM users WHERE person_id=?", (p["id"],)).fetchone()
        if u and (u["role"] == "admin" or u["last_login"] or not reset_unused): continue
        pw = gen_password()
        if u:
            c.execute("UPDATE users SET pw_hash=?, must_change=1, invite_hash=NULL WHERE id=?", (hash_pw(pw), u["id"])); c.commit(); end_user_sessions(u["id"]); name = u["username"]
        else:
            name = p["id"]
            if c.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone(): name = f"{p['id']}-{secrets.randbelow(900) + 100}"
            c.execute("INSERT INTO users(username,pw_hash,role,person_id,must_change,created) VALUES(?,?,?,?,1,?)", (name, hash_pw(pw), "member", p["id"], now()))
        c.commit(); out.append((p["id"], p["full_name"], name, pw))
    return out
