"""Version + update check. The app never touches Docker itself (no docker.sock in this container):
  - checks the GitHub releases API at most once a day (cached in /data/update_check.json),
  - "Update now" / auto-update ask an optional Watchtower sidecar over its token-protected HTTP API to pull the new image
    and recreate only this container (label-scoped). Without the sidecar, Heartbeat shows the Portainer / compose steps."""
import datetime, json, os, re, urllib.request
import config
HERE = os.path.dirname(__file__)
REPO_DEFAULT = "deanayoung3-droid/fletcher-fortis"
CACHE = os.path.join(os.environ.get("DATA_DIR", "/data"), "update_check.json")

def current():
    for p in (os.path.join(HERE, "..", "VERSION"), "/app/VERSION"):
        try: return open(p).read().strip().lstrip("v")
        except OSError: pass
    return "0.0.0"

def parse(v):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([\w.]+))?", (v or "").strip())
    if not m: return (0, 0, 0, "")
    return (int(m[1]), int(m[2]), int(m[3]), m[4] or "~")   # "~" sorts after any pre-release tag: 1.0.0 > 1.0.0-rc1
def newer(a, b): return parse(a) > parse(b)

def repo(): return (config.get("GITHUB_REPO") or REPO_DEFAULT).strip().strip("/")
def _load():
    try: return json.load(open(CACHE))
    except Exception: return {}

def check(force=False):
    """{current, latest, newer, url, notes, checked, error}. Network at most once per 24 h unless forced."""
    d = _load(); cur = current()
    fresh = d.get("checked") and (datetime.datetime.now() - datetime.datetime.fromisoformat(d["checked"])).total_seconds() < 86400
    if force or not fresh or d.get("repo") != repo() or d.get("channel") != config.get("UPDATE_CHANNEL", "stable"):
        try:
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo()): raise ValueError("bad repo name")
            beta = config.get("UPDATE_CHANNEL", "stable") == "beta"
            req = urllib.request.Request(f"https://api.github.com/repos/{repo()}/releases" + ("?per_page=10" if beta else "/latest"), headers={"Accept": "application/vnd.github+json", "User-Agent": "fletcher-fortis/" + cur})
            r = json.loads(urllib.request.urlopen(req, timeout=15).read(2_000_000))  # nosec B310: fixed https host
            if beta:   # newest non-draft release, pre-releases included
                r = next((x for x in r if not x.get("draft")), None)
                if not r: raise urllib.error.HTTPError(req.full_url, 404, "none", None, None)
            d = {"latest": (r.get("tag_name") or "").lstrip("v"), "url": r.get("html_url"), "notes": (r.get("body") or "")[:20000], "published": r.get("published_at"), "error": ""}
        except urllib.error.HTTPError as e: d = {**d, "error": "no releases published yet" if e.code == 404 else f"GitHub API HTTP {e.code}"}
        except Exception as e: d = {**d, "error": type(e).__name__}
        d.update(checked=datetime.datetime.now().isoformat(timespec="seconds"), repo=repo(), channel=config.get("UPDATE_CHANNEL", "stable"))
        try:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True); tmp = CACHE + ".tmp"; json.dump(d, open(tmp, "w")); os.replace(tmp, CACHE)
        except OSError: pass
    return {**d, "current": cur, "newer": bool(d.get("latest")) and newer(d.get("latest"), cur), "repo": repo(),
            "image": f"ghcr.io/{repo().lower()}:{d.get('latest') or 'latest'}", "watchtower": watchtower_ready()}

def cached():
    """For page views: never blocks on the network; refreshes in the background when the cache is older than a day."""
    d = _load(); stale = not d.get("checked") or (datetime.datetime.now() - datetime.datetime.fromisoformat(d["checked"])).total_seconds() >= 86400 or d.get("repo") != repo()
    if stale and os.environ.get("UPDATE_CHECK", "true") == "true":
        import threading; threading.Thread(target=check, daemon=True).start()
    cur = current()
    return {**d, "current": cur, "newer": bool(d.get("latest")) and newer(d.get("latest"), cur), "repo": repo(), "image": f"ghcr.io/{repo().lower()}:{d.get('latest') or 'latest'}", "watchtower": watchtower_ready()}

def watchtower_ready(): return bool(os.environ.get("WATCHTOWER_URL") and config.get("WATCHTOWER_TOKEN"))

def trigger():
    """Ask the Watchtower sidecar to update this container (async). Returns a short status string."""
    if not watchtower_ready(): return "Watchtower sidecar not configured"
    url = os.environ["WATCHTOWER_URL"].rstrip("/")
    if not re.match(r"^https?://[\w.-]+(:\d+)?$", url): return "WATCHTOWER_URL must be like http://watchtower:8080"
    req = urllib.request.Request(url + "/v1/update?async=true", data=b"", method="POST", headers={"Authorization": "Bearer " + config.get("WATCHTOWER_TOKEN")})
    try: r = urllib.request.urlopen(req, timeout=20); return f"update requested (HTTP {r.status}); the app restarts in a minute if a new image exists"  # nosec B310: admin-configured sidecar
    except urllib.error.HTTPError as e: return f"Watchtower refused: HTTP {e.code}"
    except Exception as e: return f"Watchtower unreachable: {type(e).__name__}"

def auto_tick():
    """Daily from the scheduler: only acts when AUTO_UPDATE is on and a newer release exists."""
    if config.get("AUTO_UPDATE", "false") != "true": return None
    c = check()
    return trigger() if c["newer"] else None
