"""Home Assistant area of each entity for the analyses (ADR 064).

Relations between two entities in one area (motion, light and CO2 of one room) are
explained by the room's occupancy: the correlation analysis flags them ``same_area``.
The area of an entity is its own area in HA, else the area of its device. Where neither
is set, the entity id and name are matched against the areas of the installation (their
ids, names and aliases, without diacritics): a single matching area is used with the
source ``name``; no match or two different areas leave the entity without an area.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import duckdb

SOURCE_HA = "ha"
SOURCE_NAME = "name"
MIN_NAME_CHARS = 3


@dataclass(frozen=True, slots=True)
class AreaInfo:
    area_id: str
    names: tuple[str, ...] = ()  # name and aliases


@dataclass(frozen=True, slots=True)
class EntityArea:
    area_id: str
    source: str  # SOURCE_HA | SOURCE_NAME


def tokens(text: str) -> tuple[str, ...]:
    """Lower-case ASCII words of a text (diacritics removed, any separator)."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return tuple(t for t in re.split(r"[^a-z0-9]+", plain.lower()) if t)


def _contains(words: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return n > 0 and any(words[i : i + n] == phrase for i in range(len(words) - n + 1))


def match_area(text: str, areas: Iterable[AreaInfo]) -> str | None:
    """The one area whose id, name or alias appears as whole words in ``text``; the
    longest phrase wins ties between overlapping names of different areas (``koupelna``
    vs ``koupelna_dole``); None when nothing or two different areas match equally."""
    words = tokens(text)
    best: dict[str, int] = {}
    for area in areas:
        for phrase in {tokens(area.area_id), *(tokens(n) for n in area.names)}:
            # Very short names ("wc", "1") would match by accident.
            if len("".join(phrase)) >= MIN_NAME_CHARS and _contains(words, phrase):
                best[area.area_id] = max(best.get(area.area_id, 0), len(phrase))
    if not best:
        return None
    top = max(best.values())
    winners = [a for a, n in best.items() if n == top]
    return winners[0] if len(winners) == 1 else None


def resolve(
    entities: Iterable[tuple[str, str | None, str | None]], areas: Iterable[AreaInfo]
) -> dict[str, EntityArea]:
    """Area of each entity from ``(entity_id, area_id, name)``; entities without one
    are left out."""
    known = list(areas)
    out: dict[str, EntityArea] = {}
    for entity_id, area_id, name in entities:
        if area_id:
            out[entity_id] = EntityArea(area_id, SOURCE_HA)
            continue
        object_id = entity_id.split(".", 1)[-1]
        found = match_area(object_id, known) or (match_area(name, known) if name else None)
        if found:
            out[entity_id] = EntityArea(found, SOURCE_NAME)
    return out


def same_area(areas: Mapping[str, EntityArea], a: str, b: str) -> bool:
    x, y = areas.get(a), areas.get(b)
    return x is not None and y is not None and x.area_id == y.area_id


def load(con: duckdb.DuckDBPyConnection, alias: str) -> dict[str, EntityArea]:
    """Areas of the entities in the lake (``silver.entities``, area registry)."""

    def exists(table: str) -> bool:
        row = con.execute(
            "SELECT count(*) FROM (SELECT table_name AS t, schema_name AS s, "
            "database_name AS d FROM duckdb_tables() UNION ALL SELECT view_name, "
            "schema_name, database_name FROM duckdb_views()) "
            "WHERE d = $d AND s = 'silver' AND t = $t",
            {"d": alias, "t": table},
        ).fetchone()
        return bool(row and row[0])

    if not exists("entities"):
        return {}
    infos: list[AreaInfo] = []
    if exists("registry_current"):
        rows = con.execute(
            "SELECT item_id, payload ->> 'name', "  # noqa: S608 - alias is ours
            "coalesce(CAST(payload -> 'aliases' AS VARCHAR[]), []) "
            f"FROM {alias}.silver.registry_current WHERE registry = 'areas'"
        ).fetchall()
        infos = [
            AreaInfo(str(i), tuple(str(x) for x in [n, *(al or [])] if x)) for i, n, al in rows
        ]
    present = {
        str(r[0])
        for r in con.execute(
            "SELECT column_name FROM duckdb_columns() "
            "WHERE database_name = $d AND schema_name = 'silver' AND table_name = 'entities'",
            {"d": alias},
        ).fetchall()
    }
    cols = ", ".join(c if c in present else f"NULL AS {c}" for c in ("area_id", "name"))
    entities = con.execute(
        f"SELECT entity_id, {cols} FROM {alias}.silver.entities"  # noqa: S608 - alias is ours
    ).fetchall()
    return resolve(((str(e), a, n) for e, a, n in entities), infos)
