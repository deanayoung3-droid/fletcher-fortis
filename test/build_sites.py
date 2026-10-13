"""Build optout/sites.yaml (generic, data-driven) from: the local 1,076-broker DB, optional private definitions,
Optery's public list (People Search Site + Phone Directory), BADBOOL, and the hand-tuned overrides.
Re-run after test/probe_all.py to merge discovered search templates + health. Usage: python3 test/build_sites.py [probe_results.json]"""
import yaml, json, re, os, sys, glob
from urllib.parse import urlparse
APP = os.path.join(os.path.dirname(__file__), ".."); SRC = os.environ.get("SRC_DIR", "/tmp/src")
def host(u):
    h = urlparse(u if "//" in u else "//" + u).netloc.lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h
def sid(d): return re.sub(r"[^a-z0-9]+", "", d.rsplit(".", 1)[0]) if d.count(".") <= 1 else re.sub(r"[^a-z0-9]+", "-", d)
OPT = re.compile(r"opt|privacy|remov|suppress|claim|ccpa|do-not-sell|delete", re.I)
# parent families: by the domain the opt-out lands on (data-driven), plus a few well documented ownerships
PARENT_BY_OPTOUT = {"beenverified.com": "beenverified", "peopleconnect.us": "peopleconnect", "intelius.com": "peopleconnect",
                    "peoplefinders.com": "peoplefinders", "spokeo.com": "spokeo", "whitepages.com": "whitepages", "mylife.com": "mylife",
                    "system1.com": "system1", "peoplewhiz.com": "peoplewhiz", "infotracer.com": "infotracer", "truthfinder.com": "peopleconnect"}
PARENT_KNOWN = {"beenverified": ["beenverified.com", "peoplelooker.com", "neighborwho.com", "ownerly.com", "numberguru.com", "bumper.com", "yellowbook.com"],
                "peopleconnect": ["intelius.com", "ussearch.com", "classmates.com", "truthfinder.com", "instantcheckmate.com", "zabasearch.com", "addresses.com", "anywho.com", "peoplesmart.com"],
                "peoplefinders": ["peoplefinders.com", "publicrecordsnow.com"],
                "whitepages": ["whitepages.com", "411.com"],
                "privacy-control network": []}
PARENT_OPT = {"beenverified": "https://www.beenverified.com/app/optout/search", "peopleconnect": "https://suppression.peopleconnect.us/login",
              "peoplefinders": "https://www.peoplefinders.com/opt-out", "whitepages": "https://www.whitepages.com/suppression_requests"}
S = {}
def add(dom, name, src, optout=None, email=None, search=None, captcha=None, extra=None):
    if not dom or "." not in dom or dom.endswith(("clarity.ms", "googletagmanager.com", "doubleclick.net")): return
    k = sid(dom); s = S.setdefault(k, {"id": k, "name": name or dom, "domain": dom, "sources": [], "search_url": None, "extractor": "generic", "optout": {}})
    if src not in s["sources"]: s["sources"].append(src)
    if search and not s["search_url"]: s["search_url"] = search
    o = s["optout"]
    if optout and not o.get("url"): o.update(type="form", url=optout)
    if email and not o.get("to"): o.setdefault("type", "email"); o["to"] = email
    if captcha is not None: o["captcha"] = bool(captcha)
    if extra: s.update(extra)
# 1. local DB (includes CA registry + Vermont + eraser) — people-search, background-check, public-records
db = yaml.safe_load(open(f"{APP}/data/brokers_merged.yaml"))
for b in db["brokers"]:
    if b.get("category") in ("people-search", "background-check", "public-records"):
        add(host(b["domain"]), b["name"], "db:" + "+".join(b.get("sources") or []), b.get("optout_url") or None, b.get("email") or None, captcha=b.get("captcha"))
# 3. Optery public list
for x in json.load(open(f"{SRC}/optery_ps.json")):
    ls = [l for l in x["links"] if not re.search(r"cdn\.|amplitude|dreamdata|clarity\.ms", l)]
    if not ls: continue
    d = host(ls[0]); oo = next((l for l in ls[1:] if OPT.search(l)), None)
    add(d, x["name"], "optery:" + x["type"].lower().replace(" ", "-"), oo)
    if oo and host(oo) != d:
        for od, par in PARENT_BY_OPTOUT.items():
            if host(oo).endswith(od): S[sid(d)]["parent"] = par
    if oo and "privacy-control" in oo: S[sid(d)]["parent"] = "privacy-control network"
# 4. BADBOOL people-search section
b = open(f"{SRC}/badbool.md").read(); i = b.find("## People Search Sites"); sec = b[i:b.find("\n## ", i + 10)]
for blk in re.split(r"^###\s*", sec, flags=re.M)[1:]:
    nm = re.sub(r"[^\w .’'&-]", "", blk.split("\n", 1)[0]).strip(); ls = re.findall(r"\]\((https?://[^)]+)\)", blk)
    if not ls: continue
    d = host(ls[0]); oo = next((l for l in ls if OPT.search(l)), None)
    if d.endswith(("google.com", "acxiom.com", "zoominfo.com", "onetrust.com")): continue
    add(d, nm, "badbool", oo)
for par, ds in PARENT_KNOWN.items():
    for d in ds:
        if sid(d) in S: S[sid(d)]["parent"] = par
# 5. hand-tuned overrides from the previous round win
for o in yaml.safe_load(open(f"{APP}/optout/sites.hand.yaml"))["sites"]:
    dom = o.pop("domain", None) or (host(o["search_url"]) if o.get("search_url") else o["id"] + ".com")
    k = sid(dom) if sid(dom) in S else o["id"]; base = S.get(k, {"id": k, "domain": dom, "sources": [], "extractor": "generic", "optout": {}})
    card = o.pop("card", None); base.update({kk: v for kk, v in o.items() if kk != "id"}); base["sources"] = sorted(set(base["sources"]) | {"hand-tuned"})
    if card: base["extractor"] = {"card": card, **({"name_sel": o["name_sel"]} if o.get("name_sel") else {})}
    S[k] = base
# 6. merge probe results (discovered templates + health)
if len(sys.argv) > 1:
    for r in json.load(open(sys.argv[1])):
        s = S.get(r["site"])
        if not s: continue
        if r.get("discovered") and r.get("group") == "works" and not s.get("search_url"): s["search_url"] = r["discovered"]; s["search_discovered"] = True
        if r.get("funnel_parent") and not s.get("parent"): s["parent"] = r["funnel_parent"]
        if r.get("redirect"): s["redirects_to"] = r["redirect"]
        s["health"] = {"status": r["group"], "checked": r.get("when"), "evidence": r.get("evidence", "")[:200]}
for s in S.values():
    if s.get("parent") in PARENT_OPT and not s["optout"].get("url"): s["optout"].update(type="parent_suppression", url=PARENT_OPT[s["parent"]])
    if not s["optout"]: s["optout"] = {"type": "unknown"}
    s["sources"] = sorted(s["sources"])
out = sorted(S.values(), key=lambda s: (s.get("parent") or "~", s["id"]))
hdr = ("# GENERATED by test/build_sites.py from data/brokers_merged.yaml (DB + CA/VT registries), Optery public list, BADBOOL,\n"
       "# and optout/sites.hand.yaml (hand-tuned, wins). Edit sites.hand.yaml, then re-run.\n"
       "# search_url placeholders: {first} {last} {city} {state} (slug), {first_q} {last_q} {city_q} (URL-encoded).\n"
       "# extractor: 'generic' (name+age+city text patterns) or {card: css, name_sel: css}. signals: not_found / block regexes override the defaults.\n"
       "# health.status: works | blocked | unreachable_dc | dead | no_search | broken  (set by test/probe_all.py)\n")
open(f"{APP}/optout/sites.yaml", "w").write(hdr + yaml.safe_dump({"parents": {k: {"optout": PARENT_OPT.get(k)} for k in PARENT_KNOWN}, "sites": out}, sort_keys=False, width=200, allow_unicode=True))
import collections; print(len(out), "sites;", collections.Counter(s.get("parent") for s in out).most_common(8), "with search_url:", sum(1 for s in out if s.get("search_url")))
