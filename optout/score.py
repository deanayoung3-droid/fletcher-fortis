"""Privacy score (0-100) per person, with the breakdown shown on the person page.

Everything we know about is worth points; each item keeps a share of its points until it is resolved:
  people-search listings (up to 60 points, scaled so a handful of listings is not as bad as dozens)
    still visible / not yet requested ........ 100% (reappeared: also counts extra)
    request sent, no confirmation yet ........ 70%  (counts a little)
    broker confirmed by email ("removed", "no records found"), waiting for the re-scan ... 25% (counts mostly; the listing is only
                                               marked Removed after a re-scan confirms it)
    removed, confirmed by re-scan ............ 0%   (counts fully)
  registered data brokers (request-based, up to 15 points, by share of brokers)
    not sent yet / failed .................... 100%
    sent, acknowledged, asked for more ....... 70%
    covered by California DROP ............... 50%
    confirmed deleted ........................ 10%
    "no records found" ....................... 0% (clear)
  sites that blocked the automated scan: 0.75 point each, at most 10 (unknown risk)
"""
import datetime
from db import now

OPEN_FULL = ("candidate", "approved", "needs_human", "blocked_auth", "reappeared")
LISTING_PTS, REG_PTS, UNKNOWN_EACH, UNKNOWN_MAX = 60, 15, 0.75, 10

def _grade(v): return "ABCDEFG"[min(6, (100 - v) // 13)]

def compute(c, pid):
    confirmed = {r["listing_id"] for r in c.execute("SELECT DISTINCT listing_id FROM inbox WHERE person_id=? AND listing_id IS NOT NULL AND kind IN ('removed','not_found')", (pid,))}
    L = {"removed": 0, "broker_confirmed": 0, "sent": 0, "open": 0, "reappeared": 0}; unknown = 0
    for l in c.execute("SELECT id, status FROM listings WHERE person_id=?", (pid,)):
        st = l["status"]
        if st in ("not_me", "checked_clear"): continue
        if st == "manual_check": unknown += 1
        elif st == "confirmed_removed": L["removed"] += 1
        elif st == "reappeared": L["reappeared"] += 1
        elif st in OPEN_FULL: L["open"] += 1
        elif st in ("submitted", "pending"): L["broker_confirmed" if l["id"] in confirmed else "sent"] += 1
    n_l = sum(L.values()); res_l = L["open"] + L["reappeared"] + 0.7 * L["sent"] + 0.25 * L["broker_confirmed"]
    scale_l = LISTING_PTS / (n_l + 2) if n_l else 0
    R = {"clear": 0, "deleted": 0, "drop": 0, "sent": 0, "todo": 0}
    try: rows = c.execute("SELECT status, reply_kind FROM reg_requests WHERE person_id=?", (pid,)).fetchall()
    except Exception: rows = []
    for r in rows:
        st = r["status"]
        if st in ("people_search", "no_channel"): continue
        if st == "completed": R["clear" if r["reply_kind"] == "not_found" else "deleted"] += 1
        elif st == "covered_by_drop": R["drop"] += 1
        elif st in ("sent", "acknowledged", "needs_info", "denied"): R["sent"] += 1
        else: R["todo"] += 1   # queued, failed, blocked_auth, dry_run
    n_r = sum(R.values()); scale_r = REG_PTS / n_r if n_r else 0
    lost = lambda cnt, share, scale: round(cnt * share * scale, 1)
    parts = [  # (label, count, points still lost, points recovered)
        ("Listings removed, confirmed by re-scan", L["removed"], 0, lost(L["removed"], 1, scale_l)),
        ("Listings the broker says are removed, waiting for the re-scan", L["broker_confirmed"], lost(L["broker_confirmed"], .25, scale_l), lost(L["broker_confirmed"], .75, scale_l)),
        ("Listings with a removal request sent, no confirmation yet", L["sent"], lost(L["sent"], .7, scale_l), lost(L["sent"], .3, scale_l)),
        ("Listings still visible (to review or being filed)", L["open"], lost(L["open"], 1, scale_l), 0),
        ("Listings that came back after removal", L["reappeared"], lost(L["reappeared"], 1, scale_l), 0),
        ("Registered brokers that found no records", R["clear"], 0, lost(R["clear"], 1, scale_r)),
        ("Registered brokers that confirmed deletion", R["deleted"], lost(R["deleted"], .1, scale_r), lost(R["deleted"], .9, scale_r)),
        ("Registered brokers covered by California DROP", R["drop"], lost(R["drop"], .5, scale_r), lost(R["drop"], .5, scale_r)),
        ("Registered brokers with a request sent", R["sent"], lost(R["sent"], .7, scale_r), lost(R["sent"], .3, scale_r)),
        ("Registered brokers not contacted yet", R["todo"], lost(R["todo"], 1, scale_r), 0),
        ("Sites that blocked the automated scan (unknown)", unknown, round(min(UNKNOWN_MAX, UNKNOWN_EACH * unknown), 1), 0)]
    pen = res_l * scale_l + (R["todo"] + 0.7 * R["sent"] + 0.5 * R["drop"] + 0.1 * R["deleted"]) * scale_r + min(UNKNOWN_MAX, UNKNOWN_EACH * unknown)
    v = max(0, min(100, round(100 - pen)))
    return {"value": v, "grade": _grade(v), "open": L["open"] + L["reappeared"] + L["sent"] + L["broker_confirmed"], "removed": L["removed"],
            "parts": [dict(label=a, count=b, lost=c_, gained=d) for a, b, c_, d in parts if b], "listings": L, "registry": R, "unknown": unknown}

def ensure(c): c.execute("CREATE TABLE IF NOT EXISTS score_history(person_id TEXT, day TEXT, value INT, PRIMARY KEY(person_id, day))")

def record(c, pid, value):
    """One value per person per day (latest wins), for 'why your score changed' and the trend."""
    ensure(c); c.execute("INSERT INTO score_history(person_id, day, value) VALUES(?,?,?) ON CONFLICT(person_id, day) DO UPDATE SET value=excluded.value", (pid, now()[:10], value)); c.commit()

def change(c, pid, value):
    """(previous value, its day) from the most recent earlier day, or (None, None)."""
    ensure(c); r = c.execute("SELECT value, day FROM score_history WHERE person_id=? AND day<? ORDER BY day DESC LIMIT 1", (pid, now()[:10])).fetchone()
    return (r["value"], r["day"]) if r else (None, None)
