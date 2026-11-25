"""Home Assistant adapter of the usefulness score (ADR 062 point 9, ADR 068).

Reads the public rulebook ``rules/actionability.toml``, sorts every entity into one of
its families (by domain, device class, unit, platform and words of its id and name)
and gives ``hacore.usefulness`` the metadata of the entities in the lake: area (as
resolved by ``hacore.areas``), device class, integration, device and family.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

import duckdb

from hacore import areas
from hacore.usefulness import CATEGORIES, Meta, Rule, Rulebook, Scorer

FINDING_TYPES = ("correlation", "anomaly", "change_point")
OTHER = "other"


@dataclass(frozen=True, slots=True)
class Match:
    domain: frozenset[str] = frozenset()
    device_class: frozenset[str] = frozenset()
    unit: frozenset[str] = frozenset()
    platform: frozenset[str] = frozenset()
    keywords: tuple[tuple[str, ...], ...] = ()

    def fits(self, e: EntityInfo, words: tuple[str, ...]) -> bool:
        return (
            (not self.domain or e.domain in self.domain)
            and (not self.device_class or e.device_class in self.device_class)
            and (not self.unit or e.unit in self.unit)
            and (not self.platform or e.platform in self.platform)
            and (not self.keywords or any(areas.contains(words, k) for k in self.keywords))
        )


@dataclass(frozen=True, slots=True)
class Family:
    name: str
    matches: tuple[Match, ...]
    examples: tuple[Mapping[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class EntityInfo:
    entity_id: str
    name: str | None = None
    device_class: str | None = None
    unit: str | None = None
    platform: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class Rules:
    rulebook: Rulebook
    families: tuple[Family, ...]
    derived_platforms: frozenset[str]

    def family(self, e: EntityInfo) -> str:
        words = areas.tokens(f"{e.entity_id.split('.', 1)[-1]} {e.name or ''}")
        for fam in self.families:
            if any(m.fits(e, words) for m in fam.matches):
                return fam.name
        return OTHER


def _set(raw: Any) -> frozenset[str]:
    return frozenset(str(x) for x in (raw or ()))


def parse(data: Mapping[str, Any]) -> Rules:
    """Rules from the decoded TOML; ValueError on an unknown category or type."""
    families = tuple(
        Family(
            str(f["name"]),
            tuple(
                Match(
                    _set(m.get("domain")),
                    _set(m.get("device_class")),
                    _set(m.get("unit")),
                    _set(m.get("platform")),
                    tuple(areas.tokens(k) for k in m.get("keywords", ())),
                )
                for m in f["match"]
            ),
            tuple(f.get("examples", ())),
        )
        for f in data.get("family", ())
    )
    rules: dict[tuple[str, str], Rule] = {}
    for fam, by_type in data.get("rules", {}).items():
        for ftype, rule in by_type.items():
            if ftype not in FINDING_TYPES:
                raise ValueError(f"rules.{fam}: unknown finding type {ftype}")
            if rule["category"] not in CATEGORIES:
                raise ValueError(f"rules.{fam}.{ftype}: unknown category {rule['category']}")
            rules[(fam, ftype)] = Rule(str(rule["category"]), int(rule.get("priority", 1)))
    links = frozenset(frozenset(str(x) for x in pair) for pair in data.get("links", ()))
    return Rules(
        Rulebook(str(data["version"]), rules, links),
        families,
        _set(data.get("derived_platforms")),
    )


@cache
def load_rules() -> Rules:
    text = resources.files("hacore").joinpath("rules/actionability.toml").read_text("utf-8")
    return parse(tomllib.loads(text))


def metadata(
    entities: Iterable[EntityInfo],
    entity_areas: Mapping[str, areas.EntityArea],
    devices: Mapping[str, str],
    rules: Rules,
) -> dict[str, Meta]:
    return {
        e.entity_id: Meta(
            group=a.area_id if (a := entity_areas.get(e.entity_id)) else None,
            kind=e.device_class,
            source=e.platform,
            parent=devices.get(e.entity_id),
            family=rules.family(e),
            derived=e.platform in rules.derived_platforms,
        )
        for e in entities
    }


def load_scorer(con: duckdb.DuckDBPyConnection, alias: str) -> Scorer:
    """Scorer with the metadata of the entities in ``silver.entities``."""
    rules = load_rules()
    present = {
        str(r[0])
        for r in con.execute(
            "SELECT column_name FROM duckdb_columns() "
            "WHERE database_name = $d AND schema_name = 'silver' AND table_name = 'entities'",
            {"d": alias},
        ).fetchall()
    }
    if "entity_id" not in present:
        return Scorer(rulebook=rules.rulebook)
    wanted = ("name", "device_class", "unit_of_measurement", "platform", "device_id")
    cols = ", ".join(c if c in present else f"NULL AS {c}" for c in wanted)
    rows = con.execute(
        f"SELECT entity_id, {cols} FROM {alias}.silver.entities"  # noqa: S608 - alias is ours
    ).fetchall()
    infos = [EntityInfo(str(e), n, dc, u, p) for e, n, dc, u, p, _ in rows]
    devices = {str(e): str(d) for e, *_, d in rows if d is not None}
    meta = metadata(infos, areas.load(con, alias), devices, rules)
    return Scorer(meta, rules.rulebook)
