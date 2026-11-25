"""Domain context of the analysed series and the rules that explain relations by it
(ADR 064).

The analyses know nothing about the domain. A ``Context`` gives them two facts that
statistics cannot learn from the data:

* **groups** - entity -> group (a room in a home, a zone of a building, a production
  line): a relation inside one group is explained by what the group shares (its
  occupancy, its machine) and is flagged ``group_flag``;
* **drivers** - observed entities that drive a whole set of others (presence of the
  residents, the shift plan of a factory): a relation explained by one of them (it is in
  ``explained_by``) is flagged ``driver_flag``; a relation with a driver itself is
  direct, never a hidden ``common_factor``.

An adapter fills the context for its domain and names the flags (``hacore.areas`` for
Home Assistant: ``same_area``, ``house_context``). Explained relations are kept and
ranked after the unflagged ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

COMMON_FACTOR = "common_factor"


@dataclass(frozen=True, slots=True)
class Context:
    groups: Mapping[str, str] = field(default_factory=dict)
    drivers: frozenset[str] = frozenset()
    group_flag: str = "same_group"
    driver_flag: str = "context_driver"

    def same_group(self, a: str, b: str) -> bool:
        ga = self.groups.get(a)
        return ga is not None and ga == self.groups.get(b)

    def explain(self, a: str, b: str, flags: list[str], explained_by: list[str]) -> list[str]:
        """Flags of the relation a - b after the context rules."""
        out = list(flags)
        if self.same_group(a, b):
            out.append(self.group_flag)
        if a in self.drivers or b in self.drivers:
            # An observed driver is not hidden: its relations are direct even when the
            # global factors (which it largely is) absorb them.
            out = [f for f in out if f != COMMON_FACTOR]
        elif self.group_flag not in out and any(e in self.drivers for e in explained_by):
            out.append(self.driver_flag)
        return out


EMPTY = Context()
