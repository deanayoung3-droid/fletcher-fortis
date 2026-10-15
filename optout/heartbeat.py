"""Live health for the Heartbeat page: every moving part with a green/amber/red state and plain-language detail."""
import os, shutil, datetime
import remover, config, db

def _ago(ts):
    if not ts: return "never"
    try: d = datetime.datetime.now() - datetime.datetime.fromisoformat(ts[:19])
    except Exception: return ts[:16]
    s = int(d.total_seconds())
    return "just now" if s < 90 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago" if s < 86400 else f"{d.days} d ago"

def resolved(fail_ts, ok_ts):
    """True when a failure happened before the latest success of the same thing, so it is history, not a live problem."""
    return bool(fail_ts and ok_ts and str(fail_ts)[:19] < str(ok_ts)[:19])

# Error event kind -> the event kinds that prove the same part worked again afterwards.
RECOVERY = {"scan_error": ("scan_complete",), "recheck_error": ("scan_complete",), "registry_error": ("registry_request",),
            "chain_error": ("chain_submitted", "chain_pending"), "retry_error": ("automation_run",), "scheduler_error": ("lifecycle_run", "automation_run")}

def status(c):
    import main
    one = lambda q, a=(): c.execute(q, a).fetchone()
    out = []
    def add(key, label, state, value, detail="", icon="activity"): out.append({"key": key, "label": label, "state": state, "value": value, "detail": detail, "icon": icon})
    tick = main.SCHED.get("tick")
    alive = bool(tick) and (datetime.datetime.now() - datetime.datetime.fromisoformat(tick)).total_seconds() < 300
    add("scheduler", "Scheduler", "ok" if alive else ("warn" if os.environ.get("SCHEDULER", "true") == "true" else "bad"), "running" if alive else "starting" if os.environ.get("SCHEDULER", "true") == "true" else "off",
        f"last tick {_ago(tick)} · automation pass {_ago(main.SCHED.get('lifecycle'))}", "clock")
    last = one("SELECT ts FROM events WHERE kind='scan_complete' ORDER BY id DESC LIMIT 1"); running = one("SELECT 1 FROM events WHERE kind='scan_started' AND ts > COALESCE((SELECT MAX(ts) FROM events WHERE kind='scan_complete'),'')")
    days = (datetime.datetime.now() - datetime.datetime.fromisoformat(last["ts"][:19])).days if last else None
    add("scan", "Last scan", "ok" if days is not None and days <= 8 else "warn", _ago(last["ts"]) if last else "never", f"next {main._next_scan():%a %b %d} at {int(config.get('SCAN_HOUR') or 3):02d}:00" + (" · scan running now" if running else "") + ("" if config.get("SCAN_ENABLED") == "true" else " · weekly scan is OFF"), "search")
    cov = main._coverage(); h = cov["count"]; tot = cov["total"]; good = h.get("works", 0)
    add("scanner", "Scanner health", "ok" if tot and good / tot > .5 else "warn", f"{good}/{tot} sites searchable" if tot else "not checked yet", ", ".join([f"{v} {main.HEALTH_LABEL.get(k, (k,))[0].lower()}" for k, v in sorted(h.items(), key=lambda x: -x[1]) if v and k != "works"][:3]), "grid")
    q = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM listings GROUP BY status")}
    add("queue", "Removal queue", "warn" if q.get("candidate") else "ok", f"{q.get('candidate', 0)} to review · {q.get('approved', 0) + q.get('submitted', 0) + q.get('pending', 0)} in progress",
        f"{q.get('confirmed_removed', 0)} removed · {q.get('reappeared', 0)} reappeared · {q.get('manual_check', 0) + q.get('needs_human', 0)} need a person", "chart")
    ready = remover.provider_ready()
    add("sending", "Sending", "warn" if remover.DRY else ("ok" if ready else "bad"), "dry run (nothing leaves)" if remover.DRY else ("live" if ready else "live but not configured"), f"{remover.FROM_NAME} <{remover.FROM_ADDRESS}>", "zap")
    wk = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    o = {r["s"]: r["n"] for r in c.execute("SELECT CASE WHEN state='sent' THEN 'sent' WHEN state='dry_run' THEN 'dry' WHEN state LIKE 'blocked%' THEN 'blocked' WHEN state='queued' THEN 'queued' ELSE 'failed' END s, COUNT(*) n FROM outbox WHERE ts>? GROUP BY s", (wk,))}
    lf = one("SELECT MAX(ts) t FROM outbox WHERE ts>? AND state NOT IN ('sent','dry_run','queued') AND state NOT LIKE 'blocked%'", (wk,))["t"]
    ls = one("SELECT MAX(ts) t FROM outbox WHERE state='sent'")["t"]
    fresh = o.get("failed") and not resolved(lf, ls)   # only a failure newer than the last delivery makes this red
    add("delivery", "Email delivery (7 days)", "bad" if fresh else "ok", f"{o.get('sent', 0)} sent · {o.get('dry', 0)} dry run",
        (f"{o.get('failed', 0)} failed" + ("" if fresh or not o.get("failed") else f" earlier, resolved (last delivered {_ago(ls)})")) + f" · {o.get('blocked', 0)} held by safety gates · {o.get('queued', 0)} queued", "mail")
    ib = one("SELECT ts FROM inbox ORDER BY id DESC LIMIT 1"); sec = len(config.get("INBOUND_SECRET") or "") >= 32
    add("inbound", "Inbound mail", "ok" if sec and ib else "warn", ("receiving" if ib else "waiting for first message") if sec else "not configured", f"last message {_ago(ib['ts']) if ib else 'never'} · link auto-click {'on' if config.get('INBOUND_AUTOCLICK') == 'true' else 'off'}", "mail")
    dom = remover.FROM_ADDRESS.split("@")[-1]
    add("dns", "DNS (SPF / DKIM / DMARC)", "idle", dom if not dom.endswith("example.com") else "no domain yet", "press Check DNS to look it up live", "shield")
    dbp = os.environ.get("DB_PATH") or os.path.join(os.environ.get("DATA_DIR", "/data"), "optout.db")
    try: du = shutil.disk_usage(os.path.dirname(dbp) or "."); free = du.free / du.total; size = os.path.getsize(dbp) / 1e6
    except Exception: free, size = 1, 0
    add("disk", "Disk & database", "ok" if free > .1 else "bad", f"{size:.1f} MB database", f"{free * 100:.0f}% disk free · encrypted at rest", "lock")
    day = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec="seconds")
    errs = c.execute("SELECT kind, detail, ts FROM events WHERE ts>? AND (kind LIKE '%error%' OR kind LIKE 'ALERT%') ORDER BY id DESC", (day,)).fetchall()
    ok_at = {}
    def recovered(e):
        kinds = RECOVERY.get(e["kind"])
        if not kinds: return False
        if e["kind"] not in ok_at: ok_at[e["kind"]] = one(f"SELECT MAX(ts) t FROM events WHERE kind IN ({','.join('?' * len(kinds))})", kinds)["t"]
        return resolved(e["ts"], ok_at[e["kind"]])
    real = [e for e in errs if "error" in e["kind"]]; live = [e for e in real if not recovered(e)]
    n, m = len(real), len(live); shown = [e for e in errs if e not in real or e in live]
    add("errors", "Errors (24 h)", "ok" if not m else ("warn" if m < 20 else "bad"), f"{n} error{'s' if n != 1 else ''}" + (f" · {n - m} resolved" if n - m else ""),
        "; ".join(f"{e['kind']}: {(e['detail'] or '')[:60]}" for e in shown[:2]) or ("all fixed by a later successful run" if n else "nothing logged"), "alert")
    return out
