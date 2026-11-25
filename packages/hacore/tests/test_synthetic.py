import numpy as np
import pytest

from hacore.synthetic import ARCHETYPES, HouseholdSpec, Relation, generate
from hacore.synthetic.evaluate import current_method, evaluate, main, score, specs, summarise
from hacore.synthetic.generator import OUTSIDE_TEMP


@pytest.mark.parametrize("archetype", ARCHETYPES)
def test_household_shape_and_determinism(archetype: str) -> None:
    spec = HouseholdSpec(archetype, n_series=40, days=30, seed=3)
    h = generate(spec)
    assert h.grid.values.shape == (30 * 24, len(h.grid.series))
    assert abs(len(h.grid.series) - 40) <= 3
    assert len(set(h.keys)) == len(h.keys)
    assert OUTSIDE_TEMP in h.keys
    assert set(h.grid.local_dow) <= set(range(1, 8))
    # Missing hours exist, but every series reaches the end of the window.
    assert np.isnan(h.grid.values).any()
    assert not np.isnan(h.grid.values[-72:]).any()
    shares = [j for j, s in enumerate(h.grid.series) if s.metric == "share"]
    if shares:
        v = h.grid.values[:, shares]
        assert np.nanmin(v) >= 0
        assert np.nanmax(v) <= 1
    again = generate(spec)
    assert again.keys == h.keys
    np.testing.assert_array_equal(again.grid.values, h.grid.values)
    other = generate(HouseholdSpec(archetype, n_series=40, days=30, seed=4))
    assert not np.array_equal(other.grid.values[:, 0], h.grid.values[:, 0])


def test_relations_follow_the_graph() -> None:
    h = generate(HouseholdSpec("power_user", n_series=60, days=30, seed=1))
    keys = set(h.keys)
    direct = h.direct_pairs()
    assert direct
    a, b, w, _lag = direct[0]
    assert h.relation(a, b) is Relation.DIRECT
    assert h.relation(b, a) is Relation.DIRECT
    assert h.planted_strength(a, b) == w
    chains = [k for k in keys if k.endswith("_c")]
    for c in chains:
        stem = c[: -len("_c")]
        assert h.relation(f"{stem}_a", f"{stem}_b") is Relation.INDIRECT
    commons = [k[: -len("_a")] for k in keys if k.endswith("_a") and k[:-2] + "_b" in keys]
    hidden_common = [s for s in commons if f"h:{s.split('.')[1]}" in h.parents]
    for stem in hidden_common:
        assert h.relation(f"{stem}_a", f"{stem}_b") is Relation.COMMON
    others = sorted(k for k in keys if ".other_" in k)
    assert h.relation(others[0], others[1]) is Relation.UNRELATED


def test_weather_and_duplicates() -> None:
    households = (
        generate(HouseholdSpec("house_pv_battery", n_series=40, days=30, seed=seed))
        for seed in range(20)
    )
    h = next(h for h in households if any(k.startswith("light.") for k in h.keys))
    pv = next(k for k in h.keys if "pv_power" in k)
    light = next(k for k in h.keys if k.startswith("light."))
    temp = next(k for k in h.keys if k.endswith("_temperature") and k != OUTSIDE_TEMP)
    # Both follow the sunshine and nothing else in common.
    assert h.relation(pv, light) is Relation.WEATHER
    # Sunshine and outside temperature are independent drivers in the model.
    assert h.relation(pv, temp) is Relation.UNRELATED
    assert h.duplicates
    a, b = sorted(next(iter(h.duplicates)))
    assert h.relation(a, b) is Relation.DUPLICATE


def test_planted_strength_is_realised() -> None:
    h = generate(HouseholdSpec("power_user", n_series=80, days=60, seed=5))
    idx = {k: j for j, k in enumerate(h.keys)}
    errors = []
    for a, b, w, lag in h.direct_pairs():
        x = h.grid.values[: len(h.grid.hours) - lag, idx[a]]
        y = h.grid.values[lag:, idx[b]]
        ok = ~np.isnan(x) & ~np.isnan(y)
        errors.append(abs(abs(np.corrcoef(x[ok], y[ok])[0, 1]) - w))
    assert errors
    assert float(np.median(errors)) < 0.08


def test_events_are_inserted() -> None:
    h = generate(HouseholdSpec("house_heat_pump", n_series=60, days=90, seed=1, events=True))
    assert h.events
    t = len(h.grid.hours)
    for e in h.events:
        assert e.key in h.keys
        assert 0 <= e.start < e.end <= t
        assert e.kind in {"spike", "dropout", "drift", "level_shift"}
    assert not generate(HouseholdSpec("house_heat_pump", 60, days=90, seed=1)).events


def test_spec_validation() -> None:
    with pytest.raises(ValueError, match="archetype"):
        HouseholdSpec("castle", 10)
    with pytest.raises(ValueError, match="at least"):
        HouseholdSpec("apartment", 3)


def test_current_method_on_synthetic_households() -> None:
    """Baseline of the production method (ADR 042): no findings between unrelated
    series, the strong planted relations found; it has no indirect/common flags, so
    the gate FDR is high - the gap that ADR 062 closes."""
    metrics = evaluate(specs([30], seeds=2, days=60))
    rows = summarise(metrics)
    assert {r["archetype"] for r in rows} == set(ARCHETYPES)
    findings = sum(m.findings for m in metrics)
    false_strict = sum(m.false_strict for m in metrics)
    assert findings > 0
    assert false_strict / findings <= 0.05
    strong = [m.recall_strong for m in metrics if m.recall_strong is not None]
    assert strong
    assert float(np.mean(strong)) >= 0.8


def test_score_counts_unflagged_common_as_false() -> None:
    h = generate(HouseholdSpec("apartment", n_series=30, days=30, seed=0))
    keys = h.keys
    pair = next(
        (a, b)
        for i, a in enumerate(keys)
        for b in keys[i + 1 :]
        if h.relation(a, b) is Relation.COMMON
    )

    def finding(flags: list[str]) -> dict[str, object]:
        ea, _, sa = pair[0].partition("=")
        eb, _, sb = pair[1].partition("=")
        return {
            "entity_id": ea,
            "state": sa or None,
            "related_entity_id": eb,
            "related_state": sb or None,
            "flags": flags,
        }

    unflagged = score(h, [finding([])], 0.0)
    flagged = score(h, [finding(["common_factor"])], 0.0)
    assert (unflagged.false, unflagged.false_strict) == (1, 0)
    assert (flagged.false, flagged.false_strict) == (0, 0)


def test_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--sizes", "12", "--seeds", "1", "--days", "30", "--archetypes", "apartment"]) == 0
    out = capsys.readouterr().out
    assert '"archetype": "apartment"' in out
    assert current_method()  # callable factory


def test_areas_follow_the_spec() -> None:
    full = generate(HouseholdSpec("apartment", 40, days=30, seed=2, area_coverage=1.0))
    assert full.room_of
    assert full.area_coverage() == 1.0
    assert full.ha_area == full.room_of
    none = generate(
        HouseholdSpec("apartment", 40, days=30, seed=2, area_coverage=0.0, descriptive_names=0.0)
    )
    assert not none.ha_area
    assert none.area_coverage() == 0.0
    named = generate(
        HouseholdSpec("apartment", 40, days=30, seed=2, area_coverage=0.0, descriptive_names=1.0)
    )
    assert named.area_coverage() == 1.0  # found from the names
    # The series themselves do not depend on the areas.
    np.testing.assert_array_equal(full.grid.values, none.grid.values)
    with pytest.raises(ValueError, match="between 0 and 1"):
        HouseholdSpec("apartment", 40, area_coverage=1.5)


def test_same_area_is_left_out_of_the_gate_fdr() -> None:
    h = generate(HouseholdSpec("apartment", 30, days=60, seed=1, area_coverage=1.0))
    findings = current_method()(h)
    m = score(h, findings, 0.0)
    assert m.same_area > 0
    assert m.gate_findings == m.findings - m.same_area
    assert m.fdr_all >= m.fdr_strict


def test_presence_entity_is_an_observed_driver() -> None:
    """ADR 064: the residents' presence entity drives the home's occupancy; its
    relations with the rooms are direct and never flagged as a hidden factor."""
    h = generate(HouseholdSpec("minimalist", 60, days=90, seed=3, presence=1.0))
    assert "person.resident=on" in h.keys
    assert (
        "person.resident=on"
        not in generate(HouseholdSpec("minimalist", 60, days=90, seed=3, presence=0.0)).keys
    )
    motion = next(k for k in h.keys if k.endswith("_motion=on"))
    assert h.relation("person.resident=on", motion) is Relation.DIRECT
    findings = current_method()(h)
    with_person = [
        f for f in findings if "person.resident" in (f["entity_id"], f["related_entity_id"])
    ]
    assert with_person
    assert not any("common_factor" in f["flags"] for f in with_person)
