"""Start order of the compose stack (deploy/compose).

Every service that reads the secrets volume must wait for secrets-init, directly or
through its dependencies. Otherwise a plain ``docker compose up -d`` starts it alongside
secrets-init on the first start, it exits before its file exists, and compose stops
with "dependency failed to start: container ... is unhealthy".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose"


class _Loader(yaml.SafeLoader):
    """Reads compose's merge tags (!override, !reset) as plain values."""


def _tagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)  # type: ignore[arg-type]


_Loader.add_multi_constructor("!", _tagged)


def _load(name: str) -> dict[str, Any]:
    data = yaml.load((COMPOSE / name).read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506
    services: dict[str, Any] = data["services"]
    return services


def _merged(*files: str) -> dict[str, dict[str, Any]]:
    """Services after the override files; depends_on is replaced, as with !override."""
    services: dict[str, dict[str, Any]] = {}
    for name in files:
        for service, spec in _load(name).items():
            merged = dict(services.get(service, {}))
            merged.update(spec or {})
            services[service] = merged
    return {s: spec for s, spec in services.items() if "full" not in spec.get("profiles", [])}


def _needs(services: dict[str, dict[str, Any]], service: str) -> set[str]:
    seen: set[str] = set()
    todo = [service]
    while todo:
        for dep in services.get(todo.pop(), {}).get("depends_on", {}) or {}:
            if dep not in seen:
                seen.add(dep)
                todo.append(dep)
    return seen


@pytest.mark.parametrize(
    "files",
    [("compose.yaml",), ("compose.yaml", "compose.lite.yaml")],
    ids=["full", "lite"],
)
def test_secrets_readers_wait_for_secrets_init(files: tuple[str, ...]) -> None:
    services = _merged(*files)
    readers = [
        name
        for name, spec in services.items()
        if name != "secrets-init"
        and any(str(v).startswith("secrets:") for v in spec.get("volumes", []))
    ]
    assert readers
    missing = [name for name in readers if "secrets-init" not in _needs(services, name)]
    assert not missing, f"start before secrets-init has written their files: {missing}"
