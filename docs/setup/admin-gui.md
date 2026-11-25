# Admin GUI and Dagster UI (phase 1a.3)

`https://<public-host>/admin/` (or `https://<local-host>/admin/` in [local access](local-access.md))
shows the state of the installation. Sign-in with Entra ID or, with
`HAMMOND_AUTH_MODE=ha`, with a [Home Assistant administrator account](ha-login.md).

| Page | Content |
| --- | --- |
| Overview | Home Assistant connection (version, latency, entities), last state and statistic in the lake, catalog by kind with stale entities, drift of the last 7 days |
| Catalog | searchable entity profile (kind, change rate, value range, stale, range shift) |
| Drift | changes between daily profiles, last 30 days |
| Retention | configured retention periods with their cut-off dates, size of every lake table |
| Findings | confidence of the last correlation runs (low power, area coverage, analysed entities without an area, ADR 067), current findings with *Dismiss* (for good or for 30/90/365 days, with a reason) and the list of dismissed findings with *Undo* |
| Analysis settings | thresholds of correlations, anomalies and change points (target q, minimum \|r\|, z cap, windows, method), with a preview of their effect on the current findings and the history of changes |
| Audit | last 200 calls of MCP tools and of this GUI, with the identity provider (`entra`, `ha`, `token`) |
| Tokens | local access only: issue and revoke personal tokens, connect instructions and the Claude Desktop extension ([local access](local-access.md)) |
| Dagster | the Dagster UI (`/admin/dagster/`): runs, schedules, launching jobs |

Retention values are set in `.env` (`HAMMOND_*_RETENTION_DAYS`) and applied by the
ingest; the GUI shows them read-only.

### Analysis settings (ADR 060)

A value saved on this page wins over the ingest's environment variable
(`HAMMOND_CORRELATION_MAX_Q`, …), which wins over the default. The next analysis run
(daily) uses it; nothing restarts. *Reset* hands the setting back to the environment or
the default. Changes are kept as a history (who, when, value, reason) in the schema
`analysis` of the audit database and every change is in the audit. The GUI's role may
only read and append there; the ingest's role may only call
`analysis.current_settings()`. If the ingest cannot reach the database, it runs with the
environment and logs a warning.

*Preview* counts the current findings that would remain with a stricter target q or
minimum |r|. It is exact when tightening; a looser value may also bring back weaker
candidates that were not stored. The thresholds set the rate of false findings, not
their number: a larger home rightly finds more (ADR 060, ADR 062).

## Access (ADR 041)

Two independent checks:

1. **Network:** Caddy accepts `/admin` only from `ADMIN_CIDR` (the network of your admin
   machine); from anywhere else it answers `403`.
2. **Identity:** the GUI signs you in with Entra ID (OpenID Connect, code flow with PKCE)
   and requires the app role `HAmmond.Admin`. The session cookie is signed, `Secure`,
   `HttpOnly`, limited to `/admin` and valid for 8 hours. Caddy asks the GUI
   (`forward_auth`) before every request to the Dagster UI.

Every page view, sign-in and refused sign-in lands in the audit table
(`tool = 'admin:<page>'`).

## Setup

1. **Entra:** app registration → **Authentication → Add a platform → Web**, redirect URI
   `https://<public-host>/admin/auth/callback` (next to the existing
   `https://<public-host>/auth/callback`). Make sure your user has the `HAmmond.Admin`
   role (step 6 of [the Entra guide](entra-app-registration.md)).
2. **`.env`:**

   ```dotenv
   ADMIN_CIDR=<admin-cidr>
   HAMMOND_ADMIN_SESSION_KEY=<openssl rand -hex 32>
   AUDIT_ADMIN_PG_PASSWORD=<openssl rand -hex 32>
   ```

3. **Reaching the VM from the admin network:** the public name resolves to the public
   IP. Most routers rewrite the source address of such "hairpin" connections, so Caddy
   would not see your admin address. Add a local DNS record on your router
   `<public-host> → <vm-dmz-ip>` (same certificate, direct path) and a firewall rule
   `<admin-cidr> → <vm-dmz-ip>` TCP 443.
4. `sudo docker compose pull && sudo docker compose up -d`, then open
   `https://<public-host>/admin/` from the admin machine.

Fallback without Caddy: `ssh -L 3000:127.0.0.1:3000 <admin>@<vm>` and
`http://127.0.0.1:3000/admin/dagster/` (Dagster only, no sign-in, loopback of the VM).
