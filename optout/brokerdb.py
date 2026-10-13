"""Merge + dedupe broker sources into /data/brokers_merged.yaml (normalized). Run weekly by the scheduler; also CLI.
Sources: Eraser built-in list, CA CPPA registry CSV (live), optional extra sources in EXTRA (Vermont: portal-only, no bulk export)."""
import csv, glob, io, os, re, sys, json, urllib.request, yaml, socket, datetime
from urllib.parse import urlparse
SEED = "/app/brokers"
BASE = os.environ.get("BROKER_SRC", "/data/brokers")   # writable copy (the image is read-only); seeded from /app/brokers
if not os.path.isdir(BASE) and os.path.isdir(SEED):
    import shutil
    try: shutil.copytree(SEED, BASE)
    except OSError: BASE = SEED
OUT = os.environ.get("BROKER_OUT", "/data/brokers_merged.yaml")
CA_URL = "https://cppa.ca.gov/data_broker_registry/registry.csv"
ERASER_RAW = "https://raw.githubusercontent.com/digisamroc/eraser/main/data/brokers.yaml"

def dom(u):
    if not u: return ""
    u = u.strip(); u = u if "://" in u else "http://" + u
    d = (urlparse(u).hostname or "").lower()
    return d[4:] if d.startswith("www.") else d
def key(name): return re.sub(r"\b(inc|llc|ltd|corp|co|corporation|company|the)\b|[^a-z0-9]", "", (name or "").lower())
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

def fetch(url, path):
    try:
        if not url.startswith("https://"): raise ValueError("https only")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 family-optout"})
        data = urllib.request.urlopen(req, timeout=60).read(50_000_000)  # nosec B310
        tmp = path + ".tmp"; open(tmp, "wb").write(data); os.replace(tmp, path); return True
    except Exception as e: print("refresh failed", url, e); return False

def from_eraser(p):
    for b in yaml.safe_load(open(p))["brokers"]:
        yield dict(name=b["name"], domain=dom(b.get("website")), optout_url=b.get("opt_out_url") or "", email=b.get("email") or "",
                   method="email" if b.get("email") else "web_form", captcha=None, category=b.get("category"), src="eraser")
def from_ca(p):
    for r in csv.DictReader(open(p, encoding="utf-8-sig", errors="ignore")):
        g = lambda pre: next((v for k, v in r.items() if k and k.startswith(pre)), "") or ""
        em = (EMAIL.findall(g("Data broker primary contact email")) or [""])[0]
        yield dict(name=g("Data broker name").strip(), domain=dom(g("Data broker primary website")), optout_url=g("Data broker's primary website that contains").strip(),
                   email=em, method="email" if em else "web_form", captcha=None, category="ca-registered", src="ca_registry")
EXTRA = []   # (generator(base_dir), refresh(base_dir)) pairs for private/optional sources

def merge(refresh=False):
    if refresh:
        fetch(CA_URL, f"{BASE}/ca_registry.csv"); fetch(ERASER_RAW, f"{BASE}/eraser.yaml")
        for _g, r in EXTRA: r(BASE)
    counts, merged, by = {}, [], {}
    for gen in (*(g(BASE) for g, _r in EXTRA), from_eraser(f"{BASE}/eraser.yaml"), from_ca(f"{BASE}/ca_registry.csv")):
        for b in gen:
            counts[b["src"]] = counts.get(b["src"], 0) + 1
            k = b["domain"] or key(b["name"]); k2 = key(b["name"])
            hit = by.get(k) or by.get(k2)
            if hit:  # fill blanks, keep provenance
                for f in ("optout_url", "email", "domain", "category"):
                    if not hit[f] and b[f]: hit[f] = b[f]
                if hit["captcha"] is None: hit["captcha"] = b["captcha"]
                if b["src"] not in hit["sources"]: hit["sources"].append(b["src"])
                if hit["email"] and hit["method"] == "web_form" and not hit["optout_url"]: hit["method"] = "email"
            else:
                b["sources"] = [b.pop("src")]; merged.append(b); by[k] = b; by[k2] = b
    out = {"generated": datetime.datetime.now().isoformat(timespec="seconds"), "source_counts": counts, "total": len(merged),
           "vermont": "skipped: VT SOS registry is a search portal with no bulk export (bizfilings.vermont.gov/online/DatabrokerInquire)", "brokers": merged}
    os.makedirs(os.path.dirname(OUT), exist_ok=True); yaml.safe_dump(out, open(OUT, "w"), sort_keys=False, allow_unicode=True)
    return counts, len(merged)

def check_contacts(path=OUT):
    """Cheap health check: DNS-resolve each broker domain + email domain (no email sent, no HTTP to opt-out forms)."""
    d = yaml.safe_load(open(path)); bad = 0
    for b in d["brokers"]:
        doms = {b["domain"], b["email"].split("@")[-1] if b["email"] else ""} - {""}
        b["contact_ok"] = all(_resolves(x) for x in doms) if doms else False; bad += not b["contact_ok"]
    d["contact_check"] = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "unresolvable": bad}
    yaml.safe_dump(d, open(path, "w"), sort_keys=False, allow_unicode=True); return bad
def _resolves(h):
    try: socket.setdefaulttimeout(4); socket.gethostbyname(h); return True
    except Exception: return False
if __name__ == "__main__":
    print(merge(refresh="--refresh" in sys.argv))
    if "--check" in sys.argv: print("unresolvable:", check_contacts())
