# Security principles

How xomtra protects a Home Assistant installation and its history, as the code does it
today (release 0.9.0). Proposals that are not decided yet are listed at the end.
To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## 1. Read only towards Home Assistant

- xomtra reads states, history, registries and statistics over the Home Assistant
  WebSocket API (`packages/hacore`). The only commands it sends are reads
  (`get_states`, `get_config`, `config/*_registry/list`, `recorder/list_statistic_ids`,
  `recorder/statistics_during_period`, `history/history_during_period`,
  `subscribe_events` for `state_changed`) and
  `auth/current_user` at sign-in. It never calls a service.
- Home Assistant has no read-only tokens: a token carries all rights of its user. The
  guarantee is xomtra's code, not a permission in Home Assistant. Hence:
  - with Docker or the VM, give xomtra a **separate Home Assistant user without admin
    rights** ([docker.md](setup/docker.md)); the contract tests check that such a token
    cannot change anything (`test_admin_only_commands_are_refused`);
  - the add-on uses the token the Supervisor gives it (`homeassistant_api: true`); that
    token could do more, and xomtra still only reads.
- The AI client never talks to Home Assistant. It gets MCP tools that read xomtra's own
  copy of the data (the lake); there is no tool that writes or controls anything.
- Labels in Home Assistant keep entities out of storage (`xomtra_no_ingest`) or out of the
  analysis (`xomtra_no_analysis`), ADR 044.

## 2. Who may call MCP

Every MCP request carries a bearer token; there is no anonymous access except
`XOMTRA_AUTH_MODE=none`, which the configuration refuses outside a loopback address.

| Mode | Where | How the caller is verified |
|---|---|---|
| Local (`XOMTRA_ACCESS_MODE=local`) | add-on, Docker | personal token from the admin GUI |
| Public with Entra ID (`XOMTRA_AUTH_MODE=entra`) | VM behind Caddy | OAuth 2.1 via FastMCP's OAuth proxy, one fixed tenant (multi-tenant sign-in is refused) |
| Public with Home Assistant (`XOMTRA_AUTH_MODE=ha`) | VM behind Caddy | OAuth proxy with Home Assistant as the identity provider (ADR 053) |

**Personal tokens** (`xomtra_mcp/tokens.py`, ADR 059): `xmt_<id>_<secret>` with a
256-bit secret, shown once. Only the SHA-256 hash is stored, in the schema `access` of the
Postgres database. The MCP server's database role may only call
`access.verify_token(hash)` (SECURITY DEFINER); the admin role may issue and revoke, never
delete. Each token has an owner, a role and an optional expiry; a revoked or expired token
stops working within 30 seconds (verification cache). The `xmt_` prefix lets secret
scanners recognise leaked tokens.

**OAuth (public mode):** tokens that FastMCP issues to Claude are signed with
`XOMTRA_JWT_SIGNING_KEY` (at least 32 characters, generated at install). Redirect URIs are
limited to Claude's callbacks (`XOMTRA_ALLOWED_CLIENT_REDIRECT_URIS`).

## 3. Roles and what each may see

The policy (`xomtra_mcp/policy.yaml`, ADR 038) maps roles to tools, data views, hidden
entities, hidden columns and a row limit. `tools/list` shows only allowed tools;
`tools/call` checks the tool again (`guard.py`).

- `xomtra.Admin`: all tools and views.
- `xomtra.Reader`: data tools only; never sees `person.*`, `device_tracker.*`, `zone.*`,
  `camera.*`, `image.*`, `lock.*`, `alarm_control_panel.*` and location sensors; state
  attributes (free text such as media titles, addresses, coordinates) are removed.
- A caller with no known role can only call `whoami`.

Hidden entities are filtered **in the database**: the views a call may read are created as
temporary views that already exclude them, not filtered after the query.

## 4. `query_sql` in three layers (ADR 039)

1. **SQL guard** (`sqlguard.py`, sqlglot): exactly one statement, only `SELECT`, set
   operations and CTEs, only views the role may read, no functions that read files or the
   network, run SQL from a string or expose configuration and secrets; the query is
   re-rendered with a row limit.
2. **Engine:** the lake is attached `READ_ONLY`; external file and network access is
   disabled, Python replacement scans are off and the configuration is locked
   (`data.py`).
3. **Policy views:** the query can only name the per-call views from section 3.

Each query has a timeout (`XOMTRA_QUERY_TIMEOUT_SECONDS`).

## 5. Audit (ADR 040)

Every tool call, allowed or denied, is one record: who (subject, identity provider, roles,
OAuth client), which tool and arguments, the decision, the outcome, rows and duration
(`audit.py`). Records go to the log as JSON lines and to `audit.tool_call` in Postgres,
where the MCP role may only `INSERT`. Writing is **fail-closed**: if the record cannot be
stored, the caller gets an error instead of the data.

## 6. Admin GUI

- Sign-in with a Home Assistant administrator account, Entra ID, or (add-on) through
  Home Assistant Ingress, where only the Supervisor's proxy may reach the GUI (ADR 070).
- With the Home Assistant sign-in the GUI asks Home Assistant who the user is and then
  **revokes the Home Assistant token right away**; it keeps no Home Assistant token of the
  person who signed in.
- Session cookie `HttpOnly`, `SameSite=Lax`; every form carries an HMAC CSRF token.
- `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'` (through Ingress
  `frame-ancestors 'self'`, because Home Assistant shows the GUI in its own frame); fonts
  and scripts are bundled, no third-party requests.
- In Docker and on the VM, Caddy lets `/admin` through only from `ADMIN_CIDR` (default:
  private ranges).
- First setup (Docker, ADR 077): a one-time code printed to the container log; the Home
  Assistant token entered there is stored encrypted with `XOMTRA_SETTINGS_KEY`.

## 7. Network

- **Add-on:** MCP on port 8000 for clients in the LAN with personal tokens. It is plain
  HTTP; use it only in a network you trust. It is not meant to be published to the
  internet (claude.ai and other cloud clients cannot reach it).
- **Docker:** Caddy with a local CA (`Caddyfile.local`), HTTPS in the LAN, HSTS.
- **VM, public access** (`deploy/caddy/Caddyfile`, ADR 028): TLS from Let's Encrypt via
  the DNS challenge, no open port 80. The browser sign-in paths (`/authorize`, `/consent`,
  `/auth/callback`) are reachable from anywhere; `/mcp`, `/token`, `/register`, `/revoke`
  and `/.well-known/*` only from Claude's published IP ranges (`CLAUDE_IP_RANGES`);
  `/healthz` and everything else is refused. HSTS, `nosniff`, `Referrer-Policy:
  no-referrer`, no `Server` header.

## 8. Secrets

- `deploy/compose/init-env.sh` and the `secrets-init` service generate the keys (JWT
  signing key, session key, settings key, database passwords) at install; nothing secret
  has a default in the code (`config.py`).
- Each service has its own database role with the least rights it needs (MCP: verify
  tokens and insert audit records).
- Home Assistant backups of the add-on keep the keys and Postgres, not the lake.

## 9. Containers

- Services run as non-root users (`USER 10001` / `10002`), with a read-only root
  filesystem, `cap_drop: [ALL]` and `no-new-privileges` in `compose.yaml`.
- Dagster's web UI is bound to `127.0.0.1` by default and sits behind the admin sign-in.

## 10. Supply chain

- GitHub Actions are pinned to commit SHAs, the workflow token is read-only, dependencies
  are updated by Renovate.
- Images are built only in CI, scanned with Trivy and signed with cosign (keyless,
  Sigstore). Releases promote the tested images without rebuilding and sign them again
  with the release tag's identity ([releasing.md](releasing.md)).
- `main` is protected: pull requests, required checks, signed commits, linear history.
  Release tags are signed. Secret scanning with push protection and CodeQL run on the
  repository.

How to verify a released image: [releasing.md](releasing.md); a build of `main`:
[README](../README.md).

## 11. Data and AI clients

- History stays on the machine where xomtra runs. xomtra itself sends no data anywhere.
- What the AI client fetches through MCP goes to that client's provider under the user's
  own account and its terms. Roles (section 3) and the labels (section 1) decide what it
  can fetch.
- The tool descriptions and results carry answering guidelines (ADR 073, 080, 090):
  fixed core rules (no conclusions about a person's presence, sleep or health unless
  asked, only fetched values, xomtra only reads, no jokes about accidents or threats to
  life and health, nothing discriminating, political or religious) and the answer
  guidelines set chosen on the admin page MCP setup. The sets are kept in the local audit
  database; no text for the model comes from xomtra.com or any other central service.
  Only administrators can change them. They guide the model; they are not a security
  boundary.

## 12. Public access with Home Assistant sign-in (ADR 081, 084)

Since 0.8.0 (`XOMTRA_AUTH_MODE=ha` is the default; Entra ID stays an option with
`XOMTRA_AUTH_MODE=entra`):

- By default only Home Assistant administrators may connect
  (`XOMTRA_HA_ROLE_MAP=admin=xomtra.Admin,user=`); other HA users need a role on the admin
  page **Access**.
- The Claude connector requires two-factor authentication in Home Assistant
  (`XOMTRA_HA_REQUIRE_MFA`, default `true`).
- Right after sign-in the connector **revokes the user's Home Assistant token**, like the
  admin GUI does, and keeps its own session (`access.session`, `XOMTRA_SESSION_DAYS`,
  180 days by default; in the last 14 days `server_info` asks the assistant to tell the
  user to connect again).
- Every request is checked against the session and the access list; removed or
  deactivated HA users and changed admin rights take effect within a minute, and
  **Disconnect** on the page Access ends a sign-in.

## Not decided yet

Optional TLS for the add-on's MCP port (ADR 076); signed notices
from xomtra.com verified with a pinned Ed25519 key, shown only in the admin GUI and never
passed to the model (ADR 075 as changed by ADR 090).
