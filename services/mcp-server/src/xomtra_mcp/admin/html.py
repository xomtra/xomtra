"""Minimal server-side HTML (no template engine, everything escaped)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape
from typing import Any

from xomtra_mcp.admin import view

# Colours, type and components follow the xomtra.com web (graphite, indigo accent,
# amber signal, Geist / Geist Mono). The web is dark only; the GUI adds a light variant
# and follows Home Assistant's mode (theme.js) or else the browser's.
DARK = {
    "bg": "#0b0c0f",
    "surface": "#121419",
    "raise": "#181b21",
    "line": "#252932",
    "ink": "#eceef3",
    "muted": "#959cab",
    "accent": "#7d8bff",
    "signal": "#ffb547",
    "ok": "#5fd4a0",
    "warn": "#ffb547",
    "bad": "#ff7a6b",
}
LIGHT = {
    "bg": "#f5f6f8",
    "surface": "#ffffff",
    "raise": "#eceef3",
    "line": "#dfe2e8",
    "ink": "#0b0c0f",
    "muted": "#5b6273",
    "accent": "#4c58d6",
    "signal": "#a4600a",
    "ok": "#12805a",
    "warn": "#a15c07",
    "bad": "#c0362c",
}


def _tokens(palette: dict[str, str], scheme: str) -> str:
    return "".join(f"--{k}:{v};" for k, v in palette.items()) + f"color-scheme:{scheme};"


CSS = (
    """
@font-face { font-family:"Geist"; src:url("fonts/Geist-Variable.woff2") format("woff2");
  font-weight:100 900; font-display:swap; }
@font-face { font-family:"Geist Mono"; src:url("fonts/GeistMono-Variable.woff2")
  format("woff2"); font-weight:100 900; font-display:swap; }
:root { --sans:"Geist","Segoe UI",system-ui,sans-serif;
  --mono:"Geist Mono",ui-monospace,Menlo,monospace; """
    + _tokens(LIGHT, "light")
    + "}\n@media (prefers-color-scheme: dark) { :root:not([data-theme=light]) { "
    + _tokens(DARK, "dark")
    + "} }\n:root[data-theme=dark] { "
    + _tokens(DARK, "dark")
    + "}\n"
    + """
* { box-sizing:border-box; }
html { background:var(--bg); }
body { margin:0; font:400 15px/1.55 var(--sans); color:var(--ink); background:var(--bg);
  -webkit-font-smoothing:antialiased; }
a { color:var(--accent); text-underline-offset:2px; }
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible,
summary:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
header { position:sticky; top:0; z-index:5; display:flex; gap:6px 18px; align-items:center;
  flex-wrap:wrap; padding:10px 20px; border-bottom:1px solid var(--line);
  background:color-mix(in srgb, var(--bg) 88%, transparent); backdrop-filter:blur(10px); }
.brand { display:flex; align-items:center; gap:9px; margin-right:10px; color:var(--ink);
  text-decoration:none; }
.brand svg { width:26px; height:26px; flex:none; }
.brand b { font:600 19px/1 var(--sans); letter-spacing:-.045em; }
.brand small { font:500 10.5px/1 var(--mono); letter-spacing:.08em; text-transform:uppercase;
  color:var(--muted); border:1px solid var(--line); border-radius:99px; padding:4px 7px; }
nav { display:flex; gap:2px 4px; flex-wrap:wrap; }
nav a { color:var(--muted); text-decoration:none; font-size:14px; padding:5px 9px;
  border-radius:7px; }
nav a:hover { color:var(--ink); }
nav a[aria-current=page] { color:var(--ink); background:var(--raise); }
header form, header .who { margin-left:auto; display:flex; gap:10px; align-items:center;
  font:400 12.5px/1 var(--mono); }
main { padding:28px 20px 56px; max-width:1300px; margin:0 auto; }
h1 { font:600 clamp(24px,3vw,30px)/1.1 var(--sans); letter-spacing:-.035em; margin:0 0 18px; }
h2 { font:500 12px/1 var(--mono); letter-spacing:.1em; text-transform:uppercase;
  color:var(--accent); margin:34px 0 12px; }
p { margin:0 0 12px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:12px; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:12px;
  padding:14px 16px; }
.card .muted { font:500 11px/1.3 var(--mono); letter-spacing:.07em; text-transform:uppercase; }
.card .v { font:500 24px/1.2 var(--mono); margin-top:8px; font-variant-numeric:tabular-nums;
  letter-spacing:-.02em; overflow-wrap:anywhere; }
.muted { color:var(--muted); }
.ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
p.ok, p.bad, p.warn { color:var(--ink); background:var(--surface); border:1px solid var(--line);
  border-left:3px solid var(--c); border-radius:8px; padding:10px 14px; }
p.ok { --c:var(--ok); } p.bad { --c:var(--bad); } p.warn { --c:var(--warn); }
.scroll + p, .scroll + h2 + p { margin-top:14px; }
.scroll { overflow-x:auto; background:var(--surface); border:1px solid var(--line);
  border-radius:12px; }
table { border-collapse:collapse; width:100%; font-size:13.5px; }
th, td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line);
  vertical-align:top; }
tr:last-child td { border-bottom:0; }
tr:hover td { background:color-mix(in srgb, var(--raise) 55%, transparent); }
th { position:sticky; top:0; background:var(--surface); color:var(--muted);
  font:500 11px/1.3 var(--mono); letter-spacing:.07em; text-transform:uppercase; }
th.num { text-align:right; }
td.num { text-align:right; font-family:var(--mono); font-size:12.5px;
  font-variant-numeric:tabular-nums; }
code, pre, input.token { font-family:var(--mono); }
code { font-size:12.5px; color:var(--signal); }
input, select, button { font:inherit; font-size:14px; padding:7px 10px;
  border:1px solid var(--line); border-radius:8px; background:var(--surface); color:var(--ink); }
input[type=checkbox] { accent-color:var(--accent); padding:0; }
button { cursor:pointer; font-weight:500; background:var(--ink); color:var(--bg);
  border-color:var(--ink); }
button:hover { opacity:.88; }
button[value=reset], button[value=preview] { background:var(--surface);
  color:var(--ink); border-color:var(--line); }
form.filters { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:14px; }
label { display:inline-flex; gap:6px; align-items:center; }
pre { background:var(--surface); border:1px solid var(--line); border-radius:10px;
  padding:12px 14px; overflow-x:auto; font-size:12.5px; }
pre code { color:inherit; }
input.token { max-width:100%; width:100%; color:var(--signal); }
details { border-top:1px solid var(--line); border-bottom:1px solid var(--line);
  padding-block:10px; margin-bottom:12px; }
summary { cursor:pointer; font-weight:500; }
ol, ul { padding-left:22px; }
li { margin-bottom:4px; }
@media (max-width:700px) { header { position:static; padding-inline:16px; }
  main { padding-inline:16px; }
  header form, header .who { margin-left:0; } }
"""
)

# xomtra mark (as on xomtra.com): two time series crossing; the amber dot is where they
# correlate. Colours come from the theme tokens.
MARK = (
    '<svg viewBox="0 0 40 40" aria-hidden="true"><rect x=".5" y=".5" width="39" height="39" '
    'rx="10" fill="var(--surface)" stroke="var(--line)"/><path d="M7 30 C16 30 24 10 33 10" '
    'fill="none" stroke="var(--accent)" stroke-width="4" stroke-linecap="round"/>'
    '<path d="M7 10 C16 10 24 30 33 30" fill="none" stroke="var(--ink)" stroke-width="4" '
    'stroke-linecap="round"/><circle cx="20" cy="20" r="4.2" fill="var(--signal)" '
    'stroke="var(--surface)" stroke-width="2"/></svg>'
)

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
    links = "".join(
        f'<a href="{v.prefix}{path}"{" aria-current=page" if label == title else ""}>'
        f"{escape(label)}</a>"
        for path, label in nav
    )
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
        f"<title>{escape(title)} · xomtra admin</title>"
        f'<link rel=icon href="{v.prefix}/favicon.svg" type="image/svg+xml">'
        f'<script src="{v.prefix}/theme.js"></script>'
        f'<link rel=stylesheet href="{v.prefix}/static.css"></head><body>'
        f'<header><a class=brand href="{v.prefix}/">{MARK}<b>xomtra</b><small>admin</small></a>'
        f"<nav>{links}</nav>{logout}</header>"
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

    head = "".join(
        f'<th class="num">{escape(c)}</th>' if i in numeric else f"<th>{escape(c)}</th>"
        for i, c in enumerate(columns)
    )
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
