"""Optional Cloudflare Access check (defence in depth when the app sits behind Cloudflare Access, e.g. a home server).

Cloudflare Access puts a signed token (RS256 JWT) in the Cf-Access-Jwt-Assertion header. When PLATFORM_CF_TEAM_DOMAIN and
PLATFORM_CF_AUD are set, the app verifies it against https://<team>/cdn-cgi/access/certs (signature, iss, aud, exp).
CF_ACCESS_MODE:
  off ...... (default) not checked
  log ...... checked and shown on Heartbeat; requests are never refused
  require .. requests without a valid token get 403 (except /healthz and /inbound/*, which have their own secret and
             reach the app through an Access service token)
The app's own login stays the source of truth: a valid Access token never logs anyone in, and the plain
Cf-Access-Authenticated-User-Email header is never trusted on its own."""
import base64, json, os, threading, time, urllib.request
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_JWKS = {"at": 0.0, "keys": {}}; _L = threading.Lock(); LAST = {}
EXEMPT = ("/healthz", "/inbound/", "/static/", "/favicon.ico")

def team(): return (os.environ.get("PLATFORM_CF_TEAM_DOMAIN") or "").strip().removeprefix("https://").rstrip("/")
def aud(): return (os.environ.get("PLATFORM_CF_AUD") or "").strip()
def mode():
    m = (os.environ.get("CF_ACCESS_MODE") or "off").lower()
    return m if m in ("off", "log", "require") and team() and aud() else "off"

def _b64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def _int(s): return int.from_bytes(_b64(s), "big")

def _keys(force=False, fetch=None):
    with _L:
        if force or time.time() - _JWKS["at"] > 3600 or not _JWKS["keys"]:
            raw = (fetch or (lambda u: urllib.request.urlopen(u, timeout=10).read()))(f"https://{team()}/cdn-cgi/access/certs")  # nosec B310 (fixed https)
            d = json.loads(raw); _JWKS["keys"] = {k["kid"]: rsa.RSAPublicNumbers(_int(k["e"]), _int(k["n"])).public_key() for k in d.get("keys", []) if k.get("kty") == "RSA"}
            _JWKS["at"] = time.time()
        return _JWKS["keys"]

def verify(token, fetch=None, now=None):
    """Returns the claims of a valid token, or raises ValueError with the reason."""
    try: h64, p64, s64 = token.split(".")
    except Exception: raise ValueError("malformed token")
    hdr = json.loads(_b64(h64)); claims = json.loads(_b64(p64))
    if hdr.get("alg") != "RS256": raise ValueError("unexpected algorithm")
    key = _keys(fetch=fetch).get(hdr.get("kid")) or _keys(force=True, fetch=fetch).get(hdr.get("kid"))
    if not key: raise ValueError("unknown signing key")
    try: key.verify(_b64(s64), f"{h64}.{p64}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except Exception: raise ValueError("bad signature")
    t = now or time.time()
    if claims.get("iss") != f"https://{team()}": raise ValueError("wrong issuer")
    a = claims.get("aud"); a = a if isinstance(a, list) else [a]
    if aud() not in a: raise ValueError("wrong audience")
    if claims.get("exp") and t > float(claims["exp"]) + 60: raise ValueError("expired")
    if claims.get("nbf") and t < float(claims["nbf"]) - 60: raise ValueError("not yet valid")
    return claims

def identity(claims):
    """Person email, or 'service:<common_name>' for service tokens (mobile apps, the inbound mail Worker)."""
    return claims.get("email") or ("service:" + str(claims.get("common_name") or "token"))

def check(path, headers):
    """(allowed, identity or reason). Never logs anyone in."""
    m = mode()
    if m == "off" or any(path == e or (e.endswith("/") and path.startswith(e)) for e in EXEMPT): return True, None
    tok = headers.get("cf-access-jwt-assertion") or ""
    try: who = identity(verify(tok)) if tok else None; why = None if tok else "no Access token"
    except Exception as e: who, why = None, str(e) or type(e).__name__
    LAST.update(at=time.strftime("%Y-%m-%d %H:%M:%S"), ok=bool(who), who=who or "", why=why or "", path=path[:80])
    return (bool(who) or m != "require"), (who or why)
