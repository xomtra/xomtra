"""Secrets volume of the compose edition (ADR 077)."""

import stat
from pathlib import Path

import pytest

from hacore import secretstore as ss


def read(path: Path) -> dict[str, str]:
    return ss.parse(path.read_text())


def test_first_start_generates_everything(tmp_path: Path) -> None:
    master = ss.init(tmp_path, env={}, detected="192.0.2.10")
    assert set(master) == set(ss.KEYS)
    assert all(len(v) == 40 and v.isalnum() for v in master.values())
    assert len(set(master.values())) == len(master)
    assert stat.S_IMODE((tmp_path / "master.env").stat().st_mode) == 0o600
    for name in ("postgres-init", "seaweedfs", "mcp", "admin", "ingest", "caddy"):
        assert stat.S_IMODE((tmp_path / f"{name}.env").stat().st_mode) == 0o400
    mcp = read(tmp_path / "mcp.env")
    assert mcp["XOMTRA_LAKE_PG_PASSWORD"] == master["DUCKLAKE_PG_PASSWORD"]
    assert mcp["XOMTRA_AUDIT_PG_PASSWORD"] == master["AUDIT_PG_PASSWORD"]
    assert mcp["XOMTRA_BASE_URL"] == "https://192.0.2.10"
    assert "XOMTRA_AUDIT_ADMIN_PASSWORD" not in mcp  # only what the MCP server needs
    admin = read(tmp_path / "admin.env")
    assert admin["XOMTRA_ADMIN_SESSION_KEY"] == master["XOMTRA_ADMIN_SESSION_KEY"]
    assert admin["XOMTRA_SETTINGS_KEY"] == master["XOMTRA_SETTINGS_KEY"]
    assert "XOMTRA_JWT_SIGNING_KEY" not in admin
    assert read(tmp_path / "caddy.env") == {"XOMTRA_LOCAL_HOST": "192.0.2.10"}
    assert read(tmp_path / "postgres-init.env")["PGPASSWORD"] == master["POSTGRES_PASSWORD"]
    assert (tmp_path / "postgres.password").read_text() == master["POSTGRES_PASSWORD"] + "\n"


def test_later_starts_keep_values_and_add_new_keys(tmp_path: Path) -> None:
    first = ss.init(tmp_path, env={})
    assert ss.init(tmp_path, env={}) == first
    # An older volume without a key of a newer version.
    lines = [f"{k}={v}" for k, v in first.items() if k != "XOMTRA_SETTINGS_KEY"]
    (tmp_path / "master.env").chmod(0o600)
    (tmp_path / "master.env").write_text("\n".join(lines) + "\n")
    later = ss.init(tmp_path, env={})
    assert {k: v for k, v in later.items() if k != "XOMTRA_SETTINGS_KEY"} == {
        k: v for k, v in first.items() if k != "XOMTRA_SETTINGS_KEY"
    }
    assert len(later["XOMTRA_SETTINGS_KEY"]) == 40


def test_existing_env_is_taken_over(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    env = {"POSTGRES_PASSWORD": "old-pg", "S3_SECRET_KEY": "old-s3", "OPS_PG_PASSWORD": "change-me"}
    master = ss.init(tmp_path, env=env)
    assert master["POSTGRES_PASSWORD"] == "old-pg"
    assert master["S3_SECRET_KEY"] == "old-s3"
    assert master["OPS_PG_PASSWORD"] not in {"", "change-me"}
    # The stored value wins over a later, different value in .env.
    again, notes = ss.resolve_master(master, {"POSTGRES_PASSWORD": "other"})
    assert again["POSTGRES_PASSWORD"] == "old-pg"
    assert any("ignored" in n for n in notes)


def test_derived_values_respect_env() -> None:
    assert ss.derived({}, "192.0.2.10") == {
        "XOMTRA_LOCAL_HOST": "192.0.2.10",
        "XOMTRA_BASE_URL": "https://192.0.2.10",
    }
    assert ss.derived({"XOMTRA_LOCAL_HOST": "xomtra.lan"}, "192.0.2.10") == {
        "XOMTRA_BASE_URL": "https://xomtra.lan"
    }
    assert ss.derived({"XOMTRA_BASE_URL": "https://x"}, "192.0.2.10") == {
        "XOMTRA_LOCAL_HOST": "192.0.2.10"
    }
    assert ss.derived({}, "2001:db8::5")["XOMTRA_BASE_URL"] == "https://[2001:db8::5]"
    assert ss.derived(
        {"XOMTRA_ACCESS_MODE": "public", "XOMTRA_PUBLIC_HOST": "m.example.org"}, "x"
    ) == {"XOMTRA_BASE_URL": "https://m.example.org"}
    assert ss.derived({}, None) == {}


def test_load_env(tmp_path: Path) -> None:
    file = tmp_path / "mcp.env"
    file.write_text("# comment\nXOMTRA_A=1\nXOMTRA_B=https://x/y=z\n")
    env = {"XOMTRA_A": "0"}
    assert ss.load_env(env) == []  # not set: nothing happens
    env[ss.ENV_VAR] = str(file)
    assert ss.load_env(env) == ["XOMTRA_A", "XOMTRA_B"]
    assert env["XOMTRA_A"] == "1"
    assert env["XOMTRA_B"] == "https://x/y=z"
    env[ss.ENV_VAR] = str(tmp_path / "missing.env")
    assert ss.load_env(env) == []


def test_render_refuses_line_breaks() -> None:
    with pytest.raises(ValueError, match="line break"):
        ss.render({"A": "x\ny"})
