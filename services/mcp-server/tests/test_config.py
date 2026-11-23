import pytest
from pydantic import ValidationError

from hammond_mcp.config import CLAUDE_REDIRECT_URIS, Settings

ENTRA = {
    "HAMMOND_BASE_URL": "https://mcp.example.org",
    "HAMMOND_ENTRA_TENANT_ID": "00000000-0000-0000-0000-000000000001",
    "HAMMOND_ENTRA_CLIENT_ID": "00000000-0000-0000-0000-000000000002",
    "HAMMOND_ENTRA_CLIENT_SECRET": "s3cr3t-value",
    "HAMMOND_JWT_SIGNING_KEY": "x" * 48,
    "HAMMOND_AUDIT_PG_HOST": "postgres",
    "HAMMOND_AUDIT_PG_PASSWORD": "audit-pw",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for key in list(os.environ):
        if key.startswith("HAMMOND_"):
            monkeypatch.delenv(key)


def _set(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_entra_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, ENTRA)
    s = Settings()
    assert s.auth_mode == "entra"
    assert s.entra_required_scopes == ["mcp.access"]
    assert s.allowed_client_redirect_uris == CLAUDE_REDIRECT_URIS
    assert "s3cr3t-value" not in repr(s)


def test_csv_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, ENTRA | {"HAMMOND_ENTRA_REQUIRED_SCOPES": "a, b"})
    assert Settings().entra_required_scopes == ["a", "b"]


def test_entra_requires_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, {k: v for k, v in ENTRA.items() if k != "HAMMOND_JWT_SIGNING_KEY"})
    with pytest.raises(ValidationError, match="HAMMOND_JWT_SIGNING_KEY"):
        Settings()


def test_short_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, ENTRA | {"HAMMOND_JWT_SIGNING_KEY": "short"})
    with pytest.raises(ValidationError, match="32"):
        Settings()


@pytest.mark.parametrize("tenant", ["common", "organizations", "consumers"])
def test_multi_tenant_rejected(monkeypatch: pytest.MonkeyPatch, tenant: str) -> None:
    _set(monkeypatch, ENTRA | {"HAMMOND_ENTRA_TENANT_ID": tenant})
    with pytest.raises(ValidationError, match="tenant"):
        Settings()


def test_no_auth_only_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, {"HAMMOND_AUTH_MODE": "none", "HAMMOND_HOST": "0.0.0.0"})
    with pytest.raises(ValidationError, match="loopback"):
        Settings()
    monkeypatch.setenv("HAMMOND_HOST", "127.0.0.1")
    assert Settings().auth_mode == "none"


def test_entra_requires_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {k: v for k, v in ENTRA.items() if not k.startswith("HAMMOND_AUDIT")}
    _set(monkeypatch, env)
    with pytest.raises(ValidationError, match="HAMMOND_AUDIT_PG_PASSWORD"):
        Settings()


def test_audit_dsn_and_empty_policy_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, ENTRA | {"HAMMOND_POLICY_FILE": "", "HAMMOND_AUDIT_PG_PASSWORD": "p'w"})
    s = Settings()
    assert s.policy_file is None
    assert s.audit_dsn is not None
    assert "password='p\\'w'" in s.audit_dsn
    assert "dbname='audit'" in s.audit_dsn
