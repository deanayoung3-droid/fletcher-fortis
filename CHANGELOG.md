# Changelog

All notable changes are listed here. The project follows [Semantic Versioning](https://semver.org): MAJOR for changes that need manual steps, MINOR for features, PATCH for fixes. Database migrations always run automatically on start, after a backup.

## [1.2.2] - 2026-10-14
### Added
- **Delete person** (admin, on the person page): type the name to confirm. Removes listings, broker requests, sent and received mail, authorization, login, sessions and screenshots, and cancels queued emails. The audit event holds counts only, no personal info.
- **Family invites are always created** when you add someone (their email is now required, except for your own profile). The invite has a link to set a password, review and sign. Invites go to family, not brokers, so they are sent as soon as email is set up, even in dry run. Without email they show as "Invite pending: email not set up" with a copyable link, and they go out automatically once email works.
- Person page shows the invite status (Not sent / Sent / Opened / Accepted / Expired) with **Resend invite**. Links expire after 7 days (was 72 hours), and resending makes a new link. No removal is filed until the person signs.
### Changed
- Setup wizard, Email sending: numbered steps (Resend account, add domain, copy records into Cloudflare as DNS only), the exact records with a live **Check DNS** that turns each row green (Resend CNAME or MX/TXT style), then API key and test email.
- Setup wizard, Inbound: small numbered cards; the Worker variables shown as a "Variable name (Key)" / "Value" table with separate copy buttons, "+ Add" and "Secret only for INBOUND_SECRET" notes; the "Send to a Worker" routing rule; a **Test inbound** check showing when mail last arrived. Short copy with optional "Why?" notes.
- Cloudflare Worker comments no longer mention Portainer or app.example.com.

## [1.2.1] - 2026-10-14
### Fixed
- Heartbeat > Email DNS: Resend's newer setup (send.<domain> as a CNAME to send.forge.rmta.net) now counts as OK for SPF and the bounce MX, as does the classic amazonses setup.
- DMARC `v=DMARC1; p=none;` without rua is accepted. Found values show cleanly (with the CNAME they came through), and the root SPF gets its own row instead of being mixed into the send SPF.

## [1.2.0] - 2026-10-13
### Added
- **Inbox** page (sidebar): every email sent and received in one place: broker requests, follow-ups, escalations, broker replies, verification emails and weekly digests.
  Tabs for All, Needs you, Pending, In progress and Completed; filters for person, broker and direction; search. Dry-run emails show as "Draft (dry run)".
- Thread view with the full message, the linked listing or broker request, a timeline of related mail and events, and a "Mark handled" action.
- Members see only their own mail. Junk and phishing stay on Calls & mail.
- Fly.io and VPS (cloud-init) deploy files with a host comparison in docs/CLOUD.md.

## [1.1.1] - 2026-10-13

### Fixed
- Docker image build: the Cloudflare Email Worker script shown in the setup wizard was excluded by `.dockerignore`, so the 1.1.0 image was never published. 1.1.1 is the first image with the setup wizard.

## [1.1.0] - 2026-10-13

### Added
- **First-run setup wizard** (seven steps: admin account, basics, email sending with a test button, inbound replies, optional keys, first person, done). A brand-new install redirects every page to `/setup`, which only opens with a one-time token printed in the container log. Optional steps can be skipped; everything is stored in the database and editable in Heartbeat.
- **Setup checklist** card on the dashboard until the important items are done (can be hidden).
- Settings: time zone (`TZ`), state of residence (`HOME_STATE`), update channel (`UPDATE_CHANNEL`: stable or beta).
- The generated data encryption key is shown once on the Done screen with a "save this key" notice, and its creation is logged.

### Changed
- Project website: calmer layout with more whitespace, a tighter type scale and fewer borders and shadows; install steps now show how to find the setup link.

### Unchanged
- Installs that already have an admin, or set `ADMIN_PASSWORD`, never see the wizard.

## [1.0.0] - 2026-10-13

First public release.

### Added
- **People-search removal:** weekly scans of 430+ people-search sites, match scoring, evidence screenshots, and removals through parent suppression, web form or privacy email. Each removal is confirmed by a re-scan, re-checked weekly and re-filed automatically if the listing comes back.
- **Registered data brokers:**
  - Imports the official state registries: California (CPPA), Oregon (DFR) and Texas (Secretary of State), refreshed monthly. A Vermont export can be imported by file.
  - Brokers are deduplicated by domain and name, and sorted into seven categories.
  - Brokers with no public listing get deletion and opt-out requests worded for their kind of data, with the same follow-up and escalation as listings.
- **Broker coverage view:** registered brokers by category, with request status per person.
- **Tailored legal requests:** chosen by the person's state (CCPA/CPRA and the Delete Act, plus 19 other state laws), sent as an authorized agent with the person's own e-signed authorization.
- **Needs you page:**
  - Human tasks grouped by person, each with the reason.
  - Blocked sites are retried automatically from the server's IP with backoff.
  - Optional CAPTCHA solver (2Captcha or CapSolver) with a monthly budget, off by default.
- **Heartbeat:**
  - Health checks, with every setting editable in the app.
  - Shows the current version against the latest GitHub release, with that release's changelog.
  - Optional one-click and automatic updates through a Watchtower sidecar, off by default.
- **Weekly summary emails** for each member and for the family.
- **Security:**
  - AES-256-SIV field encryption.
  - Read-only, non-root container with every Linux capability dropped.
  - CSRF tokens and a strict Content-Security-Policy.
  - Dry run on by default.
