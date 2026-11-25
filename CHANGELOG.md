# Changelog

All notable changes to HAmmond are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (see
[docs/releasing.md](docs/releasing.md)). Before 1.0.0 a minor release may contain
breaking changes; they are listed under **Changed** with an upgrade note.

Add an entry under `[Unreleased]` in every pull request with a user-visible change
(sections: Added, Changed, Deprecated, Removed, Fixed, Security).

## [Unreleased]

### Fixed

- Anomalies v2 found nothing on a real installation: a few nearly constant sensors had
  a robust scale near 0 and z in the millions, which made the pooled reference so heavy
  that no episode passed. The scale is now at least the sensor's resolution and the
  episode statistic caps `|z|` at 50.
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

[Unreleased]: https://github.com/zettabyte-cz/HAmmond/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/zettabyte-cz/HAmmond/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/zettabyte-cz/HAmmond/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/zettabyte-cz/HAmmond/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/zettabyte-cz/HAmmond/releases/tag/v0.1.0
