"""Validation of the analyses on public datasets (layer 2 of the evaluation, ADR 062
point 8b, ADR 065).

Real data have no known truth, so the analyses are checked against a known null:
series of *different* households cannot be related. ``crosshouse`` assembles mixed
households from the houses of a public dataset, each house shifted in time by whole
weeks so that they share neither weather nor dates, runs the correlation and anomaly
analyses and counts the findings between houses, which are all false. Planted episodes
in the real series measure the recall of the anomaly analysis.

The datasets are downloaded by the nightly validation workflow and checked against the
SHA-256 in ``datasets.json``; nothing from them is stored in the repository and only
aggregate metrics leave the run.
"""
