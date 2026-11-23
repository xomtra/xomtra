# Admin GUI and Dagster UI (phase 1a.3)

`https://<public-host>/admin/` shows the state of the installation:

| Page | Content |
| --- | --- |
| Overview | Home Assistant connection (version, latency, entities), last state and statistic in the lake, catalog by kind with stale entities, drift of the last 7 days |
| Catalog | searchable entity profile (kind, change rate, value range, stale, range shift) |
| Drift | changes between daily profiles, last 30 days |
| Retention | configured retention periods with their cut-off dates, size of every lake table |
| Audit | last 200 calls of MCP tools and of this GUI |
| Dagster | the Dagster UI (`/admin/dagster/`): runs, schedules, launching jobs |

Retention values are set in `.env` (`HAMMOND_*_RETENTION_DAYS`) and applied by the
ingest; the GUI shows them read-only.

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
