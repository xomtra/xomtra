"""Policy layer (ADR 038): Entra app roles -> allowed tools, data views and entities.

The policy is a YAML file (default: the packaged ``policy.yaml``). Every tool call goes
through :class:`xomtra_mcp.guard.PolicyMiddleware`, which resolves the caller's
:class:`Access` once and puts it into a context variable for the data layer.
"""

from __future__ import annotations

import fnmatch
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from xomtra_mcp.identity import Identity

ALL = "*"
# Tools every authenticated caller may use, whatever the roles.
ALWAYS_ALLOWED = frozenset({"whoami"})
# Entity patterns are inlined into SQL (GLOB); keep them to a safe alphabet.
_PATTERN = re.compile(r"^[a-z0-9_.*?]+$")
# Column names are inlined into SQL (EXCLUDE); plain identifiers only.
_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*$")


class PolicyError(ValueError):
    """The policy file is invalid."""


class AccessDenied(PermissionError):  # noqa: N818 - reads better at the call site
    """The caller's roles do not allow this tool, view or entity."""


@dataclass(frozen=True, slots=True)
class RoleGrant:
    tools: frozenset[str]
    views: frozenset[str]
    deny_entities: frozenset[str]
    max_rows: int
    hide_columns: frozenset[tuple[str, str]] = frozenset()  # (view, column)


@dataclass(frozen=True, slots=True)
class Access:
    """What one caller may do: the union of the grants of all of their roles."""

    roles: tuple[str, ...] = ()
    tools: frozenset[str] = ALWAYS_ALLOWED
    views: frozenset[str] = frozenset()
    deny_entities: tuple[str, ...] = ()
    max_rows: int = 0
    known_views: frozenset[str] = field(default_factory=frozenset)
    hidden_columns: frozenset[tuple[str, str]] = frozenset()  # (view, column)

    def can_call(self, tool: str) -> bool:
        return ALL in self.tools or tool in self.tools

    def can_view(self, view: str) -> bool:
        return view in self.known_views and (ALL in self.views or view in self.views)

    @property
    def allowed_views(self) -> list[str]:
        return sorted(v for v in self.known_views if self.can_view(v))

    def columns_hidden_in(self, view: str) -> list[str]:
        return sorted(c for v, c in self.hidden_columns if v == view)

    def entity_allowed(self, entity_id: str) -> bool:
        return not any(fnmatch.fnmatchcase(entity_id, p) for p in self.deny_entities)

    def require_entity(self, entity_id: str) -> None:
        if not self.entity_allowed(entity_id):
            raise AccessDenied(f"Access to {entity_id} is denied by policy")

    def require_view(self, view: str) -> None:
        if not self.can_view(view):
            raise AccessDenied(f"View {view!r} is not available to your roles")


@dataclass(frozen=True, slots=True)
class Policy:
    roles: dict[str, RoleGrant]

    @classmethod
    def load(cls, path: Path | None = None) -> Policy:
        if path is None:
            text = resources.files("xomtra_mcp").joinpath("policy.yaml").read_text("utf-8")
        else:
            text = path.read_text("utf-8")
        return cls.parse(yaml.safe_load(text))

    @classmethod
    def parse(cls, data: Any) -> Policy:
        if not isinstance(data, dict) or not isinstance(data.get("roles"), dict):
            raise PolicyError("policy must contain a 'roles' mapping")
        roles: dict[str, RoleGrant] = {}
        for name, grant in data["roles"].items():
            if not isinstance(grant, dict):
                raise PolicyError(f"role {name}: expected a mapping")
            patterns = frozenset(str(p) for p in grant.get("deny_entities") or [])
            bad = [p for p in patterns if not _PATTERN.match(p)]
            if bad:
                raise PolicyError(f"role {name}: invalid entity patterns {bad}")
            max_rows = int(grant.get("max_rows", 1000))
            if not 1 <= max_rows <= 100_000:
                raise PolicyError(f"role {name}: max_rows must be 1..100000")
            hidden = grant.get("hide_columns") or {}
            if not isinstance(hidden, dict):
                raise PolicyError(f"role {name}: hide_columns must map view -> columns")
            hide_columns = frozenset(
                (str(view), str(col)) for view, cols in hidden.items() for col in cols or []
            )
            bad = [
                f"{v}.{c}" for v, c in hide_columns if not (_COLUMN.match(v) and _COLUMN.match(c))
            ]
            if bad:
                raise PolicyError(f"role {name}: invalid hide_columns {sorted(bad)}")
            roles[str(name)] = RoleGrant(
                tools=frozenset(str(t) for t in grant.get("tools") or []),
                views=frozenset(str(v) for v in grant.get("views") or []),
                deny_entities=patterns,
                max_rows=max_rows,
                hide_columns=hide_columns,
            )
        return cls(roles=roles)

    def access_for(self, identity: Identity, known_views: frozenset[str]) -> Access:
        grants = [self.roles[r] for r in identity.roles if r in self.roles]
        if not identity.authenticated or not grants:
            return Access(roles=identity.roles, known_views=known_views)
        denied = frozenset.intersection(*(g.deny_entities for g in grants))
        hidden = frozenset.intersection(*(g.hide_columns for g in grants))
        return Access(
            roles=identity.roles,
            tools=frozenset().union(*(g.tools for g in grants)) | ALWAYS_ALLOWED,
            views=frozenset().union(*(g.views for g in grants)),
            deny_entities=tuple(sorted(denied)),
            max_rows=max(g.max_rows for g in grants),
            known_views=known_views,
            hidden_columns=hidden,
        )


_current: ContextVar[Access | None] = ContextVar("xomtra_access", default=None)


def set_current_access(access: Access) -> object:
    return _current.set(access)


def reset_current_access(token: object) -> None:
    _current.reset(token)  # type: ignore[arg-type]


def current_access() -> Access:
    access = _current.get()
    if access is None:  # a tool was called without the policy middleware
        raise AccessDenied("No access context")
    return access
