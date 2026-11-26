# Changelog

## 0.7.0 - 2026-11-26

### Added

- Docker Compose installs with one command (ADR 077): `docker compose up -d` works
  without a `.env`. The new one-shot service `secrets-init` generates every internal
  password and key into the Docker volume `secrets` (one file per service, readable
  only by its user) and finds the server's LAN address for the local access mode.
- Setup page in the admin GUI (`/admin/setup`): the Home Assistant URL and token are
  entered in the browser after the first start, checked against Home Assistant and
  stored encrypted in Postgres. Until then a one-time link is printed to
  `docker compose logs admin`; the stream and the hourly ingest wait for the connection.
  Signed-in administrators can change the URL or token later on the same page.
- Guide [docs/setup/docker.md](docs/setup/docker.md).

### Changed

- Tone of answers with xomtra data: humour is now the default style, with at least one
  joke or witty remark in every answer (about the house and the data, never about people,
  safety, health or a fault, and never at the cost of exact facts). Applies to the server
  instructions, `answer_guidelines` and the rules in tool descriptions (ADR 073, 080).
- Compose defaults: `XOMTRA_ACCESS_MODE=local`, `XOMTRA_AUTH_MODE=ha`, `ADMIN_CIDR` all
  private ranges; no variable is required any more. Public access still needs its
  values in `.env`.
- `init-env.sh` is an optional guide and no longer writes internal passwords.
  **Upgrade note:** an existing `.env` with passwords keeps working; on the first start
  `secrets-init` takes them over into the volume `secrets`, after which they can be
  removed from `.env`. Back up the volume `secrets` together with the data volumes.
- The documentation and the Claude Desktop extension now say precisely what "read only"
  means: xomtra only reads and never calls Home Assistant services, and the assistant
  gets read-only tools over xomtra's own data. Home Assistant has no read-only tokens,
  so this is guaranteed by xomtra's code; the Docker guide explains why a separate
  non-admin user is still recommended (access can be revoked at any time).

## 0.6.2 - 2026-11-26

### Fixed

- The default guidelines for answering with xomtra data (findings are hypotheses, no
  conclusions about people unless asked, friendly tone) now also reach clients that do
  not pass the server instructions to the model, such as a proxy between Claude and a
  local MCP server: a short version is part of every data tool description, and the
  full text comes with `server_info`, `get_findings` and `get_weekly_summary`
  (`answer_guidelines`).

## 0.6.1 - 2026-11-26

### Added

- Admin GUI, Tokens and Connect clients: a note on which AI assistants can connect to
  the local access mode (assistants on a computer in the LAN, not cloud connectors
  such as claude.ai or ChatGPT).
- Admin GUI footer with the version (links to its release notes), xomtra.com, the
  documentation and bug reports. The links open in a new tab and send no Referer.

### Changed

- Claude Desktop extension: the xomtra icon and xomtra.com as its homepage.
- Admin GUI overview: smaller card values that fit on one line, clearer catalog column
  names (entity kind, entities, stale entities), the profiling time in the local
  format, and the backup note in one box.

### Fixed

- Claude Desktop extension did not start in Claude Desktop's built-in Node.js
  ("Unable to connect to extension server"): the bridge only started when run as the
  main script. It now always starts.
- Claude Desktop extension: a server it cannot connect to (wrong address, or on macOS
  Claude not allowed in Local Network) now gives an error with a hint within 10 s
  instead of Claude Desktop's "Unable to connect to extension server" after a minute.
- Admin GUI: Safari saved the Claude Desktop extension as `xomtra.zip`. It is now
  sent as `application/octet-stream` with a plain file name and saves as
  `xomtra.mcpb`.
- Admin GUI after an upgrade: the browser kept the previous version's stylesheet and
  script (cached for an hour or a day) and showed the new pages half unstyled. Their
  URLs now carry a version (also the tips and the icon), so an upgrade loads the new
  ones, and the token diagram stays readable without its stylesheet.
- Home Assistant add-on: the update dialog said "No changelog found". The add-on now
  ships its own `CHANGELOG.md` with the last five released versions;
  `scripts/release.py prepare` writes it and `check` refuses a tag without it.

## 0.6.0 - 2026-11-26

### Added

- Admin GUI and add-on options in Czech, German, Polish and Dutch: the GUI follows Home
  Assistant's language (outside Home Assistant the browser's), English stays the
  default. Entity names, values and code stay as they are.
- Analysis settings: "Reset all to defaults" sets every changed setting back to its
  default in one step (confirmed with a tick box, recorded in the history and audit).
- Admin GUI: explanations of the columns and cards on hover or focus, in the language of
  Home Assistant (English, Czech, German, Polish, Dutch); numbers with digit grouping,
  shares as percentages, dates and times in Home Assistant's number, date and time
  format and time zone (outside Home Assistant the browser's).
- Free disk space of the lake on the admin overview. Below
  `XOMTRA_WORKER_STOP_FREE_RATIO` (default 3 %) the worker pauses the stream and the
  writing jobs until there is space again, then catches up from Home Assistant's
  history; the retention still runs (ADR 072).
- Home Assistant add-on: the overview and the documentation say that Home Assistant
  backups do not include the lake (history and findings) and recommend xomtra on its
  own virtual machine for backups of the whole history.
- Default guidance for the LLM in the MCP server's instructions (ADR 073): findings as
  hypotheses with their strength and window, statements based on fetched data, local
  time, no conclusions about a person unless asked, a friendly tone with the odd joke or irony,
  and no advice on locks or alarms from a finding alone. Advisory for the model; the
  server's access rules are unchanged.
- Tokens page: a picture of how a token connects an LLM agent with xomtra and that
  xomtra only reads Home Assistant, with the page's actions on it.

### Changed

- The admin GUI has the look of xomtra.com: the xomtra mark and wordmark, Geist and
  Geist Mono (bundled, no external requests), graphite and indigo colours. It is dark
  by default; behind Home Assistant Ingress it turns light when Home Assistant is in
  light mode. The add-on has an icon and a logo for the add-on store.
- The add-on's website link in Home Assistant points to https://xomtra.com instead of
  the GitHub repository.

### Fixed

- Home Assistant add-on: after a restore from a backup (which leaves the lake out) it
  failed on Parquet files the catalog still named. It now detects the missing lake,
  starts a new one and loads the history from Home Assistant again; tokens, audit and
  analysis settings stay.
- Worker on a new lake (first start, or after a restore without it): the live stream
  and the missed jobs both created the `bronze` schema, one failed on the DuckLake
  transaction conflict and the stream reconnected. The worker now creates the lake's
  schemas and tables once before the stream and the jobs start.
- Admin GUI behind Home Assistant Ingress: forms (issuing a token, settings) were still
  refused with "Cross-site request refused" when Home Assistant is opened over plain
  `http://` in the LAN. Browsers send `Sec-Fetch-Site` only to https origins, and the
  Origin does not match the forwarded host that reaches the add-on. Behind Ingress the
  forms now carry a token bound to the Home Assistant user and the Ingress session;
  `Sec-Fetch-Site: cross-site` or `same-site` is still refused.
- Postgres no longer logs DuckLake's commit retries ("duplicate key value violates
  unique constraint ducklake_snapshot_pkey") as errors: the stream and the jobs write
  at the same time and DuckLake retries such a commit by design. Errors of the catalog
  database are left to the services, which log them.

## 0.5.1 - 2026-11-26

### Fixed

- Home Assistant add-on: it did not start ("expected url" for `mcp_url`). The empty
  default of the optional option failed the Supervisor's check of `url?`; optional
  options now have no default, and a test keeps the defaults in line with the schema.
- `migrate-from-hammond.sh` stopped at its first step: `docker compose -p hammond down`
  read the new `compose.yaml` with the old `.env` and failed on the missing `XOMTRA_*`
  variables. It now stops and removes the old containers and networks by their compose
  labels.
- Postgres (add-on and compose) logs errors without the failed statement. DuckLake
  retries a commit when another writer took its snapshot id, and the statement filled
  the add-on log with Home Assistant data.
- Claude Desktop extension downloaded from the admin GUI: for a plain `http://` address
  (the add-on in the LAN) *Allow unencrypted http://* is checked by default; before, the
  extension refused the add-on's endpoint until the user found the checkbox.
- Admin GUI behind Home Assistant Ingress: every form (issuing a token, settings) was
  refused with 403 when Home Assistant is opened through another address than its own
  host, e.g. behind a reverse proxy. The cross-site check now trusts the browser's
  `Sec-Fetch-Site` header and compares the Origin only where that header is missing.

Older versions: https://github.com/xomtra/xomtra/blob/main/CHANGELOG.md
