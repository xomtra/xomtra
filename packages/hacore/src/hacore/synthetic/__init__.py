"""Synthetic households with known truth for evaluating the analyses (ADR 062 point 8a).

Layer 1 of the evaluation (ADEMP, Morris et al. 2019): a structural causal model of a
home generates hourly series the way xomtra sees them (``correlations.Grid``), together
with the truth: which pairs are related directly, through a third series, through a
hidden common driver or only through the weather, and where anomalies and change
points were inserted. ``hacore.synthetic.evaluate`` runs an analysis on many generated
households and reports metrics per archetype and size, and the 90th percentile over
households.

No data leaves the process and nothing is downloaded; real public datasets (layer 2)
are a separate nightly job.
"""

from hacore.synthetic.generator import (
    ARCHETYPES,
    Household,
    HouseholdSpec,
    InsertedEvent,
    Relation,
    generate,
)

__all__ = [
    "ARCHETYPES",
    "Household",
    "HouseholdSpec",
    "InsertedEvent",
    "Relation",
    "generate",
]
