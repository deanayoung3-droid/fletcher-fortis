"""Full backup and restore, for moving an installation (e.g. Fly -> home server) without anyone needing server access.

Export: a consistent snapshot of the SQLite database (sqlite backup API, so it is safe while the app runs) plus the files in
DATA_DIR (screenshots, signed authorization PDFs, broker and registry lists) and the small state in DB_DIR, as a tar.gz that
is encrypted while it is written (AES-256-GCM in 1 MiB chunks, key derived from FF_DATA_KEY, chunk order and the last chunk
authenticated). The archive can only be opened by an installation with the same FF_DATA_KEY; the personal data inside the
database stays field-encrypted as well. Not included: the key itself, old pre-migration database copies, temp files.

Import: checks the key fingerprint first (a different FF_DATA_KEY is refused with a clear message, nothing changed), saves a
safety export of the current data, then writes the database into the live file (sqlite backup API) and replaces the files."""
import io, os, sys, json, shutil, sqlite3, struct, tarfile, tempfile, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # also runnable as `python -m optout.backup` from /app
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import crypto, paths, db

MAGIC = b"FFBACKUP1\n"; CHUNK = 1 << 20
SKIP_DIRS = {"backups", "secrets", ".restore", ".backup-tmp", "eraser-home", "bulk", "lost+found"}
DB_FILES = ("optout.db", "optout.db-wal", "optout.db-shm")
STATE_FILES = ("site_health.json", "update_check.json")   # small state: restored into DB_DIR whichever side they came from

def _key(): return crypto.derived(b"backup-archive-v1")
def fingerprint(): return crypto.derived(b"backup-fingerprint-v1", 8).hex()
def tmp_dir():
    d = os.environ.get("BACKUP_TMP") or os.path.join(paths.db_dir(), ".backup-tmp"); os.makedirs(d, mode=0o700, exist_ok=True); return d

class _EncWriter(io.RawIOBase):
    def __init__(self, f, header):
        self.f, self.buf, self.n, self.aes = f, bytearray(), 0, AESGCM(_key())
        self.prefix = os.urandom(4); h = json.dumps(header).encode()
        f.write(MAGIC + struct.pack(">I", len(h)) + h + self.prefix); self.hdr = h
    def writable(self): return True
    def _emit(self, data, last):
        nonce = self.prefix + struct.pack(">Q", self.n); aad = self.hdr + struct.pack(">Q?", self.n, last)
        ct = self.aes.encrypt(nonce, bytes(data), aad); self.f.write(struct.pack(">I?", len(ct), last) + ct); self.n += 1
    def write(self, b):
        self.buf += b
        while len(self.buf) > CHUNK: self._emit(self.buf[:CHUNK], False); del self.buf[:CHUNK]
        return len(b)
    def close(self):
        if not self.closed: self._emit(self.buf, True); self.buf = bytearray()
        super().close()

class _DecReader(io.RawIOBase):
    def __init__(self, f):
        if f.read(len(MAGIC)) != MAGIC: raise ValueError("This is not a Fletcher Fortis backup file.")
        (hl,) = struct.unpack(">I", f.read(4)); self.hdr = f.read(hl); self.header = json.loads(self.hdr)
        if self.header.get("key") != fingerprint(): raise ValueError("This backup was made with a different FF_DATA_KEY. Set the same key as the old installation, then import again. Nothing was changed.")
        self.prefix = f.read(4); self.f, self.n, self.buf, self.done, self.aes = f, 0, b"", False, AESGCM(_key())
    def readable(self): return True
    def _next(self):
        h = self.f.read(5)
        if len(h) < 5: raise ValueError("The backup file is incomplete (truncated).")
        ln, last = struct.unpack(">I?", h); ct = self.f.read(ln)
        try: pt = self.aes.decrypt(self.prefix + struct.pack(">Q", self.n), ct, self.hdr + struct.pack(">Q?", self.n, last))
        except Exception: raise ValueError("The backup file is damaged or was changed.")
        self.n += 1; self.done = last; return pt
    def readinto(self, b):
        while not self.buf and not self.done: self.buf = self._next()
        k = min(len(b), len(self.buf)); b[:k] = self.buf[:k]; self.buf = self.buf[k:]; return k

def _walk(root, arc_prefix, tar, skip_db=False):
    n = 0
    if not os.path.isdir(root): return 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if (skip_db and fn in DB_FILES) or fn.startswith(".") or fn.endswith((".tmp", ".part", ".ffb", ".ffbackup")): continue   # never a backup inside a backup
            full = os.path.join(dp, fn); rel = os.path.relpath(full, root)
            if os.path.islink(full): continue
            tar.add(full, arcname=f"{arc_prefix}/{rel}", recursive=False); n += 1
    return n

def export(out_path=None):
    """Write an encrypted backup; returns (path, summary). The caller deletes the file after sending it."""
    t = tmp_dir(); snap = os.path.join(t, f"snapshot-{os.getpid()}.db")
    src = sqlite3.connect(paths.db_path()); dst = sqlite3.connect(snap)
    with dst: src.backup(dst)
    dst.execute("PRAGMA journal_mode=DELETE"); src.close(); dst.close()
    out_path = out_path or os.path.join(t, f"fletcher-fortis-{datetime.datetime.now():%Y%m%d-%H%M%S}.ffbackup")
    summary = {"version": db.app_version(), "created": db.now(), "key": fingerprint(), "files": 0, "state": 0}
    try:
        with open(out_path, "wb") as f:
            w = _EncWriter(f, summary)
            with tarfile.open(fileobj=w, mode="w|gz") as tar:
                tar.add(snap, arcname="db/optout.db")
                summary["files"] = _walk(paths.data_dir(), "data", tar, skip_db=os.path.realpath(paths.db_dir()) == os.path.realpath(paths.data_dir()))
                if os.path.realpath(paths.db_dir()) != os.path.realpath(paths.data_dir()): summary["state"] = _walk(paths.db_dir(), "state", tar, skip_db=True)
            w.close()
    finally:
        try: os.remove(snap)
        except OSError: pass
    os.chmod(out_path, 0o600); return out_path, summary

def _ver(v):
    try: return tuple(int(x) for x in str(v or "0").lstrip("v").split("-")[0].split("."))
    except ValueError: return (0,)

def peek(fileobj):
    """Header of a backup if this install can open it (same FF_DATA_KEY), else ValueError. Reads only the header."""
    return _DecReader(fileobj).header

def _safe_members(tar):
    for m in tar:
        name = m.name
        if m.issym() or m.islnk() or m.isdev() or name.startswith("/") or ".." in name.split("/") or not name.split("/")[0] in ("db", "data", "state"): continue
        yield m

def import_(fileobj, safety=True):
    """Restore a backup into this installation. Returns a summary dict; raises ValueError (nothing changed) for a wrong key
    or a damaged file detected before anything is written."""
    r = _DecReader(fileobj); header = r.header
    if _ver(header.get("version")) > _ver(db.app_version()): raise ValueError(f"This backup comes from a newer version ({header.get('version')}) than this install ({db.app_version()}). Update this install first. Nothing was changed.")
    stage = tempfile.mkdtemp(prefix="restore-", dir=tmp_dir())
    try:
        n = 0
        with tarfile.open(fileobj=r, mode="r|gz") as tar:
            for m in _safe_members(tar):
                if m.isdir(): continue
                target = os.path.join(stage, m.name); os.makedirs(os.path.dirname(target), exist_ok=True)
                with tar.extractfile(m) as s, open(target, "wb") as d: shutil.copyfileobj(s, d)
                n += 1
        snap = os.path.join(stage, "db", "optout.db")
        if not os.path.exists(snap): raise ValueError("The backup has no database in it.")
        chk = sqlite3.connect(snap); ok = chk.execute("PRAGMA integrity_check").fetchone()[0]; chk.close()
        if ok != "ok": raise ValueError("The database inside the backup failed its integrity check.")
        pre = None
        if safety:
            bd = os.path.join(paths.db_dir(), "backups"); os.makedirs(bd, mode=0o700, exist_ok=True)
            for old in sorted(f for f in os.listdir(bd) if f.startswith("pre-import-"))[:-1]: os.remove(os.path.join(bd, old))   # keep the newest 2
            pre = export(os.path.join(bd, f"pre-import-{datetime.datetime.now():%Y%m%d-%H%M%S}.ffbackup"))[0]
        live = sqlite3.connect(paths.db_path(), timeout=30); src = sqlite3.connect(snap)
        with live: src.backup(live)   # replaces the live database's content in place; open connections see the new data
        src.close(); live.close()
        copied = 0
        for part, root in (("data", paths.data_dir()), ("state", paths.db_dir())):
            base = os.path.join(stage, part)
            if not os.path.isdir(base): continue
            for dp, _, fns in os.walk(base):
                for fn in fns:
                    rel = os.path.relpath(os.path.join(dp, fn), base)
                    if fn in DB_FILES and os.path.dirname(rel) == "": continue
                    dest = os.path.join(paths.db_dir() if rel in STATE_FILES else root, rel); os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.move(os.path.join(dp, fn), dest + ".part"); os.replace(dest + ".part", dest); copied += 1
        db.init()   # apply this version's migrations to the restored database
        return {"from_version": header.get("version"), "created": header.get("created"), "entries": n, "files": copied, "safety_copy": os.path.basename(pre) if pre else None}
    finally: shutil.rmtree(stage, ignore_errors=True)

def save_upload(src, limit):
    """Copy an uploaded file object to a private temp file (streamed, size-limited). Returns the path."""
    fd, path = tempfile.mkstemp(prefix="upload-", suffix=".ffb", dir=tmp_dir()); n = 0
    with os.fdopen(fd, "wb") as out:
        while True:
            b = src.read(CHUNK)
            if not b: break
            n += len(b)
            if n > limit: out.close(); os.remove(path); raise ValueError(f"The file is larger than {limit // 1_000_000} MB.")
            out.write(b)
    return path

MAX_UPLOAD = int(os.environ.get("BACKUP_MAX_MB", "500")) * 1_000_000

def main(argv=None):
    """CLI, runs inside the container:  python -m optout.backup export /tmp/ff-backup.ffb
                                        python -m optout.backup import /tmp/ff-backup.ffb   (then restart the container)
                                        python -m optout.backup check  /tmp/ff-backup.ffb   (opens with this FF_DATA_KEY?)"""
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) != 2 or a[0] not in ("export", "import", "check"): print(main.__doc__); return 2
    cmd, path = a
    try:
        if cmd == "export":
            out, s = export(os.path.abspath(path))
            print(f"exported {out} ({os.path.getsize(out)} bytes, version {s['version']}, {s['files']} files, {s['state']} state files)"); return 0
        with open(path, "rb") as f:
            if cmd == "check": h = peek(f); print(f"ok: opens with this FF_DATA_KEY (version {h.get('version')}, created {h.get('created')})"); return 0
            res = import_(f)
        print(f"restored backup from version {res['from_version']} created {res['created']}: {res['files']} files; safety copy {res['safety_copy']}. Restart the container now.")
        return 0
    except ValueError as e: print(f"error: {e}", file=sys.stderr); return 1

if __name__ == "__main__": sys.exit(main())
