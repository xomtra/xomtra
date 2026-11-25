"""Hourly series of the houses of a public dataset, whatever its format."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

type FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class House:
    """Hourly means of one house: ``hours`` (UTC, datetime64[h], starting on a UTC
    midnight) x ``channels``."""

    house: str
    hours: npt.NDArray[np.datetime64]
    channels: list[str]
    values: FloatArray  # hours x channels, NaN = missing
    areas: dict[str, str] = field(default_factory=dict)  # channel -> room, if known

    @property
    def days(self) -> int:
        return len(self.hours) // 24


def from_hourly(
    house: str,
    first_hour: int,
    channels: list[str],
    values: FloatArray,
    areas: dict[str, str] | None = None,
) -> House:
    """``values`` from the UTC hour ``first_hour`` (hours since the epoch), padded to
    start on a UTC midnight so that houses can be shifted by whole days."""
    pad = first_hour % 24
    if pad:
        values = np.vstack([np.full((pad, len(channels)), np.nan), values])
        first_hour -= pad
    hours = np.datetime64(first_hour, "h") + np.arange(len(values)).astype("timedelta64[h]")
    return House(house=house, hours=hours, channels=channels, values=values, areas=areas or {})
