"""Privacy score (0-100) per person, with the breakdown shown on the person page.

Score = 100 minus the points still lost. Two shares:

People-search listings: 60 points, spread over the listings found (plus a small cushion, so two listings are not as bad as
twenty). Each listing keeps part of its share until it is resolved:
    still visible / not yet filed / came back .. 100% lost
    removal request sent, no reply yet ......... 70% lost   (sending counts a little)
    broker emailed "removed", re-scan pending .. 15% lost   (most of the points)
    broker says it holds no records ............ 0%         (clear)
    gone on re-scan ............................ 0%         (all the points)

Registered data brokers: 40 points.
    Reach (15 points): the share of registry brokers we have contacted or that California DROP covers. A broker not contacted
        yet costs its share; one already waiting in the automatic queue costs a third of that.
    Confirmations (25 points, on a curve): every deletion confirmation or "no records" reply earns points back,
        25 x (1 - e^(-confirmed/25)): about +1 for the first, +6 for the first 7, +16 for 25, full marks around 100
        (or as soon as every broker in scope has answered).
        Brokers never un-confirm, so the score only goes up as replies arrive.

Sites that blocked the automated scan cost 0.75 point each, at most 10 (unknown risk).
"""
import datetime, math
from db import now

FORMULA = "v2"            # bump when the weighting changes; snapshots are recomputed once
FORMULA_NOTE = "Score formula updated Oct 15, 2026 (v1.3.9): broker confirmations now move the score. History recalculated."
OPEN_FULL = ("candidate", "approved", "needs_human", "blocked_auth", "reappeared")
LISTING_PTS, REACH_PTS, CONF_PTS, CONF_TAU, QUEUED_SHARE, UNKNOWN_EACH, UNKNOWN_MAX = 60, 15, 25, 25.0, 1 / 3, 0.75, 10
RES = {"open": 1.0, "reappeared": 1.0, "sent": 0.7, "broker_confirmed": 0.15, "clear": 0.0, "removed": 0.0}
CONTACTED_ST = ("sent", "acknowledged", "needs_info", "denied", "completed")

def _grade(v): return "ABCDEFG"[min(6, (100 - v) // 13)]
def conf_points(n, scope=10**6):
    """Points earned by n confirmations, on a curve that reaches full marks when every broker in scope has confirmed."""
    return CONF_PTS * min(1.0, (1 - math.exp(-n / CONF_TAU)) / (1 - math.exp(-max(scope, 1) / CONF_TAU)))

def _d(x): return (x or "")[:10]

def compute(c, pid, day=None):
    """Score now, or as of the end of `day` (YYYY-MM-DD) rebuilt from dates (for the history chart)."""
    on = (lambda d: bool(d) and d <= day) if day else (lambda d: True)
    replies = {}
    for r in c.execute("SELECT listing_id, kind, ts FROM inbox WHERE person_id=? AND listing_id IS NOT NULL AND kind IN ('removed','not_found')", (pid,)):
        if on(_d(r["ts"])) and replies.get(r["listing_id"]) != "removed": replies[r["listing_id"]] = r["kind"]   # a deletion confirmation wins over "no records"
    L = {k: 0 for k in RES}; unknown = 0
    for l in c.execute("SELECT id, status, first_seen, submitted_at, confirmed_at FROM listings WHERE person_id=?", (pid,)):
        st = l["status"]
        if day:
            if not on(_d(l["first_seen"])): continue
            if st == "confirmed_removed" and not on(_d(l["confirmed_at"])): st = "submitted"
            if st in ("submitted", "pending") and l["submitted_at"] and not on(_d(l["submitted_at"])): st = "approved"
        if st in ("not_me", "checked_clear"): continue
        if st == "manual_check": unknown += 1
        elif st == "confirmed_removed": L["removed"] += 1
        elif st == "reappeared": L["reappeared"] += 1
        elif st in OPEN_FULL: L["open"] += 1
        elif st in ("submitted", "pending"): L[{"removed": "broker_confirmed", "not_found": "clear"}.get(replies.get(l["id"]), "sent")] += 1
    n_l = sum(L.values()); scale = LISTING_PTS / (n_l + 2) if n_l else 0
    R = {"deleted": 0, "clear": 0, "reached": 0, "queued": 0, "todo": 0}
    try: rows = c.execute("SELECT r.id, r.status, r.reply_kind, r.created, o.ts sent_ts FROM reg_requests r LEFT JOIN outbox o ON o.id=r.outbox_id WHERE r.person_id=?", (pid,)).fetchall()
    except Exception: rows = []
    dates = _reg_reply_dates(c, pid) if day else {}
    for r in rows:
        st = r["status"]
        if st in ("people_search", "no_channel"): continue
        if day and st == "completed" and not on(dates.get(r["id"]) or ""): st = "sent"
        if day and st in CONTACTED_ST and not on(_d(r["sent_ts"])): st = "queued"
        if day and st == "covered_by_drop" and not on(_d(r["created"])): st = "queued"
        if st == "completed": R["clear" if r["reply_kind"] == "not_found" else "deleted"] += 1; R["reached"] += 1
        elif st in CONTACTED_ST or st == "covered_by_drop": R["reached"] += 1
        elif st == "queued": R["queued"] += 1
        else: R["todo"] += 1   # failed, blocked_auth, dry_run
    scope = R["reached"] + R["queued"] + R["todo"]
    nconf = R["deleted"] + R["clear"]; got = conf_points(nconf, scope) if scope else 0
    per = REACH_PTS / scope if scope else 0
    pts = lambda n, share: round(n * share * scale, 1)
    parts = [  # (label, count, points still lost, points earned back)
        ("Listings gone on re-scan", L["removed"], 0, pts(L["removed"], 1)),
        ("Listings the broker confirmed removed (re-scan pending)", L["broker_confirmed"], pts(L["broker_confirmed"], .15), pts(L["broker_confirmed"], .85)),
        ("Listings where the broker has no records", L["clear"], 0, pts(L["clear"], 1)),
        ("Listing removal requests sent, no reply yet", L["sent"], pts(L["sent"], .7), pts(L["sent"], .3)),
        ("Listings still visible (to review or being filed)", L["open"], pts(L["open"], 1), 0),
        ("Listings that came back after removal", L["reappeared"], pts(L["reappeared"], 1), 0),
        ("Broker deletion confirmations", R["deleted"], 0, round(got * R["deleted"] / nconf, 1) if nconf else 0),
        ("Brokers that replied \"no records\"", R["clear"], 0, round(got * R["clear"] / nconf, 1) if nconf else 0),
        ("Registered brokers that have not confirmed yet", scope - nconf, round(CONF_PTS - got, 1) if scope else 0, 0),
        ("Registered brokers contacted or covered by California DROP", R["reached"], 0, round(R["reached"] * per, 1)),
        ("Registered brokers waiting in the automatic queue", R["queued"], round(R["queued"] * per * QUEUED_SHARE, 1), round(R["queued"] * per * (1 - QUEUED_SHARE), 1)),
        ("Registered brokers not contacted (failed or blocked)", R["todo"], round(R["todo"] * per, 1), 0),
        ("Sites that blocked the automated scan (unknown)", unknown, round(min(UNKNOWN_MAX, UNKNOWN_EACH * unknown), 1), 0)]
    pen = sum(L[k] * RES[k] for k in L) * scale + ((CONF_PTS - got) + (R["queued"] * QUEUED_SHARE + R["todo"]) * per if scope else 0) + min(UNKNOWN_MAX, UNKNOWN_EACH * unknown)
    v = max(0, min(100, round(100 - pen)))
    return {"value": v, "grade": _grade(v), "open": L["open"] + L["reappeared"] + L["sent"] + L["broker_confirmed"],
            "removed": L["removed"] + L["broker_confirmed"] + R["deleted"], "clear": R["clear"] + L["clear"], "lost": round(pen, 1),
            "parts": [dict(label=a, count=b, lost=c_, gained=d) for a, b, c_, d in parts if b], "listings": L, "registry": R, "unknown": unknown}

def ensure(c): c.execute("CREATE TABLE IF NOT EXISTS score_history(person_id TEXT, day TEXT, value INT, PRIMARY KEY(person_id, day))")

def record(c, pid, value):
    """One value per person per day (latest wins), for 'why your score changed' and the trend."""
    ensure(c); c.execute("INSERT INTO score_history(person_id, day, value) VALUES(?,?,?) ON CONFLICT(person_id, day) DO UPDATE SET value=excluded.value", (pid, now()[:10], value)); c.commit()

def change(c, pid, value):
    """(previous value, its day) from the most recent earlier day, or (None, None)."""
    ensure(c); r = c.execute("SELECT value, day FROM score_history WHERE person_id=? AND day<? ORDER BY day DESC LIMIT 1", (pid, now()[:10])).fetchone()
    return (r["value"], r["day"]) if r else (None, None)


def recompute_history(c, max_days=90):
    """Rebuild every person's daily snapshots with the current formula (dates come from listings, replies and sends).
    Returns the number of snapshots written."""
    ensure(c); today = datetime.date.fromisoformat(now()[:10]); n = 0
    for p in c.execute("SELECT id FROM people").fetchall():
        pid = p["id"]
        r = c.execute("SELECT MIN(d) FROM (SELECT MIN(substr(first_seen,1,10)) d FROM listings WHERE person_id=? UNION ALL SELECT MIN(day) FROM score_history WHERE person_id=?)", (pid, pid)).fetchone()[0]
        try: start = max(datetime.date.fromisoformat(r), today - datetime.timedelta(days=max_days))
        except Exception: start = today
        c.execute("DELETE FROM score_history WHERE person_id=?", (pid,))
        d = start
        while d <= today:
            v = compute(c, pid)["value"] if d == today else compute(c, pid, d.isoformat())["value"]
            c.execute("INSERT INTO score_history(person_id, day, value) VALUES(?,?,?)", (pid, d.isoformat(), v)); n += 1; d += datetime.timedelta(days=1)
    c.commit(); return n

def history(c, pid, days=30):
    """[(day, value)] for the last `days` days that have a snapshot."""
    ensure(c); since = (datetime.date.fromisoformat(now()[:10]) - datetime.timedelta(days=days - 1)).isoformat()
    return [(r["day"], r["value"]) for r in c.execute("SELECT day, value FROM score_history WHERE person_id=? AND day>=? ORDER BY day", (pid, since))]

# ---------- shared removal counts (home tile, family cards, person page and score all read these) ----------
FOUND_EXCLUDE = ("not_me", "manual_check", "checked_clear")
CONTACTED = ("sent", "acknowledged", "needs_info", "denied", "completed")

def _reg_reply_dates(c, pid=None):
    """{reg_request id: date of the first broker reply that confirmed deletion / no records}, matched like regreq.sync_replies."""
    try: reqs = c.execute("SELECT id, person_id, target, outbox_id FROM reg_requests WHERE status='completed'" + (" AND person_id=?" if pid else ""), (pid,) if pid else ()).fetchall()  # nosec B608
    except Exception: return {}
    by_oid = {r["outbox_id"]: r["id"] for r in reqs if r["outbox_id"]}; by_addr = {}
    for r in reqs: by_addr.setdefault((r["person_id"], (r["target"] or "").lower()), []).append(r["id"])
    out = {}
    for i in c.execute("SELECT i.ts, i.outbox_id, o.person_id, o.to_addr FROM inbox i JOIN outbox o ON o.id=i.outbox_id WHERE i.kind IN ('removed','not_found') ORDER BY i.ts"):
        q = by_oid.get(i["outbox_id"])
        if not q:
            cand = by_addr.get((i["person_id"], (i["to_addr"] or "").lower())) or []; q = cand[0] if len(cand) == 1 else None
        if q and q not in out: out[q] = (i["ts"] or "")[:10]
    return out

def removals(c, pid=None, days=12):
    """Removed = verified by re-scan (listing status confirmed_removed) + confirmed by the broker by email (people-search listings still
    waiting for their re-scan, and registered brokers that confirmed deletion). 'No records found' is shown separately as clear: nothing
    was removed, but nothing is exposed there either. Denominator = exposures found + registered brokers contacted that may hold data."""
    w = " AND person_id=?" if pid else ""; a = (pid,) if pid else ()
    found = c.execute(f"SELECT COUNT(*) FROM listings WHERE status NOT IN {FOUND_EXCLUDE}" + w, a).fetchone()[0]  # nosec B608
    verified = {r["id"]: (r["confirmed_at"] or "")[:10] for r in c.execute("SELECT id, confirmed_at FROM listings WHERE status='confirmed_removed'" + w, a)}  # nosec B608
    lconf, lclear = {}, {}
    for r in c.execute("SELECT i.listing_id, i.kind, MIN(i.ts) ts FROM inbox i JOIN listings l ON l.id=i.listing_id WHERE i.kind IN ('removed','not_found') AND l.status IN ('submitted','pending')" + (" AND l.person_id=?" if pid else "") + " GROUP BY i.listing_id, i.kind", a):  # nosec B608
        (lconf if r["kind"] == "removed" else lclear).setdefault(r["listing_id"], (r["ts"] or "")[:10])
    for k in lconf: lclear.pop(k, None)
    try: regs = c.execute("SELECT id, status, reply_kind, updated FROM reg_requests WHERE status IN %s" % (CONTACTED,) + w, a).fetchall()  # nosec B608
    except Exception: regs = []
    dates = _reg_reply_dates(c, pid) if regs else {}
    rconf = {r["id"]: dates.get(r["id"]) or (r["updated"] or "")[:10] for r in regs if r["status"] == "completed" and r["reply_kind"] != "not_found"}
    rclear = {r["id"]: dates.get(r["id"]) or (r["updated"] or "")[:10] for r in regs if r["status"] == "completed" and r["reply_kind"] == "not_found"}
    contacted = len(regs)
    broker = len(lconf) + len(rconf); removed = len(verified) + broker; clear = len(lclear) + len(rclear)
    denom = found + contacted - len(rclear)
    today = datetime.date.fromisoformat(now()[:10]); span = [(today - datetime.timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    ds = sorted(d for d in list(verified.values()) + list(lconf.values()) + list(rconf.values()) if d)
    series = [sum(1 for d in ds if d <= day) for day in span]   # cumulative removed per day, backfilled from re-scan and reply dates
    return {"removed": removed, "verified": len(verified), "broker": broker, "broker_listings": len(lconf), "broker_registry": len(rconf),
            "clear": clear, "found": found, "contacted": contacted, "denom": denom, "pct": round(100 * removed / denom) if denom else 0,
            "days": span, "series": series}
