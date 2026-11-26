import json

import pytest

from hacore.public import rating_kit as rk

# Distinctive values of the hidden fields: none may reach a rater.
HIDDEN = {
    "score": "-0.8137",
    "q_value": "0.000731",
    "strength": "0.6421",
    "evidence": "0.9173",
    "nontriviality": "0.4459",
    "usefulness": "0.5518",
    "action_category": "savings",
    "trivial_rule": "T5",
    "cell": "correlation|kept|savings",
    "weight": "7.3311",
    "split": "validation",
    "dataset": "ideal",
}


def row(kind: str = "correlation", **kw: str) -> dict[str, str]:
    base = {
        "sample_id": "abc123",
        "house": "home65",
        "finding_type": kind,
        "entity": "sensor.home65_kitchen_temperature",
        "related": "sensor.home65_boiler_gas",
        "lag_hours": "2",
        "flags": "same_area indirect trivial",
        "period_start": "2017-03-01 05:00:00+00:00",
        "period_end": "2017-03-01 09:00:00+00:00",
        "observed": "812.5",
        "expected": "120.25",
        "human": "True",
        **HIDDEN,
    }
    return base | kw


@pytest.mark.parametrize("kind", ["correlation", "anomaly", "change_point"])
def test_description_is_blinded(kind: str) -> None:
    text = rk.describe(row(kind))
    rubric = rk.load_rubric()
    # What the AI raters get besides the rubric (which names the categories).
    reqs = json.dumps([r["params"]["messages"] for r in rk.requests([row(kind)], rubric, "m")])
    for value in HIDDEN.values():
        assert value not in text
        assert value not in reqs
    assert "home65" not in text
    # The method's own judgements stay out, context facts are in plain words.
    assert "indirect" not in text
    assert "trivial" not in text.lower()


def test_correlation_direction_and_room() -> None:
    text = rk.describe(row())
    assert "falls 2 h later" in text
    assert "same room" in text


def test_requests_one_per_role_temperature_zero() -> None:
    rubric = rk.load_rubric()
    rows = [row(sample_id=f"s{i}") for i in range(5)]
    reqs = rk.requests(rows, rubric, "model-x")
    assert len(reqs) == 5 * len(rubric.roles) == 15
    assert {r["params"]["temperature"] for r in reqs} == {0}
    assert all(len(r["params"]["messages"]) == 1 for r in reqs)
    # Every role sees every finding, each in an order of its own.
    orders = {
        role: [r["custom_id"] for r in reqs if r["custom_id"].endswith(role)]
        for role in rubric.roles
    }
    assert len({tuple(o) for o in orders.values()}) > 1


def test_forms_have_an_order_per_rater_and_no_hidden_columns() -> None:
    rows = [row(sample_id=f"s{i}") for i in range(12)]
    out = rk.forms(rows, ["a", "b", "c"])
    assert set(out["a"][0]) == set(rk.FORM_COLUMNS)
    assert [r["sample_id"] for r in out["a"]] != [r["sample_id"] for r in out["b"]]
    assert sorted(r["sample_id"] for r in out["c"]) == sorted(r["sample_id"] for r in rows)


def test_rubric_text_in_docs() -> None:
    from pathlib import Path

    doc = (Path(__file__).parents[3] / "docs/evaluation/usefulness-rubric.md").read_text("utf-8")
    rubric = rk.load_rubric()
    assert f"version {rubric.version}" in doc
    for level in rubric.data["usefulness"]["levels"]:
        assert level["text"] in doc


@pytest.mark.parametrize(
    ("first", "revote", "result", "rule"),
    [
        ([3, 4, 3], None, 3, "median"),
        ([1, 3, 2], None, None, "discuss"),
        ([1, 3, 2], [2, 2, 3], 2, "majority"),
        ([1, 4, 2], [1, 2, 3], 2, "revote_median"),
    ],
)
def test_adjudicate(
    first: list[int], revote: list[int] | None, result: int | None, rule: str
) -> None:
    a = rk.adjudicate(first, revote)
    assert (a.result, a.rule) == (result, rule)


def test_parse_results() -> None:
    ok = {
        "custom_id": "s1-owner",
        "result": {
            "type": "succeeded",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"usefulness": 3, "trivial": False, "category": "fault", "reason": "x"}
                        ),
                    }
                ]
            },
        },
    }
    bad = {"custom_id": "s2-owner", "result": {"type": "errored"}}
    off = json.loads(json.dumps(ok))
    off["custom_id"] = "s3-adviser"
    off["result"]["message"]["content"][0]["text"] = '{"usefulness": 5}'
    ratings, failed = rk.parse_results(json.dumps(x) for x in (ok, bad, off))
    assert ratings == [rk.Rating("s1", "ai:owner", 3, False, "fault")]
    assert failed == ["s2-owner", "s3-adviser"]
