"""Transactional mail that isn't a broker request (onboarding invites, test sends). Same gates as everything else:
written to the outbox first; really sent only when a provider is configured AND DRY_RUN is off."""
import os, html
import remover, config
from db import conn, now
from crypto import enc

def send(to, subject, text, html_body, kind="mail", person_id=None):
    c = conn()
    cur = c.execute("INSERT INTO outbox(ts,person_id,to_addr,subject,body,state,channel,html) VALUES(?,?,?,?,?,?,?,?)", (now(), person_id, to, enc(subject), enc(text), "queued", kind, enc(html_body)))
    ref = str(cur.lastrowid)
    if remover.DRY: state = "dry run: recorded in the outbox, not sent"
    elif not remover.provider_ready(): state = "not sent: email isn't set up yet (copy the link instead)"
    else: state = remover._resend(f"{remover.FROM_NAME} <{remover.FROM_ADDRESS}>", to, remover.FROM_ADDRESS, subject, text, None, ref, html_body)
    c.execute("UPDATE outbox SET state=? WHERE id=?", ("sent" if state.startswith("sent") else ("dry_run" if remover.DRY else state), ref)); c.commit()
    return "sent" if state.startswith("sent") else state

def onboarding(first, admin, link):
    """Welcome email with a one-time button link (expires in 72 h). Returns (subject, text, html)."""
    app = config.get("APP_NAME") or "Fletcher Fortis"; color = config.get("BRAND_COLOR") or "#5559e3"; E = html.escape
    base = (config.get("PUBLIC_URL") or "").rstrip("/")
    subject = f"{first}, your {app} account is ready"
    steps = [("Set a password", "Takes ten seconds."), ("Check your details", "Names, cities, phones. Relatives are already filled in."),
             ("Sign the authorization", "Draw your signature so we can file removals for you."), ("That's it", "We scan 470+ people-search sites and remove you automatically.")]
    text = "\n".join([f"Hi {first},", "", f"@{admin} set up {app} for you. It finds your personal information on people-search and data-broker sites and gets it removed, automatically.",
        "", "Get started (one-time link, expires in 72 hours):", link, "", *[f"{i}. {a}: {b}" for i, (a, b) in enumerate(steps, 1)],
        "", "No Social Security number, date of birth or ID is ever asked for.", "If you weren't expecting this, ignore it and the link will expire.", "", f"{app}"])
    F = "font-family:Geist,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    logo = f'<img src="{E(base)}/static/email-logo.png" width="28" height="35" alt="" style="display:block;border:0">' if base else ""
    rows = "".join(f'<tr><td width="34" valign="top" style="padding:9px 0"><div style="width:24px;height:24px;border-radius:999px;background:#f1f0fd;color:{E(color)};{F}font-size:12px;font-weight:700;line-height:24px;text-align:center">{i}</div></td><td style="padding:9px 0;{F}font-size:14px;color:#1c1b1a"><b>{E(a)}</b><br><span style="color:#6b6760;font-size:13px">{E(b)}</span></td></tr>' for i, (a, b) in enumerate(steps, 1))
    h = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light only"><title>{E(subject)}</title>
<style>@media (max-width:520px){{.wrap{{width:100%!important}}.px{{padding-left:20px!important;padding-right:20px!important}}}}</style></head>
<body style="margin:0;padding:0;background:#ece9e2"><div style="display:none;max-height:0;overflow:hidden">Set a password, check your details, sign. We handle the rest.</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ece9e2"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" class="wrap" width="540" cellspacing="0" cellpadding="0" style="width:540px;max-width:540px;background:#ffffff;border-radius:22px;overflow:hidden">
<tr><td class="px" style="padding:26px 32px 0"><table role="presentation" cellspacing="0" cellpadding="0"><tr><td style="padding-right:10px">{logo}</td><td style="{F}font-size:17px;font-weight:600;letter-spacing:-.02em;color:#0a0a0a">{E(app)}</td></tr></table></td></tr>
<tr><td class="px" style="padding:26px 32px 0"><div style="{F}font-size:30px;line-height:1.12;font-weight:500;letter-spacing:-.04em;color:#0a0a0a">Hi {E(first)}, let's get you off<br>people-search sites.</div>
<div style="{F}font-size:14.5px;line-height:1.55;color:#57534e;margin-top:12px">@{E(admin)} set up {E(app)} for you. It finds your personal information on data-broker sites and gets it removed, automatically, then keeps watching.</div></td></tr>
<tr><td class="px" style="padding:24px 32px 0"><a href="{E(link)}" style="display:inline-block;background:{E(color)};color:#ffffff;text-decoration:none;{F}font-size:15px;font-weight:600;padding:14px 24px;border-radius:999px">Get started &rarr;</a>
<div style="{F}font-size:12px;color:#8a857c;margin-top:10px">One-time link · expires in 72 hours</div></td></tr>
<tr><td class="px" style="padding:22px 32px 0"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-top:1px solid #f1efe9">{rows}</table></td></tr>
<tr><td class="px" style="padding:18px 32px 28px;{F}font-size:12px;line-height:1.5;color:#8a857c">No Social Security number, date of birth or ID is ever asked for. If you weren't expecting this, ignore it and the link will expire.<br><br>Button not working? Paste this link: <span style="color:#57534e;word-break:break-all">{E(link)}</span></td></tr>
</table></td></tr></table></body></html>"""
    return subject, text, h

def send_test(to):
    """Setup wizard 'Send test email': one plain message to the admin's own address, straight through Resend, so the key and
    from-address can be checked before going live. It is the only send that ignores DRY_RUN, and it never reaches a broker."""
    if not os.environ.get("RESEND_API_KEY"): return "add a Resend API key first"
    app = config.get("APP_NAME") or "Fletcher Fortis"
    subject = f"{app}: test email"
    text = f"This is a test from {app}. If you can read it, sending works.\n\nDry run is still on: no removal requests go out until you switch it off in Heartbeat."
    body = f'<div style="font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;line-height:1.55;color:#1c1917"><p>This is a test from <b>{html.escape(app)}</b>. If you can read it, sending works.</p><p style="color:#78716c">Dry run is still on: no removal requests go out until you switch it off in Heartbeat.</p></div>'
    st = remover._resend(f"{remover.FROM_NAME} <{remover.FROM_ADDRESS}>", to, remover.FROM_ADDRESS, subject, text, None, "setup-test-" + now(), body)
    return "sent" if st.startswith("sent") else st
