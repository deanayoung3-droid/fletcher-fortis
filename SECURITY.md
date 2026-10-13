# Fletcher Fortis security notes

What protects the family's data, how to operate it safely, and what is still open. Reviewed 12 Oct 2026.

## Accounts and sessions
- **App login replaces HTTP Basic.** Server-side sessions: a random 256-bit token is set in the `ff_session` cookie
  (`HttpOnly`, `Secure` when `COOKIE_SECURE=true` (default), `SameSite=Lax`, 7 days). Only its SHA-256 is stored, so a stolen database does not contain usable sessions.
- **Passwords:** bcrypt (cost 12), at least 12 characters, must not contain the username. Unknown usernames still pay the bcrypt cost (no user enumeration by timing).
- **Rate limiting:** 5 failed logins per username or 20 per client IP within 15 minutes means a 15-minute lockout (HTTP 429). Behind Cloudflare the client IP comes from `CF-Connecting-IP` (`TRUST_CF_HEADER=true`).
- **Onboarding:** the admin either creates a **single-use invite link** (72 h; only its hash is stored) or sets an **initial password** that must be changed at first sign-in. Changing a password ends the user's other sessions. **Logout** deletes the session server-side.
- **First admin:** created once from `ADMIN_USERNAME`/`ADMIN_PASSWORD`. After that the env value is ignored, so change the password in the app.

## Authorization
- Roles: `admin` (Alex) sees and manages everyone. A `member` sees and edits **only their own** profile, listings, screenshots, proof pack and reminders.
  Anything else returns 404 (others' pages) or 403 (admin pages). Covered by `test/test_app.py::test_member_sees_only_own`.
- Members can approve, mark "Not me" and upload their own signed authorization. **Only the admin can enable sending** (`send_enabled`, per person), toggle monitoring or bulk CCPA, and manage accounts.
- Nothing is sent while `DRY_RUN=true`, regardless of these settings.

## CSRF, XSS, clickjacking
- Every state-changing route requires the per-session CSRF token (hidden `csrf` field or `X-CSRF-Token` header). POSTs with a foreign `Origin`/`Referer` are rejected. Login and invite forms are covered by the Origin check and `SameSite`.
- Jinja2 autoescaping everywhere. No inline scripts or `on*=` handlers (tested). All JavaScript is in `/static/app.js` and `/static/theme.js`.
- Headers on every response:
  - `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'`
  - `Strict-Transport-Security: max-age=63072000; includeSubDomains`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` (nothing leaks to broker sites; `no-referrer` would break the Origin check), `Permissions-Policy`, `Cross-Origin-Opener-Policy`/`Cross-Origin-Resource-Policy: same-origin`, `Cache-Control: no-store`
  - `style-src` keeps `'unsafe-inline'` only for `style="--i:…"` animation attributes. No user data is ever placed in a style attribute.

## Personal data at rest
- **Field-level encryption** (AES-256-SIV, `cryptography`) of every PII column:
  - people: names, cities, emails, phones, profile JSON
  - listings: URLs and extracted fields
  - events: details
  - outbox: subjects and bodies
  - inbound mail
  - call logs
  
  SIV is deterministic so `url = ?` lookups still work. The only leak is whether two values are equal.
- **Key:** `FF_DATA_KEY` (env) or a Docker/Portainer secret file at `FF_DATA_KEY_FILE` (default `/run/secrets/ff_data_key`). The stack refuses to start without it.
  If the app runs without one (dev), it generates `/data/secrets/ff_data_key` (0600) and shows a warning banner to the admin.
  **Back up the key separately from the volume.** Without it the data cannot be recovered.
- Existing plaintext rows are encrypted automatically on first start.
- **Test:** the raw DB and WAL contain no names, phones, emails or cities (`test_raw_db_has_no_plaintext_pii`, and the e2e check).
- Files: the SQLite DB is 0600, authorization PDFs 0600 in a 0700 directory, and the temporary Eraser config (it holds an SMTP key) is written 0600 and deleted right after the run.
- Screenshots are not encrypted (they are images of public listings). They are only served to an authorized user.
- **Never stored, by design:** SSN, full date of birth, driver's license, passport or other ID numbers.
  There are no such columns, and the profile validator rejects values that look like them (SSN patterns, full dates, CA DL format, words like "SSN", "DOB", "passport").
  Only a birth year or an age range is allowed (`test_forbidden_identifiers_rejected`).

## Files, uploads, paths
- **Authorization upload:**
  - Must be ≤ 5 MB (`MAX_PDF_MB`), start with `%PDF-` and end with `%%EOF`.
  - PDFs containing `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile` or `/OpenAction` are rejected.
  - Stored under a server-chosen name `<person-id>.pdf`. The uploaded filename is never used, so `../../etc/passwd` lands as `sam.pdf`.
  - Written atomically.
- **Screenshots and proof packs:** login plus ownership are required. The resolved real path must sit inside `SHOT_DIR`, so a tampered DB path like `/etc/passwd` or `../app.db` returns 404.
- **Person IDs** are validated (`^[a-z0-9_]{1,32}$`) before they're used in any path.

## Network-facing endpoints
- `/inbound/email` (Cloudflare Email Worker):
  - Disabled (404) unless `INBOUND_SECRET` is set. The header is compared in constant time (`secrets.compare_digest`) and the body size is capped at 10 MB.
  - The response only says `{ok, kind, matched}`.
- **Verification-link auto-click:**
  - Every hop, including redirects, must be `https`, on a broker domain from `sites.yaml` (exact domain or subdomain) and resolve to a public IP.
  - LAN, loopback and link-local addresses are refused (no SSRF into the home network).
  - The sender must also be a broker. Never while `DRY_RUN=true`.
- **Scanner:** follows detail and next-page links only on the broker's own host or domain, over http(s). No CAPTCHA solving.
- **Form automation:** only for opt-out forms without a CAPTCHA, only when `DRY_RUN=false`, the listing is approved and the admin enabled sending. Anything unclear goes to "Needs you".
- **Twilio webhooks:** off by default. When enabled, `X-Twilio-Signature` is verified (HMAC-SHA1 with `TWILIO_AUTH_TOKEN`).
- The app port is bound to `127.0.0.1` and reached only through the Cloudflare Tunnel by default.

## Secrets and logs
- No secrets in the image: `.dockerignore` excludes `.env*`, `data/`, `config/profiles/`, tests and screenshots. All secrets come from Portainer env vars or secret files.
- A log filter (`optout/logsafe.py`) scrubs every app and uvicorn log line: emails, phone numbers, SSN-like numbers, invite tokens, `/person/<id>` paths, API keys and `password=`/`token=` pairs.
  The e2e test greps the app log for family names, phones and emails and fails if any appear.
- Unhandled errors are logged as exception type and path only. Users see a branded error page without stack traces. Swagger/OpenAPI docs are disabled.

## Container
- Multi-stage build with pinned bases (`golang:1.26.9-bookworm`, `mcr.microsoft.com/playwright/python:v1.63.0-noble`) and pinned Python dependencies (`requirements.txt`).
- Runs as **uid 10001** (non-root), with a **read-only root filesystem** (writable paths are only `/data` (volume) and `/tmp` (tmpfs)), `cap_drop: [ALL]`, `no-new-privileges`, and `mem_limit`/`pids_limit`.
- App files are not writable and are byte-compiled at build time (`PYTHONDONTWRITEBYTECODE=1`).
- Chromium runs headless without its own sandbox (Playwright default in containers). The container boundary and dropped capabilities are the isolation.

## Scans run for this release
- `pip-audit -r requirements.txt`: **No known vulnerabilities found** (12 Oct 2026).
- `bandit -r optout`: no High or Medium findings left.
  - Fixed: SHA-1 swapped for SHA-256 for dedupe keys, `/tmp` usage moved to `tempfile.mkdtemp`, https-only URL fetches, and an SSRF guard on link-clicking.
  - Reviewed and annotated `# nosec`: SQL strings built only from fixed column lists or `?` placeholders, the allow-listed toggle column, the fixed-argv Eraser subprocess, and a `Markup` that wraps an escaped value.
  - Left: 5 Low "try/except/pass" findings in best-effort code paths (health file, broker refresh, log filter).

## Open items / limits (honest)
- SIV encryption is deterministic: equal values encrypt the same. SQLCipher (whole-file encryption) would hide even that, but it needs a native build. Not done.
- Rate limits are in memory (reset on restart), which is fine for a single container.
- No 2FA yet. For an extra layer, put Cloudflare Access (email one-time PIN) in front of the hostname, with a bypass for `/inbound/email`.
- Broker sites can change. Form automation reports "Needs you" whenever it can't confirm a submission.
