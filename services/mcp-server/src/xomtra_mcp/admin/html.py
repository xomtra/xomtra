"""Minimal server-side HTML (no template engine, everything escaped)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from typing import Any

from xomtra_mcp.admin import i18n, view

# Colours, type and components follow the xomtra.com web (graphite, indigo accent,
# amber signal, Geist / Geist Mono). Dark by default like the web; the light variant
# only when Home Assistant is in light mode (ui.js sets data-theme).
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
    + _tokens(DARK, "dark")
    + "}\n:root[data-theme=light] { "
    + _tokens(LIGHT, "light")
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
button.danger { background:var(--bad); border-color:var(--bad); color:var(--bg); }
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
[data-tip] { cursor:help; text-underline-offset:3px;
  text-decoration:underline dotted color-mix(in srgb, var(--muted) 60%, transparent); }
.tip { position:fixed; z-index:20; max-width:320px; padding:9px 12px; border-radius:9px;
  background:var(--raise); color:var(--ink); border:1px solid var(--line);
  font:400 13px/1.45 var(--sans); letter-spacing:0; text-transform:none;
  box-shadow:0 12px 30px -12px rgba(0,0,0,.5); pointer-events:none; }
figure.flow { margin:0 0 18px; background:var(--surface); border:1px solid var(--line);
  border-radius:12px; padding:16px; }
figure.flow svg { display:block; width:100%; height:auto; }
figure.flow .tall { display:none; max-width:360px; margin:0 auto; }
figure.flow figcaption { color:var(--muted); font-size:13.5px; margin-top:10px; }
.flow .box { fill:var(--bg); stroke:var(--line); stroke-width:1.5; }
.flow .box.us { stroke:var(--accent); }
.flow .t { fill:var(--ink); font:600 17px var(--sans); letter-spacing:-.02em; }
.flow .s { fill:var(--muted); font:400 12.5px var(--sans); }
.flow .m { fill:var(--signal); font:500 11.5px var(--mono); }
.flow .a { fill:var(--muted); font:500 11.5px var(--mono); letter-spacing:.02em; }
.flow .ln { stroke:var(--muted); stroke-width:1.5; fill:none; }
.flow .ln.req { stroke:var(--accent); }
.flow .ln.ro { stroke-dasharray:5 4; }
.flow .hd { fill:var(--muted); } .flow .hd.req { fill:var(--accent); }
.flow .hot rect { fill:var(--ink); }
.flow .hot text { fill:var(--bg); font:500 13px var(--sans); }
.flow .hot:hover rect { fill:var(--accent); }
.flow .hot:focus-visible { outline:2px solid var(--accent); }
form:target { outline:2px solid var(--accent); outline-offset:6px; border-radius:8px; }
@media (max-width:700px) { figure.flow .wide { display:none; }
  figure.flow .tall { display:block; } }
@media (max-width:700px) { header { position:static; padding-inline:16px; }
  main { padding-inline:16px; }
  header form, header .who { margin-left:0; } }
"""
)

# Explanations of the columns and cards, in the languages of xomtra.com; ui.js shows the
# one of Home Assistant's language on hover or focus, the English one is the fallback.
TIPS: dict[str, dict[str, str]] = json.loads(
    (Path(__file__).parent / "static" / "tips.json").read_text(encoding="utf-8")
)
# Version of the stylesheet and script in their URLs: browsers keep them cached (Cache-
# Control max-age), and after an upgrade the new pages would otherwise get the old CSS.
ASSET_VERSION = hashlib.sha256(
    CSS.encode()
    + b"".join(
        (Path(__file__).parent / "static" / name).read_bytes()
        for name in ("ui.js", "tips.json", "favicon.svg")
    )
).hexdigest()[:12]
# Columns holding a share in [0, 1], shown as a percentage.
PERCENT = frozenset({"numeric_share", "missing_share", "with area"})
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

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
    shown = escape(i18n.translate(title, v.lang))
    return i18n.translate(
        f"<!doctype html><html lang={v.lang}><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{shown} · xomtra admin</title>"
        f'<link rel=icon href="{v.prefix}/favicon.svg?v={ASSET_VERSION}" type="image/svg+xml">'
        f'<script src="{v.prefix}/ui.js?v={ASSET_VERSION}"></script>'
        f'<link rel=stylesheet href="{v.prefix}/static.css?v={ASSET_VERSION}"></head><body>'
        f'<header><a class=brand href="{v.prefix}/">{MARK}<b>xomtra</b><small>admin</small></a>'
        f"<nav>{links}</nav>{logout}</header>"
        f"<main><h1>{shown}</h1>{body}</main></body></html>",
        v.lang,
    )


def _tip(label: str) -> str:
    text = TIPS["en"].get(label)
    return f' data-tip="{escape(label)}" title="{escape(text)}" tabindex=0' if text else ""


def _num(value: float, pct: bool) -> str:
    """A number with digit grouping; ui.js reformats it in Home Assistant's locale."""
    if pct:
        shown = f"{value:.1%}"
    elif isinstance(value, int):
        shown = f"{value:,}"
    elif abs(value) >= 1000:
        shown = f"{value:,.0f}"
    else:
        shown = f"{value:.4g}"
    kind = "p" if pct else "n"
    return f'<data class="{kind}" value="{value!r}">{escape(shown)}</data>'


def _when(value: datetime | date) -> str:
    """A time in UTC; ui.js shows it in Home Assistant's time zone and date format."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        iso = value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        shown = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    else:
        iso = shown = value.isoformat()
    return f'<time datetime="{iso}">{escape(shown)}</time>'


def fmt(value: Any, *, pct: bool = False) -> str:
    """A value for a table cell or card: numbers grouped, shares as %, times as <time>."""
    if isinstance(value, bool) or value is None:
        return e(value)
    if isinstance(value, (int, float)):
        return _num(value, pct)
    if isinstance(value, (datetime, date)):
        return _when(value)
    if isinstance(value, str) and _ISO.match(value):
        try:
            return _when(datetime.fromisoformat(value))
        except ValueError:
            pass
    return e(value)


def table(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    numeric: set[int] | None = None,
    *,
    trusted_html: set[int] | None = None,
) -> str:
    """``trusted_html``: columns holding markup built by the app itself (not escaped)."""
    numeric, trusted = numeric or set(), trusted_html or set()

    pct = {i for i, c in enumerate(columns) if c in PERCENT}

    def cell(i: int, v: Any) -> str:
        if i in trusted:
            return f"<td>{v}</td>"
        shown = fmt(v, pct=i in pct)
        return f'<td class="num">{shown}</td>' if i in numeric else f"<td>{shown}</td>"

    head = "".join(
        f"<th{' class="num"' if i in numeric else ''}{_tip(c)}>{escape(c)}</th>"
        for i, c in enumerate(columns)
    )
    body = "".join("<tr>" + "".join(cell(i, v) for i, v in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><tr>{head}</tr>{body}</table></div>'


def cards(items: Iterable[tuple[str, Any, str]]) -> str:
    """(label, value, css class of the value)."""
    return (
        '<div class="cards">'
        + "".join(
            f'<div class="card"><div class="muted"><span{_tip(label)}>{escape(label)}</span></div>'
            f'<div class="v {escape(cls)}">{fmt(value)}</div></div>'
            for label, value, cls in items
        )
        + "</div>"
    )


# Presentation attributes the stylesheet overrides; they keep the picture readable in
# both themes when the stylesheet is missing or stale (an SVG shape is black by default).
_FB = "#959cab"
_FB_LINE = f"fill='none' stroke='{_FB}' stroke-width='1.5'"


def _defs(p: str) -> str:
    """Arrow heads; ids per picture, a marker in a hidden SVG does not render."""
    return (
        "<defs>"
        + "".join(
            f"<marker id='{p}{n}' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' "
            f"markerHeight='7' orient='auto-start-reverse'><path class='{cls}' "
            f"fill='{_FB}' d='M0 0L10 5L0 10z'/></marker>"
            for n, cls in (("a", "hd"), ("b", "hd req"))
        )
        + "</defs>"
    )


def _box(
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    title: str,
    lines: Sequence[str],
    us: bool = False,
    mono: str = "",
) -> str:
    out = (
        f"<rect class='box{' us' if us else ''}' {_FB_LINE} x='{x}' y='{y}' width='{w}' "
        f"height='{h}' rx='12'/>"
    )
    out += f"<text class='t' fill='{_FB}' x='{x + 18}' y='{y + 32}'>{escape(title)}</text>"
    for i, line in enumerate(lines):
        y_line = y + 56 + i * 18
        out += f"<text class='s' fill='{_FB}' x='{x + 18}' y='{y_line}'>{escape(line)}</text>"
    if mono:
        out += f"<text class='m' fill='{_FB}' x='{x + 18}' y='{y + h - 16}'>{escape(mono)}</text>"
    return out


def _line(cls: str, marker: str, d: str) -> str:
    return f"<path class='{cls}' {_FB_LINE} marker-end='url(#{marker})' d='{d}'/>"


def _label(x: int, y: int, text: str) -> str:
    return f"<text class='a' fill='{_FB}' x='{x}' y='{y}'>{escape(text)}</text>"


def _hotspot(x: int, y: int, w: int, href: str, label: str, *, download: bool = False) -> str:
    """A clickable pill on the picture (an SVG link, no script)."""
    return (
        f"<a class='hot' href='{escape(href)}'{' download' if download else ''}>"
        f"<rect fill='#7d8bff' x='{x}' y='{y}' width='{w}' height='30' rx='15'/>"
        f"<text fill='#0b0c0f' x='{x + w // 2}' y='{y + 20}' text-anchor='middle'>"
        f"{escape(label)}</text></a>"
    )


AGENT = ("LLM agent", ("Claude Desktop, Claude Code", "or another MCP client"), "")
XOMTRA = (
    "xomtra add-on",
    ("MCP server, checks the token", "own copy of the history,", "analyses"),
    "",
)
HA = ("Home Assistant", ("entities, states", "and statistics"), "read only")


def token_flow(prefix: str, mcpb: str) -> str:
    """How a token connects an LLM agent, xomtra and Home Assistant (Tokens page).

    The boxes carry the page's actions: download the Claude Desktop extension on the
    agent, issue a token on xomtra (jumps to the form, ``#issue``).
    """
    get = _hotspot(
        22, 144, 210, f"{prefix}/connect/{mcpb}", "↓ Claude Desktop extension", download=True
    )
    issue = _hotspot(363, 144, 170, "#issue", "+ Issue a token")
    get_t = _hotspot(
        38, 86, 210, f"{prefix}/connect/{mcpb}", "↓ Claude Desktop extension", download=True
    )
    issue_t = _hotspot(38, 316, 170, "#issue", "+ Issue a token")
    wide = (
        "<svg class='wide' viewBox='0 0 960 236' role='img' aria-labelledby='flow-t'>"
        "<title id='flow-t'>LLM agent sends requests with the token to xomtra; xomtra answers "
        "from its own copy of the Home Assistant history, which it reads from Home Assistant."
        "</title>"
        + _defs("w")
        + _box(4, 40, 270, 150, title=AGENT[0], lines=AGENT[1], mono=AGENT[2])
        + _box(345, 40, 270, 150, title=XOMTRA[0], lines=XOMTRA[1], us=True, mono=XOMTRA[2])
        + _box(686, 40, 270, 150, title=HA[0], lines=HA[1], mono=HA[2])
        + get
        + issue
        + _line("ln req", "wb", "M278 88H339")
        + _label(280, 24, "1 request")
        + _label(280, 38, "+ Bearer token")
        + _line("ln", "wa", "M341 142H282")
        + _label(280, 212, "2 answer: data,")
        + _label(280, 226, "findings")
        + _line("ln ro", "wa", "M682 115H621")
        + _label(621, 24, "3 reads")
        + _label(621, 38, "continuously")
        + "</svg>"
    )
    # Hidden by attribute as well as by the stylesheet, so that only one picture shows
    # without it.
    tall = (
        "<svg class='tall' display='none' viewBox='0 0 330 560' role='img' "
        "aria-labelledby='flow-t2'>"
        "<title id='flow-t2'>LLM agent, xomtra and Home Assistant</title>"
        + _defs("t")
        + _box(20, 0, 290, 130, title=AGENT[0], lines=AGENT[1], mono=AGENT[2])
        + _box(20, 210, 290, 150, title=XOMTRA[0], lines=XOMTRA[1], us=True, mono=XOMTRA[2])
        + _box(20, 440, 290, 120, title=HA[0], lines=HA[1], mono=HA[2])
        + get_t
        + issue_t
        + _line("ln req", "tb", "M110 134V204")
        + _label(120, 165, "1 request")
        + _label(120, 180, "+ token")
        + _line("ln", "ta", "M230 206V138")
        + _label(238, 165, "2 answer")
        + _line("ln ro", "ta", "M165 436V366")
        + _label(175, 405, "3 reads")
        + "</svg>"
    )
    return (
        f"<figure class='flow'>{wide}{tall}<figcaption>Issue a token and paste it into the "
        "agent; for Claude Desktop download the extension, it asks for the token. The agent "
        "sends every request with it to xomtra (1); xomtra checks it and answers with data "
        "and findings within the token's role (2). xomtra reads Home Assistant on its own "
        "and only reads (3): the agent never talks to Home Assistant and cannot switch "
        "anything. The model sees only the answers it asked for.</figcaption>"
        "</figure>"
    )
