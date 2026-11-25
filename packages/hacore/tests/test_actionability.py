import duckdb
import pytest

from hacore import actionability as act
from hacore.actionability import EntityInfo

RULES = act.load_rules()


def example_entities() -> list[tuple[str, dict[str, str]]]:
    return [(f.name, ex) for f in RULES.families for ex in f.examples]


@pytest.mark.parametrize(("family", "example"), example_entities())
def test_every_example_lands_in_its_family(family: str, example: dict[str, str]) -> None:
    e = EntityInfo(
        example["entity_id"],
        example.get("name"),
        example.get("device_class"),
        example.get("unit"),
        example.get("platform"),
    )
    assert RULES.family(e) == family


def test_every_family_has_an_example_and_a_rule_per_type() -> None:
    for fam in RULES.families:
        assert fam.examples, fam.name
        for ftype in act.FINDING_TYPES:
            assert RULES.rulebook.rule(fam.name, ftype) is not None, (fam.name, ftype)
    assert RULES.rulebook.rule(act.OTHER, "anomaly") is None  # "other" is unclassified


def test_links_and_rules_name_known_families() -> None:
    names = {f.name for f in RULES.families}
    assert {fam for fam, _ in RULES.rulebook.rules} <= names
    assert set().union(*RULES.rulebook.links) <= names
    assert RULES.rulebook.version.count(".") == 2


def test_unknown_entities_are_other() -> None:
    assert RULES.family(EntityInfo("input_text.note")) == act.OTHER


def test_parse_rejects_unknown_categories() -> None:
    with pytest.raises(ValueError, match="unknown category"):
        act.parse(
            {
                "version": "1.0.0",
                "family": [{"name": "x", "match": [{"domain": ["sensor"]}]}],
                "rules": {"x": {"anomaly": {"category": "maybe"}}},
            }
        )
    with pytest.raises(ValueError, match="unknown finding type"):
        act.parse({"version": "1.0.0", "rules": {"x": {"weird": {"category": "none"}}}})


def test_load_scorer_reads_the_lake() -> None:
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS lake")
    con.execute("CREATE SCHEMA lake.silver")
    con.execute(
        "CREATE TABLE lake.silver.entities (entity_id VARCHAR, name VARCHAR, "
        "area_id VARCHAR, device_class VARCHAR, unit_of_measurement VARCHAR, "
        "platform VARCHAR, device_id VARCHAR)"
    )
    con.execute(
        "INSERT INTO lake.silver.entities VALUES "
        "('sensor.kitchen_temperature', 'Kitchen', 'kitchen', 'temperature', '°C', 'zha', 'd1'),"
        "('sensor.fridge_power', 'Fridge', 'kitchen', 'power', 'W', 'template', NULL)"
    )
    scorer = act.load_scorer(con, "lake")
    t = scorer.meta["sensor.kitchen_temperature"]
    assert (t.group, t.kind, t.source, t.parent, t.family) == (
        "kitchen",
        "temperature",
        "zha",
        "d1",
        "temperature",
    )
    p = scorer.meta["sensor.fridge_power"]
    assert p.derived
    assert p.family == "power"
    assert scorer.ranking_version == f"r0-{RULES.rulebook.version}"
    empty = duckdb.connect()
    empty.execute("ATTACH ':memory:' AS lake")
    assert act.load_scorer(empty, "lake").meta == {}
