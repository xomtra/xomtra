import duckdb
import pytest

from hacore import areas
from hacore.areas import AreaInfo, EntityArea

AREAS = [
    AreaInfo("obyvak", ("Obývák", "Obývací pokoj")),
    AreaInfo("koupelna", ("Koupelna",)),
    AreaInfo("koupelna_dole", ("Koupelna dole",)),
    AreaInfo("kitchen", ("Kitchen",)),
    AreaInfo("wc", ("WC",)),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sensor.obyvak_teplota", "obyvak"),
        ("Teplota obývací pokoj", "obyvak"),  # alias, diacritics
        ("sensor.koupelna_vlhkost", "koupelna"),
        ("sensor.koupelna_dole_vlhkost", "koupelna_dole"),  # the longer name wins
        ("light.kitchen_ceiling", "kitchen"),
        ("sensor.kitchenette_power", None),  # whole words only
        ("sensor.wc_light", None),  # too short to trust
        ("sensor.kitchen_or_obyvak", None),  # two areas: none
        ("sensor.th_3f2a_co2", None),
    ],
)
def test_match_area(text: str, expected: str | None) -> None:
    assert areas.match_area(text, AREAS) == expected


def test_resolve_prefers_the_ha_area() -> None:
    out = areas.resolve(
        [
            ("sensor.obyvak_teplota", "kitchen", None),
            ("sensor.th_1_co2", None, "CO2 obývák"),
            ("sensor.th_2_co2", None, None),
        ],
        AREAS,
    )
    assert out == {
        "sensor.obyvak_teplota": EntityArea("kitchen", areas.SOURCE_HA),
        "sensor.th_1_co2": EntityArea("obyvak", areas.SOURCE_NAME),
    }
    assert areas.same_area(out, "sensor.obyvak_teplota", "sensor.obyvak_teplota")
    assert not areas.same_area(out, "sensor.obyvak_teplota", "sensor.th_1_co2")
    assert not areas.same_area(out, "sensor.th_1_co2", "sensor.th_2_co2")


def test_load_from_the_lake() -> None:
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS lake")
    assert areas.load(con, "lake") == {}
    con.execute("CREATE SCHEMA lake.silver")
    con.execute(
        "CREATE TABLE lake.silver.registry_current (registry VARCHAR, item_id VARCHAR, "
        "payload JSON)"
    )
    con.execute(
        "INSERT INTO lake.silver.registry_current VALUES "
        """('areas', 'obyvak', '{"name": "Obývák", "aliases": ["Living room"]}'), """
        """('areas', 'kitchen', '{"name": "Kuchyně"}')"""
    )
    con.execute(
        "CREATE TABLE lake.silver.entities (entity_id VARCHAR, area_id VARCHAR, name VARCHAR)"
    )
    con.execute(
        "INSERT INTO lake.silver.entities VALUES "
        "('sensor.a', 'kitchen', NULL), ('sensor.b', NULL, 'Living room CO2'), "
        "('sensor.c', NULL, NULL)"
    )
    assert areas.load(con, "lake") == {
        "sensor.a": EntityArea("kitchen", areas.SOURCE_HA),
        "sensor.b": EntityArea("obyvak", areas.SOURCE_NAME),
    }
    # Older lakes: silver.entities without area columns.
    con.execute("CREATE TABLE lake.silver.e2 AS SELECT entity_id FROM lake.silver.entities")
    con.execute("DROP TABLE lake.silver.entities")
    con.execute("ALTER TABLE lake.silver.e2 RENAME TO entities")
    assert areas.load(con, "lake") == {}
