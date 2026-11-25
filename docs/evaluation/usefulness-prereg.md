# Preregistration: usefulness of findings

Status: **registered before any rating is collected.** The hash of the commit that adds
this file is its time stamp. A change after the first rating is a new version of this
file with the reason, and the report names the version it follows.

**Version 2** (before any rating, ADR 069): the first draw on IDEAL gave about two human
ratings per test house, too few for NDCG@10 per household. The primary endpoint is
therefore NDCG@10 on AI ratings of all test findings, with a required human pool on the
top of the rankings (section 6); the human gate sample is drawn inside validation and
test (section 2).

Decisions behind it: ADR 062 point 9, ADR 068, ADR 069; the methodology (sections 8.2–8.8) is
summarised here so that this file stands on its own.

## 1. Question

Does a ranking model trained on rated findings order the findings of a household
better than the conservative ranking R0 = √(strength · evidence) that HAmmond uses
today? If not, releases keep R0.

## 2. Findings rated

- **Frame:** every finding of the v2 methods (correlations, anomalies, change points)
  with q ≤ the target q of its type, on the public datasets of the nightly validation
  (IDEAL first; REFIT, UK-DALE and CASAS when they are in the manifest), after the
  episodes are merged into events. Trivial relations are in the frame too, so that the
  filter can be checked.
- **Strata:** type (3) × trivial (2) × action category (5 incl. none and unclassified
  merged) = 30 cells, at least 20 findings per cell where a cell has them (rare
  actionable cells are oversampled), uniform over dataset and strength tercile inside
  a cell. Population estimates are weighted by 1 / selection probability. About
  **1,000 findings** for AI rating.
- **Split:** by **household** (each household in exactly one set): calibration 60 %,
  validation 20 %, test 20 %, stratified by dataset. Every house is analysed alone, in
  90-day windows: validation and test houses in their **last** window (forward
  simulation; for a history of at least 360 days it lies in the last 25 %),
  calibration houses in up to four windows without overlap over their history.
- **Tool:** `python -m hacore.public.usefulness_sample` (manual job `usefulness-sample`
  of the validation workflow). Channel names are mapped to device classes so that the
  rulebook sorts them into families; the run reports the families of the channels and
  the coverage. Inside a cell the draw alternates over the strength terciles.
- **Human sample (gate):** **200 findings**, 100 from validation and 100 from test, each
  drawn stratified inside its set (at least 5 per cell where a cell has them); the rest
  of the 1,000 comes from calibration. The AI raters rate all 1,000 (and, for section 6,
  every finding of the test houses). Weights refer to the frame of the finding's set.
  The ranking model is trained only on AI ratings of the calibration set; human and
  training ratings never overlap. A repeated gate after a changed rubric uses 100
  **new** findings from the validation reserve.
- **Seed:** the sample is drawn with seed 20261125; the list of finding ids and their
  strata is committed before rating starts.

## 3. Ratings

- **Rubric:** usefulness 1–4 (1 nothing to learn, 2 interesting but no action,
  3 worth checking, 4 act on it), trivial yes/no, action category. Piloted on 20
  findings outside the sample; the pilot may change the wording, not the scale.
- **Human panel (ground truth):** three raters (the author and two beta testers), blind
  to the ranking and the other raters, in random order. Independent ratings are kept.
  Adjudication: spread ≤ 1 → median; spread ≥ 2 → discussion and a new independent
  vote, majority of 2 of 3, else median; the author has no casting vote. Recorded in
  `adjudication.csv` (finding id, three ratings, result, rule, one-sentence reason).
- **AI raters:** three roles defined by the rubric's questions (technician: fault or
  data error?; owner: would I do something?; energy adviser: savings?). Exact model id,
  temperature 0, versioned prompt and rubric in the repository, fixed order seed, one
  finding per call, raw outputs (JSONL) kept with dates. Where available a second model
  of another vendor as a check of self-preference. The AI consensus is the median of
  the roles.

## 4. Gate for using AI ratings

All three must hold:

1. humans among themselves (independent ratings, before adjudication): Krippendorff's
   α (ordinal) ≥ 0.667;
2. AI consensus vs. human consensus: α, **lower bound of the 95 % interval ≥ 0.60**;
3. non-inferiority: α(AI, consensus) − mean α(human, consensus of the other two)
   ≥ −0.10.

Reported besides: α per component (usefulness, trivial, category), α among the AI
roles, quadratic-weighted Cohen's κ (secondary).

**Intervals:** two-sided 95 % percentile bootstrap, 2,000 resamples of **households**
(findings of one household stay together); the gate uses the lower bound.

**Sample size.** Normal approximation for weighted κ (p0 ≈ 0.8, pe ≈ 0.4): half width
≈ 1.96 · 0.67 / √n, i.e. ±0.19 at 50, ±0.13 at 100, ±0.09 at 200. Checked by
simulation (`hacore.public.ratings.simulate_alpha_power`: latent usefulness with a
household effect, SD 0.3, 20 households, three humans and the AI with independent
noise, the scale cut at fixed points, cluster bootstrap); results in section 8.

If the gate fails: the rubric is revised and the gate is repeated on a new sample, or
a model with at most five inputs is trained on human ratings only and must pass the
same test (section 6). Until then releases keep R0.

## 5. Ranking model

Ordinal logistic regression of the rating 1–4 on strength S, evidence E,
non-triviality N, action category (one-hot), finding type (one-hot) and episode length
or strength; at most 15 coefficients, L2 penalty, non-negative coefficients of S, E
and N (monotone). Trained on the AI ratings of the calibration set (only after the
gate); the penalty is chosen by grouped cross-validation over households in the
calibration set and the human ratings of the validation set. The **test set is used
once.** Coefficient signs and order must agree in ≥ 80 % of bootstrap resamples, else
the model is simplified.

## 6. Primary analysis

- **Ratings (ADR 069):** the AI consensus rates **every** finding of the test houses
  (about 2,500 at IDEAL, × 3 roles). 100 human ratings over 51 test houses are about
  two per house, too few for a ranking metric per household. The AI ratings count only
  if the gate of section 4 holds unchanged; otherwise the primary analysis is not run
  and releases keep R0.
- **Test rater:** the test set is rated by another model than the one whose ratings
  trained the ranking model, preferably of another vendor, with the frozen prompt and
  rubric; self-preference is reported. No AI rater sees strength, evidence, q, the
  score or any order (checked by a CI test of the prompt in the repository), so that its ratings do
  not follow R0 by construction.
- **Metric:** NDCG@10 per household (gain 2^(rating − 1) − 1, AI consensus rating) on
  the test set, averaged over households. Secondary: precision@10 (relevant =
  rating ≥ 3).
- **Baselines:** random order, the ranking of ADR 047 (unflagged first, |score|), R0;
  upper bound: the order of the AI consensus ratings.
- **Human pool (required, ADR 069):** in 15 test houses drawn with the seed, the panel
  rates the union of the top 10 of the model, R0 and the random order (pooling as in
  TREC, about 330–375 findings, same rubric and adjudication; findings already rated by
  the panel keep their rating). The ideal DCG is computed from the pool.
- **Success:** both hold:
  (a) AI: NDCG@10(model) − NDCG@10(R0) > 0 with the **lower bound of the 95 % paired
  cluster-bootstrap interval > 0** (1,000 resamples of households);
  (b) humans on the pool: the point estimate of the same difference ≥ 0 and the lower
  bound of its interval > −0.05 (non-inferiority).
  Otherwise the release ships R0 (`ranking_version` r0-…).
- α(AI consensus, human consensus) is reported on the pool too; if its lower bound is
  below 0.60, the AI result is reported as unreliable on the top of the ranking.
- Reported with and without the findings flagged `indirect` or `common_factor`; if
  hiding them lowers NDCG@10 (upper bound of the difference < 0), the default display
  becomes `lower` (ADR 068 point 7).

## 7. Other reports

Rulebook coverage (share of findings not `unclassified`; target ≥ 70 %), the share of
human-trivial findings among those the filter keeps and removes, events (anomaly ↔
annotated event ± 2 h, change point ± 2 days) as an independent second view where
datasets have annotations, with annotation coverage. Only aggregates are published;
ratings of public findings are published with the rubric and prompts.

## 8. Simulation of the sample size

`simulate_alpha_power` (seed 0, 100 simulations, 300 cluster-bootstrap resamples
each, 20 households, household effect SD 0.3). Rater noise 0.45 corresponds to an
expected α(AI, consensus) of about 0.80, noise 0.60 to about 0.72:

| Noise | n | Mean α | Mean half width of the 95 % interval | Share with lower bound ≥ 0.60 |
|---|---|---|---|---|
| 0.45 | 50 | 0.80 | 0.11 | 0.82 |
| 0.45 | 100 | 0.81 | 0.07 | 0.99 |
| 0.45 | 200 | 0.82 | 0.05 | 1.00 |
| 0.60 | 50 | 0.72 | 0.14 | 0.36 |
| 0.60 | 100 | 0.73 | 0.10 | 0.60 |
| 0.60 | 200 | 0.74 | 0.06 | 0.96 |

With an AI as good as α ≈ 0.72, 200 human ratings pass the gate in 96 % of the
simulations, 100 only in 60 %. The sample stays at **200**. The cluster bootstrap with
20 households gives narrower intervals than the normal approximation of section 4
(which ignored the clustering); the gate uses the bootstrap.

**Power of the human pool (condition (b) of section 6).**
`simulate_pool_power` (seed 0, 100 simulations, 300 cluster-bootstrap resamples each,
50 findings per household, three raters with noise 0.55): R0 and the model rank by the
latent usefulness plus normal noise (R0 SD 1.0; model SD 1.0, 0.8, 0.6), the pool is the
union of the top 10 of the model, R0 and a random order.

| Households | True difference NDCG@10 | Share meeting (b) |
|---|---|---|
| 10 | 0.00 | 0.35 |
| 10 | +0.05 | 0.78 |
| 10 | +0.09 | 1.00 |
| 15 | 0.00 | 0.46 |
| 15 | +0.05 | 0.91 |
| 15 | +0.09 | 1.00 |
| 20 | 0.00 | 0.42 |
| 20 | +0.05 | 0.95 |
| 20 | +0.09 | 1.00 |

A model no better than R0 is held back about half the time, which is intended (R0 is
the default and needs no human support). A model better by 0.05 passes in 78 % of the
simulations with 10 households and in 91 % with 15; the pool has **15 households**.

