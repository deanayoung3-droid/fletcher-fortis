# Changelog

All notable changes are listed here. The project follows [Semantic Versioning](https://semver.org): MAJOR for changes that need manual steps, MINOR for features, PATCH for fixes. Database migrations always run automatically on start, after a backup.

## [1.3.10] - 2026-10-16

### Added
- **Web forms for registry brokers that don't take email.** A broker's form URL is saved on the broker (`broker_forms`), from `brokers/form_overrides.yaml` (LeadPost: https://app.leadpost.com/PrivacyRequest) or learned from a "Needs info" reply that points to a web form or portal (the reply's own words or its links; policy and terms pages are ignored). Every person's request to that broker that has not had a form attempt becomes "Web form queued".
- **Automatic form attempts.** Queued forms run on the scan worker (one browser at a time), live only, only for people who signed and have sending enabled, paced by `FORM_PER_HOUR` (default 6), round-robin across people. Submitted → "Request sent" (channel form). CAPTCHA, unrecognised form or no success message → "Needs you: web form" with the reason. Load errors retry up to 3 times, then Needs you. Brokers with a form and no email go the same way instead of being recorded straight to Needs you.

### Fixed
- Each broker reply is applied to a registry request once, so a later web-form step is not undone by an older reply. Form requests get no email follow-ups.

## [1.3.9] - 2026-10-15

### Changed
- **Privacy score: broker confirmations now move it.** Listings are worth 60 points and registered data brokers 40.
  - Listings: a listing gone on re-scan earns all its points, a broker's "removed" email 85%, a request sent 30%; "no records" counts as clear. A small cushion keeps a couple of listings from counting as much as dozens.
  - Registry reach (15 points): share of registry brokers contacted or covered by California DROP. A broker not contacted yet costs its share, a third of that while it waits in the automatic queue (gentler than before).
  - Registry confirmations (25 points, on a curve): each deletion confirmation or "no records" reply earns about a point early on (+6 for the first 7), full marks around 100 confirmations or when every broker in scope has answered. Confirmations are never taken back, so the score only rises as replies arrive.
  - Sites that blocked the automated scan still cost 0.75 each, at most 10.
- **"Why this score" in points.** The breakdown shows "100 − points still lost = score", then each contribution, e.g. "+5.6 from 7 broker deletion confirmations".
- **Score history.** New "Privacy score, by day" chart on the person page, with a note about the formula change. Daily snapshots are recalculated once with the new formula from listing, send and reply dates.

## [1.3.8] - 2026-10-15

### Fixed
- **Removed stuck at 0.** The home "Removed" tile only counted listings verified by a re-scan, so broker emails confirming deletion never showed. It now counts re-scan-verified removals plus deletions confirmed by the broker (people-search listings and registered brokers), with the split in the subtext ("2 verified by re-scan · 7 confirmed by broker"). The percentage is out of exposures found plus registered brokers contacted, and "no records found" is shown as its own clear count. A listing's status still only becomes Removed after a re-scan.
- **Flat sparkline.** The Removed tile's sparkline is the cumulative removed count per day for the last 12 days, backfilled from re-scan and reply dates.
- **Counts out of sync.** Home tile, family cards, person page and privacy score read one shared count (`score.removals`), and a new broker reply updates the registry request and today's score snapshot as soon as the email is processed, not on the next hourly run. Weekly "Broker confirmed" chart counts one point per request (brokers often send two emails).
- **Replies left as "other" or misclassified.** The classifier ignores our own quoted letter and folds line wraps. New wording for "no records found", and "we don't take requests by email, use our web form / portal" is now Needs info. "Any additional information" in a ticket auto-reply is no longer Needs info. Stored replies are re-classified once on upgrade.
- **Registry reply status.** The latest broker reply sets the request status (an acknowledgement after a "use our form" reply moves it to Acknowledged); a completed request stays completed.

## [1.3.7] - 2026-10-15

### Fixed
- **"Unverified" on every person.** The person hero showed "Unverified" whenever some sites blocked the automated scan, even with removals in progress. It now reads Exposed / Removing / Partly checked / Protected, and the detail line explains removals in progress and sites that could not be scanned. It has nothing to do with consent or identity checks.
- **Superseded dry-run drafts.** Drafts already replaced by a real send, by the live removal chain, or by a registry request that is queued, sent or covered by CA DROP are now archived (`dry_run_archived`) and hidden from the Inbox, with a "Show archived drafts" toggle. People without signed consent stay locked.
- **Registry requests stuck after a daily-quota error.** Resend "daily email sending quota" failures are requeued without spending a retry, and the batch pauses (`registry_paused`) instead of burning the queue. Old failed attempts whose request is back in the queue show as "Not sent · retried".
- **One person starving the registry queue.** Registry sends now go round-robin across people instead of oldest-first.
- **Privacy score never recovered.** New score model: a re-scan removal counts fully, a broker confirmation email counts mostly, a sent request counts a little, and "no records found" counts as clear. The person page gets a "Why this score" breakdown with the change since the last snapshot, and an exposures-over-time chart with a "Broker confirmed" series (also on the home chart). Daily score snapshots are recorded.
- **Inbound replies not linked or classified.** Replies from privacy platforms that drop the plus-address tag are matched to the person and broker from the quoted letter. New `not_found` class ("no records found"), wider removed/ack patterns, and a one-time re-link and re-classify of stored replies. Broker replies now update registry request status (no downgrades).

## [1.3.6] - 2026-10-14
### Fixed
- **Inbox and Family & accounts disagreed on how many emails were sent.** The Inbox listed every outgoing record, and dry-run drafts that going live had superseded (`dry_run_superseded`) and registry web-form records (`needs_human`) were labelled "Sent · awaiting reply", so the page looked like several hundred emails had gone out. Family & accounts "Sent" counted listings in the submitted state (removals filed), not emails, so it showed far fewer and left out registry requests and invites.
- Both pages (and the Heartbeat delivery card) now use one definition, `mailbox.mail_counts` / `mailbox.out_class`: **Sent** is a real delivery only; **Drafted (dry run)** (including superseded drafts), **Not sent** (errors and holds), **Queued** and **Web form for a person** are kept apart. Every message is counted once under its person, so the per-person numbers on Family & accounts add up to the Inbox total.
### Changed
- Inbox shows a counts strip (Sent, Drafted (dry run), Not sent, Received) that follows the person filter and member scope. Superseded drafts show as "Draft (dry run, not sent)" and web-form records as "Web form for a person".
- Family & accounts: each person shows "Emails sent" and "Drafted (dry run)" with a link to their Inbox, the page shows the family total, and the listing tile formerly called "Sent" is now "Removals filed".

## [1.3.5] - 2026-10-14
### Fixed
- **Resend rate limit (429).** All email goes through one paced sender (at most about 2 per second, `SEND_INTERVAL_MS`, default 500), so hourly registry and broker batches no longer burst past Resend's 10 requests per second. 429 and 5xx answers (and network errors) are retried with backoff, honouring `Retry-After`, using the same idempotency key so a retry can never send twice. Registry requests that still failed for a temporary reason go back in the queue on the next hourly run (up to 5 tries); permanent errors such as 403 stay failed. Requests that already failed with 429 are re-queued automatically on upgrade.
- **Approved removals were not being worked.** The hourly run only picked approved listings whose `submitted_at` was more than a day old, but approving (including approvals made before going live) sets `submitted_at` to now, and a queued chain lost in a restart left the listing hidden for a day. Listings now record `chain_at` when a chain actually starts, and each hourly run queues up to `CHAIN_PER_HOUR` approved listings that have not been chained in the last day (already-queued chains count against the limit). Chains still run one at a time on the single worker under the browser lock (1.3.3 memory safety).
### Changed
- Fly example config: `DRY_RUN = "false"` (live sending), matching a deployment that has gone live.

## [1.3.4] - 2026-10-14
### Fixed
- **Heartbeat no longer shows old failures as live problems.** In **Email sending**, a failure older than the latest successful delivery is marked **Resolved** and folded under "N earlier errors, resolved by a later delivery"; a new status pill (Working / Failing / Nothing sent yet) follows the most recent result. The **Email delivery (7 days)** card is red only when the newest failure is newer than the last delivery, and **Errors (24 h)** no longer counts an error as open once the same part ran successfully afterwards (for example a scan error followed by a completed scan).
### Added
- `/healthz` also reports the running `version`.

## [1.3.3] - 2026-10-14
### Fixed
- **Live removals could start many browsers at once.** Each live removal chain ran in its own thread with its own Chromium, so turning dry run off with a backlog launched dozens of browsers and ran out of memory. Live chains now go through the single scan worker (one browser at a time, shared with scans), and at most `CHAIN_PER_HOUR` (default 10) are started per hourly run.

## [1.3.2] - 2026-10-14
### Fixed
- **Going live sends the real requests.** When dry run is turned off, drafts written in dry run go back into the queue and are sent for real (consent and rate limits still apply). Before, they counted as sent, and the follow-up letters 20 and 45 days later would have gone out for requests the broker never received. Follow-ups now go out only after a real send.

## [1.3.1] - 2026-10-14
### Added
- **California DROP, automated around the one human step.** California requires the person to verify residency personally (California Identity Gateway, CCPA regs § 7620), and DROP has no public consumer API, so that step and the final Submit stay with the person. Everything else is automated:
  - Needs you shows a single **Verify with California (2 min)** task. It opens a page with 3 short steps, every DROP field prefilled from the profile with copy buttons, and the **authorized-agent disclosure** (agent name, email, trade name; § 7621) from the signed authorization.
  - DROP emails (sent to `privacy+<id>@` or forwarded by the person) are picked up by the inbound worker. The DROP ID is stored encrypted and shown masked, and status updates move the tracker: Waiting for residency check → Submitted → Brokers processing → Results in.
  - The per-broker status list from DROP can be pasted in. Matching registry brokers are marked (deleted, opted out, exempt, record not found), people-search sites get a "CA DROP" attempt, and our own re-scan still decides whether a listing is gone. Coverage counts every California-registered broker as reached once DROP is submitted.
- **ID-upload and phone-only opt-outs** follow the same pattern: an assist page with the listing URL and every field prefilled, a call script with the number (phone), and clear steps; Needs you shows "(3 min)" / "(5 min)". We never store or send ID documents.

## [1.3.0] - 2026-10-14
### Added
- **Method chain per broker ("try everything").** Every broker in the roster runs every official channel in order and falls through on a block, CAPTCHA or failure. To find listings: direct scan, then the CAPTCHA solver on the challenge page (reCAPTCHA, hCaptcha, Turnstile), then a stealth browser profile after a backoff, then the optional residential proxy. To remove them: the official web form (parent-company suppression first, CAPTCHA solver when a challenge appears), then the official privacy email with the tailored legal letter, then the state data-broker registry contact, then a blind opt-out for sites that block every search.
- Every attempt is stored (method, result, time, error) and shown on the listing (**Methods tried**) and in Heartbeat › **Coverage**: the best method reached per broker and the overall % covered.
- New settings: `PROXY_URL` (residential proxy, off unless set; now a Heartbeat setting instead of an environment-only variable) and `STEALTH_RETRY` (default on).
- `python3 chain.py report [person]` prints coverage totals.
### Changed
- Blocked searches no longer go to **Needs you**; the chain keeps trying and the progress shows in Coverage. Needs you is now only for steps that truly need a person: photo ID uploads, phone-only opt-outs, the California DROP residency check, signatures, and CAPTCHA forms when the solver is off and no other channel exists.
- Blind opt-outs are a step of the chain and are still governed by the existing **blind requests** setting (off by default). Consent is required, DRY_RUN still gates every broker send, and only a re-scan confirms a removal.
- In live mode, removals whose channels all failed are retried once a day instead of going to Needs you.

## [1.2.3] - 2026-10-14
### Fixed
- **Email sending (Resend) failed with 403.** Requests had no User-Agent, so Resend's Cloudflare rejected them ("error code: 1010"). Requests now send a `FletcherFortis/<version>` User-Agent, and errors are shown in plain words (for example "Resend 403: domain is not verified").
- Send errors now appear in Heartbeat (**Email sending**) and on the invite ("Not sent: <reason>", retried automatically). Heartbeat has **Send test email to me**, which shows Resend's exact response.
- Screenshots rebuilt: 1280px WebP full page plus a crop of the matched record (small files), a **Before** capture at discovery and an **After** capture at confirmed removal shown side by side, thumbnails on listing cards and Inbox threads, a lightbox (zoom, next/previous, download, capture date and URL), and a **Retake** button when a capture fails.
- Setup wizard no longer overflows at 390px; sidebar no longer highlights two people whose ids share a prefix.
### Added
- **Consent lock:** until a person signs, their page shows only profile basics, the invite status and a "Waiting for … to sign" banner. Scan, approve, remove, listing and screenshot routes return 403. The admin's own profile needs the admin's signature too.
- **Onboarding "Your details" step** after signing (Accept, Sign, Your details, Done), pre-filled from what the admin entered. Required: name, current city and state, and a phone or email. Never asks for SSN, full birth date or ID numbers.
- **First scan runs automatically** once onboarding is saved (never before consent). Setting `SCAN_ON_CREATE` (default on) in Heartbeat. The person page and setup show "First scan running".
### Changed
- Fly example uses 2 GB of memory.

## [1.2.2] - 2026-10-14
### Added
- **Delete person** (admin, on the person page): type the name to confirm. Removes listings, broker requests, sent and received mail, authorization, login, sessions and screenshots, and cancels queued emails. The audit event holds counts only, no personal info.
- **Family invites are always created** when you add someone (their email is now required, except for your own profile). The invite has a link to set a password, review and sign. Invites go to family, not brokers, so they are sent as soon as email is set up, even in dry run. Without email they show as "Invite pending: email not set up" with a copyable link, and they go out automatically once email works.
- Person page shows the invite status (Not sent / Sent / Opened / Accepted / Expired) with **Resend invite**. Links expire after 7 days (was 72 hours), and resending makes a new link. No removal is filed until the person signs.
### Fixed
- **Scan now** no longer stacks: scans run on one background worker, a repeat click while a scan is queued or running is ignored, and only one Chromium runs at a time (scans and automation share a lock), with low-memory flags. Before, each click started its own browser, which could starve a 1 GB machine.
- The person page shows live progress ("Scanning… X of Y sites") with the button disabled, then the result count and any errors. Heartbeat has a **Scan log**.
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
