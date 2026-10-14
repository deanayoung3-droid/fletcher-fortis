# Fletcher Fortis on a home server (Docker + Portainer)

Stack file: `deploy/portainer-stack.yml` · example env vars: `deploy/stack.env.example`.
Example target: **https://app.example.com** on Portainer CE (standalone Docker). Replace the domain with yours.
So **Cloudflare Tunnel is the default**: creating the tunnel's public hostname adds the DNS record automatically. You don't need port forwarding or a home IP in DNS.
`DOMAIN` below means `app.example.com`.

## 1. Import the image (Portainer → Images → Import)
1. Copy `fletcher-fortis-image.tar.gz` (about 1 GB, built 12 Oct 2026) to the PC you use to open Portainer.
2. Portainer → environment `local` → **Images → Import** → choose the file → **Upload**. Portainer accepts the .tar.gz as is.
   CLI alternative on the server: `docker load -i fletcher-fortis-image.tar.gz`.
3. Check that **Images** now lists `localhost/fletcher-fortis:latest`. The stack uses that tag (`IMAGE` env var).

The image has no secrets, profiles, or family data in it (see `.dockerignore`). Never push it to a public registry anyway.
To rebuild it yourself: `docker build -t localhost/fletcher-fortis:latest .` in this folder.

## 2. Create the stack (Portainer → Stacks → Add stack → Web editor)
1. **Stacks → Add stack**, name it `fletcher-fortis`, build method **Web editor**.
2. Paste the whole of `deploy/portainer-stack.yml`. It has no `build:` line; it runs the imported image.
3. **Environment variables → Advanced mode**: paste `deploy/stack.env.example` and fill it in. Two values are required:
   - `FF_DATA_KEY`: the encryption key for all family data in the database. Generate it with `openssl rand -base64 32`.
     **Store a copy in your password manager.** If you lose it, the encrypted names, addresses, phones and emails in the database are unrecoverable. If it leaks, together with a copy of the volume, that data is readable.
   - `ADMIN_PASSWORD`: optional. Leave it empty to use the first-run setup wizard instead (recommended).
     If set, it is used only to create the admin account the first time the app starts, and the wizard is skipped. After you change the password in the app, you can blank this variable.
4. Keep `DRY_RUN=true`. **Deploy the stack.** Check `curl http://127.0.0.1:8080/healthz` on the server; it should return `{"ok":true,"dry_run":true}`.
   Portainer will also show the container as *healthy* (built-in healthcheck, every 30 s).

### First-run setup wizard
1. Portainer → **Containers → fletcher-fortis app container → Logs** (or `docker logs <container> 2>&1 | grep "Setup URL"`). Look for:
   ```
   Setup URL: http://localhost:8080/setup?token=...
   ```
   Replace `localhost` with the server's address (or open it through your tunnel). The token is new on every restart and stops working once the admin exists. Without it, `/setup` only explains where to find it.
2. The wizard has seven steps: **admin account → basics (name, public URL, time zone, state) → email sending (Resend, with a test button) → inbound replies (Cloudflare Email Worker) → optional keys (CAPTCHA, updates) → first person → done**. Steps 3 to 6 can be skipped and finished later; everything is editable in Heartbeat → Settings.
3. If `FF_DATA_KEY` isn't set, the key generated on first boot is shown on the Done screen. Copy it to your password manager.

### Accounts (after first deploy)
1. Open `https://DOMAIN/login` and sign in as `alex`. Admins land on **Family**.
2. **Family → Add member**: Sam, Jordan and Riley. For each one, either:
   - **Create invite link** (valid 72 h, single use; the person sets their own password), sent to them yourself by text; or
   - **Set initial password**; they must change it the first time they sign in.
3. Members see and edit only their own profile and listings. They can approve a listing or mark it "not me".
   Only the admin can enable sending for a person (*Sending* toggle), and nothing goes out while `DRY_RUN=true`.
4. Profiles hold names (including nicknames and maiden names), current and past addresses, phones, emails, relatives, and birth year or age range.
   The app refuses SSNs, full dates of birth, and driver's license or ID numbers. Saving a profile re-scans that person.

### Hardening already in the stack
- The container runs as uid 10001, not root. It has a read-only root filesystem, `cap_drop: ALL` and `no-new-privileges`. `/tmp` is a tmpfs, and only `/data` and `/config` are writable volumes.
- Security headers on every response: CSP, HSTS (when served over https), X-Frame-Options DENY, nosniff, Referrer-Policy and Permissions-Policy.
- Login is rate limited (5 failures per username or 20 per IP in 15 min) and uses a CSRF token on every form. Cookies are `Secure` and `HttpOnly`, and PII is scrubbed from logs. Details are in `SECURITY.md`.

### Environment variables
| Var | Default | What it does |
|---|---|---|
| `DOMAIN` | app.example.com | Public hostname (Caddy, and the links in emails) |
| `IMAGE` | localhost/fletcher-fortis:latest | Image tag imported in step 1 |
| `FF_DATA_KEY` | *(required)* | Encryption key for family data. Back it up. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | *(optional)* | Creates the admin account |
| `DRY_RUN` | true | Nothing is emailed or submitted while true |
| `SCAN_DELAY_MS`, `SCAN_MAX_QUERIES_PER_SITE`, `SCAN_MAX_PAGES`, `SCAN_MAX_DETAILS` | 8000, 6, 2, 3 | Deep-scan pacing and depth per site (name variants × cities, result pages, detail pages followed) |
| `MAIL_FILTER_ENABLED` | false | IMAP labeling (needs app passwords) |
| `SUMMARY_TO` | | Weekly summary recipient |
| `SEND_PROVIDER` | resend | `resend` (shared authorized-agent address) or `smtp` (legacy per-person Gmail) |
| `RESEND_API_KEY` | | Resend *sending access* key limited to `example.com`; only needed when going live |
| `FROM_ADDRESS` / `FROM_NAME` | privacy@example.com / Fletcher Fortis Privacy | Sender of removal requests. Reply-To is `privacy+<person-id>@…` |
| `INBOUND_SECRET` | | Shared secret for `POST /inbound/email` (Cloudflare Email Worker). The endpoint returns 404 while this is empty |
| `INBOUND_AUTOCLICK` | true | Click verification links in broker replies (https, allowlisted broker domains only, never while `DRY_RUN=true`) |
| `PROXY_URL` | | Optional residential proxy for scanning |
| `TWILIO_ENABLED`, `TWILIO_AUTH_TOKEN` | false | Optional call screening |
| `APP_BIND`, `APP_PORT` | 127.0.0.1, 8080 | Where the app port is published on the host |
| `COMPOSE_PROFILES` | tunnel | `tunnel`, `caddy`, or empty for your own proxy |
| `CLOUDFLARE_TUNNEL_TOKEN` | | Token for the `tunnel` profile |

Data lives in named volumes: `ff-data` (SQLite, screenshots, authorization PDFs) and `ff-config` (optional profiles).
Back up `ff-data`, for example with the Portainer volume browser or `docker run --rm -v fletcher-fortis_ff-data:/d -v $PWD:/b alpine tar czf /b/ff-data.tgz -C /d .`.

## 3. Make it reachable at https://DOMAIN (choose ONE)
**Option 1: Cloudflare Tunnel (default for app.example.com, no port forwarding).** The zone is already on Cloudflare.
1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → *Create tunnel* (Cloudflared) → copy the token.
2. *Public hostname*: subdomain `app`, domain `example.com`, service type `HTTP`, URL `app:8080`. Cloudflare creates the `app` CNAME (proxied) for you.
3. The stack env already has `COMPOSE_PROFILES=tunnel`. Paste the token into `CLOUDFLARE_TUNNEL_TOKEN` and *Update the stack*. The `cloudflared` container joins the `fletcher-fortis` network, so `app:8080` resolves.
   Leave `APP_BIND=127.0.0.1`: the app is then not exposed on the LAN at all, only through the tunnel.
4. Verify: `https://app.example.com/healthz` returns `{"ok":true,"dry_run":true}`.
5. Optional extra lock: add a Cloudflare Access policy (email OTP for the family) in front of the hostname. **Add a Bypass policy for path `/inbound/email`**, otherwise broker replies can't reach the app.

**Option 2: Nginx Proxy Manager (if you already run it).**
1. Attach NPM to the `fletcher-fortis` network (`docker network connect fletcher-fortis <npm-container>`), or set `APP_BIND=0.0.0.0` and use the server's LAN IP.
2. NPM → Hosts → Proxy Host: domain `DOMAIN`, scheme `http`, forward host `app` (or the LAN IP), port `8080`, *Block common exploits* on.
3. SSL tab: request a Let's Encrypt certificate and turn on *Force SSL* and *HTTP/2*.
4. On the router, forward 80 and 443 to the NPM host, and point the DNS A record for `DOMAIN` at your home IP (use DDNS if it changes).

**Option 3: bundled Caddy.** Set `COMPOSE_PROFILES=caddy`, forward 80 and 443 to the server, and point `DOMAIN` at your home IP. Caddy gets the certificate automatically.
Do not enable it if another proxy already uses 80/443.

Any other proxy (Traefik, SWAG, …) works the same way: forward `DOMAIN` to `http://app:8080` on the `fletcher-fortis` network.

## 4. Email on example.com: send from privacy@example.com (Resend), receive broker replies (Cloudflare)
Everything is built and switched **off**. Real email needs all of: `RESEND_API_KEY` set, `DRY_RUN=false`, the admin *Sending* toggle per person, and that person's e-signed authorization.
Check progress any time in the app: **Go-live checklist** (admin) → *Test email setup* (read-only DNS checks, never sends).

### 4a. Sending (Resend)
1. Resend → **Domains → Add domain** `example.com` (region us-east-1).
2. Cloudflare → example.com → **DNS → Records**: add exactly the records Resend shows (all **DNS only**, grey cloud):
   | Type | Name | Content |
   |---|---|---|
   | TXT | `resend._domainkey` | DKIM key copied from Resend (`p=MIGf…`) |
   | MX | `send` | `feedback-smtp.us-east-1.amazonses.com` (priority 10) |
   | TXT | `send` | `v=spf1 include:amazonses.com ~all` |
   | TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:dmarc@example.com; adkim=r; aspf=r` (tighten to `p=quarantine` after 2–4 clean weeks) |
   The `send` subdomain carries Resend's bounce MX/SPF, so it does not clash with the inbound MX on the root.
3. Resend → **Verify DNS records**, then **API keys → Create** with *Sending access* limited to `example.com` → paste into `RESEND_API_KEY` in the stack.
4. One shared sender for now: `FROM_ADDRESS=privacy@example.com`. Replies come back to `privacy+<person-id>@example.com`.
   Once a key is set, *Family & accounts* unlocks a per-person **send-from / reply email** (used as Reply-To).

### 4b. Inbound (Cloudflare Email Routing → Email Worker → app)
1. Cloudflare → example.com → **Email → Email Routing → Enable** (adds MX `route1/2/3.mx.cloudflare.net` + SPF on the root).
2. **Workers & Pages → Create → Worker** `ff-inbound`, paste `deploy/cloudflare-email-worker.js`, **Deploy**.
   Settings → Variables and Secrets: `APP_URL` = `https://app.example.com/inbound/email` (text), `INBOUND_SECRET` = Secret (`openssl rand -hex 32`). Optional `FORWARD_TO` = a verified destination that also gets a copy.
3. Email Routing → **Routing rules → Create address** `privacy@example.com` → *Send to a Worker* → `ff-inbound`; turn on **Subaddressing** so `privacy+alex@…` also routes there (or set the Catch-all to the Worker).
4. Put the same `INBOUND_SECRET` in the Portainer stack and *Update the stack*. Without it `/inbound/email` returns 404.
5. If Cloudflare Access protects the hostname, add a **Bypass** policy for `/inbound/email` (the secret header protects it).
6. Test: mail `privacy@example.com` from your phone. It appears under **Calls & mail → Junk & phishing** (not a broker) or **Broker replies**.

What the app does with a reply: matches the person by the `[FF-alex-12]` subject tag, the `privacy+alex@` address, or the broker sender domain, and the listing by the tag or the broker's open listing.
"Removed" → listing Pending until the next re-scan confirms it's gone; "need more info"/"denied" → **Needs you**; verification links are clicked only when sender **and** link host are broker domains (https only, public IPs only), only with `INBOUND_AUTOCLICK=true` and never while `DRY_RUN=true` (logged as "would-click").
Mail that isn't from a broker or a reply to our request is labeled junk/phishing and its links are never clicked.

### 4c. Weekly summary email
Every Monday 08:00 Pacific (`TZ=America/Los_Angeles`, `SUMMARY_HOUR=8`): each member gets their own summary at their profile email; the admin also gets a family-wide one (`SUMMARY_TO` overrides the admin address).
Disabled until a provider key is set **and** `DRY_RUN=false` (`SUMMARY_ENABLED=false` turns it off for good). Preview and send-test: **Heartbeat → Settings → Email**.

### 4d. Family logins and e-sign
Family & accounts → **Generate logins for everyone** creates `username = person id` (e.g. `sam`, `jordan`, `riley`) with a random one-time password, shown once; each must change it at first sign-in.
CLI alternative in the container console: `python /app/optout/scripts/create_family_accounts.py`.
Each member then opens **Authorization** and e-signs ("I authorize Fletcher Fortis to submit privacy requests on my behalf" + typed full name). Name, time, IP and account are recorded and a PDF letter is generated and attached to their requests. The admin can't sign for anyone else; a signed-PDF upload remains under *Other options*.
Tests: `bash test/run-tests.sh`.

## 5. Notes
- Headless Chromium needs about 1 GB of RAM. The stack limits the app to 900 MB.
- Home (residential) IPs are blocked less often by people-search sites than datacenter IPs, but expect CAPTCHAs on some sites (see the README).
- Go live only after the admin approves: members sign during onboarding, verify the Resend domain and add the key in Heartbeat → Settings, then untick DRY_RUN (type LIVE). No restart needed.

## 6. Why a home server helps scanning (and the optional proxy)
Datacenter IPs (Hetzner, DigitalOcean, AWS, this build box) are blocked outright by many people-search sites: Cloudflare "Just a moment…" pages, HTTP 403, or the TCP connection is simply closed.
Running Fletcher Fortis on the home server means scans leave from your **residential IP**, which is what a normal visitor looks like, so far more sites answer. Sites in the `unreachable_dc` group of the last probe should be retried from home first.

`PROXY_URL` (stack env) is **optional and off by default**. Leave it empty at home. Set it only if you host somewhere with a datacenter IP and bring your own residential proxy (`http://user:pass@host:port`). Fletcher Fortis never ships or buys one.
Neither a home IP nor a proxy is used to solve CAPTCHAs: a site that still shows a challenge goes to the **Needs you** queue with a prefilled search link.

## Updating
Releases are published as multi-arch images (amd64 + arm64) at `ghcr.io/deanayoung3-droid/fletcher-fortis:<version>` and `:latest`.
Heartbeat → Status → **Version** shows the running version, the latest release (checked once a day) and its changelog.
On every start the app backs up the encrypted database to `/data/backups/` (newest 5 kept) whenever the version changed, then applies migrations automatically.

- **Portainer (manual):** Stacks → fletcher-fortis → **Pull and redeploy** (or Editor → *Update the stack* with "Re-pull image" on).
- **Compose (manual):** `docker compose pull && docker compose up -d`.
- **One-click / automatic (optional):** add `autoupdate` to `COMPOSE_PROFILES` and set `WATCHTOWER_TOKEN` to a long random string.
  This starts a Watchtower sidecar that only touches the container labelled `com.centurylinklabs.watchtower.enable=true`, has no published port, never polls on its own, and updates only when the app calls its token-protected API: **Update now** in Heartbeat, or daily when *Settings → Updates → Install new releases automatically* is on (off by default).
  Docker's socket is mounted into the sidecar only, never into the app container.
- **No registry (fallback):** build or import the tarball image and set `IMAGE=localhost/fletcher-fortis:latest`.
