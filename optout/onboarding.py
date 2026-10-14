"""First-run setup wizard: one-time setup token, wizard progress and the 'what's configured / what's left' checklist.
The token exists only while no account exists and ADMIN_PASSWORD is unset. It is printed to the container log on every boot
(new value each boot), never stored, and dies as soon as the admin account is created."""
import os, json, secrets, hmac, zoneinfo
import config, crypto
from db import conn

STEPS = [(1, "Admin account", False), (2, "Basics", False), (3, "Email sending", True), (4, "Inbound replies", True),
         (5, "Optional keys", True), (6, "First person", True), (7, "Done", False)]
COOKIE = "ff_setup"
_TOKEN = [None]

def bootstrap_pending(c=None):
    """True on a brand-new install: no accounts and no ADMIN_PASSWORD to create one from."""
    c = c or conn()
    if os.environ.get("ADMIN_PASSWORD") or os.environ.get("DASHBOARD_PASSWORD"): return False
    return not c.execute("SELECT 1 FROM users LIMIT 1").fetchone()

def base_url():
    pub = config.get("PUBLIC_URL") or os.environ.get("PUBLIC_URL")
    return (pub or f"http://localhost:{os.environ.get('HOST_PORT', '8080')}").rstrip("/")

def init_token(c=None):
    """Called at startup. Prints the setup URL when the install still needs its first admin."""
    if not bootstrap_pending(c): _TOKEN[0] = None; return None
    _TOKEN[0] = os.environ.get("SETUP_TOKEN") or secrets.token_urlsafe(24)
    bar = "=" * 72
    print(f"\n{bar}\n Fletcher Fortis is ready for first-run setup.\n Setup URL: {base_url()}/setup?token={_TOKEN[0]}\n"
          f" (Use your server's address instead of localhost if you're on another machine.\n  The token changes on every restart and stops working once the admin exists.)\n{bar}\n", flush=True)
    return _TOKEN[0]

def token_ok(t): return bool(_TOKEN[0] and t and hmac.compare_digest(str(t), _TOKEN[0]))
def burn_token(): _TOKEN[0] = None

# ---- wizard state (app_meta 'setup', JSON). Absent = an install that predates the wizard: nothing is shown or forced. ----
def state(c=None):
    r = (c or conn()).execute("SELECT v FROM app_meta WHERE k='setup'").fetchone()
    try: return json.loads(r["v"]) if r else None
    except ValueError: return None
def save_state(st, c=None):
    c = c or conn(); c.execute("INSERT INTO app_meta(k,v) VALUES('setup',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps(st),)); c.commit(); return st
def update(c=None, **kw):
    st = state(c) or {"step": 1, "skipped": [], "done": False, "key_ack": False, "dismissed": False}; st.update(kw); return save_state(st, c)
def mark(step, skipped, c=None):
    st = state(c) or update(c); sk = set(st.get("skipped", []))
    (sk.add if skipped else sk.discard)(step)
    return update(c, skipped=sorted(sk), step=max(st.get("step", 1), min(step + 1, 7)))

def valid_tz(tz):
    try: return bool(tz) and tz in zoneinfo.available_timezones()
    except Exception: return False

def checklist(c, user):
    """[(key, label, done, optional, hint, href)] for the done screen and the dashboard card."""
    st = state(c) or {}
    pid = user["person_id"] if user else None
    person = c.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone() if pid else None
    key_ok = crypto.KEY_SOURCE != "generated" or st.get("key_ack")
    H = "/admin/heartbeat?tab=settings"
    return [
        ("admin", "Admin account", True, False, "You can invite family members from Family & accounts.", "/family"),
        ("basics", "App name, public URL, time zone and state", bool(config.get("PUBLIC_URL")) and bool(config.get("TZ")), False, "Public URL is used in links inside emails.", "/setup/2"),
        ("key", "Encryption key backed up", bool(key_ok), False, "Store the data key in your password manager, or set FF_DATA_KEY.", "/setup/7#key"),
        ("email", "Email sending (Resend)", bool(config.get("RESEND_API_KEY")) and bool(config.get("FROM_ADDRESS")), True, "Needed before removal requests can go out.", "/setup/3"),
        ("inbound", "Inbound replies (Cloudflare Email Worker)", bool(st.get("inbound_ok")), True, "Lets broker replies and verification links come back to the app.", "/setup/4"),
        ("captcha", "CAPTCHA solver", config.get("CAPTCHA_PROVIDER", "off") != "off" and bool(config.get("CAPTCHA_API_KEY")), True, "Only for opt-out forms behind a CAPTCHA.", "/setup/5"),
        ("person", "First person added", bool(person), False, "Add yourself, then family members.", "/setup/6"),
        ("live", "Dry run switched off", config.get("DRY_RUN", "true") == "false", True, "Last step, when you've reviewed the generated requests.", H),
    ]

def show_card(c, user):
    st = state(c)
    if not st or st.get("dismissed") or not user or user["role"] != "admin": return None
    items = checklist(c, user)
    return items if any(not done for k, _, done, _, _, _ in items if k not in ("live", "captcha")) else None   # until everything that matters is done
