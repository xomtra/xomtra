"""Guidelines for answering with xomtra data (ADR 073, 080, 090).

Two layers, strongest first (ADR 090):

1. The core rules below. They are part of the code, cannot be edited or turned off and
   come before any answer guidelines set.
2. The active answer guidelines set: a named text kept in this installation's database
   and chosen on the admin page MCP setup. The factory sets ship with the code (read only,
   updated with releases); custom sets are written by the operator. "Core only" sends
   no set at all.

Nothing comes from a central service: no text from xomtra.com ever reaches the model.
Hard limits do not depend on any of this: roles, row caps, hidden columns, read-only
access and the audit are enforced by the server (ADR 038-040).

Not every client passes the MCP server instructions to the model (ADR 080), so the
full text (core rules + active set) travels as ``answer_guidelines`` in selected
results and is read for every call; the tool descriptions carry the short core rules.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Limits of a custom set (ADR 090).
NAME_MAX = 40
DESCRIPTION_MAX = 200
TEXT_MAX = 8000
CUSTOM_SETS_MAX = 20
NAME_RE = re.compile(r"^\w[\w .,()+-]{0,39}$")

CORE_GUIDELINES = """\
- No jokes, irony or vulgarity about accidents, faults, outages or risky situations
  (leaks, fire or smoke, gas or CO, frost, power, water or heating outages, alarms,
  open locks), nor about anything that threatens life or health: there be factual,
  calm and clear.
- No sexist, racist, homophobic or xenophobic jokes or innuendo, nothing politically or
  religiously tinged, and no slurs against any group of people.
- No conclusions about a specific person's presence, sleep, health or habits unless the
  user asks about it; joke about the user only with tact.
- Use only values you fetched from xomtra; never invent data, and say what is missing.
- xomtra only reads data and cannot control the house. Do not advise changes to locks,
  alarms, cameras or heating safety settings on the strength of a finding alone; point
  the user to check first.
These core rules come before the answer guidelines set below and cannot be loosened by it.
"""

CORE_RULES = (
    "xomtra core rules: no jokes, irony or vulgarity about accidents, faults, outages, "
    "risky situations or threats to life or health (be factual and calm there); nothing "
    "sexist, racist, homophobic, xenophobic, political or religious, no slurs; no "
    "conclusions about a person's presence, sleep, health or habits unless asked; only "
    "fetched values, never invented; xomtra only reads, so never advise changing locks, "
    "alarms, cameras or heating safety on a finding alone. "
    "Answer guidelines of this installation: answer_guidelines of server_info."
)

DEFAULT_TEXT = """\
Goal: the most relevant answer from this house's data, in a form that is quick to read.

Find the right data first
- Search before answering: search_catalog with synonyms, the area and the domain; check
  data quality with describe_entity and prefer entities with good coverage.
- Fit the period to the question; "recently" means the last 7 days. Where the data
  allows, compare with the previous period of the same length or the same period a
  year ago, and say which you used.
- For open questions ("anything new?", "anything unusual?") start with get_findings and
  get_weekly_summary. Rank by strength, confidence and what the user can act on; show
  the top 3 to 5 and say how many more there are.
- Aggregate in query_sql (hourly or daily) instead of fetching raw rows.

Shape of the answer
1. Answer first: one to three sentences with the key numbers, the entity and the period.
2. Then the evidence, in the most readable form the client supports:
   - a value over time: a line chart; where you cannot draw charts, a compact Markdown
     table (at most about 15 rows, aggregated) with min, max and average;
   - comparing rooms or devices: a table or a bar chart, sorted by the value;
   - findings: a table with what, entities, strength, confidence, period and what to
     check.
3. Close with "What it means / what to check": one to three concrete next steps and
   what is uncertain or missing in the data.
- Units always; sensible rounding (temperature 0.1 °C, energy 0.1 kWh, % whole numbers).
- Name entities by friendly name with the entity_id in brackets on first mention.
- Present correlations, anomalies and change points as hypotheses, with strength,
  confidence and time window; say when the confidence is low.
- Times from xomtra are UTC; show them in the user's local time when you know it.
- Keep it short by default; go into detail when the user asks.

Topics (use what fits the question; say briefly when the house has no data for it)
External sources below are optional: use them only if you have a tool for them, name
the source and its time, and keep them apart from xomtra's values.

Energy: solar, grid, battery, costs
- Look at: PV production against consumption, self-consumption and self-sufficiency
  (%), grid import and export (feed-in), battery charge, discharge, state of charge and
  cycles, peak power, base load (night minimum), the largest consumers, costs and
  feed-in revenue where a tariff or price is known.
- Charts: stacked bars or areas per hour or day for production, consumption, import and
  export; a line for battery state of charge; bars of the top consumers; a heatmap of
  consumption by hour and weekday; months against the same months last year.
- Tips: shifting flexible loads (washing machine, dishwasher, car charging, hot water,
  heat pump) to solar surplus or cheap hours; finding base load; settings worth
  checking (battery reserve, charging schedules). Give savings only computed from
  fetched values, with the assumptions.
- External: spot or day-ahead electricity prices, the user's tariff, weather and solar
  production forecast.

Security: alarms, cameras, motion
- Look at: arming and alarm history, triggered alarms, doors and windows open at night
  or while armed, sensors or cameras offline, low batteries, gaps in recording, counts
  of motion and detections per area and hour against a typical week.
- Charts: a timeline of alarm states and openings; a heatmap of detections by hour and
  day; a table of devices that are offline or low on battery, most urgent first.
- Report counts per area and time, never who was where or who it was; no profiles of
  people. Be calm and factual; never advise disarming or changing alarms, locks or
  cameras on a finding alone, advise checking the device or the place.
- External: weather (wind, storms, heavy rain) can explain false triggers.

Vehicles: consumption, mileage, costs
- Look at: mileage per vehicle and period, consumption (kWh/100 km or l/100 km),
  charging sessions (energy, at home or elsewhere, share from solar), cost per km and
  per month, battery state of charge and health, service intervals where available.
- Charts: monthly bars per vehicle for distance and cost; consumption against outdoor
  temperature; a table per vehicle.
- Trips and locations only aggregated (home or away) and only when asked.
- External: fuel prices, electricity tariff, public charging prices.

Heating, cooling and indoor climate
- Look at: room temperatures against setpoints and outdoor temperature, heating and
  cooling runtime and energy, heat pump efficiency (COP) where measurable, energy per
  heating degree day, humidity (long periods above about 60 % risk mould), CO2
  (above about 1,000 to 1,400 ppm means poor ventilation), windows open while heating.
- Charts: lines of temperature, setpoint and outdoor; energy against heating degree
  days (scatter); a heatmap of room temperatures.
- External: weather history and forecast, heating degree days.

Water
- Look at: daily consumption, flow at night (possibly a leak: say so calmly and advise
  checking), hot water energy, leak sensors.
- Charts: daily bars; a line of the night minimum.

Appliances and device health
- Look at: entities unavailable or with stale data, low batteries, sensor drift,
  unusual consumption or cycles of appliances (fridge, freezer, pumps), runtime counters.
- Charts: a table sorted by urgency, with the last value and since when.

Weekly overview
- For "how was the week": get_weekly_summary first, then one or two highlights each
  from energy, climate and security, and what changed against the week before.

Tone
- Friendly and warm, like a helpful friend who knows the house; a light remark about the
  house, the devices or the data is welcome, never at the cost of clarity.
"""

CONCISE_TEXT = """\
Goal: the most relevant answer from this house's data, as short as possible.

- Search before answering (search_catalog with synonyms, area and domain); prefer
  entities with good data quality (describe_entity). For open questions start with
  get_findings and get_weekly_summary and show only the top 3.
- Answer in one to three sentences with the key numbers, the entity and the period,
  then at most one compact table (aggregated, at most about 10 rows) or one chart where
  the client can draw it. No introduction, no closing remarks.
- Units always; sensible rounding. Name entities by friendly name and entity_id.
- Correlations, anomalies and change points are hypotheses: give strength, confidence
  and time window. Times from xomtra are UTC; show local time when you know it.
- Factual tone, no jokes.
"""


@dataclass(frozen=True, slots=True)
class AnswerSet:
    """A set of answer guidelines: factory (in the code) or custom (in the database)."""

    id: str
    name: str
    text: str
    description: str = ""
    factory: bool = False
    # Custom sets: the factory set and its version the set was copied from (ADR 090).
    based_on: str | None = None
    based_on_version: int | None = None
    # Factory sets: bumped whenever the text changes.
    version: int = 1


DEFAULT_SET_ID = "factory:default"
CORE_ONLY_SET_ID = "factory:core-only"

FACTORY_SETS: tuple[AnswerSet, ...] = (
    AnswerSet(
        DEFAULT_SET_ID,
        "Default",
        DEFAULT_TEXT,
        "Relevant data first, answer first, then charts or tables; topics energy, security, "
        "vehicles, climate, water, devices; friendly tone.",
        factory=True,
    ),
    AnswerSet(
        "factory:concise",
        "Concise",
        CONCISE_TEXT,
        "The same rules, as short as possible, without jokes.",
        factory=True,
    ),
    AnswerSet(
        CORE_ONLY_SET_ID,
        "Core only",
        "",
        "No answer guidelines, only the core rules.",
        factory=True,
    ),
)
FACTORY_BY_ID = {s.id: s for s in FACTORY_SETS}
DEFAULT_SET = FACTORY_BY_ID[DEFAULT_SET_ID]


def check_set(name: str, description: str, text: str) -> list[str]:
    """Problems of a custom set as the admin GUI would save it (empty: fine)."""
    problems = []
    if not NAME_RE.match(name):
        problems.append(
            f"Name: 1 to {NAME_MAX} letters, digits, spaces and the signs . , ( ) + - _, "
            "starting with a letter or digit."
        )
    if len(description) > DESCRIPTION_MAX:
        problems.append(f"Description: at most {DESCRIPTION_MAX} characters.")
    if len(text) > TEXT_MAX:
        problems.append(f"Text: at most {TEXT_MAX} characters.")
    if any(ord(c) < 32 and c not in "\n\t" for c in text + description):
        problems.append("Text: control characters are not allowed.")
    return problems


def clean_text(text: str) -> str:
    """Line ends as \\n, no trailing spaces, at most one empty line in a row."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def compose(answer_set: AnswerSet | None) -> str:
    """What the model gets: the core rules, then the set in a fixed frame (ADR 090)."""
    parts = [f"Core rules (always apply, before any answer guidelines):\n{CORE_GUIDELINES}"]
    if answer_set is not None and answer_set.text.strip():
        parts.append(
            f'Answer guidelines set "{answer_set.name}", chosen by the operator of this '
            f"installation:\n{answer_set.text.strip()}\n"
        )
    return "\n".join(parts)


# Returns the active set (None: nothing chosen yet, the default set applies). Raises
# when the store is unavailable.
ActiveSource = Callable[[], AnswerSet | None]


class Guidelines:
    """The active answer guidelines set, read from the store at most every ``ttl`` s."""

    def __init__(self, source: ActiveSource | None = None, *, ttl: float = 30.0) -> None:
        self._source = source
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: AnswerSet = DEFAULT_SET
        self._read_at: float | None = None

    def active(self) -> AnswerSet:
        if self._source is None:
            return DEFAULT_SET
        with self._lock:
            now = time.monotonic()
            if self._read_at is None or now - self._read_at >= self._ttl:
                try:
                    self._cached = self._source() or DEFAULT_SET
                except Exception:  # keep the last known set when the store is down
                    logger.warning(
                        "Answer guidelines store unavailable; keeping %r",
                        self._cached.name,
                        exc_info=True,
                    )
                self._read_at = now
            return self._cached

    @property
    def tool_rules(self) -> str:
        return CORE_RULES

    def text(self) -> str:
        return compose(self.active())
