"""Fictional demo data for README / landing-page screenshots. Never contacts a broker: evidence pages are rendered locally.
Usage: DB_PATH=/tmp/x/optout.db SHOT_DIR=/tmp/x/shots FF_DATA_KEY=... PROFILES_DIR=test/profiles python3 test/demo/seed_shots.py"""
import os, sys, json, random, datetime, asyncio, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "optout"))
import db, profiles
from crypto import enc
c = db.init(); profiles.seed(c); random.seed(11); T = datetime.datetime.now().replace(hour=3, minute=12, second=0, microsecond=0)
SHOTS = os.environ.get("SHOT_DIR", "/tmp/ff-shots"); os.makedirs(SHOTS, exist_ok=True)
BR = [("truepeoplesearch", "TruePeopleSearch", "#1d4ed8"), ("fastpeoplesearch", "FastPeopleSearch", "#0f766e"), ("usphonebook", "USPhonebook", "#b91c1c"),
      ("thatsthem", "ThatsThem", "#7c3aed"), ("searchpeoplefree", "SearchPeopleFree", "#ea580c"), ("familytreenow", "FamilyTreeNow", "#15803d"),
      ("clustrmaps", "ClustrMaps", "#0369a1"), ("neighborwho", "NeighborWho", "#be185d"), ("radaris", "Radaris", "#334155"), ("peoplefinders", "PeopleFinders", "#9333ea"),
      ("spokeo", "Spokeo", "#0891b2"), ("nuwber", "Nuwber", "#65a30d"), ("cyberbackgroundchecks", "CyberBackgroundChecks", "#dc2626"), ("smartbackgroundchecks", "SmartBackgroundChecks", "#4f46e5")]
# (status, days ago first seen)
PLAN = {"alex": [("confirmed_removed", 80), ("confirmed_removed", 74), ("confirmed_removed", 66), ("confirmed_removed", 51), ("confirmed_removed", 39), ("submitted", 20), ("submitted", 12), ("pending", 9), ("reappeared", 60), ("candidate", 4), ("candidate", 2), ("manual_check", 6), ("manual_check", 6)],
        "sam": [("confirmed_removed", 78), ("confirmed_removed", 58), ("confirmed_removed", 44), ("confirmed_removed", 30), ("submitted", 16), ("needs_human", 10), ("manual_check", 6)],
        "jordan": [("confirmed_removed", 70), ("confirmed_removed", 47), ("submitted", 15), ("candidate", 3)],
        "riley": [("confirmed_removed", 64), ("confirmed_removed", 36), ("blocked_auth", 8)]}
c.execute("UPDATE people SET needs_auth=1 WHERE id='riley'")
def page(name, color, p):
    return f"""<html><body style="margin:0;font:15px/1.5 Arial;background:#f3f4f6"><div style="background:{color};color:#fff;padding:14px 28px;font:bold 22px Arial">{name}</div>
<div style="padding:22px 28px"><h2 style="margin:6px 0 16px">We found 3 people named {p['full_name']}</h2>
<div style="background:#fff;border:3px solid #ef4444;border-radius:8px;padding:16px 18px"><b style="font-size:19px;color:{color}">{p['full_name']}</b><br>Lives in Springfield, CA · Used to live in Shelbyville, CA<br>Phone: (555) ***-****</div></div></body></html>"""
async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch(); pg = await b.new_page(viewport={"width": 1000, "height": 560})
        for pid, plan in PLAN.items():
            p = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
            for (st, ago), (sid, name, col) in zip(plan, random.sample(BR, len(plan))):
                fs = T - datetime.timedelta(days=ago); shot = f"{SHOTS}/demo_{pid}_{sid}.png"
                if st not in ("manual_check",): await pg.set_content(page(name, col, p)); await pg.screenshot(path=shot)
                sub = fs + datetime.timedelta(days=2) if st not in ("candidate", "blocked_auth", "manual_check", "needs_human") else None
                conf = sub + datetime.timedelta(days=min(21, ago - 4)) if st in ("confirmed_removed", "reappeared") else None
                url = f"https://www.{sid}.com/find/{p['first'].lower()}-{p['last'].lower()}-springfield-ca" if st != "manual_check" else f"https://www.{sid}.com/"
                f = {"name": p["full_name"], "city": "Springfield, CA", "snippet": f"{p['full_name']}, Springfield CA. Previously Shelbyville CA. Relatives listed, partial phone shown."}
                c.execute("INSERT INTO listings(person_id,site,url,fields,screenshot,status,score,first_seen,last_seen,last_checked,submitted_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (pid, sid, enc(url), enc(json.dumps(f)), None if st == "manual_check" else shot, st, random.randint(78, 97), fs.isoformat(), T.isoformat(), T.isoformat(), sub and sub.isoformat(), conf and conf.isoformat()))
                lid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                ev = [(fs, "new_candidate", name)] + ([(sub, "removal_sent", f"CCPA email to privacy@{sid}.com")] if sub else []) + ([(conf, "confirmed_removed", f"{name} page gone")] if conf else [])
                for ts, k, d in ev: c.execute("INSERT INTO events(ts,person_id,listing_id,kind,detail) VALUES(?,?,?,?,?)", (ts.isoformat(), pid, lid, k, d))
                if st == "manual_check": c.execute("INSERT OR REPLACE INTO retry(listing_id,attempts,next_at,kind) VALUES(?,?,?,'blocked')", (lid, 1, (T + datetime.timedelta(days=2)).isoformat()))
        await b.close()
    c.commit()
asyncio.run(main())
print("seeded", c.execute("SELECT COUNT(*) FROM listings").fetchone()[0], "fictional listings")
