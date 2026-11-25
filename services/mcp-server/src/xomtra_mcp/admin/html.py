"""Minimal server-side HTML (no template engine, everything escaped)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape
from typing import Any

from xomtra_mcp.admin import view

CSS = """
:root { --fg:#1d2330; --muted:#5c6475; --bg:#f6f7f9; --card:#fff;
  --line:#dde1e7; --accent:#1f5fbf; --bad:#b42318; --ok:#067647; --warn:#a15c07; }
@media (prefers-color-scheme: dark) { :root { --fg:#e6e8ec; --muted:#a3aab8; --bg:#14171c;
  --card:#1c2027; --line:#2c323c; --accent:#7aa7f0; --bad:#f97066; --ok:#47cd89;
  --warn:#fdb022; } }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.45 system-ui, sans-serif; color:var(--fg); background:var(--bg); }
header { display:flex; gap:16px; align-items:center; flex-wrap:wrap; padding:12px 20px;
  background:var(--card); border-bottom:1px solid var(--line); }
header strong { margin-right:12px; }
header a { color:var(--fg); text-decoration:none; }
header a:hover { color:var(--accent); }
header form, header .who { margin-left:auto; }
main { padding:20px; max-width:1300px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 16px; }
h2 { font-size:16px; margin:24px 0 8px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:12px 14px; }
.card .v { font-size:22px; font-weight:600; }
.muted { color:var(--muted); }
.ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
.scroll { overflow-x:auto; background:var(--card); border:1px solid var(--line);
  border-radius:8px; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line);
  vertical-align:top; }
th { position:sticky; top:0; background:var(--card); }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
code { font-size:12px; }
input, select, button { font:inherit; padding:5px 8px; border:1px solid var(--line);
  border-radius:6px; background:var(--card); color:var(--fg); }
button { cursor:pointer; }
form.filters { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
pre { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px;
  overflow-x:auto; font-size:12px; }
input.token { font-family:ui-monospace, monospace; max-width:100%; }
"""

NAV = (
    ("/", "Overview"),
    ("/catalog", "Catalog"),
    ("/drift", "Drift"),
    ("/retention", "Retention"),
    ("/findings", "Findings"),
    ("/analysis", "Analysis settings"),
    ("/audit", "Audit"),
    ("/tokens", "Tokens"),
    ("/dagster/", "Dagster"),
)


def e(value: Any) -> str:
    if value is None:
        return '<span class="muted">-</span>'
    if isinstance(value, float):
        return escape(f"{value:.4g}")
    if isinstance(value, (list, tuple)):
        return escape(", ".join(str(v) for v in value))
    return escape(str(value))


def page(title: str, body: str, user: str | None) -> str:
    v = view.current.get()
    nav = (item for item in NAV if v.dagster or item[0] != "/dagster/")
    links = "".join(f'<a href="{v.prefix}{path}">{escape(label)}</a>' for path, label in nav)
    logout = (
        f'<form method="post" action="{v.prefix}/logout"><span class="muted">{e(user)}</span> '
        '<button type="submit">Sign out</button></form>'
        if user and v.sign_out
        else f'<span class="muted who">{e(user)}</span>'
        if user
        else ""
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)} · Xomtra admin</title>"
        f'<link rel=stylesheet href="{v.prefix}/static.css"></head><body>'
        f"<header><strong>Xomtra admin</strong>{links}{logout}</header>"
        f"<main><h1>{escape(title)}</h1>{body}</main></body></html>"
    )


def table(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    numeric: set[int] | None = None,
    *,
    trusted_html: set[int] | None = None,
) -> str:
    """``trusted_html``: columns holding markup built by the app itself (not escaped)."""
    numeric, trusted = numeric or set(), trusted_html or set()

    def cell(i: int, v: Any) -> str:
        if i in trusted:
            return f"<td>{v}</td>"
        return f'<td class="num">{e(v)}</td>' if i in numeric else f"<td>{e(v)}</td>"

    head = "".join(f"<th>{escape(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(cell(i, v) for i, v in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><tr>{head}</tr>{body}</table></div>'


def cards(items: Iterable[tuple[str, Any, str]]) -> str:
    """(label, value, css class of the value)."""
    return (
        '<div class="cards">'
        + "".join(
            f'<div class="card"><div class="muted">{escape(label)}</div>'
            f'<div class="v {escape(cls)}">{e(value)}</div></div>'
            for label, value, cls in items
        )
        + "</div>"
    )
