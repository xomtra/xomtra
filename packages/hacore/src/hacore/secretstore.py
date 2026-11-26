"""Internal secrets of the compose edition in a volume (ADR 077).

``python -m hacore.secrets_init`` runs as the one-shot service ``secrets-init`` on
every ``docker compose up``, before everything else:

- first start: generates every internal password and key into ``master.env`` of the
  secrets volume (root only). An installation that still has them in ``.env`` (ADR 061,
  ``init-env.sh``) passes them in the environment and they are taken over instead, so
  that the Postgres roles and the S3 keys keep matching;
- later starts: keeps every value, only adds keys introduced by a newer version;
- every start: writes one file per consumer with only the values it needs, under the
  variable names it reads, owned by the user it runs as and readable only by it
  (mode 0400). Non-secret values derived at start (the server's address in the LAN)
  go there too.

The services read their file with :func:`load_env` (``XOMTRA_SECRETS_FILE``), which
the packages call on import, so ``docker compose exec`` sees the values as well.
Postgres reads ``postgres.password`` (``POSTGRES_PASSWORD_FILE``) and the shell based
helpers source their ``*.env`` file.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import secrets
import socket
import string
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_VAR = "XOMTRA_SECRETS_FILE"
DEFAULT_DIR = Path("/secrets")
MASTER = "master.env"
ALPHABET = string.ascii_letters + string.digits

# Every internal secret; the names are those of .env before ADR 077, so that an existing
# .env can be taken over as is. XOMTRA_SETTINGS_KEY encrypts the Home Assistant token in
# Postgres (ADR 077).
KEYS = (
    "POSTGRES_PASSWORD",
    "DUCKLAKE_PG_PASSWORD",
    "OPS_PG_PASSWORD",
    "AUDIT_PG_PASSWORD",
    "AUDIT_ADMIN_PG_PASSWORD",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "XOMTRA_JWT_SIGNING_KEY",
    "XOMTRA_ADMIN_SESSION_KEY",
    "XOMTRA_SETTINGS_KEY",
)
# Values the generator of init-env.sh and .env.example used as "not set".
PLACEHOLDERS = {"", "change-me"}

UID_ROOT = 0
UID_POSTGRES = 999  # postgres image
UID_XOMTRA = 10001  # xomtra-mcp and xomtra-ingest images
UID_CADDY = 10002  # xomtra-caddy image

_LAKE = {
    "XOMTRA_LAKE_PG_PASSWORD": "DUCKLAKE_PG_PASSWORD",
    "XOMTRA_S3_ACCESS_KEY": "S3_ACCESS_KEY",
    "XOMTRA_S3_SECRET_KEY": "S3_SECRET_KEY",
}


@dataclass(frozen=True, slots=True)
class Consumer:
    """One file of the volume: variable name -> master key, and its owner."""

    name: str
    uid: int
    values: Mapping[str, str]
    # Derived, non-secret values (see :func:`derived`) this consumer gets.
    derived: tuple[str, ...] = ()


CONSUMERS = (
    Consumer(
        "postgres-init",
        UID_POSTGRES,
        {
            "PGPASSWORD": "POSTGRES_PASSWORD",
            "DUCKLAKE_PASSWORD": "DUCKLAKE_PG_PASSWORD",
            "OPS_PASSWORD": "OPS_PG_PASSWORD",
            "AUDIT_PASSWORD": "AUDIT_PG_PASSWORD",
            "AUDIT_ADMIN_PASSWORD": "AUDIT_ADMIN_PG_PASSWORD",
        },
    ),
    Consumer(
        "seaweedfs", UID_ROOT, {"S3_ACCESS_KEY": "S3_ACCESS_KEY", "S3_SECRET_KEY": "S3_SECRET_KEY"}
    ),
    Consumer(
        "mcp",
        UID_XOMTRA,
        {
            **_LAKE,
            "XOMTRA_AUDIT_PG_PASSWORD": "AUDIT_PG_PASSWORD",
            "XOMTRA_JWT_SIGNING_KEY": "XOMTRA_JWT_SIGNING_KEY",
            "XOMTRA_SETTINGS_KEY": "XOMTRA_SETTINGS_KEY",
        },
        ("XOMTRA_BASE_URL",),
    ),
    Consumer(
        "admin",
        UID_XOMTRA,
        {
            **_LAKE,
            "XOMTRA_AUDIT_ADMIN_PASSWORD": "AUDIT_ADMIN_PG_PASSWORD",
            "XOMTRA_ADMIN_SESSION_KEY": "XOMTRA_ADMIN_SESSION_KEY",
            "XOMTRA_SETTINGS_KEY": "XOMTRA_SETTINGS_KEY",
        },
        ("XOMTRA_BASE_URL",),
    ),
    Consumer(
        "ingest",
        UID_XOMTRA,
        {
            **_LAKE,
            "XOMTRA_OPS_PG_PASSWORD": "OPS_PG_PASSWORD",
            "XOMTRA_SETTINGS_KEY": "XOMTRA_SETTINGS_KEY",
        },
    ),
    Consumer("caddy", UID_CADDY, {}, ("XOMTRA_LOCAL_HOST",)),
)


def generate(length: int = 40) -> str:
    """Random [A-Za-z0-9]: safe in env files, URLs and libpq strings."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def parse(text: str) -> dict[str, str]:
    """``KEY=value`` lines (no quoting: every value here is [A-Za-z0-9] or a URL)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def render(values: Mapping[str, str]) -> str:
    for key, value in values.items():
        if "\n" in value or "\n" in key:
            raise ValueError(f"{key}: line break in value")
    return "".join(f"{k}={v}\n" for k, v in values.items())


def resolve_master(
    existing: Mapping[str, str], env: Mapping[str, str]
) -> tuple[dict[str, str], list[str]]:
    """Master values and a log of what happened to each key.

    Kept values win; a missing key is taken from the environment (an existing .env)
    or generated.
    """
    master = {k: v for k, v in existing.items() if v not in PLACEHOLDERS}
    notes: list[str] = []
    for key in KEYS:
        offered = (env.get(key) or "").strip()
        if key in master:
            if offered not in PLACEHOLDERS and offered != master[key]:
                notes.append(f"{key}: kept the stored value, the one in .env is ignored")
            continue
        if offered not in PLACEHOLDERS:
            master[key] = offered
            notes.append(f"{key}: taken over from .env")
        else:
            master[key] = generate()
            notes.append(f"{key}: generated")
    return master, notes


def detect_host_address() -> str | None:
    """The host's own address in the LAN (the source address of its default route).

    ``secrets-init`` runs in the host's network namespace for this; no packet is sent.
    """
    for target in ("192.0.2.1", "2001:db8::1"):
        family = socket.AF_INET6 if ":" in target else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect((target, 9))
                address = str(s.getsockname()[0])
        except OSError:
            continue
        if address and not address.startswith(("127.", "::1", "0.")):
            return address
    return None


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def derived(env: Mapping[str, str], detected: str | None) -> dict[str, str]:
    """Non-secret values computed at start, unless set in .env (ADR 077 point 2).

    Local access (the default) needs the name or address clients use; without one in
    .env, the host's LAN address is used.
    """
    mode = (env.get("XOMTRA_ACCESS_MODE") or "local").strip()
    out: dict[str, str] = {}
    if mode == "local":
        host = (env.get("XOMTRA_LOCAL_HOST") or "").strip() or (detected or "")
        if host and not (env.get("XOMTRA_LOCAL_HOST") or "").strip():
            out["XOMTRA_LOCAL_HOST"] = host
    else:
        host = (env.get("XOMTRA_PUBLIC_HOST") or "").strip()
    base = (env.get("XOMTRA_BASE_URL") or "").strip()
    if not base and host:
        out["XOMTRA_BASE_URL"] = f"https://{_url_host(host)}"
    return out


def _write(path: Path, text: str, uid: int, mode: int = 0o400) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if os.geteuid() == 0:
        os.chown(tmp, uid, uid)
    tmp.chmod(mode)
    tmp.replace(path)


def init(
    directory: Path = DEFAULT_DIR,
    env: Mapping[str, str] | None = None,
    detected: str | None = None,
) -> dict[str, str]:
    """Creates or completes the volume; returns the master values."""
    e = os.environ if env is None else env
    directory.mkdir(parents=True, exist_ok=True)
    master_file = directory / MASTER
    existing = parse(master_file.read_text(encoding="utf-8")) if master_file.exists() else {}
    master, notes = resolve_master(existing, e)
    if master != existing:
        _write(master_file, render(master), UID_ROOT, 0o600)
    for note in notes:
        logger.info("%s", note)
    extra = derived(e, detected)
    for c in CONSUMERS:
        values = {var: master[key] for var, key in c.values.items()}
        values.update({k: v for k, v in extra.items() if k in c.derived})
        _write(directory / f"{c.name}.env", render(values), c.uid)
    _write(directory / "postgres.password", master["POSTGRES_PASSWORD"] + "\n", UID_POSTGRES)
    if "XOMTRA_LOCAL_HOST" in extra:
        logger.info("Address of this server in the LAN: %s", extra["XOMTRA_LOCAL_HOST"])
    return master


def load_env(environ: os._Environ[str] | dict[str, str] | None = None) -> list[str]:
    """Puts the values of ``$XOMTRA_SECRETS_FILE`` into the environment (if set).

    The file wins over the environment: it holds what ``secrets-init`` decided for this
    start. Returns the names that were set. A file that cannot be read is an error only
    when the variable is set, so nothing changes outside compose.
    """
    env = os.environ if environ is None else environ
    path = env.get(ENV_VAR)
    if not path:
        return []
    try:
        values = parse(Path(path).read_text(encoding="utf-8"))
    except OSError as err:
        print(f"xomtra: cannot read {ENV_VAR}={path}: {err}", file=sys.stderr)
        return []
    for key, value in values.items():
        env[key] = value
    return list(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hacore.secrets_init")
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="create or complete the secrets volume")
    p_init.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="secrets-init: %(message)s")
    if args.command == "init":
        init(args.dir, detected=detect_host_address())
        logger.info("Secrets are in place.")
    return 0
