import sqlite3, os, datetime, re
from crypto import enc, dec
DB = os.environ.get("DB_PATH", "/data/optout.db")
def now(): return datetime.datetime.now().isoformat(timespec="seconds")

class Row(dict):
    """dict row that also supports row[0]; encrypted values are decrypted transparently."""
    def __getitem__(self, k): return list(self.values())[k] if isinstance(k, int) else dict.__getitem__(self, k)
    def __getattr__(self, k):
        try: return dict.__getitem__(self, k)
        except KeyError: raise AttributeError(k)
def _factory(cur, row): return Row(zip([d[0] for d in cur.description], (dec(v) for v in row)))

def conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    new = not os.path.exists(DB)
    c = sqlite3.connect(DB, check_same_thread=False, timeout=15); c.row_factory = _factory
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON")
    if new: os.chmod(DB, 0o600)
    return c

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(id TEXT PRIMARY KEY, full_name TEXT, first TEXT, last TEXT, city TEXT, state TEXT,
  prior_cities TEXT, email TEXT, phone TEXT, age_min INT, age_max INT, enabled INT DEFAULT 1,
  needs_auth INT DEFAULT 0, auth_pdf TEXT, bulk_ccpa INT DEFAULT 0, notes TEXT);
-- no SSN / DOB / ID columns by design
CREATE TABLE IF NOT EXISTS listings(id INTEGER PRIMARY KEY, person_id TEXT, site TEXT, url TEXT, fields TEXT,
  screenshot TEXT, status TEXT DEFAULT 'candidate', first_seen TEXT, last_seen TEXT, last_checked TEXT,
  submitted_at TEXT, confirmed_at TEXT, UNIQUE(person_id, site, url));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, person_id TEXT, listing_id INT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, ts TEXT, person_id TEXT, to_addr TEXT, subject TEXT, body TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, ts TEXT, from_num TEXT, verdict TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY, ts TEXT, person_id TEXT, listing_id INT, outbox_id INT,
  from_addr TEXT, to_addr TEXT, subject TEXT, kind TEXT, links TEXT, clicked TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS broker_stats(site TEXT, channel TEXT, sent INT DEFAULT 0, confirmed INT DEFAULT 0, failed INT DEFAULT 0, PRIMARY KEY(site, channel));
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT, role TEXT NOT NULL DEFAULT 'member',
  person_id TEXT, must_change INT DEFAULT 0, invite_hash TEXT, invite_expires TEXT, created TEXT, last_login TEXT);
CREATE TABLE IF NOT EXISTS snooze(k TEXT PRIMARY KEY, until TEXT);
CREATE TABLE IF NOT EXISTS retry(listing_id INTEGER PRIMARY KEY, attempts INT DEFAULT 0, next_at TEXT, last TEXT, kind TEXT DEFAULT 'blocked');
INSERT OR IGNORE INTO retry(listing_id, attempts, next_at, kind) SELECT id, 0, datetime('now','localtime','+1 day'), 'blocked' FROM listings WHERE status='manual_check';
CREATE TABLE IF NOT EXISTS reg_requests(id INTEGER PRIMARY KEY, person_id TEXT, broker TEXT, category TEXT, status TEXT, channel TEXT, target TEXT,
  outbox_id INT, stage INT DEFAULT 0, created TEXT, sent_at TEXT, updated TEXT, reply_kind TEXT, UNIQUE(person_id, broker));
CREATE TABLE IF NOT EXISTS app_meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INT NOT NULL, csrf TEXT NOT NULL, created TEXT, expires TEXT, ip TEXT);
"""
MIGRATE = [("outbox", "listing_id INT"), ("outbox", "reply_to TEXT"), ("people", "profile TEXT"), ("people", "send_enabled INT DEFAULT 0"),
           ("listings", "dedupe TEXT"), ("listings", "score INT"),
           ("people", "esign_name TEXT"), ("people", "esign_at TEXT"), ("people", "esign_ip TEXT"), ("people", "esign_user TEXT"), ("people", "send_from TEXT"), ("people", "esign_sig TEXT"), ("people", "drop_at TEXT"), ("users", "email TEXT"), ("users", "onboarded INT DEFAULT 1"), ("users", "name TEXT"),
           ("outbox", "stage INT DEFAULT 0"), ("outbox", "channel TEXT"), ("outbox", "html TEXT"),
           ("users", "invite_state TEXT"), ("users", "invite_sent_at TEXT"), ("users", "invite_opened_at TEXT"), ("users", "invite_error TEXT")]
# columns holding PII: encrypted at rest
ENCRYPTED = {"people": ["full_name", "first", "last", "city", "prior_cities", "email", "phone", "notes", "profile", "esign_name", "esign_ip", "send_from", "esign_sig"],
             "listings": ["url", "fields"], "events": ["detail"], "outbox": ["subject", "body", "html"], "users": ["email", "name"],
             "inbox": ["from_addr", "to_addr", "subject", "links", "clicked", "body"], "calls": ["from_num", "detail"]}

def app_version():
    for p in (os.path.join(os.path.dirname(__file__), "..", "VERSION"), "/app/VERSION"):
        try: return open(p).read().strip().lstrip("v")
        except OSError: pass
    return "0.0.0"

def backup(reason, keep=5):
    """Consistent copy of the (already field-encrypted) database before a migration. Kept next to the data, 0600, newest `keep` only."""
    if not os.path.exists(DB): return None
    d = os.path.join(os.path.dirname(DB), "backups"); os.makedirs(d, mode=0o700, exist_ok=True)
    out = os.path.join(d, f"optout-{datetime.datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^\w.-]', '_', reason)[:40]}.db")
    src = sqlite3.connect(DB); dst = sqlite3.connect(out)
    with dst: src.backup(dst)
    dst.execute("PRAGMA journal_mode=DELETE")      # a single self-contained file
    src.close(); dst.close(); os.chmod(out, 0o600)
    for old in sorted(f for f in os.listdir(d) if f.startswith("optout-") and f.endswith(".db"))[:-keep]: os.remove(os.path.join(d, old))
    return out

def init():
    """Open the DB, back it up when the app version changed since the last start, then apply migrations (idempotent)."""
    existed = os.path.exists(DB); ver = app_version()
    if existed:
        try:
            r = sqlite3.connect(DB); old = r.execute("SELECT v FROM app_meta WHERE k='version'").fetchone(); r.close(); old = old[0] if old else "pre-1.0.0"
        except sqlite3.OperationalError: old = "pre-1.0.0"
        if old != ver: backup(f"{old}-to-{ver}")
    c = conn(); c.executescript(SCHEMA)
    for t, col in MIGRATE:
        try: c.execute(f"ALTER TABLE {t} ADD COLUMN {col}")
        except sqlite3.OperationalError: pass
    encrypt_existing(c)
    c.execute("INSERT INTO app_meta(k,v) VALUES('version',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (ver,)); c.commit(); return c

def encrypt_existing(c):
    """One-time migration: encrypt any plaintext PII left from older versions."""
    raw = sqlite3.connect(DB)
    for t, cols in ENCRYPTED.items():
        key = "id" if t != "people" else "id"
        for row in raw.execute(f"SELECT {key},{','.join(cols)} FROM {t}"):  # nosec B608
            vals = list(row[1:])
            if any(isinstance(v, str) and v and not v.startswith("enc1:") for v in vals):
                raw.execute(f"UPDATE {t} SET {','.join(c_ + '=?' for c_ in cols)} WHERE {key}=?", [enc(v) for v in vals] + [row[0]])  # nosec B608
    raw.commit(); raw.close()

def event(c, kind, detail="", person_id=None, listing_id=None):
    c.execute("INSERT INTO events(ts,person_id,listing_id,kind,detail) VALUES(?,?,?,?,?)", (now(), person_id, listing_id, kind, enc(detail))); c.commit()
