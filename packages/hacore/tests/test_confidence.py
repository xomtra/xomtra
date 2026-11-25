from hacore import confidence
from hacore.context import Context


def test_low_with_few_series_or_poor_coverage() -> None:
    ctx = Context(
        groups={"sensor.a": "kitchen", "sensor.b": "kitchen"}, drivers=frozenset({"person.x"})
    )
    few = confidence.assess(
        ["sensor.a", "sensor.a", "sensor.b", "weather.t"], ctx, exclude="weather.t"
    )
    assert few.series == 3
    assert few.entities == 2
    assert few.group_coverage == 1.0
    assert few.level == confidence.LOW
    assert few.reasons == ["only 3 series analysed (< 30)"]

    many = [f"sensor.s{i}" for i in range(40)]
    groups = dict.fromkeys(many[:36], "room")
    ok = confidence.assess(many, Context(groups=groups))
    assert ok.level == confidence.NORMAL
    assert ok.ungrouped == many[36:]

    poor = confidence.assess(many, Context(groups=dict.fromkeys(many[:20], "room")))
    assert poor.level == confidence.LOW
    assert "50% of the entities have an area" in poor.reasons[0]
    assert "ungrouped" not in poor.as_dict()  # no entity names for readers
