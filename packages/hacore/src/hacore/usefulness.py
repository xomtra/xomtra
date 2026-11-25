"""Usefulness of a finding and its conservative ranking R0 (ADR 062 point 9, ADR 068).

A finding that is statistically real and direct can still be trivial (two sensors of
one device) or of no use (nothing anyone could do about it). This module scores every
finding with the parts of the formal definition in the methodology:

* **strength S** in [0, 1], monotone in the effect above its minimum: correlation
  (|r| - 0.3) / 0.7, anomaly (|z| - 3) / 7, change point (d - 2) / 6;
* **evidence E** in [0, 1] = log(q* / q) / log(q* / q_min), a function of q only, with
  ``q*`` the target FDR of the type and ``q_min`` a fixed constant of the method;
* **triviality T** (codes T1-T6, relations only): a trivial relation is flagged
  ``trivial`` and kept out of the ranking (shown only with all flags);
* **non-triviality N** in [0, 1] = 1 - share of shared metadata of the two entities
  (group, kind, source, parent); 1 for a finding of one entity;
* **actionability A**: a category of the rulebook for the entity's family and the
  finding type (``fault``, ``savings``, ``automation``, ``behaviour``, ``none``), or
  ``unclassified`` without a rule;
* **R0** = sqrt(S * E), the ranking until a learnt model passes its gate; ties are
  broken by the rule's priority.

The module knows nothing about the domain: an adapter (``hacore.actionability`` for
Home Assistant) gives the metadata of the entities and the rulebook.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from hacore.significance import NULL_PAIRS, NULL_SHIFTS

CORRELATION = "correlation"
ANOMALY = "anomaly"
CHANGE_POINT = "change_point"

TRIVIAL = "trivial"
NONE = "none"
UNCLASSIFIED = "unclassified"
# Order of the categories when two rules of equal priority apply (ADR 062, 8.5).
CATEGORIES = ("fault", "savings", "automation", "behaviour", NONE)

# Smallest q of a type: fixed constants of the method, so that E is comparable between
# runs and installations (methodology 8.2). Correlations: one over the null sample of
# shifted pairs. Anomalies and change points pool their null over all series and have
# a generalised Pareto tail beyond it; 1e-6 is the order of a pooled sample of a large
# installation (a smaller q is clipped to E = 1).
Q_MIN: Mapping[str, float] = {
    CORRELATION: 1 / (NULL_PAIRS * NULL_SHIFTS + 1),
    ANOMALY: 1e-6,
    CHANGE_POINT: 1e-6,
}

# Strength: (|effect| - minimum) / span, clipped to [0, 1].
STRENGTH: Mapping[str, tuple[float, float]] = {
    CORRELATION: (0.3, 0.7),
    ANOMALY: (3.0, 7.0),
    CHANGE_POINT: (2.0, 6.0),
}

# Duplicate measurement of one quantity (T2).
DUPLICATE_R = 0.95
# Collection artefact (T6): share of missing hours the two series have in common.
MISSING_OVERLAP = 0.8


@dataclass(frozen=True, slots=True)
class Meta:
    """What an adapter knows about one entity."""

    group: str | None = None  # a room, a zone
    kind: str | None = None  # the measured quantity (device class)
    source: str | None = None  # the integration that collects it
    parent: str | None = None  # the device it belongs to
    family: str | None = None  # the rulebook's entity family
    derived: bool = False  # computed from other entities (a helper)


@dataclass(frozen=True, slots=True)
class Rule:
    category: str
    priority: int = 1


@dataclass(frozen=True, slots=True)
class Rulebook:
    """Actionability rules: (family, finding type) -> category, and the explicit
    physical links between families whose relation is trivial (T5)."""

    version: str = "0.0.0"
    rules: Mapping[tuple[str, str], Rule] = field(default_factory=dict)
    links: frozenset[frozenset[str]] = frozenset()

    def rule(self, family: str | None, finding_type: str) -> Rule | None:
        return None if family is None else self.rules.get((family, finding_type))

    def linked(self, a: str | None, b: str | None) -> bool:
        return a is not None and b is not None and frozenset((a, b)) in self.links


def strength(finding_type: str, score: float | None) -> float:
    if score is None or not math.isfinite(score):
        return 0.0
    low, span = STRENGTH[finding_type]
    return min(max((abs(score) - low) / span, 0.0), 1.0)


def evidence(finding_type: str, q: float | None, q_target: float) -> float:
    q_min = Q_MIN[finding_type]
    if q is None or not math.isfinite(q) or q_target <= q_min:
        return 0.0
    q = max(q, 1e-300)
    return min(max(math.log(q_target / q) / math.log(q_target / q_min), 0.0), 1.0)


def nontriviality(a: Meta | None, b: Meta | None) -> float:
    """1 - share of the four metadata the two entities share (unknown = not shared)."""
    if a is None or b is None:
        return 1.0
    pairs = ((a.group, b.group), (a.kind, b.kind), (a.source, b.source), (a.parent, b.parent))
    return 1.0 - sum(x is not None and x == y for x, y in pairs) / 4


def _same(x: str | None, y: str | None) -> bool:
    return x is not None and x == y


def trivial_rule(
    f: Mapping[str, Any],
    a: Meta | None,
    b: Meta | None,
    rulebook: Rulebook,
    missing_overlap: float | None = None,
) -> str | None:
    """The first triviality code (T1-T6) that applies to a relation, or None."""
    flags = set(f.get("flags") or ())
    a, b = a or Meta(), b or Meta()
    lag = abs(int(f.get("lag_hours") or 0))
    r = abs(float(f.get("score") or 0.0))
    checks = (
        # T1 one device
        ("T1", "same_device" in flags or _same(a.parent, b.parent)),
        # T2 one quantity measured twice
        (
            "T2",
            "duplicate_suspect" in flags
            or (lag == 0 and r >= DUPLICATE_R and _same(a.kind, b.kind)),
        ),
        # T3 a helper and (likely) its source: same family, at most an hour apart
        ("T3", a.derived != b.derived and lag <= 1 and _same(a.family, b.family)),
        # T4 one quantity twice in one room
        ("T4", _same(a.group, b.group) and _same(a.kind, b.kind)),
        # T5 known physics of the rulebook
        ("T5", rulebook.linked(a.family, b.family)),
        # T6 collection artefact: one integration, same outages
        (
            "T6",
            lag == 0
            and _same(a.source, b.source)
            and missing_overlap is not None
            and missing_overlap >= MISSING_OVERLAP,
        ),
    )
    return next((code for code, hit in checks if hit), None)


@dataclass(frozen=True, slots=True)
class Scorer:
    meta: Mapping[str, Meta] = field(default_factory=dict)
    rulebook: Rulebook = field(default_factory=Rulebook)

    @property
    def ranking_version(self) -> str:
        return f"r0-{self.rulebook.version}"

    def action(self, finding_type: str, entities: Iterable[str]) -> Rule:
        """The rule with the highest priority among the entities' families."""
        rules = [
            r
            for e in entities
            if (r := self.rulebook.rule(self.meta.get(e, Meta()).family, finding_type))
        ]
        if not rules:
            return Rule(UNCLASSIFIED, 0)
        return max(rules, key=lambda r: (r.priority, -_category_order(r.category)))

    def score(
        self,
        f: dict[str, Any],
        finding_type: str,
        q_target: float,
        missing_overlap: float | None = None,
    ) -> dict[str, Any]:
        """Add the usefulness columns to finding ``f`` (and the ``trivial`` flag)."""
        a = self.meta.get(f["entity_id"])
        related = f.get("related_entity_id")
        b = self.meta.get(related) if related else None
        f["strength"] = round(strength(finding_type, f.get("score")), 4)
        f["evidence"] = round(evidence(finding_type, f.get("q_value"), q_target), 4)
        f["nontriviality"] = 1.0 if not related else nontriviality(a, b)
        code = (
            trivial_rule(f, a, b, self.rulebook, missing_overlap)
            if finding_type == CORRELATION and related
            else None
        )
        f["trivial_rule"] = code
        if code is not None and TRIVIAL not in f["flags"]:
            f["flags"] = [*f["flags"], TRIVIAL]
        rule = self.action(finding_type, [e for e in (f["entity_id"], related) if e])
        f["action_category"] = rule.category
        f["_priority"] = rule.priority
        f["usefulness"] = round(math.sqrt(f["strength"] * f["evidence"]), 4)
        f["rules_version"] = self.rulebook.version
        f["ranking_version"] = self.ranking_version
        return f


def _category_order(category: str) -> int:
    return CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES)


def sort_key(f: Mapping[str, Any]) -> tuple[float, int, int, float]:
    """R0 descending, ties by rule priority, category order, then the raw effect."""
    return (
        -float(f.get("usefulness") or 0.0),
        -int(f.get("_priority") or 0),
        _category_order(str(f.get("action_category") or UNCLASSIFIED)),
        -abs(float(f.get("score") or 0.0)),
    )


EMPTY = Scorer()
