"""Confidence of a correlation run (ADR 067, low-power indicator).

False correlations in a small or poorly described installation are not weak pairs near
the threshold but strong ones with a shared cause the analysis cannot see (measured on
the synthetic households: a stricter q or minimum |r| did not lower the false discovery
rate, a complete context did). So a low-power run keeps its thresholds and says so:
``level`` is ``low`` with fewer than ``MIN_SERIES`` analysed series or less than
``MIN_GROUP_COVERAGE`` of the analysed entities in a group (an area in Home Assistant).
Readers treat its findings as hypotheses; the admin GUI lists the entities without a
group, which the user can place.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from hacore.context import Context

MIN_SERIES = 30
MIN_GROUP_COVERAGE = 0.8
NORMAL = "normal"
LOW = "low"


@dataclass(frozen=True, slots=True)
class Confidence:
    series: int
    entities: int
    grouped: int
    drivers: int
    level: str
    reasons: list[str] = field(default_factory=list)
    ungrouped: list[str] = field(default_factory=list)

    @property
    def group_coverage(self) -> float | None:
        return round(self.grouped / self.entities, 3) if self.entities else None

    def as_dict(self) -> dict[str, Any]:
        """Without entity names (logged and returned to every reader)."""
        return {
            "level": self.level,
            "reasons": self.reasons,
            "series": self.series,
            "entities": self.entities,
            "group_coverage": self.group_coverage,
            "drivers": self.drivers,
        }


def assess(
    series_entities: Iterable[str], context: Context, *, exclude: str | None = None
) -> Confidence:
    """Confidence of a run over the series of ``series_entities`` (one item per
    series, an entity may have several); ``exclude`` = the outside temperature."""
    items = [e for e in series_entities if e != exclude]
    entities = sorted(set(items))
    ungrouped = [e for e in entities if context.groups.get(e) is None]
    drivers = sum(1 for e in entities if e in context.drivers)
    grouped = len(entities) - len(ungrouped)
    reasons = []
    if len(items) < MIN_SERIES:
        reasons.append(f"only {len(items)} series analysed (< {MIN_SERIES})")
    if entities and grouped / len(entities) < MIN_GROUP_COVERAGE:
        reasons.append(
            f"{grouped / len(entities):.0%} of the entities have an area "
            f"(< {MIN_GROUP_COVERAGE:.0%})"
        )
    return Confidence(
        series=len(items),
        entities=len(entities),
        grouped=grouped,
        drivers=drivers,
        level=LOW if reasons else NORMAL,
        reasons=reasons,
        ungrouped=ungrouped,
    )
