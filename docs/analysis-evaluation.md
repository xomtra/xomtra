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

Each metric is reported per archetype and size and as the 90th percentile over the
households of a cell.
