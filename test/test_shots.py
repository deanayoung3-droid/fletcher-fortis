"""v1.2.3 screenshots: real 1280px WebP captures (full + cropped record), before/after proof, retake, fallback, consent lock."""
import os, sys, tempfile, pathlib, re, asyncio
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
sys.path.insert(0, str(ROOT / "optout"))
from fastapi.testclient import TestClient
from PIL import Image
import main, db, shots, scanjobs
APW = "admin correct horse battery"
PAGE = "<html><body style='margin:0;font:16px sans-serif'><div style='height:900px;background:#eee'>header</div>" \
       "<article style='padding:20px;border:1px solid #ccc;width:600px'><h2>Alex Rivera</h2><p>Age 41 · Springfield, CA</p></article>" \
       "<div style='height:1500px'></div></body></html>"

def client():
    c = TestClient(main.app, follow_redirects=False); c.post("/login", data={"username": "alex", "password": APW}); return c

N = [0]
def listing(pid):
    N[0] += 1
    c = db.conn(); lid = c.execute("INSERT INTO listings(person_id,site,url,first_seen,status) VALUES(?,?,?,?,?)", (pid, "mockemail", db.enc(f"https://x.invalid/{pid}/{N[0]}"), db.now(), "candidate")).lastrowid; c.commit(); return lid

async def snap(lid, pid, kind):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch(); pg = await b.new_page(); await pg.set_content(PAGE)
        sid = await shots.capture(pg, {"id": lid, "person_id": pid}, kind, "Alex", "Rivera"); await b.close(); return sid

def test_capture_webp_full_and_crop_and_proof():
    c = db.conn(); c.execute("UPDATE people SET esign_at=? WHERE id='alex'", (db.now(),)); c.commit()
    lid = listing("alex"); sid = asyncio.run(snap(lid, "alex", "before"))
    row = db.conn().execute("SELECT * FROM shots WHERE id=?", (sid,)).fetchone()
    assert row["ok"] == 1 and row["full"].endswith(".webp") and row["crop"].endswith("_crop.webp")
    im = Image.open(row["full"]); assert im.format == "WEBP" and im.width == 1280
    cr = Image.open(row["crop"]); assert cr.width == 1280 and cr.height < 600   # just the record
    assert row["bytes"] < 400_000
    a = client(); r = a.get(f"/shots/{sid}/crop"); assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "attachment" in a.get(f"/shots/{sid}/full?download=1").headers.get("content-disposition", "")
    asyncio.run(snap(lid, "alex", "after"))
    page = a.get(f"/listing/{lid}").text
    assert "Proof · before and after" in page and "Before" in page and "After" in page and 'data-lb-group="proof-' in page and 'id="lb"' in page
    assert f"/shots/{sid}/crop" in a.get("/person/alex").text   # card thumbnail

def test_failure_fallback_and_retake(monkeypatch):
    lid = listing("alex")
    c = shots.ensure(); c.execute("INSERT INTO shots(listing_id,person_id,kind,ts,ok,error) VALUES(?,?,?,?,0,?)", (lid, "alex", "before", db.now(), "TimeoutError: page did not load")); c.commit()
    a = client(); page = a.get(f"/listing/{lid}").text; assert "Capture failed" in page and "TimeoutError" in page and "Retake" in page
    seen = []; monkeypatch.setattr(scanjobs, "enqueue_shot", lambda l, kind="retake": seen.append(l) or True)
    t = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = a.post(f"/listing/{lid}/retake", data={"csrf": t}); assert r.status_code == 303 and seen == [lid]

def test_consent_lock_hides_shots():
    c = db.conn(); c.execute("UPDATE people SET esign_at=NULL, auth_pdf=NULL WHERE id='sam'"); c.commit()
    lid = listing("sam"); c = shots.ensure(); d = os.environ["SHOT_DIR"]; os.makedirs(d, exist_ok=True); f = os.path.join(d, f"sam_{lid}_before_x.webp")
    Image.new("RGB", (10, 10)).save(f, "WEBP")
    sid = c.execute("INSERT INTO shots(listing_id,person_id,kind,ts,full,ok) VALUES(?,?,?,?,?,1)", (lid, "sam", "before", db.now(), f)).lastrowid; c.commit()
    a = client(); assert a.get(f"/shots/{sid}/full").status_code == 403
    assert f"/shots/{sid}" not in a.get("/person/sam").text
