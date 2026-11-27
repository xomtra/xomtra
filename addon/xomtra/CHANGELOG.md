# Changelog

## 0.9.2 - 2026-11-27

### Added

- Claude Desktop without trusting a certificate: in the local access mode the extension
  downloaded from the admin GUI (**Download for Claude Desktop** after issuing a token,
  also on Connect clients) carries the root certificate of the server's own CA and
  trusts only it (pinned), so nothing has to be imported into the computer. The admin
  GUI reads the certificate from Caddy inside the compose network
  (`XOMTRA_LOCAL_CA_URL`, an internal listener on port 8081 that is not published). The
  token is still entered at install and never part of the file. An extension from
  before a new installation of the server says to download it again. Claude Code and
  other clients still need the CA trusted as described in docs/setup/local-access.md.

  Upgrade note (Docker compose): take the new `deploy/compose/compose.yaml` together
  with the new images (it sets `XOMTRA_LOCAL_CA_URL`), then `docker compose up -d`.
  With an older compose file the download keeps working, only without the certificate.

### Changed

- Admin GUI, clearer structure and texts: the menu starts with Findings, Answers and
  Tokens; Drift is called Entity changes; the page Answers has one introduction and folds
  the text of a factory set; Tokens says in one line which assistants can connect (the
  details stay on Connect clients); Analysis settings opens with the advice to leave
  working settings alone and no longer cites ADR numbers; Retention in the add-on names
  the add-on's options instead of Dagster and `.env`; Findings explains a dismissal
  without internal names and translates its columns and types. Czech and German texts
  reviewed (German now uses "du", as Home Assistant does).
- Admin GUI, Tokens: the picture of the flow no longer has the Claude Desktop button
  (the page of a new token has the download), and the arrow labels sit above and below
  the boxes, so that longer translations no longer run into them. The note on the style
  of the answers moved from Tokens and Connect clients to the page Answers.
- Admin GUI (Docker compose): the menu no longer links to the Dagster UI; it stays
  reachable for administrators at `/admin/dagster/`.
- Admin GUI: the page Access is in the menu only where it has something to manage (the
  Claude connector with Home Assistant sign-in, public mode). With personal tokens, the
  add-on or Entra ID it had only a sentence; `/admin/access` leads to Tokens, which
  carries the Entra note.
- Admin GUI: when Home Assistant cannot be reached (setup page and Overview), a sentence
  says why and what to try, in every language of the admin GUI: `https://` on a plain
  HTTP port ("try http://"), an untrusted certificate, a name this server cannot
  resolve, nothing listening on the port, another server or a proxy without WebSockets
  answering, a timeout, or a rejected token. The technical message follows as a detail.

### Fixed

- An address that answers HTTP but not with a WebSocket upgrade no longer ends in an
  internal error on the setup page; it is reported like any other connection error.
- Setup page: whitespace copied with the access token (a line break from a wrapped
  terminal line, a trailing newline) is dropped instead of making Home Assistant reject
  the token, and the token field asks password managers not to fill in a saved password
  (`autocomplete=new-password`).
- Setup page: an address without a port (`http://192.168.1.10`) means port 80 or 443,
  where often another server answers. The setup now tries Home Assistant's port 8123 as
  well and stores that address if it works; otherwise the error says that the port is
  missing. The example in the URL field shows the port.
- Claude Desktop extension: a certificate the computer does not trust
  (`unable to get local issuer certificate`) is no longer reported with the network
  hint (Local Network permission). The message says to choose `xomtra-ca.crt` as
  *Certificate of the server*, that a chosen file is not the CA of this server, or that
  the certificate is for another name.
- Docs (local access) and the Connect page: trusting xomtra's CA is required for Claude
  Desktop and Claude Code, with the commands for macOS, Windows and Linux and how to
  compare the fingerprint on both sides.

## 0.9.1 - 2026-11-27

### Added

- Findings record their provenance in `catalog.finding` and the weekly snapshots:
  `analyzer` (`correlations` or `anomalies`), `pack` and `pack_version` (the parameter
  pack, `home`) and `source_instance` (`ha`). The columns are added to existing lakes on
  the next analysis run; older rows stay empty (ADR 089, preparation for data plugins).
- Development: `uv run lint-imports` checks the boundaries between the domain-neutral
  modules and the Home Assistant modules (import-linter contracts, also in CI).

### Changed

- Docker compose: the installation ends with the one-time setup link on the terminal,
  `docker compose up -d && docker compose exec admin xomtra-admin setup-link`, instead of
  searching for it in `docker compose logs admin` (where it stays as well). The new
  command `xomtra-admin setup-link` prints the link while Home Assistant is not
  connected, and where to sign in once it is.
- Docs (Docker compose): new step "0. Address" (the LAN address works without settings;
  a name goes into `.env` before the first start and only has to resolve on the
  clients). Changing the address later needs `docker compose up -d --force-recreate`,
  not just `up -d`, so that every service takes the new address.

### Fixed

- Docker compose: the first `docker compose up -d` of a new installation could stop with
  "dependency failed to start: container xomtra-seaweedfs-1 is unhealthy". SeaweedFS
  started alongside `secrets-init`, exited before its S3 keys were written, and compose
  gave up although the container came up healthy a few seconds later. SeaweedFS now
  waits for `secrets-init`; a test checks that every service reading the secrets volume
  does (full and lite edition).

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

Older versions: https://github.com/xomtra/xomtra/blob/main/CHANGELOG.md
