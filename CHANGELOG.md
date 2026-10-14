# Changelog

All notable changes are listed here. The project follows [Semantic Versioning](https://semver.org): MAJOR for changes that need manual steps, MINOR for features, PATCH for fixes. Database migrations always run automatically on start, after a backup.

## [Unreleased]

### Added
- **First-run setup wizard** (seven steps: admin account, basics, email sending with a test button, inbound replies, optional keys, first person, done). A brand-new install redirects every page to `/setup`, which only opens with a one-time token printed in the container log. Optional steps can be skipped; everything is stored in the database and editable in Heartbeat.
- **Setup checklist** card on the dashboard until the important items are done (can be hidden).
- Settings: time zone (`TZ`), state of residence (`HOME_STATE`), update channel (`UPDATE_CHANNEL`: stable or beta).
- The generated data encryption key is shown once on the Done screen with a "save this key" notice, and its creation is logged.

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
