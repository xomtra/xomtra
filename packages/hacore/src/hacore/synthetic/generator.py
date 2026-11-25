"""Structural causal model of a home: hourly series with known relations (ADR 062, 8a).

A household is drawn from an *archetype* with parameters sampled from ranges, so that
every seed is a different home. Hidden drivers (outside temperature, solar irradiance,
occupancy of the home and of each room) generate the sensors HAmmond sees; planted
relations of known strength and lag (automations, chains A -> C -> B and hidden common
drivers of 3-5 series) are added on top, and the rest are unrelated sensors. Realistic defects:
quantisation, missing hours, outage blocks, a Zigbee-like outage of a whole room,
duplicated entities.

The truth is a graph over observed series and hidden nodes (``Household.parents``);
``Household.relation(a, b)`` classifies any observed pair:

``duplicate``   one quantity measured twice
``direct``      one series is a parent of the other
``indirect``    one is an ancestor of the other through a third observed series
``common``      both depend on a hidden (non-weather) driver or a common observed parent
``weather``     the only shared cause is the weather (outside temperature, sunshine)
``unrelated``   no shared cause apart from the daily and weekly cycle

Ranges are literature-based placeholders until the nightly job estimates them from
the public datasets of layer 2 (IDEAL, REFIT, UK-DALE, CASAS); the homelab is never a
source of parameters.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import cached_property
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt

from hacore.correlations import Grid, Series

type FloatArray = npt.NDArray[np.float64]

OUTSIDE_TEMP = "sensor.outdoor_temperature"
SOLAR = "h:solar"
HOME = "h:home"
WEATHER_NODES = frozenset({OUTSIDE_TEMP, SOLAR})

ARCHETYPES = ("apartment", "house_heat_pump", "house_pv_battery", "power_user", "minimalist")

# Climate: (yearly mean °C, yearly amplitude, daily amplitude, latitude °).
CLIMATES: dict[str, tuple[float, float, float, float]] = {
    "europe_north": (6.0, 9.0, 3.0, 60.0),
    "europe_central": (9.0, 10.0, 4.0, 50.0),
    "europe_south": (16.0, 8.0, 5.0, 40.0),
    "usa": (13.0, 12.0, 6.0, 40.0),
    "australia": (18.0, 6.0, 6.0, -34.0),
}

# Share of the series that are room sensors, planted relations and unrelated sensors;
# the rest are the archetype's energy devices.
COMPOSITION: dict[str, tuple[float, float, float]] = {
    "apartment": (0.55, 0.15, 0.25),
    "house_heat_pump": (0.45, 0.15, 0.25),
    "house_pv_battery": (0.40, 0.15, 0.25),
    "power_user": (0.35, 0.35, 0.20),
    "minimalist": (0.60, 0.10, 0.30),
}

PERSONAS = ("worker", "home_office", "shift", "family", "retired")


class Relation(StrEnum):
    DUPLICATE = "duplicate"
    DIRECT = "direct"
    INDIRECT = "indirect"
    COMMON = "common"
    WEATHER = "weather"
    UNRELATED = "unrelated"


@dataclass(frozen=True, slots=True)
class HouseholdSpec:
    archetype: str
    n_series: int
    days: int = 90
    seed: int = 0
    climate: str | None = None  # None = drawn from CLIMATES
    timezone: str = "Europe/Prague"
    # A Monday at midnight UTC, so the yearly cycle starts in winter.
    start: datetime = datetime(2025, 1, 6, tzinfo=UTC)
    events: bool = False  # insert anomalies and change points (InsertedEvent)

    def __post_init__(self) -> None:
        if self.archetype not in ARCHETYPES:
            raise ValueError(f"unknown archetype {self.archetype!r}")
        if self.n_series < 4 or self.days < 14:
            raise ValueError("a household needs at least 4 series and 14 days")


@dataclass(frozen=True, slots=True)
class InsertedEvent:
    key: str
    kind: str  # spike | dropout | drift | level_shift
    start: int  # hour index into the grid
    end: int  # exclusive; level_shift lasts to the end of the grid
    size: float  # in standard deviations of the series before insertion


@dataclass(frozen=True, slots=True)
class Parent:
    node: str
    lag: int = 0
    weight: float = 0.0  # target |r| of a planted relation, 0 = not planted


@dataclass
class Household:
    spec: HouseholdSpec
    climate: str
    persona: str
    grid: Grid
    parents: dict[str, list[Parent]]
    duplicates: set[frozenset[str]] = field(default_factory=set)
    events: list[InsertedEvent] = field(default_factory=list)

    outside_temp: str = OUTSIDE_TEMP

    @property
    def keys(self) -> list[str]:
        return [s.key for s in self.grid.series]

    @cached_property
    def _ancestors(self) -> dict[str, frozenset[str]]:
        memo: dict[str, frozenset[str]] = {}

        def visit(node: str) -> frozenset[str]:
            if node not in memo:
                memo[node] = frozenset()  # guards against cycles (none by construction)
                out: set[str] = set()
                for p in self.parents.get(node, []):
                    out.add(p.node)
                    out |= visit(p.node)
                memo[node] = frozenset(out)
            return memo[node]

        for node in list(self.parents):
            visit(node)
        return memo

    def ancestors(self, node: str) -> frozenset[str]:
        return self._ancestors.get(node, frozenset())

    def planted_strength(self, a: str, b: str) -> float:
        """Target |r| of a planted direct relation between ``a`` and ``b``, else 0."""
        for child, parent in ((a, b), (b, a)):
            for p in self.parents.get(child, []):
                if p.node == parent and p.weight:
                    return p.weight
        return 0.0

    def relation(self, a: str, b: str) -> Relation:
        if frozenset((a, b)) in self.duplicates:
            return Relation.DUPLICATE
        pa = {p.node for p in self.parents.get(a, [])}
        pb = {p.node for p in self.parents.get(b, [])}
        if b in pa or a in pb:
            return Relation.DIRECT
        anc_a, anc_b = self.ancestors(a), self.ancestors(b)
        if a in anc_b or b in anc_a:
            return Relation.INDIRECT
        shared = (anc_a | {a}) & (anc_b | {b})
        if not shared:
            return Relation.UNRELATED
        weather = set(WEATHER_NODES)
        for w in WEATHER_NODES:
            weather |= self.ancestors(w)
        if shared - weather:
            return Relation.COMMON
        return Relation.WEATHER

    def shared_hidden(self, a: str, b: str) -> frozenset[str]:
        """Hidden drivers both series depend on (``h:...`` nodes)."""
        both = (self.ancestors(a) | {a}) & (self.ancestors(b) | {b})
        return frozenset(n for n in both if n.startswith("h:"))

    def direct_pairs(self) -> list[tuple[str, str, float, int]]:
        """Planted direct relations between observed series: (parent, child, |r|, lag)."""
        observed = set(self.keys)
        return [
            (p.node, child, p.weight, p.lag)
            for child, ps in self.parents.items()
            for p in ps
            if p.weight and child in observed and p.node in observed
        ]


# ---- building blocks -----------------------------------------------------------------


def _sigmoid(x: FloatArray) -> FloatArray:
    return 1.0 / (1.0 + np.exp(-x))


def _logit(p: FloatArray) -> FloatArray:
    p = np.clip(p, 1e-3, 1 - 1e-3)
    return np.log(p / (1 - p))


class _Builder:
    def __init__(self, spec: HouseholdSpec) -> None:
        self.spec = spec
        self.rng = np.random.default_rng([spec.seed, ARCHETYPES.index(spec.archetype)])
        self.t = spec.days * 24
        start = np.datetime64(spec.start.astimezone(UTC).replace(tzinfo=None), "us")
        self.hours = start + np.arange(self.t) * np.timedelta64(1, "h")
        tz = ZoneInfo(spec.timezone)
        local = [(spec.start + timedelta(hours=i)).astimezone(tz) for i in range(0, self.t)]
        self.local_hour = np.array([d.hour for d in local], dtype=np.int64)
        self.local_dow = np.array([d.isoweekday() for d in local], dtype=np.int64)
        self.doy = np.array([d.timetuple().tm_yday for d in local], dtype=np.float64)
        self.weekend = self.local_dow >= 6
        self.series: list[Series] = []
        self.values: list[FloatArray] = []
        self.groups: list[int] = []  # room of each series (-1 = none), for room outages
        self.parents: dict[str, list[Parent]] = {}
        self.duplicates: set[frozenset[str]] = set()
        self._names: set[str] = set()

    # -- primitives --

    def ar1(self, rho: float | FloatArray, n: int = 1) -> FloatArray:
        """Unit-variance AR(1) columns (hours x n)."""
        rho = np.broadcast_to(np.asarray(rho, dtype=np.float64), (n,))
        e = self.rng.standard_normal((self.t, n)) * np.sqrt(1 - rho**2)
        out = np.empty_like(e)
        out[0] = self.rng.standard_normal(n)
        for i in range(1, self.t):
            out[i] = rho * out[i - 1] + e[i]
        return out

    def lowpass(self, x: FloatArray, tau: float) -> FloatArray:
        """First-order (RC) response with time constant ``tau`` hours."""
        a = math.exp(-1.0 / tau)
        out = np.empty_like(x)
        out[0] = x[0]
        for i in range(1, len(x)):
            out[i] = a * out[i - 1] + (1 - a) * x[i]
        return out

    @staticmethod
    def shift(x: FloatArray, lag: int) -> FloatArray:
        if lag == 0:
            return x
        return np.concatenate([np.full(lag, x[0]), x[:-lag]])

    def name(self, base: str) -> str:
        name, i = base, 2
        while name in self._names:
            name, i = f"{base}_{i}", i + 1
        self._names.add(name)
        return name

    def add(
        self,
        entity_id: str,
        values: FloatArray,
        parents: Iterable[Parent] = (),
        *,
        metric: str = "mean",
        room: int = -1,
    ) -> str:
        s = Series(entity_id, metric, "on" if metric == "share" else None)
        self.series.append(s)
        self.values.append(np.asarray(values, dtype=np.float64))
        self.groups.append(room)
        self.parents[s.key] = list(parents)
        return s.key

    def hidden(self, node: str, parents: Iterable[Parent] = ()) -> str:
        self.parents[node] = list(parents)
        return node

    @property
    def count(self) -> int:
        return len(self.series)


# ---- drivers -------------------------------------------------------------------------


def _drivers(b: _Builder, climate: str, persona: str) -> tuple[FloatArray, FloatArray, FloatArray]:
    mean, yearly, daily, lat = CLIMATES[climate]
    sign = 1.0 if lat >= 0 else -1.0
    season = -sign * yearly * np.cos(2 * np.pi * (b.doy - 15) / 365.25)
    day = daily * np.cos(2 * np.pi * (b.local_hour - 15) / 24)
    temp_out = mean + season + day + 3.0 * b.ar1(0.95)[:, 0]

    dec = math.radians(23.44) * np.sin(2 * np.pi * (284 + b.doy) / 365.0)
    phi = math.radians(lat)
    hour_angle = np.radians(15.0 * (b.local_hour + 0.5 - 12))
    sin_el = np.sin(phi) * np.sin(dec) + np.cos(phi) * np.cos(dec) * np.cos(hour_angle)
    cloudy = np.empty(b.t, dtype=bool)
    cloudy[0] = False
    flips = b.rng.random(b.t) < 0.08
    for i in range(1, b.t):
        cloudy[i] = cloudy[i - 1] ^ flips[i]
    solar = np.maximum(sin_el, 0.0) * np.where(cloudy, 0.25, 1.0)
    solar = solar * (1 + 0.1 * b.ar1(0.7)[:, 0]).clip(0.5, 1.5)

    h = b.local_hour
    night = (h < 7) | (h >= 22)
    evening = (h >= 17) & (h < 22)
    day_hours = ~night & ~evening
    profile = {
        "worker": np.where(night, 0.97, np.where(evening, 0.85, np.where(b.weekend, 0.6, 0.12))),
        "home_office": np.where(night, 0.97, np.where(day_hours, 0.8, 0.85)),
        "shift": np.where((h >= 14) & (h < 23) & ~b.weekend, 0.15, 0.85),
        "family": np.where(night, 0.98, np.where(day_hours & ~b.weekend, 0.45, 0.85)),
        "retired": np.where(night, 0.98, 0.75),
    }[persona]
    latent = 1.5 * b.ar1(0.92)[:, 0]
    home = (b.rng.random(b.t) < _sigmoid(_logit(profile) + latent)).astype(float)
    return temp_out, solar, home


# ---- household parts -----------------------------------------------------------------

ROOM_KINDS = ("living", "kitchen", "bedroom", "bathroom", "office", "kids")
ROOM_SENSORS = ("temperature", "humidity", "motion", "light", "co2", "door")
MINIMAL_SENSORS = ("motion", "door")


def _room(
    b: _Builder,
    k: int,
    *,
    temp_out: FloatArray,
    solar: FloatArray,
    home: FloatArray,
    budget: int,
    minimal: bool,
) -> int:
    kind = ROOM_KINDS[k % len(ROOM_KINDS)]
    h = b.local_hour
    pref = {
        "living": (h >= 17) & (h < 23),
        "kitchen": ((h >= 6) & (h < 9)) | ((h >= 17) & (h < 20)),
        "bedroom": (h < 7) | (h >= 22),
        "bathroom": ((h >= 6) & (h < 8)) | ((h >= 21) & (h < 23)),
        "office": (h >= 8) & (h < 17) & ~b.weekend,
        "kids": (h >= 14) & (h < 21),
    }[kind]
    p = np.where(pref, 0.7, 0.12)
    occ = home * (b.rng.random(b.t) < _sigmoid(_logit(p) + 1.2 * b.ar1(0.8)[:, 0]))
    occ_node = b.hidden(f"h:room_{k}", [Parent(HOME)])
    dark = solar < 0.05
    room_name = f"{kind}_{k}" if k >= len(ROOM_KINDS) else kind
    sensors = MINIMAL_SENSORS if minimal else ROOM_SENSORS
    rest = list(sensors[1:])
    b.rng.shuffle(rest)  # first sensor always present, the rest in random order
    order = [sensors[0], *rest]
    added = 0
    for sensor in order:
        if added >= budget:
            break
        noise = b.ar1(b.rng.uniform(0.6, 0.95))[:, 0]
        if sensor == "temperature":
            v = (
                b.rng.uniform(19, 23)
                + b.rng.uniform(0.05, 0.25) * b.lowpass(temp_out, 8.0)
                + b.rng.uniform(0.2, 0.8) * b.lowpass(occ, 3.0)
                + 0.25 * noise
            )
            v = np.round(v, 1)
            b.add(
                b.name(f"sensor.{room_name}_temperature"),
                v,
                [Parent(OUTSIDE_TEMP), Parent(occ_node)],
                room=k,
            )
        elif sensor == "humidity":
            shower = (kind == "bathroom") * occ * (b.rng.random(b.t) < 0.5) * 20
            v = np.round(
                45 + 6 * b.lowpass(occ, 2.0) + shower - 0.4 * b.lowpass(temp_out, 24.0) + 2 * noise,
                0,
            )
            b.add(
                b.name(f"sensor.{room_name}_humidity"),
                v,
                [Parent(OUTSIDE_TEMP), Parent(occ_node)],
                room=k,
            )
        elif sensor == "motion":
            v = np.clip(occ * b.rng.uniform(0.2, 0.7) + 0.05 * occ * noise, 0, 1)
            b.add(
                b.name(f"binary_sensor.{room_name}_motion"),
                v,
                [Parent(occ_node)],
                metric="share",
                room=k,
            )
        elif sensor == "light":
            v = np.clip(occ * dark * b.rng.uniform(0.5, 1.0) + 0.02 * noise * occ, 0, 1)
            b.add(
                b.name(f"light.{room_name}"),
                v,
                [Parent(occ_node), Parent(SOLAR)],
                metric="share",
                room=k,
            )
        elif sensor == "co2":
            v = np.round(450 + 500 * b.lowpass(occ, 2.0) + 30 * noise, 0)
            b.add(b.name(f"sensor.{room_name}_co2"), v, [Parent(occ_node)], room=k)
        else:  # door
            v = np.clip(occ * (b.rng.random(b.t) < 0.3) * b.rng.uniform(0.02, 0.2), 0, 1)
            b.add(
                b.name(f"binary_sensor.{room_name}_door"),
                v,
                [Parent(occ_node)],
                metric="share",
                room=k,
            )
        added += 1
    return added


def _energy(
    b: _Builder,
    archetype: str,
    *,
    temp_out: FloatArray,
    solar: FloatArray,
    home: FloatArray,
    budget: int,
) -> None:
    parts: list[tuple[str, FloatArray]] = []
    fridge = np.clip(0.06 + 0.01 * b.ar1(0.3)[:, 0], 0, None)
    if budget > 0:
        parts.append((b.add(b.name("sensor.fridge_power"), fridge * 1000), fridge))
    washer_on = (home > 0) & (b.local_hour >= 8) & (b.local_hour < 21) & (b.rng.random(b.t) < 0.03)
    washer = b.lowpass(washer_on.astype(float), 1.5) * 0.5
    if budget > 1:
        parts.append(
            (b.add(b.name("sensor.washing_machine_power"), washer * 1000, [Parent(HOME)]), washer)
        )
    hp = np.zeros(b.t)
    if archetype == "house_heat_pump" and budget > 2:
        hp = np.clip(0.25 * (18 - b.lowpass(temp_out, 12.0)), 0, None) * (
            1 + 0.15 * b.ar1(0.8)[:, 0]
        )
        parts.append(
            (b.add(b.name("sensor.heat_pump_power"), hp * 1000, [Parent(OUTSIDE_TEMP)]), hp)
        )
    base = 0.25 + 0.3 * home + 0.05 * b.ar1(0.6)[:, 0]
    consumption = base + sum(p[1] for p in parts)
    if budget > len(parts):
        cons_key = b.add(
            b.name("sensor.house_power"),
            np.round(consumption * 1000, 0),
            [Parent(HOME)] + [Parent(k) for k, _ in parts],
        )
        if budget > len(parts) + 1:
            energy_key = b.add(
                b.name("sensor.house_energy"), consumption, [Parent(cons_key)], metric="delta"
            )
            b.duplicates.add(frozenset((cons_key, energy_key)))
        if archetype == "house_pv_battery" and budget > len(parts) + 3:
            pv = b.rng.uniform(3, 8) * solar
            pv_key = b.add(b.name("sensor.pv_power"), np.round(pv * 1000, 0), [Parent(SOLAR)])
            battery = np.clip(pv - consumption, -2.5, 2.5)
            b.add(
                b.name("sensor.battery_power"),
                np.round(battery * 1000, 0),
                [Parent(pv_key), Parent(cons_key)],
            )
            grid = consumption - pv + battery
            b.add(
                b.name("sensor.grid_power"),
                np.round(grid * 1000, 0),
                [Parent(pv_key), Parent(cons_key)],
            )


def _planted(b: _Builder, archetype: str, budget: int) -> None:
    """Planted relations of known strength: direct pairs, chains A->C->B, hidden
    common drivers. Power users have mostly lag-0 automations."""
    i = 0
    while budget >= 2:
        kind = str(b.rng.choice(["direct", "chain", "common"], p=[0.5, 0.25, 0.25]))
        if kind in ("chain", "common") and budget < 3:
            kind = "direct"
        lag0 = archetype == "power_user" and b.rng.random() < 0.7

        def lag(lag0: bool = lag0) -> int:
            return 0 if lag0 else int(b.rng.integers(0, 25))

        def strength() -> float:
            return float(b.rng.uniform(0.2, 0.8))

        src = b.ar1(b.rng.uniform(0.3, 0.95))[:, 0]
        if kind == "direct":
            w, lg = strength(), lag()
            a = b.add(b.name(f"sensor.planted_{i}_a"), src)
            y = w * b.shift(src, lg) + math.sqrt(1 - w * w) * b.ar1(b.rng.uniform(0.3, 0.9))[:, 0]
            b.add(b.name(f"sensor.planted_{i}_b"), y, [Parent(a, lg, w)])
            budget -= 2
        elif kind == "chain":
            w1, w2, l1, l2 = strength(), strength(), lag(), lag()
            a = b.add(b.name(f"sensor.planted_{i}_a"), src)
            c = w1 * b.shift(src, l1) + math.sqrt(1 - w1 * w1) * b.ar1(0.7)[:, 0]
            ck = b.add(b.name(f"sensor.planted_{i}_c"), c, [Parent(a, l1, w1)])
            y = w2 * b.shift(c, l2) + math.sqrt(1 - w2 * w2) * b.ar1(0.7)[:, 0]
            b.add(b.name(f"sensor.planted_{i}_b"), y, [Parent(ck, l2, w2)])
            budget -= 3
        else:
            # A hidden driver of 3-5 series. With two or three children no
            # observational test tells it from direct relations (no tetrad); the
            # evaluation reports those apart.
            node = b.hidden(f"h:planted_{i}")
            children = min(budget, int(b.rng.integers(3, 6)))
            for side in "abdefg"[:children]:
                w, lg = strength(), lag()
                y = w * b.shift(src, lg) + math.sqrt(1 - w * w) * b.ar1(0.7)[:, 0]
                b.add(b.name(f"sensor.planted_{i}_{side}"), y, [Parent(node, lg, w)])
            budget -= children
        i += 1


def _unrelated(b: _Builder, budget: int) -> None:
    for i in range(budget):
        amp = b.rng.uniform(0, 1.0)
        daily = amp * np.sin(2 * np.pi * (b.local_hour + b.rng.uniform(0, 24)) / 24)
        x = daily + b.ar1(b.rng.uniform(0.2, 0.97))[:, 0]
        if b.rng.random() < 0.3:
            b.add(
                b.name(f"binary_sensor.other_{i}"), np.clip(_sigmoid(x) - 0.2, 0, 1), metric="share"
            )
        else:
            b.add(b.name(f"sensor.other_{i}"), np.round(20 + 3 * x, 2))


# ---- defects and events -------------------------------------------------------------


def _defects(b: _Builder, values: FloatArray) -> FloatArray:
    t, n = values.shape
    out = values.copy()
    out[b.rng.random((t, n)) < b.rng.uniform(0, 0.02, n)] = np.nan
    blocks_per_series = np.asarray(b.rng.poisson(b.spec.days / 90, n))
    for j in range(n):
        for _ in range(int(blocks_per_series[j])):
            length = int(b.rng.integers(1, 73))
            s = int(b.rng.integers(0, max(t - length, 1)))
            out[s : s + length, j] = np.nan
    rooms = sorted({g for g in b.groups if g >= 0})
    groups = np.array(b.groups)
    for room in rooms:
        for _ in range(int(b.rng.poisson(0.5 * b.spec.days / 90))):
            length = int(b.rng.integers(2, 49))
            s = int(b.rng.integers(0, max(t - length, 1)))
            out[s : s + length, groups == room] = np.nan
    # Every series reaches the end of the window (removed entities are another test).
    out[-72:] = values[-72:]
    return out


def _duplicate(b: _Builder) -> None:
    temps = [s.key for s in b.series if s.entity_id.endswith("_temperature")]
    if not temps:
        return
    key = temps[int(b.rng.integers(0, len(temps)))]
    j = [s.key for s in b.series].index(key)
    v = b.values[j] + 0.05 * b.rng.standard_normal(b.t)
    dup = b.add(
        b.name(f"{b.series[j].entity_id}_zigbee"), np.round(v, 1), [Parent(key)], room=b.groups[j]
    )
    b.duplicates.add(frozenset((key, dup)))


def _insert_events(b: _Builder, values: FloatArray) -> list[InsertedEvent]:
    """Anomalies and change points in about a tenth of the measurements, in the last
    30 days (anomalies) or the last 60 days (level shifts)."""
    t = values.shape[0]
    events: list[InsertedEvent] = []
    candidates = [j for j, s in enumerate(b.series) if s.metric == "mean" and s.key != OUTSIDE_TEMP]
    if not candidates:
        return events
    n_events = max(1, len(candidates) // 10)
    chosen = b.rng.choice(candidates, size=min(n_events, len(candidates)), replace=False)
    for j in chosen:
        x = values[:, j]
        sd = float(np.nanstd(x)) or 1.0
        kind = str(b.rng.choice(["spike", "dropout", "drift", "level_shift"]))
        if kind == "level_shift":
            size = float(b.rng.uniform(1, 5))
            start = t - int(b.rng.integers(7 * 24, 60 * 24))
            end = t
            x[start:] += size * sd
        else:
            size = float(b.rng.uniform(3, 10))
            length = int(b.rng.integers(1, 7)) if kind == "spike" else int(b.rng.integers(6, 73))
            start = t - int(b.rng.integers(length + 1, 30 * 24))
            end = start + length
            if kind == "spike":
                x[start:end] += size * sd
            elif kind == "dropout":
                x[start:end] = 0.0
                size = float(abs(np.nanmean(values[:start, j])) / sd)
            else:
                x[start:end] += np.linspace(0, size * sd, end - start)
        events.append(InsertedEvent(b.series[j].key, kind, start, end, round(size, 2)))
    return events


# ---- entry point --------------------------------------------------------------------


def generate(spec: HouseholdSpec) -> Household:
    """One synthetic household; the same spec always gives the same household."""
    b = _Builder(spec)
    climate = spec.climate or str(b.rng.choice(sorted(CLIMATES)))
    persona = str(b.rng.choice(PERSONAS))
    temp_out, solar, home = _drivers(b, climate, persona)
    b.hidden(SOLAR)
    b.hidden(HOME)
    b.add(OUTSIDE_TEMP, np.round(temp_out, 1))

    n = spec.n_series - 1
    rooms_share, planted_share, unrelated_share = COMPOSITION[spec.archetype]
    n_rooms_series = max(2, round(n * rooms_share))
    n_planted = round(n * planted_share)
    n_unrelated = round(n * unrelated_share)
    n_energy = max(0, n - n_rooms_series - n_planted - n_unrelated)
    minimal = spec.archetype == "minimalist"

    if not minimal:
        _energy(b, spec.archetype, temp_out=temp_out, solar=solar, home=home, budget=n_energy)
    k = 0
    target = b.count + n_rooms_series
    while b.count < target:
        per_room = len(MINIMAL_SENSORS) if minimal else int(b.rng.integers(3, 7))
        _room(
            b,
            k,
            temp_out=temp_out,
            solar=solar,
            home=home,
            budget=min(per_room, target - b.count),
            minimal=minimal,
        )
        k += 1
    if not minimal and b.count < spec.n_series and b.rng.random() < 0.8:
        _duplicate(b)
    _planted(b, spec.archetype, min(n_planted, spec.n_series - b.count))
    _unrelated(b, max(0, spec.n_series - b.count))

    values = np.column_stack(b.values)
    events = _insert_events(b, values) if spec.events else []
    values = _defects(b, values)
    grid = Grid(
        hours=b.hours,
        local_hour=b.local_hour,
        local_dow=b.local_dow,
        series=b.series,
        values=values,
    )
    return Household(
        spec=spec,
        climate=climate,
        persona=persona,
        grid=grid,
        parents=b.parents,
        duplicates=b.duplicates,
        events=events,
    )
