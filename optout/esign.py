"""In-app e-signature for the authorized-agent letter (Cal. Civ. Code 1798.135(e), 11 CCR 7063).
The member signs for themselves only (drawn signature + typed full legal name + checkbox). We record name, timestamp, IP and user account,
and render a plain PDF letter from it. No SSN, date of birth or ID number is ever asked for or printed."""
import os, datetime, hashlib, json
STATEMENT = "I authorize this service to submit privacy requests on my behalf"
def auth_dir(): return os.path.realpath(os.environ.get("AUTH_DIR", "/data/auth"))
def pdf_path(pid): return os.path.join(auth_dir(), f"{pid}-esign.pdf")

def letter_lines(p, signer_name, ts, ip, username, agent=None):
    agent = agent or os.environ.get("APP_NAME") or "Fletcher Fortis"
    when = datetime.datetime.fromisoformat(ts)
    return [("h", "Authorized Agent Authorization"), ("s", "California Consumer Privacy Act (CCPA / CPRA)"), ("", ""),
        ("", f"I, {signer_name}, authorize {agent} and the family administrator who operates it to act as my"),
        ("", "authorized agent to submit requests to delete, to opt out of the sale or sharing of, and to know"),
        ("", "personal information that data brokers and people-search websites hold about me, and to"),
        ("", "receive and respond to related correspondence on my behalf."), ("", ""),
        ("", "This authorization covers requests under Cal. Civ. Code 1798.105, 1798.120 and 1798.135(e),"),
        ("", "11 CCR 7063, and comparable state privacy laws. I may revoke it at any time by contacting the"),
        ("", "family administrator. It does not authorize any financial transaction."), ("", ""),
        ("b", "Consumer"), ("", f"Name: {p['full_name']}"), ("", f"City / state: {p['city'] or ''}, {p['state'] or ''}"), ("", ""),
        ("b", "Electronic signature"), ("", f"Statement accepted: \"{STATEMENT}.\""), ("", f"Typed signature: /{signer_name}/   Drawn signature: below"),
        ("", f"Signed at: {when:%B %d, %Y %H:%M} (server time)"), ("", f"Signed from IP address: {ip}"),
        ("", f"{agent} account: @{username}"), *[("", "")] * 9,
        ("s", "Signed electronically under the U.S. E-SIGN Act (15 U.S.C. 7001) and California UETA (Civ. Code 1633.7)."),
        ("s", "No Social Security number, date of birth or government ID number is collected or included."),
        ("s", f"Record id: {hashlib.sha256(f'{p['id']}|{signer_name}|{ts}|{ip}|{username}'.encode()).hexdigest()[:16]}")]

def _esc(t): return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1", "replace").decode("latin-1")
def parse_strokes(raw):
    """Signature from the canvas: JSON [[[x,y],...],...] normalised 0..1. Returns cleaned strokes or [] if it isn't a real signature."""
    try: data = json.loads(raw or "[]")
    except Exception: return []
    out = []
    for st in data[:60] if isinstance(data, list) else []:
        pts = [(round(min(max(float(x), 0), 1), 4), round(min(max(float(y), 0), 1), 4)) for x, y in (st[:800] if isinstance(st, list) else []) if isinstance(x, (int, float)) and isinstance(y, (int, float))]
        if pts: out.append(pts)
    n = sum(len(s) for s in out); xs = [x for s in out for x, _ in s]
    return out if n >= 8 and xs and max(xs) - min(xs) > 0.08 else []

def render_pdf(lines, strokes=None):
    """Tiny dependency-free single-page PDF (Helvetica, US Letter) with the drawn signature as vector paths."""
    y = 740; ops = ["BT"]; sig_y = None
    for kind, t in lines:
        size, font, gap = {"h": (18, "F2", 26), "b": (11.5, "F2", 18), "s": (9, "F1", 14)}.get(kind, (11, "F1", 15))
        if t: ops.append(f"/{font} {size} Tf 1 0 0 1 64 {y} Tm ({_esc(t)}) Tj")
        if " account: @" in t: sig_y = y - 10
        y -= gap if t else 8
    ops.append("ET")
    if strokes and sig_y:
        W, H, X0 = 200, 52, 64
        ops.append("q 0.1 0.1 0.35 RG 1.6 w 1 J 1 j")
        for st in strokes:
            seg = [f"{X0 + x * W:.1f} {sig_y - y_ * H:.1f}" for x, y_ in st]
            ops.append(seg[0] + " m " + " ".join(s + " l" for s in seg[1:]) + (" S" if len(seg) > 1 else " 0.5 0 l S"))
        ops.append(f"0.6 0.6 0.6 RG 0.5 w {X0} {sig_y - H - 4} m {X0 + W} {sig_y - H - 4} l S Q")
    stream = "\n".join(ops).encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"]
    out = b"%PDF-1.4\n"; offs = []
    for i, o in enumerate(objs, 1): offs.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out); out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)

def write_pdf(p, signer_name, ts, ip, username, strokes=None):
    d = auth_dir(); os.makedirs(d, mode=0o700, exist_ok=True); path = pdf_path(p["id"])
    if os.path.dirname(os.path.realpath(path)) != d: raise ValueError("bad path")
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f: f.write(render_pdf(letter_lines(p, signer_name, ts, ip, username), strokes))
    os.replace(path + ".tmp", path); return path

def name_matches(typed, full_name):
    n = lambda s: " ".join((s or "").lower().replace(".", " ").split())
    return bool(typed.strip()) and n(typed) == n(full_name)
