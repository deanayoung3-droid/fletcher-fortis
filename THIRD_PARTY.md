# Third-party components

| Component | Where | License | Notes |
|---|---|---|---|
| **Eraser** by digisamroc (bulk CCPA sender and broker list) | `eraser/` (built into the Docker image), `brokers/eraser.yaml` | MIT, as stated in the upstream README: https://github.com/digisamroc/eraser | Used unmodified. Copyright belongs to its authors; their README notice is kept in `eraser/README.md`. The upstream repository has no separate LICENSE file, so the README statement is the license grant we rely on. |
| **California Data Broker Registry** | `brokers/ca_registry.csv` | Public record published by the California Privacy Protection Agency | Refreshed weekly from cppa.ca.gov. |
| **Optery public broker list, BADBOOL** | Site names and opt-out URLs in `optout/sites.yaml` (facts only) | Public web pages | Used as a list of site names and public opt-out URLs, re-verified by our own health probes. |
| **Geist, Geist Mono** (Vercel) | `optout/static/fonts/`, `docs/assets/fonts/` | SIL Open Font License 1.1 (`OFL.txt`) | |
| **Tailwind CSS** (build only) | `ui/` | MIT | |
| **FastAPI, Starlette, Jinja2, Playwright, cryptography, PyYAML, bcrypt** | `requirements.txt` | MIT / BSD / Apache-2.0 | Installed from PyPI at build time. |
| **Chromium** (via Playwright) | Docker image | BSD-3-Clause and others | Downloaded at build time. |

Not included: broker definitions whose license is unknown. If you add data from another project, check its license first and list it here.
