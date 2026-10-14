"""Runtime settings edited in the app (Heartbeat > Settings, first-run setup). Stored encrypted in the `settings` table and
applied over the environment without a restart: DB value > environment variable > built-in default. Secrets are write-only in the UI."""
import os, threading
from crypto import enc, dec
_lock = threading.Lock()
US_STATES = "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
def groups():
    out = {}
    for k, f in FIELDS.items(): out.setdefault(f[1], []).append((k, f))
    return list(out.items())
# key: (label, group, kind, default)   kind: text | secret | bool | int | email | url | color
FIELDS = {
    "APP_NAME": ("App name", "Branding", "text", "Fletcher Fortis"),
    "BRAND_COLOR": ("Accent color", "Branding", "color", "#5559e3"),
    "LOGO_DATA": ("Logo (PNG, JPEG or SVG, max 200 KB)", "Branding", "image", ""),
    "TZ": ("Time zone (IANA name, e.g. America/Los_Angeles)", "Household", "text", ""),
    "HOME_STATE": ("State of residence (picks the privacy law cited in requests)", "Household", "select:" + "|".join(f"{s}={s}" for s in US_STATES), "CA"),
    "PUBLIC_URL": ("Public URL of this dashboard", "Domain", "url", ""),
    "MAIL_DOMAIN": ("Email domain", "Domain", "text", ""),
    "FROM_ADDRESS": ("Send-from address", "Email", "email", ""),
    "FROM_NAME": ("Send-from name", "Email", "text", ""),
    "RESEND_API_KEY": ("Resend API key", "Email", "secret", ""),
    "INBOUND_SECRET": ("Inbound webhook secret", "Email", "secret", ""),
    "INBOUND_AUTOCLICK": ("Auto-click verification links", "Email", "bool", "true"),
    "SUMMARY_ENABLED": ("Weekly summary emails", "Email", "bool", "true"),
    "SUMMARY_TO": ("Family summary goes to (default: admin's email)", "Email", "email", ""),
    "CF_ACCOUNT_ID": ("Cloudflare account ID (optional)", "Cloudflare", "text", ""),
    "CF_TUNNEL_HOST": ("Cloudflare Tunnel hostname (optional)", "Cloudflare", "text", ""),
    "SCAN_ENABLED": ("Weekly scans", "Scanning", "bool", "true"),
    "SCAN_ON_CREATE": ("Scan right away when a person is added or finishes onboarding", "Scanning", "bool", "true"),
    "SCAN_DAY": ("Scan day", "Scanning", "int", "6"),
    "SCAN_HOUR": ("Scan hour (0–23, local time)", "Scanning", "int", "3"),
    "SUMMARY_HOUR": ("Weekly email hour (Monday)", "Scanning", "int", "8"),
    "SCAN_DELAY_MS": ("Delay between site requests (ms)", "Scanning", "int", "8000"),
    "SCAN_MAX_QUERIES_PER_SITE": ("Max searches per site", "Scanning", "int", "6"),
    "AUTO_APPROVE": ("Auto-approve high-confidence matches", "Removals", "bool", "false"),
    "AUTO_APPROVE_MIN": ("Auto-approve minimum match score (0–100)", "Removals", "int", "85"),
    "BLIND_REQUESTS": ("Email blocked sites even without a confirmed listing (blind requests)", "Removals", "bool", "false"),
    "REGISTRY_REQUESTS": ("Registry brokers (request-based): send deletion + opt-out requests to registered data brokers", "Removals", "bool", "true"),
    "REGISTRY_PER_HOUR": ("Registry requests per hour (pacing)", "Removals", "int", "25"),
    "AUTO_REFILE": ("Re-file automatically when a listing reappears", "Removals", "bool", "true"),
    "FOLLOWUP_DAYS": ("Reminder after (days)", "Removals", "int", "20"),
    "ESCALATE_DAYS": ("Escalate after (days, CCPA deadline 45)", "Removals", "int", "45"),
    "RETRY_BLOCKED": ("Retry blocked sites automatically from this server's IP (backoff 1, 2, 4, 8, 16 days)", "Automation", "bool", "true"),
    "AUTO_RECHECK": ("Re-check sites marked \"not listed\" automatically", "Automation", "bool", "true"),
    "RECHECK_DAYS": ("Re-check after (days)", "Automation", "int", "28"),
    "CAPTCHA_PROVIDER": ("CAPTCHA solver for opt-out forms", "Automation", "select:off=Off|2captcha=2Captcha (about $3 per 1,000 solves)|capsolver=CapSolver (about $1 per 1,000 solves)", "off"),
    "CAPTCHA_API_KEY": ("CAPTCHA solver API key", "Automation", "secret", ""),
    "CAPTCHA_MONTHLY_CAP_USD": ("CAPTCHA monthly budget (USD, hard stop)", "Automation", "int", "5"),
    "AUTO_UPDATE": ("Install new releases automatically (needs the Watchtower sidecar)", "Updates", "bool", "false"),
    "WATCHTOWER_TOKEN": ("Watchtower HTTP API token (same as in the stack)", "Updates", "secret", ""),
    "UPDATE_CHANNEL": ("Update channel", "Updates", "select:stable=Stable releases|beta=Beta (includes pre-releases)", "stable"),
    "GITHUB_REPO": ("Release channel (GitHub owner/repo)", "Updates", "text", "deanayoung3-droid/fletcher-fortis"),
    "DRY_RUN": ("Dry run (nothing leaves the server)", "Safety", "bool", "true"),
}
SECRET = {k for k, v in FIELDS.items() if v[2] == "secret"}
_cache = {}
_ENV0 = {k: os.environ.get(k) for k in FIELDS}   # environment as started; the app layer goes on top

def _conn():
    import db; return db.conn()

def load():
    with _lock:
        c = _conn(); c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT, updated TEXT)")
        _cache.clear(); _cache.update({r["k"]: r["v"] for r in c.execute("SELECT k, v FROM settings")})
    return dict(_cache)

def get(k, default=None):
    if k in _cache and _cache[k] not in (None, ""): return _cache[k]
    v = _ENV0.get(k) if k in _ENV0 else os.environ.get(k)
    if v not in (None, ""): return v
    return FIELDS[k][3] if k in FIELDS and default is None else default

def is_set(k): return bool(get(k, ""))
def source(k): return "app" if _cache.get(k) else ("env" if _ENV0.get(k) else "default")

def save(values, clear=()):
    """values: {key: str}. Empty secret = keep the old one. Returns list of changed keys."""
    import db; from db import now
    c = _conn(); c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT, updated TEXT)"); changed = []
    for k, v in values.items():
        if k not in FIELDS: continue
        v = (v or "").strip()
        if k in SECRET and not v: continue
        base = _ENV0.get(k) or FIELDS[k][3]
        if k not in SECRET and v == (base or ""):            # same as env/default: don't pin it in the DB
            if k in _cache: c.execute("DELETE FROM settings WHERE k=?", (k,)); changed.append(k)
            continue
        if _cache.get(k) == v: continue
        c.execute("INSERT INTO settings(k,v,updated) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated", (k, enc(v) if v else v, now())); changed.append(k)
    for k in clear:
        if k in FIELDS: c.execute("DELETE FROM settings WHERE k=?", (k,)); changed.append(k)
    c.commit(); load(); apply(); return changed

def apply():
    """Push effective values into os.environ and the module-level flags that were read at import time."""
    for k in FIELDS:
        v = get(k, "")
        if v not in (None, ""): os.environ[k] = v
        else: os.environ.pop(k, None)
    try:
        import time; time.tzset()   # TZ from the app layer takes effect without a restart
    except Exception: pass
    import remover, inbound, mailfilter
    dry = get("DRY_RUN", "true").lower() != "false"
    remover.DRY = inbound.DRY = mailfilter.DRY = dry
    inbound.AUTOCLICK = get("INBOUND_AUTOCLICK", "true").lower() == "true"
    remover.PROVIDER = (os.environ.get("SEND_PROVIDER") or "resend").lower()
    remover.FROM_ADDRESS = get("FROM_ADDRESS") or (f"privacy@{get('MAIL_DOMAIN')}" if get("MAIL_DOMAIN") else "privacy@example.com")
    remover.FROM_NAME = get("FROM_NAME") or f"{get('APP_NAME')} Privacy"
    try:
        import main; main.refresh_globals()
    except Exception: pass

def masked(k):
    v = get(k, "")
    return ("•••• " + v[-4:]) if v and len(v) > 8 else ("set" if v else "")
