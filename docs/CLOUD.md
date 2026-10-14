# Running in the cloud

Fletcher Fortis is one container plus a persistent volume (`/data`). It needs about 1 GB RAM, because it runs headless Chromium for scans.

## $5 VPS (Hetzner, DigitalOcean, Vultr, Linode): recommended

1. Create an Ubuntu 24.04 VM with 2 GB RAM (1 GB works with swap). Point a DNS A record (e.g. `privacy.example.com`) at it.
2. Install Docker: `curl -fsSL https://get.docker.com | sh`
3. Run:
   ```bash
   git clone <your fork> ff && cd ff
   echo "FF_DATA_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" > .env   # keep a copy offline
   DOMAIN=privacy.example.com docker compose --profile https up -d
   ```
4. Open `https://privacy.example.com` and complete the setup wizard.
5. Back up the `ff-data` volume (it's encrypted; keep the key separately).

Datacenter IPs are blocked by more people-search sites than home IPs. Heartbeat shows which ones, and you can set an optional residential `PROXY_URL`.

## Fly.io

```bash
fly launch --no-deploy              # pick a name; uses the Dockerfile
fly volumes create ff_data --size 1
fly secrets set FF_DATA_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
```
Add this to `fly.toml`: `[mounts] source="ff_data" destination="/data"`, `internal_port = 8080`, and a VM with `memory = "1gb"`. Then run `fly deploy`. Fly terminates HTTPS for you.

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
