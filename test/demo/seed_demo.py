"""Screenshot-only demo data: copies the e2e DB and adds backdated, fictional listings rendered from local HTML.
Never contacts a broker; all pages are generated here and rendered with headless Chromium from data: URLs."""
import sqlite3, shutil, random, datetime, json, os, sys, asyncio
from playwright.async_api import async_playwright
src, out = sys.argv[1], sys.argv[2]; os.makedirs(out + "/shots", exist_ok=True); shutil.copy(src + "/optout.db", out + "/optout.db")
for f in os.listdir(src + "/shots"): shutil.copy(f"{src}/shots/{f}", f"{out}/shots/{f}")
c = sqlite3.connect(out + "/optout.db"); c.row_factory = sqlite3.Row; random.seed(7); T = datetime.date.today()
for r in c.execute("SELECT id, screenshot FROM listings").fetchall():  # repoint e2e screenshots into the demo dir
    if r["screenshot"]: c.execute("UPDATE listings SET screenshot=? WHERE id=?", (out + "/shots/" + os.path.basename(r["screenshot"]), r["id"]))
BR = [("truepeoplesearch","TruePeopleSearch","#1d4ed8"),("fastpeoplesearch","FastPeopleSearch","#0f766e"),("usphonebook","USPhonebook","#b91c1c"),("thatsthem","ThatsThem","#7c3aed"),
      ("searchpeoplefree","SearchPeopleFree","#ea580c"),("familytreenow","FamilyTreeNow","#15803d"),("advancedbackgroundchecks","AdvancedBackgroundChecks","#0369a1"),("neighborwho","NeighborWho","#be185d"),("publicrecordsnow","PublicRecordsNow","#334155")]
PEOPLE = {r["id"]: r for r in c.execute("SELECT * FROM people")}
PLAN = {"alex": [("confirmed_removed",200),("confirmed_removed",150),("submitted",40),("reappeared",95),("candidate",6),("candidate",3)],
        "sam": [("confirmed_removed",220),("confirmed_removed",180),("blocked_auth",20)],
        "jordan": [("confirmed_removed",120),("candidate",12)], "riley": [("confirmed_removed",160),("submitted",25)]}
def page(name, color, p, age):
    rel = ", ".join(x for x in ["Sam Rivera","Jordan Rivera","Riley Rivera","Alex Rivera"] if x != p["full_name"])[:44]
    return f"""<html><body style="margin:0;font:15px/1.5 Arial;background:#f3f4f6"><div style="background:{color};color:#fff;padding:14px 28px;font:bold 22px Arial">{name}<span style="float:right;font:14px Arial;opacity:.8">Search · Reverse Phone · Address</span></div>
<div style="padding:22px 28px"><div style="color:#6b7280;font-size:13px">Home › California › Springfield › {p['last']}</div><h2 style="margin:6px 0 16px">We found 3 people named {p['full_name']}</h2>
<div style="background:#fff;border:3px solid #ef4444;border-radius:8px;padding:16px 18px;margin-bottom:12px;box-shadow:0 0 0 4px rgba(239,68,68,.15)"><b style="font-size:19px;color:{color}">{p['full_name']}</b> &nbsp;Age {age}<br>
Lives in Springfield, CA · Used to live in Shelbyville, CA<br>Relatives: {rel}<br>Phone: (555) ***-**** · Email: {p['first'][0].lower()}****@gmail.com<div style="margin-top:10px"><span style="background:{color};color:#fff;padding:7px 14px;border-radius:5px">View Details</span></div></div>
<div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px 18px;margin-bottom:12px;opacity:.7"><b style="font-size:17px">{p['first']} M {p['last']}</b> &nbsp;Age 63<br>Lives in Fresno, CA</div>
<div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px 18px;opacity:.7"><b style="font-size:17px">{p['full_name']}</b> &nbsp;Age 34<br>Lives in Austin, TX</div></div></body></html>"""
AGES = {"alex": 15, "sam": 47, "jordan": 17, "riley": 12}
async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(); pg = await b.new_page(viewport={"width": 1100, "height": 700})
        for pid, plan in PLAN.items():
            p = PEOPLE[pid]; brs = random.sample(BR, len(plan))
            for (st, ago), (sid, name, col) in zip(plan, brs):
                await pg.set_content(page(name, col, p, AGES[pid])); shot = f"{out}/shots/demo_{pid}_{sid}.png"; await pg.screenshot(path=shot)
                fs = T - datetime.timedelta(ago); sub = fs + datetime.timedelta(2) if st not in ("candidate", "blocked_auth") else None
                conf = sub + datetime.timedelta(min(30, ago - 5)) if st in ("confirmed_removed", "reappeared") else None
                url = f"https://www.{sid}.com/find/person/{p['first'].lower().replace(' ','-')}-{p['last'].lower()}-springfield-ca"
                fields = {"name": p["full_name"], "city": "Springfield, CA", "age": AGES[pid], "snippet": f"{p['full_name']}, age {AGES[pid]}, Springfield CA. Previously Shelbyville CA. Relatives listed, partial phone and email shown."}
                c.execute("INSERT INTO listings(person_id,site,url,fields,screenshot,status,first_seen,last_seen,last_checked,submitted_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, sid, url, json.dumps(fields), shot, st, fs.isoformat() + "T03:12:00", T.isoformat() + "T03:12:00", T.isoformat() + "T03:12:00", sub and sub.isoformat() + "T09:00:00", conf and conf.isoformat() + "T03:20:00"))
                lid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                c.execute("INSERT INTO events(ts,person_id,listing_id,kind,detail) VALUES(?,?,?,?,?)", (fs.isoformat() + "T03:12:00", pid, lid, "new_candidate", f"{name} {url}"))
                if sub: c.execute("INSERT INTO events(ts,person_id,listing_id,kind,detail) VALUES(?,?,?,?,?)", (sub.isoformat() + "T09:00:00", pid, lid, "removal_sent", f"CCPA email to privacy@{sid}.com"))
                if conf: c.execute("INSERT INTO events(ts,person_id,listing_id,kind,detail) VALUES(?,?,?,?,?)", (conf.isoformat() + "T03:20:00", pid, lid, "confirmed_removed", f"{name} page gone"))
                if st == "reappeared": c.execute("INSERT INTO events(ts,person_id,listing_id,kind,detail) VALUES(?,?,?,?,?)", (T.isoformat() + "T03:14:00", pid, lid, "ALERT_reappeared", f"{name} listing is back"))
        await b.close()
    for d in range(84):  # weekly scans + rechecks for the heatmap
        day = T - datetime.timedelta(d)
        for pid in PEOPLE:
            if day.weekday() == 6 or random.random() < .12:
                for _ in range(random.randint(1, 3 if day.weekday() == 6 else 1)): c.execute("INSERT INTO events(ts,person_id,kind,detail) VALUES(?,?,?,?)", (day.isoformat() + "T03:00:00", pid, "scan_finished", "weekly scan + re-check"))
    c.commit()
asyncio.run(main())
