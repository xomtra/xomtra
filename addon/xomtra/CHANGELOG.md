# Changelog

## 0.9.0 - 2026-11-27

### Added

- Admin page **Answers** (ADR 090): named sets of answer guidelines kept in the local
  database. Factory sets **Default** (relevant data first, the answer first, then a chart
  or a table, friendly tone, with topics: energy with solar, battery, feed-in and costs;
  security with alarms, cameras and motion, counted per area and never per person;
  vehicles; heating and indoor climate; water; device health; the weekly overview, each
  with what to look at, which charts and optional external sources such as spot prices,
  weather or fuel prices), **Concise** (as short as possible, no jokes) and **Core
  only** (no guidelines); custom sets (up to 20, 8,000 characters each) are duplicated from any set and edited in
  the browser, with a preview of exactly what the assistant gets, a history with restore,
  and export and import as JSON. One set is active; a change applies within 30 seconds,
  without a restart.

### Changed

- The default answer guidelines aim at relevant results and readable answers: search
  before answering, compare periods, rank findings, answer first, charts or compact
  tables, "what to check" at the end.
- The core rules also cover conclusions about persons, invented values and read only
  access; they come before every set and cannot be changed or turned off.
- Tool descriptions carry only the short core rules; `answer_guidelines` in the results
  of `server_info`, `get_findings` and `get_weekly_summary` carry the core rules and the
  active set; other data results name the active set (`answer_guidelines_set`).

### Removed

- Add-on options `answer_style` and `default_guidelines` and the variables
  `XOMTRA_ANSWER_STYLE` and `XOMTRA_DEFAULT_GUIDELINES`, including the pub style.
  **Upgrade note:** every installation starts with the set Default (also those that had
  the pub style). If you had turned the guidelines off, activate **Core only** on the
  page Answers; a pub-like tone can be written as a custom set (within the core rules).
- The place for a central theme from xomtra.com (ADR 075): no text for the assistant ever
  comes from a central service.

## 0.8.0 - 2026-11-27

### Added

- Admin page **Access** (public access with Home Assistant sign-in, ADR 081): give HA
  users who are not administrators the role `xomtra.Reader`, and see every sign-in of
  the Claude connector with **Disconnect**.
- Claude connector sign-ins end after `XOMTRA_SESSION_DAYS` (default 180); in the last
  14 days `server_info` asks the assistant to tell the user to connect again.
- Guide [which installation to choose](docs/setup/choose-edition.md): limits of the
  add-on and of an own machine, supported AI assistants, what the external connector
  needs (domain, A record, port 443, certificate, sign-in).
- Answer style option (add-on option `answer_style`, `XOMTRA_ANSWER_STYLE`): `friendly`
  (default) or `pub`, the crude style of 0.7.x with personal humour (ADR 080).

### Changed

- Home Assistant sign-in is the default (`XOMTRA_AUTH_MODE=ha`); Entra ID stays an
  option (ADR 081). **Upgrade note:** installations with Entra ID that did not set
  `XOMTRA_AUTH_MODE` must add `XOMTRA_AUTH_MODE=entra` to `.env` (the server refuses to
  start with Entra values and no mode, with this hint).
- Public access with Home Assistant sign-in: only HA administrators get in by default
  (`XOMTRA_HA_ROLE_MAP` default `admin=xomtra.Admin,user=`); other users need a role on
  the page Access. **Upgrade note:** users who were Readers through the old default
  need that role on the page Access.
- Claude connector with Home Assistant sign-in requires two-factor authentication in
  HA (`XOMTRA_HA_REQUIRE_MFA`, default `true`).
- Default answer style is friendly with light humour; the crude pub style is an option.
- Core rules also forbid politically or religiously tinged jokes and innuendo, in every
  style and theme.
- Personal tokens expire after at most a year (30, 90 or 365 days, 90 by default); the
  option "never" is gone. Existing tokens keep their expiry.
- Admin GUI: the note on AI assistants says plainly that claude.ai needs an installation
  on its own virtual machine with a public address and that ChatGPT and other cloud
  assistants are not supported yet.

### Security

- The Claude connector keeps no Home Assistant token of the user: it is revoked right
  after the sign-in (as the admin GUI already did). Requests are checked against the
  session, the access list and, with an administrator token in xomtra's HA connection,
  HA's user directory. Audit records `connector:sign_in` (with `ha_token_revoked`) and
  `connector:session_refused`.

## 0.7.1 - 2026-11-26

### Added

- Option to turn off the default answer style (add-on option `default_guidelines`,
  `XOMTRA_DEFAULT_GUIDELINES=false` elsewhere), with a note on the admin pages for
  tokens and clients (ADR 080).
- Place for a central theme from xomtra.com (ADR 075, e.g. Christmas, Easter,
  Halloween): when one is active it comes first in `answer_guidelines` and takes
  precedence over the default style. It cannot be turned off, is forgotten 24 hours
  after it was received and never overrides the core rules.

### Changed

- Default answer style: very funny, crude and vulgar, like a mate in the pub, fitted to
  the language, country, culture and locality of the installation and to the owner's
  line of work when the user mentions it (never to wealth or status), for the user as
  the occupant of the property; teasing the user only with tact; facts stay exact
  (ADR 073, 080).
- Humour made personal and a bit politically incorrect: the assistant uses what it
  already knows about the user from its own context (memory, profile, conversation),
  never personal details inferred from the house data, never the user's clients or
  employers by name, always within the core rules (ADR 080).
- Core rules that always apply, also with the default style off and above a central
  theme: no jokes, irony or vulgarity about accidents, faults, outages, risky
  situations or threats to life or health; no sexist, racist, homophobic, xenophobic
  or political jokes or innuendo and no slurs against any group.

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

Older versions: https://github.com/xomtra/xomtra/blob/main/CHANGELOG.md
