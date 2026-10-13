"""Scrub PII and secrets from every log line (app + uvicorn): emails, phone numbers, SSN-like numbers, tokens in invite URLs, bearer keys."""
import logging, re, sys, threading, traceback
PATTERNS = [(re.compile(r"(https?://[^/\s\"']+)/[^\s\"']*"), r"\1/<path>"),  # broker URLs carry names in the path
            (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
            (re.compile(r"\(?\b\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b"), "<phone>"),
            (re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b"), "<number>"),
            (re.compile(r"/invite/[\w-]+"), "/invite/<token>"),
            (re.compile(r"/(person|family)/[^/\s\"?]+"), r"/\1/<id>"),
            (re.compile(r"(re_|sk_|Bearer\s+)[\w-]{8,}"), r"\1<redacted>"),
            (re.compile(r"(password|secret|token|key)=([^&\s]+)", re.I), r"\1=<redacted>")]
def scrub(s):
    for rx, rep in PATTERNS: s = rx.sub(rep, s)
    return s
class Scrub(logging.Filter):
    def filter(self, rec):
        try:
            if rec.exc_info:  # tracebacks (e.g. Playwright errors) can quote URLs and form values
                rec.msg = str(rec.msg) + "\n" + "".join(traceback.format_exception(*rec.exc_info)); rec.exc_info = None; rec.exc_text = None
            if isinstance(rec.args, tuple) and rec.args: rec.args = tuple(scrub(a) if isinstance(a, str) else a for a in rec.args); rec.msg = scrub(str(rec.msg))
            else: rec.msg = scrub(rec.getMessage()); rec.args = ()
        except Exception: pass
        return True
def install():
    f = Scrub()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "ff"):
        lg = logging.getLogger(name); lg.addFilter(f)
        for h in lg.handlers: h.addFilter(f)
    logging.getLogger("ff").setLevel(logging.INFO)
    lg = logging.getLogger("ff")
    sys.excepthook = lambda t, v, tb: lg.error("uncaught %s: %s", t.__name__, scrub(str(v)))
    threading.excepthook = lambda a: lg.error("thread crashed %s: %s", a.exc_type.__name__, scrub(str(a.exc_value)))
