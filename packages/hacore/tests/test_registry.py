import pytest

from hacore import HAClient, fetch_registries
from hacore.testing import VALID_TOKEN, FakeHA

FLOORS = [{"floor_id": "ground", "name": "Ground floor", "level": 0}]
AREAS = [
    {"area_id": "kitchen", "name": "Kitchen", "floor_id": "ground", "labels": ["energy"]},
    {"area_id": "garage", "name": "Garage", "floor_id": None, "labels": []},
]
LABELS = [{"label_id": "energy", "name": "Energy"}, {"label_id": "heating", "name": "Heating"}]
DEVICES = [
    {
        "id": "dev1",
        "name": "Thermometer",
        "name_by_user": None,
        "manufacturer": "Acme",
        "model": "T1",
        "area_id": "kitchen",
        "labels": ["heating"],
        "disabled_by": None,
        "future_field": 1,
    }
]
ENTITIES = [
    {
        "entity_id": "sensor.kitchen_temperature",
        "device_id": "dev1",
        "area_id": None,
        "platform": "acme",
        "labels": [],
        "disabled_by": None,
        "hidden_by": None,
        "original_name": "Temperature",
    },
    {
        "entity_id": "sensor.garage_temperature",
        "device_id": "dev1",
        "area_id": "garage",
        "platform": "acme",
        "labels": [],
        "disabled_by": None,
        "hidden_by": None,
    },
    {
        "entity_id": "switch.old",
        "device_id": None,
        "area_id": None,
        "platform": "x",
        "labels": [],
        "disabled_by": "user",
        "hidden_by": None,
    },
]


@pytest.fixture
def registries(fake_ha: tuple[FakeHA, str]) -> tuple[FakeHA, str]:
    fake, url = fake_ha
    fake.results.update(
        {
            "config/floor_registry/list": FLOORS,
            "config/area_registry/list": AREAS,
            "config/label_registry/list": LABELS,
            "config/device_registry/list": DEVICES,
            "config/entity_registry/list": ENTITIES,
        }
    )
    return fake, url


async def test_fetch_and_resolve(registries: tuple[FakeHA, str]) -> None:
    _, url = registries
    async with HAClient(url, VALID_TOKEN) as ha:
        snap = await fetch_registries(ha)

    assert set(snap.entities) == {e["entity_id"] for e in ENTITIES}
    # area inherited from device
    area = snap.area_of("sensor.kitchen_temperature")
    assert area is not None
    assert area.area_id == "kitchen"
    floor = snap.floor_of("sensor.kitchen_temperature")
    assert floor is not None
    assert floor.level == 0
    # entity area overrides device area; garage has no floor
    garage = snap.area_of("sensor.garage_temperature")
    assert garage is not None
    assert garage.area_id == "garage"
    assert snap.floor_of("sensor.garage_temperature") is None
    assert snap.labels_of("sensor.kitchen_temperature") == {"energy", "heating"}
    assert snap.area_of("sensor.unknown") is None
    assert snap.labels_of("sensor.unknown") == set()


async def test_domain_counts(registries: tuple[FakeHA, str]) -> None:
    _, url = registries
    async with HAClient(url, VALID_TOKEN) as ha:
        snap = await fetch_registries(ha)
    assert snap.domain_counts() == {"sensor": 2}
    assert snap.domain_counts(include_disabled=True) == {"sensor": 2, "switch": 1}
    assert snap.entities["switch.old"].disabled
