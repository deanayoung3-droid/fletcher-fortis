"""Tailored broker removal requests: law picked by the consumer's state, authorized-agent wording + e-signed authorization,
only the data points that broker actually showed (PII minimisation), deletion + opt-out of sale/sharing + suppression + affiliates,
statutory deadline, one-line reply path. Plain text + HTML. Follow-up (reminder) and escalation variants."""
import datetime, html, json
import profiles
# state -> (short name, full citation, deadline days, regulator for escalation)
LAWS = {
 "CA": ("CCPA/CPRA", "the California Consumer Privacy Act as amended by the CPRA (Cal. Civ. Code §§ 1798.105, 1798.120, 1798.130, 1798.135) and, for registered data brokers, the Delete Act (Cal. Civ. Code § 1798.99.80 et seq.)", 45,
        "the California Privacy Protection Agency (cppa.ca.gov/webapplications/complaint) and the California Attorney General (oag.ca.gov/privacy)"),
 "VA": ("VCDPA", "the Virginia Consumer Data Protection Act (Va. Code § 59.1-577)", 45, "the Virginia Attorney General"),
 "CO": ("CPA", "the Colorado Privacy Act (C.R.S. § 6-1-1306)", 45, "the Colorado Attorney General"),
 "CT": ("CTDPA", "the Connecticut Data Privacy Act (Conn. Gen. Stat. § 42-518)", 45, "the Connecticut Attorney General"),
 "UT": ("UCPA", "the Utah Consumer Privacy Act (Utah Code § 13-61-201)", 45, "the Utah Division of Consumer Protection"),
 "TX": ("TDPSA", "the Texas Data Privacy and Security Act (Tex. Bus. & Com. Code § 541.051)", 45, "the Texas Attorney General"),
 "OR": ("OCPA", "the Oregon Consumer Privacy Act (ORS 646A.574)", 45, "the Oregon Attorney General"),
 "MT": ("MCDPA", "the Montana Consumer Data Privacy Act (Mont. Code § 30-14-2808)", 45, "the Montana Attorney General"),
 "IA": ("ICDPA", "the Iowa Consumer Data Protection Act (Iowa Code § 715D.3)", 90, "the Iowa Attorney General"),
 "DE": ("DPDPA", "the Delaware Personal Data Privacy Act (6 Del. C. § 12D-104)", 45, "the Delaware Department of Justice"),
 "NH": ("NHPA", "New Hampshire RSA 507-H:4", 45, "the New Hampshire Attorney General"),
 "NJ": ("NJDPA", "the New Jersey Data Privacy Act (N.J.S.A. 56:8-166.10)", 45, "the New Jersey Division of Consumer Affairs"),
 "TN": ("TIPA", "the Tennessee Information Protection Act (Tenn. Code § 47-18-3304)", 45, "the Tennessee Attorney General"),
 "MN": ("MCDPA", "the Minnesota Consumer Data Privacy Act (Minn. Stat. § 325M.14)", 45, "the Minnesota Attorney General"),
 "MD": ("MODPA", "the Maryland Online Data Privacy Act (Md. Com. Law § 14-4605)", 45, "the Maryland Attorney General"),
 "IN": ("ICDPA", "the Indiana Consumer Data Protection Act (Ind. Code § 24-15-3)", 45, "the Indiana Attorney General"),
 "KY": ("KCDPA", "the Kentucky Consumer Data Protection Act (KRS 367.3615)", 45, "the Kentucky Attorney General"),
 "NE": ("NDPA", "the Nebraska Data Privacy Act (Neb. Rev. Stat. § 87-1107)", 45, "the Nebraska Attorney General"),
 "RI": ("RIDTPPA", "the Rhode Island Data Transparency and Privacy Protection Act (R.I. Gen. Laws § 6-48.1-5)", 45, "the Rhode Island Attorney General"),
 "VT": ("Vermont data broker law", "Vermont's data broker statute (9 V.S.A. § 2430 et seq.)", 45, "the Vermont Attorney General"),
}
GENERIC = ("privacy request", "applicable state consumer-privacy laws and your own published opt-out policy", 45, "the Federal Trade Commission (reportfraud.ftc.gov) and my state Attorney General")
NETWORKS = {"peopleconnect": "PeopleConnect brands (Intelius, TruthFinder, Instant Checkmate, US Search, ZabaSearch and affiliates)",
            "beenverified": "BeenVerified brands (PeopleLooker, NeighborWho, NumberGuru, Ownerly and affiliates)",
            "peoplefinders": "PeopleFinders / Confi-Chek brands (PeopleFinders, PublicRecordsNow, Archives and affiliates)",
            "whitepages": "Whitepages brands (Whitepages, 411.com and affiliates)"}

def law(state): return LAWS.get((state or "").upper(), GENERIC)

def seen_points(listings):
    """Only what the broker itself displayed: listing URLs + the snippet's visible data points."""
    urls, ages = [], set()
    for l in listings:
        if l["url"]: urls.append(l["url"])
        try: f = json.loads(l["fields"] or "{}")
        except (TypeError, ValueError): f = {}
        if f.get("age"): ages.add(str(f["age"]))
    return urls[:10], sorted(ages)

def build(p, prof, site, listings, *, agent=True, reply_to="", stage=0, sent_at=None, extra_people=()):
    """Returns (subject, text, html). stage 0 = initial, 1 = reminder, 2 = escalation."""
    short, cite, days, reg = law(p["state"]); broker = site.get("name") or site.get("id")
    legal = next((n for n in prof["names"] if n["kind"] == "legal"), prof["names"][0] if prof["names"] else {"first": p["first"], "last": p["last"]})
    names = list(dict.fromkeys([profiles.full(legal)] + [profiles.full(n) for n in prof["names"] if n is not legal]))[:4]
    city = f"{p['city']}, {p['state']}" if p["city"] else (p["state"] or "")
    urls, ages = seen_points(listings)
    net = NETWORKS.get(site.get("parent") or "")
    today = datetime.date.today(); sent = datetime.date.fromisoformat(sent_at[:10]) if sent_at else today
    due = sent + datetime.timedelta(days=days)
    subj = {0: f"{short} deletion and opt-out request: {names[0]}", 1: f"Reminder: {short} deletion request for {names[0]} (sent {sent:%b %-d})",
            2: f"Final notice before complaint: {short} request for {names[0]} overdue since {due:%b %-d}"}[stage]
    who = [names[0]] + [x for x in extra_people]
    L = []
    if stage == 1: L += [f"I'm following up on the request below, sent on {sent:%B %-d, %Y}. Please confirm it has been completed. The deadline is {due:%B %-d, %Y}.", ""]
    if stage == 2: L += [f"The statutory deadline for the request below ({due:%B %-d, %Y}) has passed without confirmation. Please complete it and confirm in writing within 10 days, or I will file a complaint with {reg}.", ""]
    L += [f"To {broker} privacy team,", ""]
    if agent: L += [f"I am the authorized agent of {', '.join(who)}. Their signed authorization is attached. Under {cite}, please:"]
    else: L += [f"Under {cite}, please:"]
    L += ["  1. Delete all personal information you hold about " + ("them" if agent else "me") + ", including the records listed below;",
          "  2. Opt " + ("them" if agent else "me") + " out of any sale or sharing of personal information, and of targeted advertising and profiling;",
          "  3. Suppress the records so they are not re-listed or re-collected from public sources;",
          "  4. Pass this deletion request to the affiliates, service providers and data partners you sold or shared the data with" + (f", including {net}." if net else ".")]
    L += ["", "Records to remove:"] + [f"  - {u}" for u in urls] if urls else ["", "Records to remove: every profile matching the identifiers below."]
    L += ["", "Identifiers (only what your site displays, to locate the records):", f"  - Name: {names[0]}"] + [f"  - Also listed as: {n}" for n in names[1:]] + ([f"  - Location: {city}"] if city else []) + ([f"  - Age shown: {', '.join(ages)}"] if ages else [])
    L += ["", f"Please do not require more verification than the law permits{' (an authorized agent may submit this request; 11 CCR § 7063)' if (p['state'] or '').upper() == 'CA' and agent else ''}. Do not ask for a Social Security number, date of birth or ID: none will be provided, and they are not needed to delete a public listing.",
          f"Please confirm completion in writing within {days} days (by {due:%B %-d, %Y}). Simply reply to this email{f' ({reply_to})' if reply_to else ''}; no portal or phone call is needed.", "", "Thank you,", ("Authorized agent for " + ", ".join(who)) if agent else names[0]]
    text = "\n".join(L)
    E = html.escape; F = "font-family:Geist,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.55;color:#1c1b1a"
    def para(t):
        if t.startswith("  - ") or t.startswith("  1") or t[:3].strip().rstrip(".").isdigit(): return f'<li style="margin:2px 0">{E(t.strip()[3:] if t.strip()[0].isdigit() else t.strip()[2:])}</li>'
        return f'<p style="margin:0 0 10px">{E(t)}</p>' if t else ""
    body = "".join(para(t) for t in L).replace("</p><li", "</p><ul style='margin:0 0 10px;padding-left:20px'><li").replace("</li><p", "</li></ul><p")
    h = f'<!doctype html><html><body style="margin:0;padding:20px;background:#ffffff"><div style="{F};max-width:620px">{body}</div></body></html>'
    return subj, text, h
