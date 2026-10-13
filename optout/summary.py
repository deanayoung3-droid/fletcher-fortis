"""Weekly summary email (Monday 08:00 Pacific): one per family member, plus a family-wide one for the admin.
HTML (inline CSS, Geist with system-sans fallback) + plain text. Sending is DISABLED unless a provider is configured AND DRY_RUN is off;
the admin can always preview it in the browser (/admin/weekly-email)."""
import os, datetime, html
from db import conn
import remover

METRICS = [("sent", "Requests sent", "#5559e3"), ("approved", "Approved", "#6a76f0"), ("pending", "Waiting on brokers", "#a16207"),
           ("removed", "Removed", "#059669"), ("reappeared", "Reappeared", "#e11d48"), ("new", "New exposures", "#ea580c")]

def week_range(today=None):
    t = today or datetime.date.today(); end = t - datetime.timedelta(days=t.weekday())   # this Monday 00:00
    if end == t: start = end - datetime.timedelta(days=7)
    else: start, end = end, t + datetime.timedelta(days=1)
    return start, end

def stats(c, pids, start, end):
    s, e = start.isoformat(), end.isoformat(); q = ",".join("?" * len(pids)); w = (*pids, s, e)
    one = lambda sql: c.execute(sql, w).fetchone()["n"]
    return {"sent": one(f"SELECT COUNT(*) n FROM listings WHERE person_id IN ({q}) AND submitted_at>=? AND submitted_at<?"),  # nosec B608
            "approved": one(f"SELECT COUNT(*) n FROM events WHERE person_id IN ({q}) AND kind IN ('approve','approved','queued') AND ts>=? AND ts<?"),  # nosec B608
            "pending": c.execute(f"SELECT COUNT(*) n FROM listings WHERE person_id IN ({q}) AND status IN ('submitted','pending','approved')", pids).fetchone()["n"],  # nosec B608
            "removed": one(f"SELECT COUNT(*) n FROM listings WHERE person_id IN ({q}) AND confirmed_at>=? AND confirmed_at<?"),  # nosec B608
            "reappeared": c.execute(f"SELECT COUNT(*) n FROM listings WHERE person_id IN ({q}) AND status='reappeared'", pids).fetchone()["n"],  # nosec B608
            "new": one(f"SELECT COUNT(*) n FROM listings WHERE person_id IN ({q}) AND first_seen>=? AND first_seen<? AND status NOT IN ('not_me','checked_clear','manual_check')"),  # nosec B608
            "review": c.execute(f"SELECT COUNT(*) n FROM listings WHERE person_id IN ({q}) AND status='candidate'", pids).fetchone()["n"]}  # nosec B608

def build(c, people, family=False, today=None):
    """Returns (subject, html, text) for one person, or for the whole family (admin)."""
    start, end = week_range(today); pids = [p["id"] for p in people] or ["-"]; s = stats(c, pids, start, end)
    base = os.environ.get("PUBLIC_URL", "").rstrip("/"); who = "Your family" if family else people[0]["first"] or people[0]["full_name"]
    link = base + ("/" if family else f"/person/{people[0]['id']}")
    rng = f"{start:%b %-d} – {(end - datetime.timedelta(days=1)):%b %-d}"
    subject = f"Your week in privacy: {s['removed']} removed, {s['new']} new" + (" · family" if family else "")
    re_rows = c.execute(f"SELECT l.site, p.full_name FROM listings l JOIN people p ON p.id=l.person_id WHERE l.status='reappeared' AND l.person_id IN ({','.join('?'*len(pids))}) LIMIT 5", pids).fetchall()  # nosec B608
    per = [(p, stats(c, [p["id"]], start, end)) for p in people] if family else []
    did = []
    if s["sent"]: did.append(f"Sent {s['sent']} removal request{'s' if s['sent'] != 1 else ''} to data brokers")
    if s["removed"]: did.append(f"Confirmed {s['removed']} listing{'s' if s['removed'] != 1 else ''} removed")
    if s["new"]: did.append(f"Found {s['new']} new exposure{'s' if s['new'] != 1 else ''} on people-search sites")
    if not did: did.append("Re-checked every broker site. Nothing new needed your attention")
    APPN = os.environ.get('APP_NAME') or 'Fletcher Fortis'
    # ---- plain text ----
    t = [f"{APPN} · weekly privacy summary ({rng})", "", f"{who}: what we did this week", *[f"  - {x}" for x in did], ""]
    t += [f"  {label:<20} {s[k]}" for k, label, _ in METRICS]
    if s["review"]: t += ["", f"{s['review']} listing(s) are waiting for your review."]
    if re_rows: t += ["", "Reappeared (we'll send a new request once you approve):"] + [f"  - {remover.SITE.get(r['site'], {}).get('name', r['site'])} · {r['full_name']}" for r in re_rows]
    if per: t += ["", "By person:"] + [f"  {p['full_name']:<24} sent {x['sent']} · removed {x['removed']} · new {x['new']} · to review {x['review']}" for p, x in per]
    t += ["", f"Open your dashboard: {link}", "", "You get this every Monday. {APPN} never sells or shares your data."]
    text = "\n".join(t)
    # ---- HTML (table layout + inline CSS for email clients) ----
    E = html.escape; F = "font-family:Geist,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    logo = f'<img src="{E(base)}/static/email-logo.png" width="28" height="35" alt="{E(os.environ.get('APP_NAME') or 'Fletcher Fortis')}" style="display:block;border:0">'
    cells = "".join(f'<td class="m" width="33%" style="padding:6px"><div style="background:#f6f5f2;border-radius:14px;padding:14px 14px 12px"><div style="{F}font-size:28px;line-height:1;font-weight:500;color:{col};letter-spacing:-.03em">{s[k]}</div><div style="{F}font-size:12px;color:#6b6760;margin-top:6px">{label}</div></div></td>' + ("</tr><tr>" if i == 2 else "") for i, (k, label, col) in enumerate(METRICS))
    did_h = "".join(f'<tr><td style="padding:5px 0;{F}font-size:14px;color:#1c1b1a"><span style="color:#059669;font-weight:700">&#10003;</span>&nbsp; {E(x)}</td></tr>' for x in did)
    re_h = "".join(f'<tr><td style="padding:6px 0;border-top:1px solid #f1efe9;{F}font-size:13.5px;color:#1c1b1a">{E(remover.SITE.get(r["site"], {}).get("name", r["site"]))}<span style="color:#8a857c"> · {E(r["full_name"])}</span></td></tr>' for r in re_rows)
    per_h = "".join(f'<tr><td style="padding:8px 0;border-top:1px solid #f1efe9;{F}font-size:13.5px;color:#1c1b1a;font-weight:600">{E(p["full_name"])}</td><td align="right" style="padding:8px 0;border-top:1px solid #f1efe9;{F}font-size:12.5px;color:#6b6760">{x["sent"]} sent · {x["removed"]} removed · {x["new"]} new{" · <b style=color:#ea580c>" + str(x["review"]) + " to review</b>" if x["review"] else ""}</td></tr>' for p, x in per)
    sec = lambda title, rows: f'<tr><td style="padding:22px 28px 0"><div style="{F}font-size:13px;font-weight:600;color:#1c1b1a;margin-bottom:6px">{title}</div><table role="presentation" width="100%" cellspacing="0" cellpadding="0">{rows}</table></td></tr>' if rows else ""
    review = f'<tr><td style="padding:18px 28px 0"><div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;padding:12px 14px;{F}font-size:13.5px;color:#9a3412"><b>{s["review"]} listing{"s" if s["review"] != 1 else ""}</b> found and waiting for your review.</div></td></tr>' if s["review"] else ""
    h = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light only"><title>{E(subject)}</title>
<style>@media (max-width:520px){{.wrap{{width:100%!important}}.m{{display:block!important;width:100%!important}}.px{{padding-left:18px!important;padding-right:18px!important}}}}</style></head>
<body style="margin:0;padding:0;background:#ece9e2"><div style="display:none;max-height:0;overflow:hidden">{E(did[0])}.</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ece9e2"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" class="wrap" width="560" cellspacing="0" cellpadding="0" style="width:560px;max-width:560px;background:#ffffff;border-radius:22px;overflow:hidden">
<tr><td class="px" style="padding:24px 28px 0"><table role="presentation" cellspacing="0" cellpadding="0"><tr><td style="padding-right:10px">{logo}</td><td style="{F}font-size:17px;font-weight:600;letter-spacing:-.02em;color:#0a0a0a">{E(APPN)}</td></tr></table></td></tr>
<tr><td class="px" style="padding:22px 28px 0"><div style="{F}font-size:12.5px;color:#8a857c;text-transform:uppercase;letter-spacing:.06em">Weekly summary · {E(rng)}</div>
<div style="{F}font-size:28px;line-height:1.15;font-weight:500;letter-spacing:-.035em;color:#0a0a0a;margin-top:6px">{E(who)}: here's what we did this week</div></td></tr>
{sec("This week", did_h)}
<tr><td class="px" style="padding:16px 22px 0"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>{cells}</tr></table></td></tr>
{review}{sec("Reappeared · needs a new request", re_h)}{sec("By person", per_h)}
<tr><td class="px" style="padding:24px 28px 28px"><a href="{E(link)}" style="display:inline-block;background:#0a0a0a;color:#ffffff;text-decoration:none;{F}font-size:14px;font-weight:600;padding:12px 20px;border-radius:999px">Open dashboard &rarr;</a></td></tr>
</table><div style="{F}font-size:11.5px;color:#8a857c;padding:16px 8px;max-width:520px">You get this every Monday. {APPN} never sells or shares your data.<br>Sent from {E(remover.FROM_ADDRESS)}.</div>
</td></tr></table></body></html>"""
    return subject, h, text

def enabled():
    """Weekly emails go out only when a provider is configured AND DRY_RUN is off (and SUMMARY_ENABLED isn't false)."""
    return remover.provider_ready() and not remover.DRY and os.environ.get("SUMMARY_ENABLED", "true") == "true"

def recipients(c):
    """[(people, family, to_address, sender_person)] — each member gets their own; the admin also gets the family-wide one."""
    people = c.execute("SELECT * FROM people WHERE enabled=1 ORDER BY rowid").fetchall(); out = []
    admin = c.execute("SELECT person_id FROM users WHERE role='admin' LIMIT 1").fetchone()
    for p in people:
        if p["email"]: out.append(([p], False, p["email"], p))
    a = next((p for p in people if admin and p["id"] == admin["person_id"]), None)
    to = os.environ.get("SUMMARY_TO") or (a["email"] if a else "")
    if people and to: out.append((people, True, to, a or people[0]))
    return out

def send_all():
    if not enabled(): return 0
    c = conn(); n = 0
    for ppl, fam, to, sender in recipients(c):
        subj, h, t = build(c, ppl, fam); remover.send_email(c, sender, to, subj, t, html=h); n += 1
    return n
