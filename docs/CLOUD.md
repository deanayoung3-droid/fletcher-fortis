# Running in the cloud

Fletcher Fortis is one container plus a persistent volume (`/data`). It needs about 1 GB RAM, because it runs headless Chromium for scans.

## Which host? (prices checked October 2026)

| Option | Monthly | RAM | Setup | Notes |
|---|---|---|---|---|
| **Fly.io** `shared-cpu-1x` 1 GB + 1 GB volume, `iad` | **~$7** | 1 GB + swap | ~10 min | US IP, HTTPS and certificates built in, card required after a short trial. **Recommended.** |
| Hetzner CAX11 (Arm) | ~$7.60 (€5.99 + €0.50 IPv4) | 4 GB | ~10 min | Most RAM per dollar, but EU-only at this price (US Hetzner starts around $20). Some US people-search sites refuse EU visitors. |
| DigitalOcean 2 GB droplet (SFO/NYC) | $12 | 2 GB | ~10 min | Same cloud-init as Hetzner, US IP. |
| Railway Hobby | $5 plan + usage (~$10–15 for 1 GB always on) | up to 1 GB used | ~10 min | Volume capped at 5 GB on Hobby. |
| Render | $25 (2 GB instance) + disk | 2 GB | ~10 min | The $7 tier has only 512 MB, which is too small for Chromium. |
| Oracle Cloud Always Free (Ampere A1) | $0 | up to 12 GB | 30–60 min | Free, but sign-up is strict and free Arm capacity is often unavailable. |

## Fly.io (recommended)

Files: [`deploy/fly/fly.toml`](../deploy/fly/fly.toml), [`deploy/fly/Dockerfile`](../deploy/fly/Dockerfile) (a thin wrapper around the release image that hands the root-owned Fly volume to the app user, then drops privileges).

```bash
curl -L https://fly.io/install.sh | sh && fly auth signup        # or: fly auth login
cd deploy/fly
# set app = "..." in fly.toml to a free name, and PUBLIC_URL / FROM_ADDRESS to your domain
fly launch --copy-config --no-deploy --name YOUR-APP --region iad
fly secrets set FF_DATA_KEY="$(openssl rand -base64 48)"         # keep a copy in your password manager
fly deploy                                                         # creates the 1 GB volume from [[mounts]]
fly logs | grep "Setup URL"                                        # one-time setup link
fly certs add app.example.com                                      # then add the DNS record it prints
```

In Cloudflare DNS, add `CNAME app → YOUR-APP.fly.dev` with the proxy **off** (DNS only, grey cloud) so Fly can issue the certificate. `fly certs show app.example.com` shows when it's ready. Until then, `https://YOUR-APP.fly.dev/setup?token=…` works too.

Cost control: one Machine, `auto_stop_machines = "off"` (the scheduler has to run), 1 GB RAM with 1 GB swap. If scans run out of memory, set `memory = "2gb"` (about $6/month more). Updates: change the tag in `deploy/fly/Dockerfile` and `fly deploy`.

## Any VPS with cloud-init (Hetzner, DigitalOcean, Vultr, …)

File: [`deploy/vps/cloud-init.yaml`](../deploy/vps/cloud-init.yaml). It installs Docker, adds 2 GB swap and a firewall (22, 80, 443), and runs the app behind Caddy (automatic HTTPS) with a Watchtower sidecar, so **Heartbeat → Update now** works. It generates `FF_DATA_KEY` and the Watchtower token into `/opt/fletcher-fortis/.env`.

1. Edit the `.env` block at the top of the file (`DOMAIN`, `PUBLIC_URL`, `FROM_ADDRESS`, `MAIL_DOMAIN`, `TZ`).
2. Create an Ubuntu 24.04 server (2 GB RAM or more) and paste the file into **Cloud config / User data**. Add your SSH key.
3. In Cloudflare DNS, add `A app → SERVER_IP` with the proxy **off** (DNS only). Caddy gets the certificate as soon as DNS resolves.
4. After about 3 minutes: `ssh root@SERVER_IP ff-setup-url` prints the one-time setup link.
5. Copy `FF_DATA_KEY` from `/opt/fletcher-fortis/.env` into your password manager.

Datacenter IPs are blocked by more people-search sites than home IPs. Heartbeat shows which ones, and you can set an optional residential `PROXY_URL`.

## Railway

New project → Deploy from GitHub repo (Dockerfile is detected) → add a Volume mounted at `/data` → set the variables `FF_DATA_KEY` and `PORT=8080` → generate a domain.

## After deploying

Open the setup link from the logs (`docker compose logs app | grep "Setup URL"`; on Fly.io `fly logs`, on Railway the Deploy Logs tab) and follow the wizard. It covers everything below; this list is for doing it by hand later.

- **Heartbeat → Settings:**
  - Set the Public URL.
  - Add your Resend key and from address. Verify the domain in Resend: Heartbeat → Check DNS shows SPF, DKIM and DMARC.
- **Inbound mail (optional, recommended):**
  - Turn on Cloudflare Email Routing for the domain.
  - Deploy `deploy/cloudflare-email-worker.js` with the same inbound secret.
  - Broker confirmations and verification links are then handled automatically.
- **Go live:** when Heartbeat is green, untick DRY_RUN and type LIVE.
