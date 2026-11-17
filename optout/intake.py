"""No-login intake. The admin makes a private link under /invite/intake/<token>. The person opens it with no account,
fills in the details the scan needs, reads the authorized-agent authorization and e-signs it. The link works once and
expires. Nothing is emailed unless the admin presses Send.

A paper authorization (photo or PDF) can be stored on the person instead. Consent is a separate step and is refused
when no file is attached. Removals stay blocked until there is an e-signature or a verified paper authorization.
"""
import os, re, time, hmac, secrets, datetime, threading
import esign, profiles, circle
from db import conn, now, event
from crypto import enc, token_hash

DAYS = 7
GET_LIMIT, POST_LIMIT, WINDOW = 60, 10, 900
MAX_BYTES = 8 * 1024 * 1024
_hits = {}; _lock = threading.Lock()

def limited(ip, kind):
    """Basic per-IP limit for the public form. Separate from the login limiter."""
    limit = GET_LIMIT if kind == "get" else POST_LIMIT
    t = time.time(); key = kind + ":" + (ip or "?")
    with _lock:
        xs = [x for x in _hits.get(key, []) if t - x < WINDOW]
        if len(xs) >= limit:
            _hits[key] = xs; return True
        xs.append(t); _hits[key] = xs; return False

def link_for(tok, base=""):
    import config
    return f"{(config.get('PUBLIC_URL') or base or '').rstrip('/')}/invite/intake/{tok}"

def create(person_id, circ="outside"):
    """New single-use link. Any unused link for the same person stops working. Returns (token, csrf, expires)."""
    if circ not in (circle.FAMILY, circle.OUTSIDE): circ = circle.OUTSIDE
    tok, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    exp = (datetime.datetime.now() + datetime.timedelta(days=DAYS)).isoformat(timespec="seconds")
    c = conn()
    if person_id:
        c.execute("UPDATE intake_links SET used_at=? WHERE person_id=? AND used_at IS NULL", (now(), person_id))
    c.execute("INSERT INTO intake_links(token_hash,person_id,circle,expires,created,csrf) VALUES(?,?,?,?,?,?)",
              (token_hash(tok), person_id, circ, exp, now(), csrf))
    c.commit(); return tok, csrf, exp

def fetch(tok):
    c = conn(); return c.execute("SELECT * FROM intake_links WHERE token_hash=?", (token_hash(tok or "x"),)).fetchone()

def state(row):
    """'ok', 'used', 'expired' or 'missing'."""
    if not row: return "missing"
    if row["used_at"]: return "used"
    if not row["expires"] or row["expires"] <= now(): return "expired"
    return "ok"

def csrf_ok(row, sent):
    return bool(row and sent and hmac.compare_digest(str(row["csrf"]), str(sent)))

def consume(row):
    c = conn(); c.execute("UPDATE intake_links SET used_at=? WHERE id=? AND used_at IS NULL", (now(), row["id"])); c.commit()

def _id_for(c, first):
    base = re.sub(r"[^a-z0-9]", "", (first or "").lower())[:24] or "person"
    pid, i = base, 2
    while c.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone() or c.execute("SELECT 1 FROM users WHERE username=?", (pid,)).fetchone():
        pid = f"{base}{i}"; i += 1
    return pid

def apply(row, prof, typed, ip, ua, strokes):
    """Create or update the person and store the e-signature. The caller has already checked the link, the form and the signature.
    Returns the person id. Marks the link used."""
    c = conn(); ts = now(); ua = (ua or "")[:400]
    pid = row["person_id"]
    if pid and c.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
        profiles.save(c, pid, prof)
    else:
        pid = _id_for(c, prof["names"][0]["first"] if prof["names"] else "")
        profiles.new_person(c, pid, prof, True, row["circle"] or circle.OUTSIDE)
    p = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    esign.write_pdf(p, typed, ts, ip, "intake-link", strokes, ua=ua)
    c.execute("""UPDATE people SET esign_name=?, esign_at=?, esign_ip=?, esign_user=?, esign_sig=?, esign_ua=?, consent_verified_at=?, needs_auth=1
                 WHERE id=?""", (enc(typed), ts, enc(ip), "intake-link", enc(__import__("json").dumps(strokes)), enc(ua), ts, pid))
    c.execute("UPDATE listings SET status='candidate' WHERE person_id=? AND status='blocked_auth'", (pid,))
    c.execute("UPDATE intake_links SET used_at=?, person_id=? WHERE id=?", (ts, pid, row["id"]))
    c.commit()
    event(c, "auth_esigned", "intake link", pid)
    return pid

def _magic(data, ext):
    if ext == ".pdf" and data.startswith(b"%PDF-"): return True
    if ext == ".jpg" and data.startswith(b"\xff\xd8\xff"): return True
    if ext == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"): return True
    if ext == ".webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP": return True
    return False

def save_paper(pid, filename, data):
    """Store a photo or PDF of a signed paper authorization. Does not mark consent. Returns the path, or an error string."""
    if not data or len(data) > MAX_BYTES: return None, "That file is empty or larger than 8 MB."
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".jpeg": ext = ".jpg"
    if ext not in (".pdf", ".jpg", ".png", ".webp") or not _magic(data, ext):
        return None, "Upload a PDF, JPEG, PNG or WebP of the signed authorization."
    d = esign.auth_dir(); os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, f"{pid}-paper{ext}")
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(d): return None, "Could not store that file."
    for old in os.listdir(d):
        if old.startswith(pid + "-paper."):
            try: os.remove(os.path.join(d, old))
            except OSError: pass
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f: f.write(data)
    os.replace(path + ".tmp", path)
    c = conn()
    # A replacement file is not consent until the admin marks it. An e-signature, if they already have one, still stands.
    c.execute("UPDATE people SET auth_pdf=?, consent_verified_at=CASE WHEN esign_at IS NOT NULL AND esign_at!='' THEN consent_verified_at ELSE NULL END WHERE id=?", (path, pid))
    c.commit(); event(c, "auth_uploaded", ext, pid)
    return path, None

def paper_ok(p):
    """True when a paper file is stored inside the authorization directory."""
    path = p.get("auth_pdf") if p else None
    if not isinstance(path, str) or not path or not os.path.isfile(path): return False
    root = os.path.realpath(esign.auth_dir()); real = os.path.realpath(path)
    return real.startswith(root + os.sep)

def mark_verified(pid):
    """Mark paper consent verified. Refuses when nothing is attached. Returns an error string or None."""
    c = conn(); p = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not p or not paper_ok(p): return "Attach a signed authorization before marking consent. A checkbox alone is not enough."
    ts = now()
    c.execute("UPDATE people SET consent_verified_at=? WHERE id=?", (ts, pid))
    c.execute("UPDATE listings SET status='candidate' WHERE person_id=? AND status='blocked_auth'", (pid,))
    c.commit(); event(c, "consent_verified", "paper authorization", pid)
    return None
