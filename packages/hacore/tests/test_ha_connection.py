"""Home Assistant connection from the environment or the admin GUI (ADR 077)."""

import pytest

from hacore import ha_connection as hc


def test_token_round_trip_and_wrong_key() -> None:
    blob = hc.encrypt_token("k" * 40, "secret-token")
    assert "secret-token" not in blob
    assert hc.decrypt_token("k" * 40, blob) == "secret-token"
    with pytest.raises(hc.StoreError, match="cannot be decrypted"):
        hc.decrypt_token("j" * 40, blob)
    with pytest.raises(hc.StoreError, match="32 characters"):
        hc.encrypt_token("short", "x")


def test_normalize_url() -> None:
    assert hc.normalize_url(" http://ha.lan:8123/ ") == "http://ha.lan:8123"
    with pytest.raises(ValueError, match="http"):
        hc.normalize_url("ha.lan:8123")


def test_resolver_prefers_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object) -> None:
        raise AssertionError("store must not be read")

    monkeypatch.setattr(hc, "load_stored", boom)
    r = hc.Resolver("http://ha:8123/", "t", "dsn", "k" * 40)
    assert r.from_env
    assert not r.storable
    conn = r.get()
    assert conn is not None
    assert (conn.url, conn.token, conn.source) == ("http://ha:8123", "t", "env")


def test_resolver_reads_the_store_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    stored = hc.HAConnection("http://ha:8123", "t", "stored")

    def load(dsn: str, key: str) -> hc.HAConnection | None:
        calls.append(dsn)
        return stored if len(calls) > 1 else None

    monkeypatch.setattr(hc, "load_stored", load)
    r = hc.Resolver(None, None, "dsn", "k" * 40, ttl=60)
    assert r.storable
    assert r.get() is None
    assert r.get() is None  # cached
    r.invalidate()
    assert r.get() == stored
    assert len(calls) == 2


def test_resolver_survives_store_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: object) -> None:
        raise hc.StoreError("down")

    monkeypatch.setattr(hc, "load_stored", fail)
    assert hc.Resolver(None, None, "dsn", "k" * 40).get() is None
    assert hc.Resolver(None, None).get() is None
    assert not hc.Resolver(None, None).storable
