"""Inbox page: unified sent/received list, tabs, filters, thread view, mark handled, member scoping. Run: python -m pytest test/test_inbox.py"""
import os, sys, tempfile, pathlib, re, json
T = tempfile.mkdtemp(); ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.update(DB_PATH=f"{T}/app.db", SHOT_DIR=f"{T}/shots", AUTH_DIR=f"{T}/auth", PROFILES_DIR=str(ROOT / "test/profiles"), SITES_FILE=str(ROOT / "test/sites.test.yaml"),
                  SCHEDULER="false", DRY_RUN="true", ADMIN_USERNAME="alex", ADMIN_PASSWORD="admin correct horse battery", COOKIE_SECURE="false",
                  FF_DATA_KEY="unit-test-key-0123456789-abcdefghijklmnop", SCAN_ON_EDIT="false", SCAN_ON_CREATE="false", INBOUND_SECRET="s", PUBLIC_URL="http://testserver")
sys.path.insert(0, str(ROOT / "optout"))
import pytest
from fastapi.testclient import TestClient
import main, db, mailbox, regreq
APW = "admin correct horse battery"; KPW = "first pass for k 123"; NPW = "my own passphrase 2026 k"

def client(): return TestClient(main.app, follow_redirects=False)
def login(c, u, pw): return c.post("/login", data={"username": u, "password": pw})
def tok(c, path): return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)

def out(pid, subj, state="dry_run", listing=None, stage=0, channel="email", to="privacy@broker.invalid", body="Please delete my data."):
    c = db.conn(); cur = c.execute("INSERT INTO outbox(ts,person_id,to_addr,subject,body,state,listing_id,stage,channel) VALUES(?,?,?,?,?,?,?,?,?)",
                                   (db.now(), pid, to, db.enc(subj), db.enc(body), state, listing, stage, channel)); c.commit(); return cur.lastrowid
def inn(pid, subj, kind, listing=None, outbox=None, frm="noreply@broker.invalid", body="Your request is complete."):
    c = db.conn(); cur = c.execute("INSERT INTO inbox(ts,person_id,listing_id,outbox_id,from_addr,to_addr,subject,kind,links,clicked,body) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                   (db.now(), pid, listing, outbox, frm, "privacy@x.invalid", db.enc(subj), kind, json.dumps(["https://broker.invalid/v?t=1"]), "[]", db.enc(body))); c.commit(); return cur.lastrowid
def listing(pid, site="mockemail", status="submitted"):
    c = db.conn(); cur = c.execute("INSERT INTO listings(person_id,site,url,fields,first_seen,last_seen,status) VALUES(?,?,?,?,?,?,?)", (pid, site, db.enc(f"https://x.invalid/{os.urandom(3).hex()}"), db.enc("{}"), db.now(), db.now(), status))
    c.commit(); return cur.lastrowid

@pytest.fixture(scope="module")
def data():
    l1 = listing("alex"); o1 = out("alex", "Delete request A", listing=l1); f1 = out("alex", "Follow-up A", listing=l1, stage=1)
    i1 = inn("alex", "Please verify your request", "verify", listing=l1, outbox=o1)
    l2 = listing("sam", status="confirmed_removed"); o2 = out("sam", "Delete request S", state="sent", listing=l2); i2 = inn("sam", "Removed!", "removed", listing=l2, outbox=o2)
    o3 = out("sam", "Blocked one", state="blocked: no address"); i3 = inn("alex", "We need more info", "need_info")
    j = inn("alex", "Win a prize", "junk"); d = out("alex", "Your weekly privacy digest", state="sent", channel="weekly_test", to="alex@x.invalid")
    return dict(l1=l1, o1=o1, f1=f1, i1=i1, o2=o2, i2=i2, o3=o3, i3=i3, j=j, d=d)

@pytest.fixture(scope="module")
def admin():
    c = client(); assert login(c, "alex", APW).status_code == 303; return c
@pytest.fixture(scope="module")
def member(admin):
    t = tok(admin, "/family")
    assert admin.post("/family/sam/account", data={"csrf": t, "action": "password", "username": "sam", "password": KPW}).status_code == 303
    c = client(); login(c, "sam", KPW); t = tok(c, "/account/password")
    assert c.post("/account/password", data={"csrf": t, "current": KPW, "password": NPW, "password2": NPW}).status_code == 303
    return c

def test_unified_list_and_statuses(data):
    items = {x["key"]: x for x in mailbox.messages(db.conn())}
    assert f"in-{data['j']}" not in items                                   # junk excluded
    assert items[f"out-{data['o1']}"]["label"] == "Draft (dry run, not sent)" and items[f"out-{data['o1']}"]["bucket"] == "pending"
    assert items[f"out-{data['f1']}"]["stage"] == "Follow-up"
    assert items[f"in-{data['i1']}"]["bucket"] == "progress" and items[f"in-{data['i2']}"]["bucket"] == "done"
    assert items[f"out-{data['o2']}"]["bucket"] == "done"                   # listing removed
    assert items[f"out-{data['o3']}"]["label"] == "Not sent" and items[f"in-{data['i3']}"]["bucket"] == "needs"
    assert items[f"out-{data['d']}"]["kind"] == "digest" and items[f"out-{data['d']}"]["broker"] == "Weekly digest"
    c = mailbox.counts(list(items.values())); assert c["all"] == sum(c[k] for k in ("needs", "pending", "progress", "done"))

def test_page_tabs_filters_search(admin, data):
    r = admin.get("/inbox"); assert r.status_code == 200
    for s in ("Delete request A", "Removed!", "Draft (dry run, not sent)", "Weekly digest", 'href="/inbox"'): assert s in r.text
    assert "Win a prize" not in r.text
    r = admin.get("/inbox?tab=done"); assert "Removed!" in r.text and "Delete request A" not in r.text
    r = admin.get("/inbox?tab=needs"); assert "We need more info" in r.text and "Blocked one" in r.text and "Removed!" not in r.text
    r = admin.get("/inbox?dir=in"); assert "Removed!" in r.text and "Delete request A" not in r.text
    r = admin.get("/inbox?person=sam"); assert "Delete request S" in r.text and "Delete request A" not in r.text
    r = admin.get("/inbox?q=verify"); assert "Please verify" in r.text and "Removed!" not in r.text
    r = admin.get("/inbox?tab=bogus&dir=sideways&q=" + "x" * 500); assert r.status_code == 200

def test_thread_view_and_handled(admin, data):
    r = admin.get(f"/inbox/out/{data['o1']}"); assert r.status_code == 200
    assert "Please delete my data." in r.text and f"/listing/{data['l1']}" in r.text and "Follow-up A" in r.text and "Please verify your request" in r.text
    r = admin.get(f"/inbox/in/{data['i1']}"); assert "https://broker.invalid/v?t=1" in r.text and "Your request is complete." in r.text
    assert admin.get("/inbox/in/999999").status_code == 404 and admin.get("/inbox/zz/1").status_code == 404 and admin.get(f"/inbox/in/{data['j']}").status_code == 404
    assert admin.post(f"/inbox/in/{data['i3']}/handled", data={}).status_code in (403, 400)   # csrf required
    t = tok(admin, f"/inbox/in/{data['i3']}")
    assert admin.post(f"/inbox/in/{data['i3']}/handled", data={"csrf": t}).status_code == 303
    assert "We need more info" in admin.get("/inbox?tab=done").text and "Mark unhandled" in admin.get(f"/inbox/in/{data['i3']}").text
    assert admin.post(f"/inbox/in/{data['i3']}/handled", data={"csrf": t, "undo": "1"}).status_code == 303
    assert "We need more info" in admin.get("/inbox?tab=needs").text

def test_member_scoped(member, data):
    r = member.get("/inbox"); assert r.status_code == 200 and "Delete request S" in r.text and "Delete request A" not in r.text and "Weekly digest" not in r.text
    assert member.get(f"/inbox/out/{data['o1']}").status_code == 404 and member.get(f"/inbox/out/{data['o2']}").status_code == 200
    assert 'href="/inbox"' in r.text

def test_logged_out_redirect():
    assert client().get("/inbox").status_code == 303

def test_one_definition_of_sent_and_family_adds_up(admin, data):
    """v1.3.6: superseded dry-run drafts and web-form records are never 'Sent'; Inbox and Family & accounts use mailbox.mail_counts,
    and per-person counts add up to the Inbox total."""
    sup = out("alex", "Old draft", state="dry_run_superseded"); form = out("alex", "Registry web form", state="needs_human", channel="form")
    rq = out("alex", "Rate limited", state="error: Resend 429: Too many requests", channel="registry_email")
    items = {x["key"]: x for x in mailbox.messages(db.conn())}
    assert items[f"out-{sup}"]["label"] == "Draft (dry run, not sent)" and items[f"out-{form}"]["label"] == "Web form for a person"
    assert items[f"out-{rq}"]["label"] == "Not sent"
    mc = mailbox.mail_counts(db.conn()); c = db.conn()
    real = c.execute("SELECT COUNT(*) FROM outbox WHERE state='sent'").fetchone()[0]
    assert mc["total"]["sent"] == real == sum(v["sent"] for v in mc["people"].values())
    assert mc["total"]["drafted"] == c.execute("SELECT COUNT(*) FROM outbox WHERE state LIKE 'dry_run%'").fetchone()[0]
    assert sum(sum(v.values()) for v in mc["people"].values()) == sum(mc["total"].values())
    assert sum(mc["total"][k] for k in ("sent", "drafted", "not_sent", "queued", "manual")) == c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    r = admin.get("/inbox").text
    assert re.search(r'data-mc="sent">%d<' % real, r) and re.search(r'data-mc="drafted">%d<' % mc["total"]["drafted"], r)
    f = admin.get("/family").text
    assert re.search(r'data-total-sent>%d<' % real, f) and "Removals filed" in f
    per = [int(x) for x in re.findall(r'data-sent>(\d+)<', f)]
    assert sum(per) + (mc["people"].get(None, {}).get("sent", 0)) == real
    r = admin.get("/inbox?person=sam").text; assert re.search(r'data-mc="sent">%d<' % mc["people"]["sam"]["sent"], r)

def test_member_counts_scoped(member, data):
    mc = mailbox.mail_counts(db.conn(), {"sam"})["total"]
    r = member.get("/inbox?person=alex").text   # a member cannot widen the counts to someone else
    assert re.search(r'data-mc="sent">0<', r)
    r = member.get("/inbox").text; assert re.search(r'data-mc="sent">%d<' % mc["sent"], r)
