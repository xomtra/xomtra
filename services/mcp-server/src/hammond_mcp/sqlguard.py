"""SQL guard for ``query_sql`` (ADR 039).

Accepts exactly one read-only query (SELECT, set operations, CTEs) that reads only the
data views the caller may use, and returns it re-rendered with a row limit. The guard
is the first of three layers; the query then runs on a DuckDB connection whose lake is
attached read-only, with external file access disabled and the configuration locked
(see ``hammond_mcp.data``), and the views it can see are already filtered by policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

MAX_SQL_LENGTH = 20_000

# Functions that read files or the network, run SQL from a string, or expose the
# engine's configuration and secrets. External access is also disabled in the engine;
# this list keeps the error early and explicit.
_DENIED_FUNCTION = re.compile(
    r"^(read_\w*|\w*_scan|glob|query|query_table|sniff_csv|parquet_\w*|iceberg_\w*|"
    r"delta_\w*|ducklake_\w*|duckdb_\w*|pragma_\w*|postgres_\w*|sqlite_\w*|mysql_\w*|"
    r"getenv|current_setting|which_secret|json_execute_serialized_sql|"
    r"json_serialize_sql|json_deserialize_sql|from_substrait\w*|get_substrait\w*|"
    r"load_aws_credentials|checkpoint|force_checkpoint|enable_\w*|disable_\w*|"
    r"sql_auto_complete|test_\w*|http_\w*|\w*_secret\w*)$"
)
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)


class SQLRejected(ValueError):  # noqa: N818 - message is shown to the caller
    """The query is not allowed."""


@dataclass(frozen=True, slots=True)
class GuardedQuery:
    sql: str
    views: frozenset[str]


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def check(sql: str, allowed_views: frozenset[str], *, max_rows: int) -> GuardedQuery:
    """Validate ``sql`` and return it with ``LIMIT max_rows + 1`` (to detect truncation)."""
    if len(sql) > MAX_SQL_LENGTH:
        raise SQLRejected(f"Query is longer than {MAX_SQL_LENGTH} characters")
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except ParseError as err:
        raise SQLRejected(f"Cannot parse the query: {err.errors[0]['description']}") from err
    if len(statements) != 1:
        raise SQLRejected("Exactly one statement is allowed")
    root = statements[0]
    if not isinstance(root, _ALLOWED_ROOTS):
        raise SQLRejected("Only SELECT queries are allowed")

    for node in root.walk():
        if isinstance(node, (exp.DML, exp.DDL, exp.Command, exp.Pragma, exp.Set)):
            raise SQLRejected(f"{node.key.upper()} is not allowed")

    used = _check_tables(root, allowed_views)

    for func in root.find_all(exp.Func):
        name = _function_name(func)
        if _DENIED_FUNCTION.match(name):
            raise SQLRejected(f"Function {name}() is not allowed")

    limit = root.args.get("limit")
    current = _literal_limit(limit)
    if current is None or current > max_rows + 1:
        root = root.limit(max_rows + 1, copy=False)
    return GuardedQuery(sql=root.sql(dialect="duckdb"), views=frozenset(used))


def _check_tables(root: exp.Expression, allowed_views: frozenset[str]) -> set[str]:
    """Every table must be a permitted view (or a CTE of the query); returns the views."""
    ctes = {str(c.alias_or_name).lower() for c in root.find_all(exp.CTE)}
    used: set[str] = set()
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SQLRejected("Table functions and file paths are not allowed")
        if table.args.get("db") or table.args.get("catalog"):
            raise SQLRejected(
                f"Use the view names without a schema ({table.sql(dialect='duckdb')})"
            )
        name = str(table.name).lower()
        if name in ctes:
            continue
        if name not in allowed_views:
            available = ", ".join(sorted(allowed_views)) or "none"
            raise SQLRejected(f"Unknown or not permitted view {name!r} (available: {available})")
        used.add(name)
    return used


def _literal_limit(limit: exp.Expression | None) -> int | None:
    if limit is None:
        return None
    value = limit.args.get("expression")
    if isinstance(value, exp.Literal) and value.is_int:
        return int(value.this)
    return None
