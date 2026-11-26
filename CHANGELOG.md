# Changelog

All notable changes to xomtra are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (see
[docs/releasing.md](docs/releasing.md)). Before 1.0.0 a minor release may contain
breaking changes; they are listed under **Changed** with an upgrade note.

Add an entry under `[Unreleased]` in every pull request with a user-visible change
(sections: Added, Changed, Deprecated, Removed, Fixed, Security).

## [Unreleased]

### Added

- Analysis settings: "Reset all to defaults" sets every changed setting back to its
  default in one step (confirmed with a tick box, recorded in the history and audit).

### Changed

- The admin GUI has the look of xomtra.com: the xomtra mark and wordmark, Geist and
  Geist Mono (bundled, no external requests), graphite and indigo colours. It is dark
  by default; behind Home Assistant Ingress it turns light when Home Assistant is in
  light mode. The add-on has an icon and a logo for the add-on store.

## [0.5.0] - 2026-11-25

### Added

- Home Assistant add-on (lite edition, in development): one container with Postgres,
  the worker and the MCP server for the LAN, supervised by s6-overlay; internal secrets
  and the Postgres cluster are generated on the first start in `/data`; Home Assistant
  is reached through the Supervisor. Built, scanned and started in CI (job `addon`).
  `XOMTRA_HA_URL=http://supervisor/core` uses the Supervisor's WebSocket endpoint.
- Admin GUI behind Home Assistant Ingress (ADR 070, add-on): `XOMTRA_AUTH_MODE=ingress`
  takes the user from the Supervisor's `X-Remote-User-*` headers, accepts only the
  Ingress proxy's address and serves its links under `X-Ingress-Path`;
  `XOMTRA_DAGSTER_UI=false` hides the Dagster link. The add-on shows the GUI in the
  sidebar for HA administrators.
- Home Assistant add-on repository (`repository.yaml`): the add-on images
  `xomtra-addon-amd64` and `xomtra-addon-aarch64` are built natively, signed and
  published with every commit of `main` and promoted with every release;
  `scripts/release.py` keeps the add-on version in step.
- Admin GUI page *Analysis settings* (ADR 060): thresholds of correlations, anomalies
  and change points can be changed without a restart, with a preview of their effect on
  the current findings and a history of changes. A stored value wins over the ingest's
  environment variable. New schema `analysis` in the audit database (created by
  `ensure-databases`), new ingest variable `XOMTRA_ANALYSIS_SETTINGS_DB` (default
  `audit`).
- Admin GUI page *Findings* (ADR 060 point 6): a finding can be dismissed (the pair of
  entities of a correlation, the entity of an anomaly or change point), for good or
  until a date, and undone. The ingest flags matching findings `dismissed` after every
  run; `get_findings` and the weekly summary hide them like the other flags.
- Low-power indicator (ADR 067): every correlation run records its confidence in
  `catalog.analysis_run`: `low` with fewer than 30 analysed series or less than 80 % of
  the analysed entities in an HA area. `get_findings` returns it per window
  (`confidence`, no entity names); the admin page *Findings* shows it with the analysed
  entities that have no area. The thresholds are not tightened: on the synthetic
  households a stricter q or |r| did not lower the false discovery rate, complete areas
  did.
- Usefulness of findings (ADR 062 point 9, ADR 068): every finding gets `strength`,
  `evidence`, `nontriviality`, `action_category` and `usefulness` = R0 =
  √(strength · evidence); findings are ranked by it within type and window. Trivial
  relations (T1–T6: one device, one quantity twice, a helper and its source, one device
  class twice in one area, known physics, a collection artefact) get the flag `trivial`
  and are hidden by default. Categories come from the public, versioned rulebook
  `hacore/rules/actionability.toml` (17 entity families). `get_findings` returns
  `usefulness`, `action_category` and `trivial_rule`, and `ranking_version` in
  `methods`; the admin page *Findings* shows them.
- Analysis setting *Indirect and common-factor relations* (ADR 062 point 10,
  `XOMTRA_CORRELATION_STRUCTURE_DISPLAY`): `hide` (default), `lower` or `show`. New
  column `hidden` in `catalog.finding` decides what `get_findings`, the weekly summary
  and the admin hide by default (older rows: any flag). The weekly summary orders by
  usefulness.
- Preregistration of the usefulness evaluation (`docs/evaluation/usefulness-prereg.md`)
  and its metrics in `hacore.public.ratings`: Krippendorff's α (ordinal), weighted κ,
  NDCG@10, precision@10, cluster bootstrap over households and the simulation that
  fixes the human sample at 200 findings. `hacore.public.usefulness_sample` draws the
  stratified sample of findings on public data (manual job `usefulness-sample` of the
  validation workflow, CSV artifact kept 30 days, CC BY citation).
- Rulebook 1.0.1: a gas or smoke detector (`binary_sensor`) is air quality, a gas meter
  energy (both were air quality).
- Validation on public data (ADR 065, Gate 1c point 6): `hacore.public` fetches the IDEAL
  hourly readings with HTTP range requests (member list pinned, CRC-32 checked) and
  measures the cross-household null of correlations and anomalies
  (`python -m hacore.public.crosshouse`); `hacore.synthetic.benchmark` measures run time
  and memory. The nightly workflow `validation.yml` runs both on amd64 and arm64 and
  prints only aggregate metrics.

### Changed

- **Breaking: hammond is now xomtra (ADR 071).** The project moved to the organization
  **xomtra** (repository `github.com/xomtra/xomtra`) and every name follows: images
  `ghcr.io/xomtra/xomtra-{mcp,ingest,caddy}` and `xomtra-addon-{amd64,aarch64}`, Python
  packages `xomtra_mcp` and `xomtra_ingest`, commands `xomtra-*`, environment variables
  `XOMTRA_*`, compose project `xomtra` (containers and volumes), lake bucket `xomtra`,
  Postgres roles `xomtra_ops`, `xomtra_audit` and `xomtra_audit_admin`, Entra roles
  `xomtra.Admin` and `xomtra.Reader`, Home Assistant labels `xomtra_no_ingest` and
  `xomtra_no_analysis`, personal tokens `xmt_…` and the add-on slug `xomtra`. Releases up
  to 0.4.1 keep the image addresses of their `images.env`; signatures of new releases
  name the new repository. Upgrade: put the `*_IMAGE` lines of the release's
  `images.env` into `.env` (still under the old names) and run
  `deploy/compose/migrate-from-hammond.sh`: it rewrites `.env`, copies the volumes,
  renames the Postgres roles, copies the bucket and updates the DuckLake catalog,
  keeping the old data until you remove it. Then add the Entra roles, rename the labels
  in Home Assistant and issue new personal tokens (the old `hmd_…` tokens stop working).
- Anomalies v2 (ADR 066): the scale of a series is at least a quarter of its 99th
  percentile deviation in the baseline. Heavy-tailed series (appliances idling most
  hours) were scaled by their idle noise, so their ordinary use saturated the z cap and
  real episodes could not stand out (recall of planted episodes on the IDEAL homes was
  about 0). Normal series are not affected.

### Fixed

- `compose.lite.yaml` defaulted to the ingest image 0.3.0, which has no `worker`;
  `scripts/release.py` now keeps it at the release version like `compose.yaml`.

## [0.4.1] - 2026-11-25

### Fixed

- Anomalies v2 found nothing on a real installation: a few nearly constant sensors had
  a robust scale near 0 and z in the millions, which made the pooled reference so heavy
  that no episode passed. The scale is now at least the sensor's resolution and the
  episode statistic caps `|z|` at 50 (`HAMMOND_ANOMALY_Z_CAP`).
- Correlations: the stored top 100 of a window held only duplicates and same-device
  pairs after the unflagged ones, so explained relations (`indirect`, `house_context`,
  ...) were never stored. Explained and unstable pairs now come before the trivial
  ones.

## [0.4.0] - 2026-11-25

### Added

- Editions (ADR 050): `HAMMOND_EDITION=full|lite` selects default retention and DuckDB
  memory; `HAMMOND_LAKE_DATA_PATH` keeps the lake data files in a local directory
  instead of object storage (no SeaweedFS needed). Existing installations are unchanged.
- `hammond-ingest worker` (ADR 050): live stream and the scheduled jobs in one process
  instead of Dagster, with runs in the new Postgres database `ops`; `hammond-ingest job`
  and `hammond-ingest runs`. `deploy/compose/compose.lite.yaml` runs the lite edition
  (no SeaweedFS, no Dagster, lake data in a local volume).
- Synthetic households with known truth for evaluating the analyses
  (`hacore.synthetic`, `python -m hacore.synthetic.evaluate`, ADR 062): archetypes,
  hidden drivers, planted relations, defects and inserted events. See
  docs/analysis-evaluation.md.
- Correlation findings carry `p_value` and `explained_by`, and new flags
  `common_factor`, `indirect` and `unstable` (ADR 062). `get_findings` returns them;
  flagged findings are hidden unless `include_flagged`.
- Correlation flag `same_area` (ADR 064): a relation between two entities in one Home
  Assistant area (or, without an area, one area named in their ids or names) is
  explained by the room's occupancy and ranked after the unflagged findings.
- Correlation flag `house_context` (ADR 064): a relation explained by a presence
  entity of the residents. Groups and drivers reach the analyses through a
  domain-neutral context (`hacore.context`); Home Assistant is its first adapter.

### Changed

- Correlation significance v2 (ADR 062, `HAMMOND_CORRELATION_METHOD=v2`, default):
  variance of `r` from the autocorrelations of both series, an empirical null of
  circularly shifted pairs instead of Bonferroni over lags, and structural flags. On
  the synthetic households the share of false or unexplained findings drops several
  times; findings between autocorrelated but unrelated series disappear. Set
  `HAMMOND_CORRELATION_METHOD=v1` for the previous method. The next run replaces the
  findings of each window.
- Anomalies and change points v2 (ADR 062, `HAMMOND_ANOMALY_METHOD=v2`, default):
  conformal p-values of anomaly episodes against the installation's own baseline weeks
  and permutation p-values of change points (AR(1) surrogates), each with the
  Benjamini–Hochberg FDR over all series (`HAMMOND_ANOMALY_MAX_Q`, 0.05) instead of
  fixed thresholds per series. With many series far fewer false anomalies; in a very
  small installation only strong ones pass. Findings carry `p_value` and `q_value`.
  Set `HAMMOND_ANOMALY_METHOD=v1` for the previous method.

## [0.3.0] - 2026-11-24

### Added

- The label ids that leave entities out are settings: `HAMMOND_LABEL_NO_INGEST` and
  `HAMMOND_LABEL_NO_ANALYSIS` (defaults `hammond_no_ingest`, `hammond_no_analysis`).
- Sign-in with a Home Assistant account (`HAMMOND_AUTH_MODE=ha`, ADR 053) for the
  Claude connector and the admin GUI: HA administrators get `HAmmond.Admin`, other users
  `HAmmond.Reader` (`HAMMOND_HA_ROLE_MAP`, optional `HAMMOND_HA_ALLOWED_USERS`,
  `HAMMOND_HA_AUTH_URL`). See docs/setup/ha-login.md.
- Local access mode (`HAMMOND_ACCESS_MODE=local`, ADR 056/059): MCP and admin GUI only in
  the LAN behind Caddy's local CA (`HAMMOND_LOCAL_HOST`, `LAN_CIDR`), personal tokens
  issued and revoked on the new admin page **Tokens**, connect instructions for Claude
  Desktop, Claude Code and other MCP clients. See docs/setup/local-access.md.
- Claude Desktop extension `hammond.mcpb` (dependency-free stdio bridge): downloadable
  from the admin GUI with the server address filled in, and attached to releases.
- `deploy/compose/init-env.sh` creates `.env` with every internal password and key
  generated (ADR 061); `--fill` completes an existing `.env` after an update.
- Audit records carry the identity provider (`idp`: `entra`, `ha`, `token`); `whoami`
  shows it, `server_info` shows the access mode.

### Changed

- The Caddy image starts through `hammond-caddy`, which picks the configuration of
  `HAMMOND_ACCESS_MODE` and checks the values it needs. In compose, `ACME_EMAIL`,
  `CLOUDFLARE_API_TOKEN`, `CLAUDE_IP_RANGES` and `HAMMOND_PUBLIC_HOST` are no longer
  required by compose itself; Caddy refuses to start without them in the public mode.
  Upgrade: nothing to do, existing `.env` files keep the public mode.
- `postgres-init` adds the column `audit.tool_call.idp` and the schema `access` (personal
  tokens) to existing installations.

Upgrade from 0.2.0: take `compose.yaml`, `postgres/ensure-databases.sh` and the new
`init-env.sh` from the tag and the image lines from `hammond-0.3.0-images.env`.
Optionally run `./init-env.sh --fill` to add the new keys with their defaults (it never
changes existing values). An `.env` without the new keys keeps working: public access
with Entra ID as before.

## [0.2.0] - 2026-11-24

### Added

- Anomalies (unusual hours against the same hour of the week) and change points
  (lasting level shifts of daily means) of single entities in `catalog.finding`, run
  daily after the correlations (`HAMMOND_ANOMALIES_ENABLED`, `HAMMOND_ANOMALY_*`,
  `HAMMOND_CHANGEPOINT_*`).
- MCP tools `get_findings` (findings with entity names, trivial ones hidden by default)
  and `get_weekly_summary` (anomalies and change points of a week, correlations new or
  gone), both also for `HAmmond.Reader`.
- Dagster job `findings_weekly` (Monday 07:00) keeping weekly snapshots of the findings
  in `catalog.finding_weekly` (view `catalog_finding_weekly`, 104 weeks).

### Fixed

- Caddy answers refused requests with a short `text/plain` body (404 for unknown paths
  such as `/`, 403 for Claude-only endpoints and `/admin` from elsewhere); the empty
  response without a Content-Type made Safari offer it as a file download.

### Security

- Entities hidden from a role are also removed from the group lists (`entity_group`,
  `related_group`) of the findings it can see; before, a hidden entity could appear
  there as a near-duplicate of a visible one.

## [0.1.0] - 2026-11-23

### Added

- MCP server (FastMCP) for Claude with sign-in through Microsoft Entra ID (OAuth proxy),
  roles `HAmmond.Admin` and `HAmmond.Reader` from Entra app roles (#2).
- Public entry through Caddy: TLS via ACME DNS-01, OAuth endpoints open, MCP and token
  endpoints only from Claude's IP ranges (#4, #5).
- Data ingest from Home Assistant: live state stream, history and statistics backfill,
  bronze and silver layers in DuckLake on SeaweedFS (S3) and Postgres, Dagster jobs,
  retention (#6, #11, #12, #13).
- Entity catalog with drift detection and MCP data tools `search_catalog`,
  `describe_entity`, `list_views`, `get_timeseries`, `query_sql`; policy per role,
  SQL guard, read-only DuckDB lockdown and an audit log in Postgres (#14).
- Admin GUI with Entra sign-in and the Dagster UI behind it, reachable only from the
  admin network (#15).
- Correlations between entities (`catalog.finding`, MCP view `catalog_finding`),
  optional outside temperature from the Open-Meteo archive (#16, #18–#21).
- Home Assistant labels `hammond_no_ingest` and `hammond_no_analysis` exclude entities
  or devices from ingest and analysis (#21).
- Release management: SemVer tags, release workflow that promotes the tested images,
  signs them and publishes a GitHub release with pinned digests (#22).

### Changed

- Role `HAmmond.Reader` no longer sees state attributes; audit records are kept for
  90 days (`AUDIT_RETENTION_DAYS`) (#17).

[Unreleased]: https://github.com/xomtra/xomtra/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/xomtra/xomtra/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/xomtra/xomtra/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/xomtra/xomtra/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/xomtra/xomtra/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/xomtra/xomtra/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/xomtra/xomtra/releases/tag/v0.1.0
