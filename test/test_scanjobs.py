"""Scans run on one background worker: repeat clicks don't stack, progress is visible, Heartbeat shows the log, the app stays responsive."""
import os, sys, tempfile, pathlib, re, time, asyncio, threading
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
sys.path.insert(0, str(ROOT / "optout"))
from fastapi.testclient import TestClient
import main, scanner, scanjobs
APW = "admin correct horse battery"

def test_scan_dedupe_progress_log_and_health(monkeypatch):
    runs = []; gate = threading.Event()
    async def fake(pid=None, progress=None):
        runs.append(pid)
        for i in range(1, 4):
            progress(i, 3, i - 1, 0)
            while not gate.is_set() and i == 2: await asyncio.sleep(0.02)
        return {}
    monkeypatch.setattr(scanner, "run_all", fake)
    import db; k = db.conn(); k.execute("UPDATE people SET esign_at=? WHERE id='alex'", (db.now(),)); k.commit()
    c = TestClient(main.app, follow_redirects=False); c.post("/login", data={"username": "alex", "password": APW})
    t = re.search(r'name="csrf" value="([^"]+)"', c.get("/person/alex").text).group(1)
    for _ in range(4): assert c.post("/person/alex/scan", data={"csrf": t}).status_code == 303
    for _ in range(100):
        j = c.get("/person/alex/scan/status").json()
        if j["state"] == "running" and j["done"] == 2: break
        time.sleep(0.02)
    assert j["state"] == "running" and j["total"] == 3
    page = c.get("/person/alex").text; assert "Scanning… 2 of 3 sites" in page and "disabled" in page
    t0 = time.time(); assert c.get("/healthz").status_code == 200 and time.time() - t0 < 1   # health answers mid-scan
    gate.set()
    for _ in range(100):
        j = c.get("/person/alex/scan/status").json()
        if j["state"] == "done": break
        time.sleep(0.02)
    assert j["state"] == "done" and j["found"] == 2 and runs == ["alex"]
    for _ in range(50):
        if scanjobs.log(main.C): break
        time.sleep(0.02)
    assert "Scan log" in c.get("/admin/heartbeat").text and scanjobs.log(main.C)[0]["found"] == 2
    assert scanjobs.enqueue("alex") is True   # finished: a new scan may start again
    gate.set()

def test_low_memory_chromium_flags():
    a = scanner.launch_kw()["args"]
    assert "--disable-dev-shm-usage" in a and "--renderer-process-limit=1" in a
