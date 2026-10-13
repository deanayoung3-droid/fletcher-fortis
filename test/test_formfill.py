"""Form automation against the local mock opt-out form only (never a real broker)."""
import os, sys, tempfile, pathlib, threading, asyncio, functools, http.server
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/f.db", FF_DATA_KEY="formfill-test-key-0123456789-abcdefgh", SITES_FILE=str(ROOT / "test/sites.test.yaml"), PROFILES_DIR=str(ROOT / "test/profiles"))
sys.path.insert(0, str(ROOT / "optout"))
import formfill
hits = []
class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): hits.append(self.path)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(ROOT / "test/mock"))); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
VALS = {"first": "Alex", "middle": "", "last": "Rivera", "full": "Alex Rivera", "email": "privacy+alex@mail.example.invalid", "phone": "", "street": "", "city": "Springfield", "state": "CA", "url": "http://x.invalid/p/1"}

def run(url, dry, captcha=False):
    from playwright.async_api import async_playwright
    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch(); pg = await b.new_page()
            try: return await formfill.fill(pg, url, VALS, f"{T}/shot.png", dry=dry, captcha_flag=captcha), pg.url
            finally: await b.close()
    return asyncio.run(go())

def test_dry_run_fills_but_never_submits():
    hits.clear(); (st, detail), url = run(f"http://127.0.0.1:{PORT}/optout-form.html", dry=True)
    assert st == "dry_run_filled" and "email" in detail and "first" in detail and "url" in detail and "state" in detail and "checkbox" in detail
    assert not any("optout-done" in h for h in hits) and os.path.exists(f"{T}/shot.png")

def test_live_submits_mock_form():
    hits.clear(); (st, detail), url = run(f"http://127.0.0.1:{PORT}/optout-form.html", dry=False)
    assert st == "submitted" and "optout-done.html" in url and "first_name=Alex" in url and "agree=on" in url and "state=CA" in url

def test_captcha_goes_to_human():
    (st, detail), _ = run(f"http://127.0.0.1:{PORT}/optout-captcha.html", dry=False); assert st == "needs_human" and "CAPTCHA" in detail
    (st, _), _ = run(f"http://127.0.0.1:{PORT}/optout-form.html", dry=False, captcha=True); assert st == "needs_human"
