"""Delete a person and everything tied to them (admin only). Audit event carries counts only, never personal info."""
import os, json
import db, scanner, esign

def _tables_with(c, col):
    out = []
    for t in [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
        if col in [r[1] for r in c.execute(f"PRAGMA table_info({t})").fetchall()]: out.append(t)  # nosec B608: names from sqlite_master
    return out

def _inside(path, root):
    try: p = os.path.realpath(path); r = os.path.realpath(root); return p.startswith(r + os.sep)
    except (TypeError, ValueError): return False

def _rm(path, root):
    if path and _inside(path, root) and os.path.isfile(path):
        try: os.remove(path); return 1
        except OSError: return 0
    return 0

def delete(c, pid):
    """Returns a dict of counts. Caller checks permissions and the typed-name confirmation."""
    q = lambda sql, a=(): c.execute(sql, a).fetchall()
    lids = [r[0] for r in q("SELECT id FROM listings WHERE person_id=?", (pid,))]
    oids = [r[0] for r in q("SELECT id FROM outbox WHERE person_id=?", (pid,))]
    iids = [r[0] for r in q("SELECT id FROM inbox WHERE person_id=?", (pid,))]
    uids = [r[0] for r in q("SELECT id FROM users WHERE person_id=?", (pid,))]
    n = {"listings": len(lids), "emails_sent": len(oids), "emails_received": len(iids), "logins": len(uids),
         "queued_cancelled": len(q("SELECT id FROM outbox WHERE person_id=? AND state='queued'", (pid,))), "files": 0}
    shots = [r[0] for r in q("SELECT screenshot FROM listings WHERE person_id=? AND screenshot IS NOT NULL", (pid,))]
    n["files"] += sum(_rm(s, scanner.SHOTS) for s in shots)
    d = scanner.SHOTS
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.startswith(pid + "_"): n["files"] += _rm(os.path.join(d, f), d)
    ad = esign.auth_dir()
    if os.path.isdir(ad):
        for f in os.listdir(ad):
            if f == f"{pid}-esign.pdf" or f.startswith(pid + "-") or f.startswith(pid + "_") or f.startswith(pid + "."): n["files"] += _rm(os.path.join(ad, f), ad)
    with c:
        if lids:
            ph = ",".join("?" * len(lids))
            c.execute(f"DELETE FROM events WHERE listing_id IN ({ph})", lids)  # nosec B608
            c.execute(f"DELETE FROM retry WHERE listing_id IN ({ph})", lids)  # nosec B608
            c.execute(f"DELETE FROM snooze WHERE k IN ({ph})", [f"l:{i}" for i in lids])  # nosec B608
        c.execute("DELETE FROM snooze WHERE k IN (?,?)", (f"auth:{pid}", f"drop:{pid}"))
        keys = [f"out-{i}" for i in oids] + [f"in-{i}" for i in iids]
        try:
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]; c.execute(f"DELETE FROM mail_handled WHERE k IN ({','.join('?' * len(part))})", part)  # nosec B608
        except Exception: pass
        if uids: c.execute(f"DELETE FROM sessions WHERE user_id IN ({','.join('?' * len(uids))})", uids)  # nosec B608
        if oids:
            try: c.execute(f"DELETE FROM inbox WHERE outbox_id IN ({','.join('?' * len(oids))})", oids)  # nosec B608
            except Exception: pass
        for t in _tables_with(c, "person_id"):
            if t != "people": c.execute(f"DELETE FROM {t} WHERE person_id=?", (pid,))  # nosec B608: names from sqlite_master
        c.execute("DELETE FROM people WHERE id=?", (pid,))
    db.event(c, "person_deleted", json.dumps(n)); return n
