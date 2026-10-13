"""Person profiles: multiple names, addresses, phones, emails, relatives, optional birth year / age range.
Stored as one encrypted JSON column (people.profile); the legacy columns are kept in sync (also encrypted).
Never stores SSNs, full dates of birth, driver's license, passport or other ID numbers: validate() rejects them."""
import yaml, glob, os, re, json, datetime
from crypto import enc
NAME_KINDS = ("legal", "variant", "nickname", "maiden")
MAX = {"names": 12, "addresses": 12, "phones": 8, "emails": 8, "relatives": 20}
SSN = re.compile(r"\b\d{3}[- .]?\d{2}[- .]?\d{4}\b")
DATE = re.compile(r"\b(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}[/.-]\d{1,2}[/.-]\d{1,2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4})\b", re.I)
IDWORDS = re.compile(r"\b(?:ssn|social security|driver'?s? ?licen[cs]e|\bDL ?#|passport|tax ?id|\bitin\b|\bein\b|date of birth|\bdob\b|license (?:no|number|#)|id (?:no|number|#))", re.I)
IDNUM = re.compile(r"\b[A-Z]\d{7}\b")             # California driver's license / ID format
NAME_OK = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ' .-]{1,40}$")
STREET_OK = re.compile(r"^[A-Za-z0-9À-ÿ#' .,/-]{1,80}$")
EMAIL_OK = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split())

def forbidden(v):
    """Return a reason if a free-text value looks like a government ID number or a full date of birth."""
    v = str(v or "")
    if IDWORDS.search(v): return "looks like an ID/SSN/date-of-birth label"
    if SSN.search(v): return "looks like a Social Security number"
    if DATE.search(v): return "looks like a full date (birth dates are never stored; use birth year only)"
    if IDNUM.search(v): return "looks like a driver's license / ID number"
    return None

def norm_phone(v):
    d = re.sub(r"\D", "", v or "")
    if len(d) == 11 and d.startswith("1"): d = d[1:]
    return d if len(d) == 10 and d[0] not in "01" else None

def fmt_phone(d): return f"({d[:3]}) {d[3:6]}-{d[6:]}" if d and len(d) == 10 else (d or "")

def from_legacy(r):
    f = (r.get("first") or "").split(); first = f[0] if f else ""; middle = " ".join(f[1:])
    names = [{"kind": "legal", "first": first, "middle": middle, "last": r.get("last") or ""}] if first else []
    addrs = [{"street": "", "city": r.get("city") or "", "state": r.get("state") or "CA", "current": True}] if r.get("city") else []
    addrs += [{"street": "", "city": c.strip(), "state": r.get("state") or "CA", "current": False} for c in (r.get("prior_cities") or "").split(",") if c.strip()]
    return {"names": names, "addresses": addrs, "phones": [x for x in [norm_phone(r.get("phone"))] if x], "emails": [r["email"]] if r.get("email") else [],
            "relatives": [], "birth_year": None, "age_min": r.get("age_min"), "age_max": r.get("age_max")}

def get(r):
    try: p = json.loads(r["profile"]) if r.get("profile") else None
    except (ValueError, TypeError): p = None
    p = p or from_legacy(r)
    for k in ("names", "addresses", "phones", "emails", "relatives"): p.setdefault(k, [])
    return p

def full(n): return " ".join(x for x in (n.get("first"), n.get("middle"), n.get("last")) if x)

def legacy_cols(p):
    legal = next((n for n in p["names"] if n["kind"] == "legal"), p["names"][0])
    cur = next((a for a in p["addresses"] if a.get("current")), p["addresses"][0] if p["addresses"] else {"city": "", "state": "CA"})
    lo, hi = age_range(p)
    return {"full_name": full(legal), "first": " ".join(x for x in (legal["first"], legal.get("middle")) if x), "last": legal["last"],
            "city": cur["city"], "state": cur["state"], "prior_cities": ",".join(dict.fromkeys(a["city"] for a in p["addresses"] if a is not cur and a["city"] and a["city"] != cur["city"])),
            "email": (p["emails"] or [""])[0], "phone": fmt_phone((p["phones"] or [""])[0]), "age_min": lo, "age_max": hi}

def age_range(p, today=None):
    y = (today or datetime.date.today()).year
    if p.get("birth_year"): return y - int(p["birth_year"]) - 1, y - int(p["birth_year"])
    return p.get("age_min"), p.get("age_max")

def name_pairs(p):
    """(first, last) search pairs from every name entry, legal first, de-duplicated."""
    out = []
    for n in sorted(p["names"], key=lambda n: NAME_KINDS.index(n["kind"]) if n["kind"] in NAME_KINDS else 9):
        k = (n["first"].split()[0] if n["first"] else "", n["last"])
        if all(k) and k not in out: out.append(k)
    return out

def places(p):
    out = []
    for a in sorted(p["addresses"], key=lambda a: not a.get("current")):
        k = (a["city"], a["state"])
        if a["city"] and k not in out: out.append(k)
    return out

def identifiers(p):
    """Everything a broker may use to find the record (for CCPA letters and form fill)."""
    return {"names": [full(n) for n in p["names"]], "addresses": [", ".join(x for x in (a.get("street"), a["city"], a["state"]) if x) for a in p["addresses"]],
            "phones": [fmt_phone(x) for x in p["phones"]], "emails": list(p["emails"]), "relatives": list(p["relatives"])}

def validate(form):
    """form: dict of lists (from a multipart/urlencoded form). Returns (profile, errors)."""
    err = []; g = lambda k: [str(x).strip() for x in form.get(k, [])]
    for k, vals in form.items():
        for v in vals:
            why = forbidden(v) if not k.startswith("phone") else (IDWORDS.search(str(v)) and "looks like an ID label")
            if why: err.append(f"Rejected a value in “{k.split('_')[0]}”: {why}. Never enter SSNs, full birth dates or ID numbers.")
    names = []
    for kind, f, m, l in zip(g("name_kind"), g("name_first"), g("name_middle"), g("name_last")):
        if not (f or l): continue
        if kind not in NAME_KINDS: kind = "variant"
        if not (f and l): err.append("Each name needs a first and a last name."); continue
        if not all(NAME_OK.match(x) for x in (f, l)) or (m and not NAME_OK.match(m)): err.append(f"Name “{f} {l}”: letters, spaces, apostrophes and hyphens only."); continue
        names.append({"kind": kind, "first": f, "middle": m, "last": l})
    if not names: err.append("Add at least one name.")
    if names and not any(n["kind"] == "legal" for n in names): names[0]["kind"] = "legal"
    cur_idx = (g("addr_current") or ["0"])[0]
    addrs = []
    for i, (st, city, state) in enumerate(zip(g("addr_street"), g("addr_city"), g("addr_state"))):
        if not (st or city): continue
        state = state.upper() or "CA"
        if not city: err.append("Each address needs a city."); continue
        if not NAME_OK.match(city): err.append(f"City “{city}”: letters only."); continue
        if st and not STREET_OK.match(st): err.append(f"Street “{st}” has unsupported characters."); continue
        if state not in STATES: err.append(f"State “{state}” is not a US state code."); continue
        addrs.append({"street": st, "city": city, "state": state, "current": str(i) == cur_idx})
    if not addrs: err.append("Add at least one city (current address).")
    elif not any(a["current"] for a in addrs): addrs[0]["current"] = True
    phones = []
    for v in g("phone"):
        if not v: continue
        d = norm_phone(v)
        if not d: err.append(f"Phone “{v}” must be a 10-digit US number."); continue
        if d not in phones: phones.append(d)
    emails = []
    for v in g("email"):
        if not v: continue
        if not EMAIL_OK.match(v): err.append(f"Email “{v}” is not valid."); continue
        if v.lower() not in emails: emails.append(v.lower())
    rel = []
    for v in g("relative"):
        if not v: continue
        if not NAME_OK.match(v) or len(v.split()) < 2: err.append(f"Relative “{v}”: give a first and last name (letters only)."); continue
        if v not in rel: rel.append(v)
    by, lo, hi = (g("birth_year") or [""])[0], (g("age_min") or [""])[0], (g("age_max") or [""])[0]
    y = datetime.date.today().year; prof = {"names": names, "addresses": addrs, "phones": phones, "emails": emails, "relatives": rel, "birth_year": None, "age_min": None, "age_max": None}
    try:
        if by:
            if not (by.isdigit() and 1900 <= int(by) <= y): raise ValueError("Birth year must be a 4-digit year (e.g. 2011).")
            prof["birth_year"] = int(by)
        elif lo or hi:
            if not (lo.isdigit() and hi.isdigit()) or not (0 < int(lo) <= int(hi) <= 120): raise ValueError("Age range must be two numbers, min ≤ max.")
            prof["age_min"], prof["age_max"] = int(lo), int(hi)
    except ValueError as e: err.append(str(e))
    for k, n in MAX.items():
        if len(prof[k]) > n: err.append(f"At most {n} {k}.")
    return prof, err

def save(c, pid, prof):
    cols = legacy_cols(prof)
    c.execute("UPDATE people SET full_name=?,first=?,last=?,city=?,state=?,prior_cities=?,email=?,phone=?,age_min=?,age_max=?,profile=? WHERE id=?",
              (enc(cols["full_name"]), enc(cols["first"]), enc(cols["last"]), enc(cols["city"]), cols["state"], enc(cols["prior_cities"]), enc(cols["email"]),
               enc(cols["phone"]), cols["age_min"], cols["age_max"], enc(json.dumps(prof)), pid))
    c.commit()

ALLOWED = {"id","full_name","first","last","city","state","prior_cities","email","phone","age_min","age_max","enabled","needs_auth","notes"}
def seed(c, d=os.environ.get("PROFILES_DIR","/config/profiles")):
    for f in sorted(glob.glob(f"{d}/*.yaml")):
        y = yaml.safe_load(open(f)); p = {k: v for k, v in y.items() if k in ALLOWED}
        p["prior_cities"] = ",".join(p.get("prior_cities") or [])
        if c.execute("SELECT 1 FROM people WHERE id=?", (p["id"],)).fetchone(): continue
        prof = from_legacy(p)
        for k in ("relatives",): prof[k] = list(y.get(k) or [])
        for k, v in p.items():
            if k in ("full_name","first","last","city","prior_cities","email","phone","notes"): p[k] = enc(v)
        p["profile"] = enc(json.dumps(prof))
        cols = ",".join(p); c.execute(f"INSERT INTO people({cols}) VALUES({','.join('?'*len(p))})", list(p.values()))  # nosec B608
    c.commit()

def new_person(c, pid, prof, needs_auth):
    c.execute("INSERT INTO people(id,enabled,needs_auth,state) VALUES(?,1,?,?)", (pid, int(needs_auth), "CA")); save(c, pid, prof)
