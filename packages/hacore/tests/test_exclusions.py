import duckdb
import pytest

from hacore import exclusions


def test_labelled_by_entity_or_device() -> None:
    devices = [{"id": "d1", "labels": ["xomtra_no_ingest"]}, {"id": "d2", "labels": None}]
    entities = [
        {"entity_id": "sensor.a", "device_id": "d1"},
        {"entity_id": "sensor.b", "device_id": "d2", "labels": ["xomtra_no_analysis"]},
        {"entity_id": "sensor.c", "device_id": None, "labels": ["other"]},
        {"entity_id": "sensor.d"},
    ]
    assert exclusions.labelled(entities, devices, {exclusions.NO_INGEST}) == {"sensor.a"}
    assert exclusions.labelled(
        entities, devices, {exclusions.NO_INGEST, exclusions.NO_ANALYSIS}
    ) == {"sensor.a", "sensor.b"}


def test_labels() -> None:
    labels = exclusions.ExclusionLabels(no_ingest="private", no_analysis="private")
    assert labels.analysis == ["private"]
    assert exclusions.DEFAULT_LABELS.analysis == [exclusions.NO_ANALYSIS, exclusions.NO_INGEST]
    with pytest.raises(ValueError, match="empty"):
        exclusions.ExclusionLabels(no_ingest="")


def test_load_no_analysis_custom_labels() -> None:
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS lake")
    con.execute("CREATE SCHEMA lake.silver")
    con.execute("CREATE TABLE lake.silver.entities (entity_id VARCHAR, label_ids VARCHAR[])")
    con.execute(
        "INSERT INTO lake.silver.entities VALUES ('sensor.a', ['skip_me']), "
        "('sensor.b', ['hidden']), ('sensor.c', ['xomtra_no_analysis']), ('sensor.d', NULL)"
    )
    assert exclusions.load_no_analysis(con) == {"sensor.c"}
    custom = exclusions.ExclusionLabels(no_ingest="hidden", no_analysis="skip_me")
    assert exclusions.load_no_analysis(con, labels=custom) == {"sensor.a", "sensor.b"}
