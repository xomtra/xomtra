# Evaluating the analyses

The analyses are evaluated before any change reaches a release (ADR 062 point 8), in
three layers:

1. **Synthetic households** with known truth, on every pull request. They evaluate
   the **correlations** only.
2. **Public datasets** without known truth, nightly (`validation.yml`): the
   cross-household null for correlations, the false alarm rate and the recall of
   planted episodes for anomalies, and the benchmark.
3. **Opt-in metrics from beta installations.** Planned, not implemented: there is no
   job and no code for it yet.

The usefulness of findings (how they are ranked) is evaluated separately, by rating, as
preregistered in [`evaluation/usefulness-prereg.md`](evaluation/usefulness-prereg.md)
(see *Usefulness*). Every number on this page was measured, and the run that produced
it is named in *Results*. Planned work is marked as planned.

## Synthetic households (`hacore.synthetic`)

A structural causal model of a home generates hourly series exactly as the analyses
see them (`correlations.Grid`):

- **Hidden drivers:** outside temperature (yearly and daily cycle plus AR(1) weather,
  five climates), sunshine (astronomical clear sky times Markov clouds), occupancy of
  the home (five personas: worker, home office, shift, family, retired) and of each
  room.
- **Sensors:** room temperature, humidity, CO₂, motion, light, door; fridge, washing
  machine, heat pump, house power and energy counter, PV, battery and grid power.
- **Planted relations** of known strength (|r| 0.2-0.8) and lag (0-24 h): direct pairs,
  chains A -> C -> B and hidden common drivers. Power users get mostly lag-0
  automations.
- **Unrelated sensors** with their own daily cycle (true negatives).
- **Defects:** quantisation, missing hours, outage blocks, outage of a whole room,
  duplicated entities (one sensor through two integrations).
- **Events** (optional): spikes, drop-outs, drifts and level shifts with their
  intervals, for the anomaly and change point evaluation.

Archetypes: `apartment`, `house_heat_pump`, `house_pv_battery`, `power_user`,
`minimalist`. Every seed draws its own climate, persona and parameters, so results
describe a distribution of homes, not one home. The parameter ranges are expert
estimates. Calibrating them from public data is planned; no job does it yet, and only
IDEAL and REFIT are in `datasets.json`. A real installation is never a source of
parameters (see *Lessons from installations*).

Every pair of series has a truth class: `duplicate`, `direct`, `indirect` (through a
third series), `common` (hidden driver), `weather` (only outside temperature or
sunshine in common) or `unrelated`.

## Metrics

`hacore.synthetic.evaluate` evaluates the correlations. The generator can plant events
for anomalies and change points, but no metric uses them yet: anomalies are measured
on public data (see *Public datasets*).

```bash
uv run python -m hacore.synthetic.evaluate --sizes 10,100,1000 --seeds 4 --days 90
```

| Metric | Meaning |
|---|---|
| `fdr_strict` | findings between unrelated series / findings |
| `fdr` | as above plus indirect and common relations without the flag `indirect` / `common_factor` (Gate 1c criterion 6) |
| `recall_strong` | planted direct relations with \|r\| >= 0.5 found |
| `recall` | all planted direct relations found |
| `duplicates_flagged` | duplicate pairs found that carry `duplicate_suspect` |
| `structure_flagged` | found planted chains and hidden drivers with at least four observed children that carry `indirect` / `common_factor` (reported) |
| `structure_strong_flagged` | the same for strong structures, every link or loading >= 0.5 (Gate 1c: >= 80 %) |
| `structure_unidentifiable` | found pairs of hidden drivers with only three observed children: no observational test can tell them from direct relations |
| `direct_overflagged` | planted direct relations found but flagged `indirect` / `common_factor` |
| `fdr_all` | `fdr` over all findings, `same_area` ones included and not excused by the flag |
| `same_area_share` | findings flagged `same_area` |
| `area_coverage` | room sensors whose area the analysis knows (HA area or name); `fdr_pooled_incomplete_areas` is the gate FDR of households below 90 % |
| `occupancy_common_share` | findings caused only by the occupancy of the home or a room (local hidden drivers) |

Each metric is reported per archetype and size and as the 90th percentile over the
households of a cell. `--method v1` evaluates the method of 0.3.0 for comparison.
`--area-coverage` (share of rooms with an HA area) and `--descriptive-names` (share of
the other rooms whose entities are named after the room) fix the area scenario; by
default every household draws its own.

## Gate 1c criterion 6 (ADR 062, ADR 064, ADR 065)

The thresholds, as the development plan states them: in **every archetype** and at the
**90th percentile over households**, for 10, 100 and 1,000 series, the false discovery
rate of correlations is at most **1.5 × the target q** (q = 0.01 by default, so 0.015),
and so is the rate on the cross-household null of public data; false anomalies and
change points are at most 1.5 × their target rate; event-based recall is at least 90 %
for planted relations with |r| >= 0.5 and planted episodes with z >= 6; at least 80 % of
strong structures are flagged; one run of 1,000 series × 365 days on arm64 takes at most 5
minutes and 1 GB RSS, and 1,000 series × 90 days at most 12 s (ADR 065). How many
households per cell decide the gate, and with which interval, is not yet fixed: today
4 households per cell are reported as point estimates and 90th percentiles. The
definitions:

- **FDR** over findings without the flag `same_area`: relations between entities in
  one Home Assistant area (motion, light and CO₂ of one room) are explained, not false;
  they are left out of both the numerator and the denominator. The report gives the FDR
  with them too, and separately for households with incomplete areas.
- **Strong structures flagged >= 80 %:** planted chains A -> C -> B and hidden drivers
  with at least four observed children, every link or loading >= 0.5. The share over
  all structures, weak ones (links 0.2-0.5) included, is reported: the tetrad test
  needs the neighbours' correlations well above their noise, so weak hidden drivers
  are a statistical limit of the test, not a failure of the gate. A hidden driver of
  three series is not identifiable from observational data (a one-factor model with
  three indicators is just identified: no tetrad constrains it), so it is reported
  apart (`structure_unidentifiable`) and not counted. `same_area` relations do not
  count as found structures.
- **Whole-home presence** (one person in a small home: kitchen motion and bedroom CO₂):
  relations between areas that an observed presence entity (`person.*`,
  `device_tracker.*`, a presence or occupancy sensor) explains are flagged
  `house_context` and treated like `same_area` (ADR 064 addendum). Without any
  presence entity the analysis cannot tell them apart; the FDR of such households
  (archetype `minimalist`) is reported as a known limit of the data, not counted
  against the gate.
- Planted direct relations must not be flagged (`direct_overflagged`).

## Public datasets: cross-household null (layer 2, `hacore.public`)

Real data have no known truth, but series of *different* homes cannot be related.
`hacore.public.crosshouse` builds mixed households from the homes of a public dataset:
a few channels per home, every home shifted by whole weeks onto one calendar, with real
start dates at least 28 days apart (no shared weather or dates, same day of the week).
Every correlation finding between homes is false.

- **IDEAL** (Edinburgh DataShare, CC BY 4.0, 255 UK homes): the hourly readings
  (`auxiliarydata/hourly_readings`, about 160 MB of a 6.4 GB archive) are fetched with
  HTTP range requests (`python -m hacore.public.fetch ideal <dir>`). The hash of the
  archive's member list (names, sizes, CRC-32) is pinned in `datasets.json`, and every
  member's CRC-32 is checked. Room sensors carry their room, which becomes the area of
  the channel, so `same_area` applies inside a home and must never appear between homes.
- **REFIT** (University of Strathclyde, CC BY 4.0): the download is behind a browser
  challenge, so it is run by hand (`--dataset refit --archive <7z>`). Its archive hash
  in `datasets.json` is still `null`; it is pinned after the first manual download.
  REFIT has no anomaly annotations, so anomaly recall is measured with episodes planted
  into real series (semi-synthetic), not against labels.

Reported, summed over the runs: `fdr_all`, `fdr_gate` (without `same_area` /
`house_context`), `fdr_default_view` (unflagged findings only), `fdr_strict` (= fdr_all:
a pair between homes is unrelated by any reading), the number of null pairs tested,
`cross_per_1000_pairs`, `context_share` and `cross_context` (must be 0). With one
channel per home (`--channels 1`) every pair is null: `null_runs_with_findings` estimates
the familywise error rate. Anomalies: findings per 100 series-weeks without planted
events, then the recall of planted episodes of 4, 6 and 8 robust deviations (3-12 hours),
split into heavy-tailed series (appliances that are idle most hours) and regular ones
(`recall_by_shape`), and the findings that appear elsewhere only after planting. The
diagnostics give the share of planted episodes among the candidates, their median p and
peak |z|, and the quantiles of the pooled reference.

```sh
uv run python -m hacore.public.fetch ideal /tmp/ideal
uv run python -m hacore.public.crosshouse --dataset ideal --data /tmp/ideal --runs 20 --channels 3
```

The output starts with a manifest (code version, commit, dataset hash, houses, parameters,
seeds, the command that reproduces it). The nightly workflow `validation.yml` runs it,
together with the benchmark (`python -m hacore.synthetic.benchmark`, 1000 series x 90 and
365 days on amd64 and arm64), and prints only aggregate metrics.

## Low-power indicator (ADR 067)

On the synthetic households, a stricter threshold did not lower the gate FDR of small
installations: their false findings are strong relations with a cause the analysis
cannot see, and a stricter q or |r| removed mostly true ones. A complete context
(areas and a presence entity) did lower it. A correlation run therefore keeps its
thresholds and reports `confidence: low` (`hacore.confidence`) with fewer than 30
analysed series or less than 80 % of the analysed entities in an area; readers treat its
findings as hypotheses, and the admin GUI lists the entities without an area. The
evaluation reports the gate FDR of households with incomplete areas separately
(`fdr_pooled_incomplete_areas`).

## Usefulness (ADR 062 point 9, ADR 068, ADR 069)

Statistical correctness does not say whether a finding is worth reading. Findings are
ranked by R0 = √(strength · evidence) with the actionability rulebook
(`hacore/rules/actionability.toml`) until a learned ranking proves better. The test is
preregistered in [`evaluation/usefulness-prereg.md`](evaluation/usefulness-prereg.md),
version 2:

- **Sample:** a stratified sample of about 1,000 findings from IDEAL houses split
  60/20/20 into calibration, validation and test, with a human sample of 100 + 100 drawn
  inside validation and test (job `usefulness-sample`, manual).
- **Gate:** AI raters may stand in for people only if Krippendorff's α between the AI
  consensus and the human consensus has a 95 % lower bound ≥ 0.60.
- **Primary endpoint:** NDCG@10 per test household from the AI consensus, counted only
  when the gate holds; the model must beat R0 with a lower bound > 0.
- **Required human pool:** in 15 test houses the panel rates the union of the top 10 of
  the model, R0 and a random order; the model must be non-inferior (point estimate ≥ 0,
  lower bound > −0.05). Otherwise releases keep R0.
- The test set is rated by another model than the one whose ratings the ranking learns
  from, and AI raters never see strength, evidence, q, scores or an order.

No rating has been collected yet, so there is no usefulness result.

## Results

Synthetic households (layer 1), gate FDR (without `same_area` / `house_context`) and
the share of strong structures flagged (Gate 1c: >= 80 %), 4 households per cell,
90 days, method v2, commit `45d1dc2` (0.6.1), run on 2026-11-26. Cells of 10 series are
not in this run.

Complete areas and a presence entity in every household
(`uv run python -m hacore.synthetic.evaluate --sizes 100,1000 --seeds 4 --days 90
--area-coverage 1 --descriptive-names 1 --presence 1`):

| Archetype | Series | Gate findings | Gate FDR | Gate FDR p90 | `fdr_all` | Strong structures flagged (found) | `occupancy_common_share` | `house_context_share` |
|---|---|---|---|---|---|---|---|---|
| apartment | 100 | 212 | 0.028 | 0.063 | 0.29 | 0.50 (2) | 0.89 | 0.011 |
| apartment | 1000 | 18,255 | 0.001 | 0.002 | 0.07 | 0.94 (78) | 0.97 | 0.004 |
| house_heat_pump | 100 | 156 | 0.051 | 0.103 | 0.30 | 0.87 (15) | 0.83 | 0.000 |
| house_heat_pump | 1000 | 12,027 | 0.003 | 0.004 | 0.08 | 0.73 (49) | 0.96 | 0.000 |
| house_pv_battery | 100 | 161 | 0.068 | 0.112 | 0.27 | 0.70 (10) | 0.79 | 0.035 |
| house_pv_battery | 1000 | 8,720 | 0.005 | 0.006 | 0.10 | 0.88 (74) | 0.94 | 0.008 |
| minimalist | 100 | 77 | 0.117 | 0.211 | 0.63 | 0.67 (3) | 0.60 | 0.000 |
| minimalist | 1000 | 716 | 0.021 | 0.031 | 0.63 | 0.91 (56) | 0.64 | 0.000 |
| power_user | 100 | 192 | 0.021 | 0.052 | 0.24 | 0.96 (23) | 0.68 | 0.015 |
| power_user | 1000 | 7,985 | 0.015 | 0.017 | 0.10 | 0.79 (141) | 0.91 | 0.005 |

In every cell: recall of strong direct relations 1.0, `direct_overflagged` 0,
`fdr_strict` <= 0.002, duplicates flagged 100 %. Against the gate: the gate FDR is above
0.015 in every cell of 100 series and in minimalist and power_user (90th percentile) at
1,000 series; strong structures are below 80 % in five cells, three of them with 2-10
structures. Run time of these cells was measured on a different machine than the
benchmark below and only compares cells.

**Occupancy is the gap.** 60-97 % of the findings are caused only by the occupancy of
the home or a room (`occupancy_common_share`), yet only 0-3.5 % carry `house_context`,
although every household here has a presence entity: most are explained by
`same_area`, the rest stay in the FDR. Occupancy without an observed presence entity is
the main source of explainable findings that the context does not catch.

Areas drawn per household (the default: some rooms without an area, some entities not
named after their room; `uv run python -m hacore.synthetic.evaluate --sizes 100,1000
--seeds 4 --days 90`):

| Archetype | Series | Area coverage (mean) | Gate FDR | Gate FDR p90 | Gate FDR, households below 90 % coverage |
|---|---|---|---|---|---|
| apartment | 100 | 0.96 | 0.045 | 0.076 | 0.077 |
| apartment | 1000 | 0.91 | 0.007 | 0.010 | 0.010 |
| house_heat_pump | 100 | 0.88 | 0.108 | 0.128 | 0.121 |
| house_heat_pump | 1000 | 0.86 | 0.015 | 0.020 | 0.020 |
| house_pv_battery | 100 | 0.82 | 0.144 | 0.158 | 0.153 |
| house_pv_battery | 1000 | 0.83 | 0.025 | 0.030 | 0.028 |
| minimalist | 100 | 0.92 | 0.333 | 0.587 | 0.545 |
| minimalist | 1000 | 0.92 | 0.266 | 0.476 | 0.476 |
| power_user | 100 | 0.86 | 0.075 | 0.146 | 0.133 |
| power_user | 1000 | 0.89 | 0.026 | 0.035 | 0.034 |

With incomplete areas the gate FDR rises in every cell; minimalist reaches 0.27-0.33.
This is what the low-power indicator announces.

Public data (layer 2), nightly run
[45806040259](https://github.com/xomtra/xomtra/actions/runs/45806040259) on 2026-11-26,
commit `0670ead`, hacore 0.5.1, IDEAL member list `b2de5006…`, 20 runs × 90 days:

| Cross-household null | 3 channels per house | 1 channel per house |
|---|---|---|
| houses / series per run (mean) | 19.9 / 59.9 | 20.2 / 20.2 |
| null pairs tested | 34,191 | 3,893 |
| findings / between homes | 276 / 2 | 0 / 0 |
| `fdr_all` / `fdr_gate` / `fdr_default_view` | 0.0072 / 0.0076 / 0 | – |
| `cross_context` (must be 0) | 0 | 0 |
| runs with any finding (FWER) | – | 0 of 20 |
| anomalies per 100 series-weeks | 0.56 | 0.60 |
| recall of planted episodes, 4 / 6 / 8 SD | 0.12 / 0.44 / 0.77 | 0.11 / 0.50 / 0.71 |
| recall, heavy-tailed / regular series | 0.33 / 0.47 | 0.28 / 0.48 |

Before the tail floor (ADR 066) the recall of planted episodes on IDEAL was 0 at 4, 6
and 8 SD (3 channels per house).

Benchmark, synthetic household of 1,001 series, same run (single process):

| Runner | Days | Correlations | Anomalies and change points | Peak RSS |
|---|---|---|---|---|
| ubuntu-26.04 (amd64) | 90 | 11.9 s | 7.2 s | 594 MiB |
| ubuntu-26.04-arm (arm64) | 90 | 11.1 s | 5.9 s | 591 MiB |
| ubuntu-26.04 (amd64) | 365 | 41.0 s | 11.0 s | 1,688 MiB |
| ubuntu-26.04-arm (arm64) | 365 | 37.0 s | 7.9 s | 1,686 MiB |

The target for 1,000 series × 90 days is 12 s (ADR 065). Hosted runners vary: the run of
2026-11-25 ([45672813889](https://github.com/xomtra/xomtra/actions/runs/45672813889))
measured 10.9 s (amd64) and 11.4 s (arm64) for the same case.

**Gate 1c criterion 6 on these runs:** met on the cross-household null of IDEAL (gate
FDR 0.0076 <= 0.015; REFIT not run), for recall of strong direct relations (1.0),
`direct_overflagged` (0), 1,000 series × 90 days (11.1-11.9 s) and the 5-minute limit
for 365 days. Not met: the synthetic gate FDR at 100 series in every archetype and at
1,000 series in minimalist and power_user, strong structures in five cells, recall of
planted anomaly episodes at 6 SD (0.44-0.50 < 0.90) and memory for 365 days (1,686 MiB >
1 GB). The target rate for false anomalies is not stated as a number, so that condition
cannot be checked yet.

Usefulness frame on IDEAL (same run 45672813889, rulebook 1.0.1): 35,960 findings in
the frame (32,388 of them correlations), 1,000 drawn, 100 + 100 for people; 91.9 % of
the findings fall into a rule family and 2.7 % are trivial.

## Lessons from installations

Two defects passed layer 1 and were found only on real data:

- **Constant and quantised sensors (fixed in 0.4.1).** A series with almost no change
  had a robust scale near 0 and anomaly z values in the millions; a few such series owned
  the tail of the pooled reference, so nothing else could pass. Fix: the scale is at
  least the sensor's resolution and |z| is capped at 50 in the episode statistic.
- **Appliances that are idle most hours (ADR 066).** The robust scale was the idle
  noise, every normal switch-on hit the cap, and planted episodes could not stand out:
  recall on IDEAL was 0. Fix: the scale is at least the series' 99th percentile
  absolute deviation / 4.

Two more came from the diagnostics of one real installation:

- Metrics over all findings can differ from what the user sees. Findings flagged
  `house_context` appeared among all findings but none reached the stored top 100,
  until 0.4.1 ordered the flagged tiers correctly.
- Grid cleaning dropped most presence entities, so the context had fewer drivers than
  the installation has.

Aggregate diagnostics of real installations are evidence of defects and become
regression scenarios; they are never a source of generator parameters or thresholds.

**Planned** (not implemented):

- generator scenarios for constant or quantised sensors and for appliances idle most
  hours;
- `null_tail_share`: the share of the pooled anomaly reference's tail held by the top
  few series;
- metrics over the stored top N (100) besides all findings;
- the number of presence drivers that survive grid cleaning in the report;
- calibration of the generator's parameter ranges from public data;
- REFIT archive hash in `datasets.json`.
