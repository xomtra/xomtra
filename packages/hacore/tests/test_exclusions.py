from hacore import exclusions


def test_labelled_by_entity_or_device() -> None:
    devices = [{"id": "d1", "labels": ["hammond_no_ingest"]}, {"id": "d2", "labels": None}]
    entities = [
        {"entity_id": "sensor.a", "device_id": "d1"},
        {"entity_id": "sensor.b", "device_id": "d2", "labels": ["hammond_no_analysis"]},
        {"entity_id": "sensor.c", "device_id": None, "labels": ["other"]},
        {"entity_id": "sensor.d"},
    ]
    assert exclusions.labelled(entities, devices, {exclusions.NO_INGEST}) == {"sensor.a"}
    assert exclusions.labelled(
        entities, devices, {exclusions.NO_INGEST, exclusions.NO_ANALYSIS}
    ) == {"sensor.a", "sensor.b"}
