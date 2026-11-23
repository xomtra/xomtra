# Changelog

All notable changes to HAmmond are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (see
[docs/releasing.md](docs/releasing.md)). Before 1.0.0 a minor release may contain
breaking changes; they are listed under **Changed** with an upgrade note.

Add an entry under `[Unreleased]` in every pull request with a user-visible change
(sections: Added, Changed, Deprecated, Removed, Fixed, Security).

## [Unreleased]

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
  signs them and publishes a GitHub release with pinned digests.

### Changed

- Role `HAmmond.Reader` no longer sees state attributes; audit records are kept for
  90 days (`AUDIT_RETENTION_DAYS`) (#17).
