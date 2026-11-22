"""Snapshot of Home Assistant registries (floors, areas, labels, devices, entities).

The registries are the backbone of the catalog: they give each entity its device, area,
floor and labels. Only fields needed downstream are kept; unknown fields are ignored so
that newer Home Assistant versions do not break parsing.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hacore.client import HAClient

type JSON = dict[str, Any]


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


@dataclass(frozen=True, slots=True)
class Floor:
    floor_id: str
    name: str
    level: int | None = None

    @classmethod
    def from_ha(cls, raw: JSON) -> Floor:
        level = raw.get("level")
        return cls(
            floor_id=raw["floor_id"],
            name=raw.get("name") or raw["floor_id"],
            level=level if isinstance(level, int) else None,
        )


@dataclass(frozen=True, slots=True)
class Area:
    area_id: str
    name: str
    floor_id: str | None = None
    labels: tuple[str, ...] = ()

    @classmethod
    def from_ha(cls, raw: JSON) -> Area:
        return cls(
            area_id=raw["area_id"],
            name=raw.get("name") or raw["area_id"],
            floor_id=_str_or_none(raw.get("floor_id")),
            labels=_tuple(raw.get("labels")),
        )


@dataclass(frozen=True, slots=True)
class Label:
    label_id: str
    name: str

    @classmethod
    def from_ha(cls, raw: JSON) -> Label:
        return cls(label_id=raw["label_id"], name=raw.get("name") or raw["label_id"])


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    name: str | None
    manufacturer: str | None = None
    model: str | None = None
    area_id: str | None = None
    labels: tuple[str, ...] = ()
    disabled: bool = False

    @classmethod
    def from_ha(cls, raw: JSON) -> Device:
        return cls(
            device_id=raw["id"],
            name=_str_or_none(raw.get("name_by_user")) or _str_or_none(raw.get("name")),
            manufacturer=_str_or_none(raw.get("manufacturer")),
            model=_str_or_none(raw.get("model")),
            area_id=_str_or_none(raw.get("area_id")),
            labels=_tuple(raw.get("labels")),
            disabled=raw.get("disabled_by") is not None,
        )


@dataclass(frozen=True, slots=True)
class Entity:
    entity_id: str
    platform: str | None = None
    device_id: str | None = None
    area_id: str | None = None
    name: str | None = None
    original_name: str | None = None
    device_class: str | None = None
    original_device_class: str | None = None
    unit_of_measurement: str | None = None
    entity_category: str | None = None
    labels: tuple[str, ...] = ()
    disabled: bool = False
    hidden: bool = False

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @classmethod
    def from_ha(cls, raw: JSON) -> Entity:
        return cls(
            entity_id=raw["entity_id"],
            platform=_str_or_none(raw.get("platform")),
            device_id=_str_or_none(raw.get("device_id")),
            area_id=_str_or_none(raw.get("area_id")),
            name=_str_or_none(raw.get("name")),
            original_name=_str_or_none(raw.get("original_name")),
            device_class=_str_or_none(raw.get("device_class")),
            original_device_class=_str_or_none(raw.get("original_device_class")),
            unit_of_measurement=_str_or_none(raw.get("unit_of_measurement")),
            entity_category=_str_or_none(raw.get("entity_category")),
            labels=_tuple(raw.get("labels")),
            disabled=raw.get("disabled_by") is not None,
            hidden=raw.get("hidden_by") is not None,
        )


@dataclass(slots=True)
class RegistrySnapshot:
    """All registries fetched at one point in time, with resolution helpers."""

    fetched_at: datetime
    floors: dict[str, Floor] = field(default_factory=dict)
    areas: dict[str, Area] = field(default_factory=dict)
    labels: dict[str, Label] = field(default_factory=dict)
    devices: dict[str, Device] = field(default_factory=dict)
    entities: dict[str, Entity] = field(default_factory=dict)

    def area_of(self, entity_id: str) -> Area | None:
        """Effective area: the entity's own area overrides the device area."""
        entity = self.entities.get(entity_id)
        if entity is None:
            return None
        area_id = entity.area_id
        if area_id is None and entity.device_id is not None:
            device = self.devices.get(entity.device_id)
            area_id = device.area_id if device else None
        return self.areas.get(area_id) if area_id else None

    def floor_of(self, entity_id: str) -> Floor | None:
        area = self.area_of(entity_id)
        if area is None or area.floor_id is None:
            return None
        return self.floors.get(area.floor_id)

    def labels_of(self, entity_id: str) -> set[str]:
        """Label ids of the entity, its device and its effective area."""
        entity = self.entities.get(entity_id)
        if entity is None:
            return set()
        result = set(entity.labels)
        if entity.device_id and (device := self.devices.get(entity.device_id)):
            result.update(device.labels)
        if area := self.area_of(entity_id):
            result.update(area.labels)
        return result

    def domain_counts(self, *, include_disabled: bool = False) -> dict[str, int]:
        counts = Counter(
            e.domain for e in self.entities.values() if include_disabled or not e.disabled
        )
        return dict(sorted(counts.items()))


REGISTRY_COMMANDS = {
    "floors": "config/floor_registry/list",
    "areas": "config/area_registry/list",
    "labels": "config/label_registry/list",
    "devices": "config/device_registry/list",
    "entities": "config/entity_registry/list",
}


async def fetch_registries(client: HAClient) -> RegistrySnapshot:
    """Fetch all registries concurrently over one connection."""
    floors, areas, labels, devices, entities = await asyncio.gather(
        *(client.send_command(cmd) for cmd in REGISTRY_COMMANDS.values())
    )
    return RegistrySnapshot(
        fetched_at=datetime.now(UTC),
        floors={f.floor_id: f for f in map(Floor.from_ha, floors)},
        areas={a.area_id: a for a in map(Area.from_ha, areas)},
        labels={lb.label_id: lb for lb in map(Label.from_ha, labels)},
        devices={d.device_id: d for d in map(Device.from_ha, devices)},
        entities={e.entity_id: e for e in map(Entity.from_ha, entities)},
    )
