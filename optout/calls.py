"""Optional Twilio screening number (forward-to-cell). Requires a Twilio number (~$1.15/mo + per-minute). Disabled unless TWILIO_ENABLED=true."""
import os, re
import hmac, hashlib, base64
from fastapi import APIRouter, Form, Response, Request, Depends, HTTPException
from db import conn, now
from crypto import enc
async def twilio_signed(r: Request):
    """Validate X-Twilio-Signature (HMAC-SHA1 of the public URL + sorted POST params, keyed with TWILIO_AUTH_TOKEN)."""
    tok = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not tok: raise HTTPException(403, "TWILIO_AUTH_TOKEN not set")
    form = await r.form(); url = os.environ.get("PUBLIC_URL", "").rstrip("/") + r.url.path
    data = url + "".join(k + form[k] for k in sorted(form.keys()))
    want = base64.b64encode(hmac.new(tok.encode(), data.encode(), hashlib.sha1).digest()).decode()
    if not hmac.compare_digest(want, r.headers.get("x-twilio-signature", "")): raise HTTPException(403)
router = APIRouter(dependencies=[Depends(twilio_signed)])
ALLOW = {re.sub(r"\D", "", x)[-10:] for x in os.environ.get("CALL_ALLOWLIST", "").split(",") if x.strip()}
FORWARD = os.environ.get("CALL_FORWARD_TO", "")
def twiml(x): return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{x}</Response>', media_type="application/xml")

@router.post("/twilio/voice")
async def voice(From: str = Form(""), CallerName: str = Form(""), StirVerstat: str = Form("")):
    num = re.sub(r"\D", "", From)[-10:]; c = conn()
    if num in ALLOW:
        v = "allow"; out = f"<Dial>{FORWARD}</Dial>"
    elif StirVerstat.endswith("C") or "SPAM" in CallerName.upper():
        v = "block"; out = "<Reject reason='rejected'/>"
    else:  # unknown: human-check gate (robocallers don't press keys)
        v = "screen"; out = f"<Gather numDigits='1' action='/twilio/screened' timeout='6'><Say>To reach the family, press 1.</Say></Gather><Hangup/>"
    c.execute("INSERT INTO calls(ts,from_num,verdict,detail) VALUES(?,?,?,?)", (now(), enc(From), v, enc(f"{CallerName} {StirVerstat}"))); c.commit()
    return twiml(out)

@router.post("/twilio/screened")
async def screened(Digits: str = Form(""), From: str = Form("")):
    conn().execute("INSERT INTO calls(ts,from_num,verdict,detail) VALUES(?,?,?,?)", (now(), enc(From), "passed" if Digits == "1" else "failed", "")).connection.commit()
    return twiml(f"<Dial>{FORWARD}</Dial>" if Digits == "1" else "<Hangup/>")
