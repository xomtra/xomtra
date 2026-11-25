# Evaluating the analyses

The analyses are evaluated on households with known truth before any change reaches a
release (ADR 062 point 8). This page describes layer 1, the synthetic households that
run on every pull request; the nightly validation on public datasets (layer 2) and the
opt-in metrics from beta installations (layer 3) are described with their jobs.

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
describe a distribution of homes, not one home. The parameter ranges are placeholders
until the nightly job estimates them from IDEAL, REFIT, UK-DALE and CASAS; a real
installation of a developer is never a source of parameters.

Every pair of series has a truth class: `duplicate`, `direct`, `indirect` (through a
third series), `common` (hidden driver), `weather` (only outside temperature or
sunshine in common) or `unrelated`.

## Metrics

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

## Gate 1c criterion 6 (ADR 062, ADR 064)

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
