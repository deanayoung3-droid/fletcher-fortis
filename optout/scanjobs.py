"""One background scan worker. Scans never run on the request loop, never stack, and only one Chromium runs at a time.
- enqueue(pid): queues a scan for one person (or None for everyone); a repeat while it's queued or running is ignored.
- BROWSER: lock held by anything that launches Chromium (scans, automation), so a 1 GB machine never runs two browsers.
- progress(pid): live state for the UI (queued / running X of Y / done / error)."""
import threading, queue, asyncio, time, json, datetime
from db import conn, event

BROWSER = threading.Lock()
_Q = queue.Queue(); _L = threading.Lock(); JOBS = {}; _W = [None]

def _key(pid): return pid or "*"
def _ts(): return datetime.datetime.now().isoformat(timespec="seconds")

def progress(pid):
    with _L: return dict(JOBS.get(_key(pid)) or {})

def busy(pid=None):
    j = progress(pid); return j.get("state") in ("queued", "running")

def enqueue(pid, why="manual"):
    """True if a new scan was queued, False if one is already queued or running for this person."""
    k = _key(pid)
    with _L:
        if (JOBS.get(k) or {}).get("state") in ("queued", "running"): return False
        JOBS[k] = {"state": "queued", "why": why, "done": 0, "total": 0, "found": 0, "errors": 0, "queued_at": _ts()}
    _Q.put((pid, why)); _ensure(); return True

def _ensure():
    if _W[0] and _W[0].is_alive(): return
    _W[0] = threading.Thread(target=_loop, name="scan-worker", daemon=True); _W[0].start()

def _update(k, **kw):
    with _L: JOBS.setdefault(k, {}).update(kw)

def _loop():
    import scanner
    while True:
        pid, why = _Q.get(); k = _key(pid); t0 = time.time()
        _update(k, state="running", started=_ts())
        def cb(done, total, found, errors): _update(k, done=done, total=total, found=found, errors=errors)
        try:
            with BROWSER: res = asyncio.run(scanner.run_all(pid, progress=cb))
            j = progress(pid); st = "done"
        except Exception as e:
            res = {}; j = progress(pid); st = "error"; _update(k, error=type(e).__name__)
        secs = int(time.time() - t0)
        _update(k, state=st, finished=_ts(), secs=secs)
        j = progress(pid)
        detail = {"why": why, "sites": j.get("total", 0), "checked": j.get("done", 0), "found": j.get("found", 0), "errors": j.get("errors", 0), "seconds": secs, "result": st}
        if st == "error": detail["error"] = j.get("error")
        try: event(conn(), "scan_log", json.dumps(detail), pid)
        except Exception: pass

def log(c, limit=12):
    """Recent scan log entries for Heartbeat (no personal data beyond the person id)."""
    out = []
    for e in c.execute("SELECT ts, person_id, detail FROM events WHERE kind='scan_log' ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        try: d = json.loads(e["detail"] or "{}")
        except ValueError: d = {}
        out.append(dict(ts=e["ts"], person=e["person_id"] or "everyone", **d))
    return out
