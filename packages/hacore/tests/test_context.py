from hacore.context import EMPTY, Context

CTX = Context(
    groups={"a": "kitchen", "b": "kitchen", "c": "bedroom"},
    drivers=frozenset({"p"}),
    group_flag="same_area",
    driver_flag="house_context",
)


def test_group_flag() -> None:
    assert CTX.explain("a", "b", [], []) == ["same_area"]
    assert CTX.explain("a", "c", [], []) == []
    assert CTX.explain("a", "x", [], []) == []  # no group: no flag


def test_driver_rules() -> None:
    # Explained by an observed driver.
    assert CTX.explain("a", "c", ["indirect"], ["p"]) == ["indirect", "house_context"]
    # Inside one group the group explains it already.
    assert CTX.explain("a", "b", ["indirect"], ["p"]) == ["indirect", "same_area"]
    # A relation with the driver itself is direct, not a hidden factor.
    assert CTX.explain("p", "c", ["common_factor", "unstable"], []) == ["unstable"]


def test_empty_context_changes_nothing() -> None:
    assert EMPTY.explain("a", "b", ["indirect"], ["p"]) == ["indirect"]
