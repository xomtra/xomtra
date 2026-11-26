# Changelog

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

## 0.5.0 - 2026-11-25

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

## 0.4.1 - 2026-11-25

### Fixed

- Anomalies v2 found nothing on a real installation: a few nearly constant sensors had
  a robust scale near 0 and z in the millions, which made the pooled reference so heavy
  that no episode passed. The scale is now at least the sensor's resolution and the
  episode statistic caps `|z|` at 50 (`HAMMOND_ANOMALY_Z_CAP`).
- Correlations: the stored top 100 of a window held only duplicates and same-device
  pairs after the unflagged ones, so explained relations (`indirect`, `house_context`,
  ...) were never stored. Explained and unstable pairs now come before the trivial
  ones.

## 0.4.0 - 2026-11-25

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

Older versions: https://github.com/xomtra/xomtra/blob/main/CHANGELOG.md
