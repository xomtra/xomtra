import math
from typing import Any

import pytest

from hacore import usefulness as u
from hacore.usefulness import Meta, Rule, Rulebook, Scorer


def test_strength_is_clipped_and_monotone() -> None:
    assert u.strength(u.CORRELATION, 0.3) == 0.0
    assert u.strength(u.CORRELATION, -0.65) == pytest.approx(0.5)
    assert u.strength(u.CORRELATION, 1.0) == 1.0
    assert u.strength(u.ANOMALY, 10.0) == 1.0
    assert u.strength(u.ANOMALY, 2.0) == 0.0
    assert u.strength(u.CHANGE_POINT, 5.0) == pytest.approx(0.5)
    assert u.strength(u.ANOMALY, None) == 0.0


def test_evidence_depends_on_q_only() -> None:
    q_min = u.Q_MIN[u.CORRELATION]
    assert u.evidence(u.CORRELATION, 0.01, 0.01) == 0.0
    assert u.evidence(u.CORRELATION, q_min, 0.01) == pytest.approx(1.0)
    assert u.evidence(u.CORRELATION, 1e-300, 0.01) == 1.0  # clipped below q_min
    mid = math.sqrt(0.01 * q_min)
    assert u.evidence(u.CORRELATION, mid, 0.01) == pytest.approx(0.5)
    assert u.evidence(u.ANOMALY, None, 0.05) == 0.0


def test_nontriviality_counts_shared_metadata() -> None:
    a = Meta(group="kitchen", kind="temperature", source="zha", parent="d1")
    assert u.nontriviality(a, Meta(group="kitchen", kind="temperature", source="zha")) == 0.25
    assert u.nontriviality(a, Meta()) == 1.0
    assert u.nontriviality(Meta(), Meta()) == 1.0  # unknown is not shared
    assert u.nontriviality(a, None) == 1.0


def corr_finding(**kw: Any) -> dict[str, Any]:
    return {
        "entity_id": "sensor.a",
        "related_entity_id": "sensor.b",
        "lag_hours": 0,
        "score": 0.6,
        "q_value": 1e-4,
        "flags": [],
        **kw,
    }


BOOK = Rulebook(
    "1.2.3",
    {
        ("power", "correlation"): Rule("savings", 2),
        ("light", "correlation"): Rule("automation", 2),
        ("power", "anomaly"): Rule("savings", 3),
        ("sensor_battery", "anomaly"): Rule("fault", 3),
    },
    frozenset({frozenset(("pv", "sun"))}),
)


@pytest.mark.parametrize(
    ("finding", "a", "b", "overlap", "code"),
    [
        (corr_finding(flags=["same_device"]), Meta(), Meta(), None, "T1"),
        (corr_finding(), Meta(parent="d"), Meta(parent="d"), None, "T1"),
        (corr_finding(flags=["duplicate_suspect"]), Meta(), Meta(), None, "T2"),
        (corr_finding(score=0.97), Meta(kind="temperature"), Meta(kind="temperature"), None, "T2"),
        (corr_finding(score=0.97, lag_hours=2), Meta(kind="t"), Meta(kind="t"), None, None),
        (corr_finding(), Meta(family="power", derived=True), Meta(family="power"), None, "T3"),
        (
            corr_finding(lag_hours=3),
            Meta(family="power", derived=True),
            Meta(family="power"),
            None,
            None,
        ),
        (corr_finding(), Meta(group="k", kind="t"), Meta(group="k", kind="t"), None, "T4"),
        (corr_finding(), Meta(group="k", kind="t"), Meta(group="k", kind="h"), None, None),
        (corr_finding(), Meta(family="sun"), Meta(family="pv"), None, "T5"),
        (corr_finding(), Meta(source="zha"), Meta(source="zha"), 0.9, "T6"),
        (corr_finding(), Meta(source="zha"), Meta(source="zha"), 0.5, None),
        (corr_finding(lag_hours=1), Meta(source="zha"), Meta(source="zha"), 0.9, None),
        (corr_finding(), Meta(), Meta(), None, None),
    ],
)
def test_trivial_rules(
    finding: dict[str, Any], a: Meta, b: Meta, overlap: float | None, code: str | None
) -> None:
    assert u.trivial_rule(finding, a, b, BOOK, overlap) == code


def test_scorer_adds_the_columns_and_flags_trivial_relations() -> None:
    scorer = Scorer(
        {
            "sensor.a": Meta(family="power", parent="d1"),
            "sensor.b": Meta(family="light", parent="d1"),
            "sensor.c": Meta(family="light"),
        },
        BOOK,
    )
    f = scorer.score(corr_finding(), u.CORRELATION, 0.01)
    assert f["trivial_rule"] == "T1"
    assert f["flags"] == [u.TRIVIAL]
    assert f["action_category"] == "savings"  # equal priority: savings before automation
    assert f["usefulness"] == pytest.approx(math.sqrt(f["strength"] * f["evidence"]), abs=1e-4)
    assert f["rules_version"] == "1.2.3"
    assert f["ranking_version"] == "r0-1.2.3"
    g = scorer.score(corr_finding(related_entity_id="sensor.c"), u.CORRELATION, 0.01)
    assert g["trivial_rule"] is None
    assert g["flags"] == []
    assert g["nontriviality"] == 1.0
    # one entity: never trivial, N = 1, unclassified without a rule
    h = scorer.score(
        {"entity_id": "sensor.c", "score": 6.0, "q_value": 1e-3, "flags": []}, u.ANOMALY, 0.05
    )
    assert h["trivial_rule"] is None
    assert h["nontriviality"] == 1.0
    assert h["action_category"] == u.UNCLASSIFIED


def test_sort_key_orders_by_r0_then_priority() -> None:
    rows = [
        {"usefulness": 0.4, "_priority": 1, "action_category": "none", "score": 0.9},
        {"usefulness": 0.4, "_priority": 3, "action_category": "fault", "score": 0.5},
        {"usefulness": 0.8, "_priority": 0, "action_category": u.UNCLASSIFIED, "score": 0.4},
    ]
    rows.sort(key=u.sort_key)
    assert [r["score"] for r in rows] == [0.4, 0.5, 0.9]


def test_r0_keeps_dominance() -> None:
    """A finding at least as strong and as significant ranks at least as high."""
    scorer = Scorer()
    weak = scorer.score(corr_finding(score=0.5, q_value=1e-3), u.CORRELATION, 0.01)
    strong = scorer.score(corr_finding(score=0.7, q_value=1e-5), u.CORRELATION, 0.01)
    assert u.sort_key(strong) < u.sort_key(weak)
