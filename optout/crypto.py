"""Field-level encryption for PII at rest (AES-256-SIV, deterministic so encrypted columns can still be matched with '=').
Key: FF_DATA_KEY (env) or the file named by FF_DATA_KEY_FILE (default /run/secrets/ff_data_key). Any string of >= 32 chars;
generate one with:  python -c "import secrets;print(secrets.token_urlsafe(48))"
If neither is set, a key is generated once into $DATA_DIR/secrets/ff_data_key (0600) and the dashboard shows a warning,
because a key stored next to the data only protects copies of the database file, not the whole volume."""
import os, base64, secrets, hashlib
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
PREFIX = "enc1:"
KEY_SOURCE = "env"
KEY_PATH = None

def _load_key():
    global KEY_SOURCE, KEY_PATH
    k = os.environ.get("FF_DATA_KEY", "").strip()
    f = os.environ.get("FF_DATA_KEY_FILE", "/run/secrets/ff_data_key")
    if not k and os.path.exists(f): k = open(f).read().strip(); KEY_SOURCE = "secret_file"
    if not k:
        p = __import__("paths").key_file()
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, secrets.token_urlsafe(48).encode()); os.close(fd)
            print(f"Generated a new data encryption key at {p}. Back it up (or set FF_DATA_KEY): without it the data can't be decrypted.", flush=True)
        k = open(p).read().strip(); KEY_SOURCE = "generated"; KEY_PATH = p
    if len(k) < 32: raise SystemExit("FF_DATA_KEY must be at least 32 characters")
    global _RAW; _RAW = k.encode()
    return HKDF(hashes.SHA256(), 64, b"fletcher-fortis-v1", b"pii-fields").derive(k.encode())
_RAW = b""
_SIV = AESSIV(_load_key())

def derived(info, n=32):
    """A separate key for another purpose (e.g. backup archives), derived from the same FF_DATA_KEY."""
    return HKDF(hashes.SHA256(), n, b"fletcher-fortis-v1", info).derive(_RAW)

def enc(v):
    if v is None or v == "" or not isinstance(v, str) or v.startswith(PREFIX): return v
    return PREFIX + base64.urlsafe_b64encode(_SIV.encrypt(v.encode(), None)).decode()

def dec(v):
    if isinstance(v, str) and v.startswith(PREFIX):
        return _SIV.decrypt(base64.urlsafe_b64decode(v[len(PREFIX):]), None).decode()
    return v

def token_hash(t): return hashlib.sha256(t.encode()).hexdigest()
