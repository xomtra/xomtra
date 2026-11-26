"""Translation of the admin GUI into Home Assistant's languages (cs, de, pl, nl).

The pages are written in English. :func:`translate` translates the finished page from
``static/i18n.json``: a text node that equals a catalog entry, a long entry (three or
more words) anywhere in a text node, a short one at its start or end (the pieces of a
sentence around a value), and column names only in table headers. Code, preformatted
text and scripts stay as they are, and so does everything that is not in the catalog
(entity names, values). The language comes from the ``xomtra_lang`` cookie, which
ui.js sets from Home Assistant's language, or else from the browser's Accept-Language.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

LANGS = ("en", "cs", "de", "pl", "nl")
COOKIE = "xomtra_lang"
CATALOG = Path(__file__).parent / "static" / "i18n.json"

_TAG = re.compile(r"(<[^>]*>)")
_SKIP = re.compile(r"<(/?)(code|pre|script|style)\b", re.IGNORECASE)
_ATTR = re.compile(r"""(placeholder=)(['"])(.*?)\2""")


def pick(cookie: str | None, accept_language: str | None) -> str:
    """The GUI language: the cookie if valid, else the first supported Accept-Language."""
    if cookie in LANGS:
        return str(cookie)
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower().split("-")[0]
        if code in LANGS:
            return code
    return "en"


@cache
def _catalog(
    lang: str,
) -> tuple[dict[str, str], list[tuple[str, str]], dict[str, str], dict[str, str]]:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    text: dict[str, str] = data["text"].get(lang, {})
    long = sorted(
        ((k, v) for k, v in text.items() if len(k.split()) >= 3),
        key=lambda kv: -len(kv[0]),
    )
    return text, long, data["attr"].get(lang, {}), data["th"].get(lang, {})


def _text(node: str, text: dict[str, str], long: list[tuple[str, str]]) -> str:
    core = node.strip()
    if not core:
        return node
    lead = node[: len(node) - len(node.lstrip())]
    trail = node[len(node.rstrip()) :]
    if core in text:
        return lead + text[core] + trail
    out = core
    for key, value in long:
        if key in out:
            out = out.replace(key, value)
    for key, value in text.items():
        if len(key.split()) >= 3:
            continue
        if out.startswith(key + " "):
            out = value + out[len(key) :]
        if out.endswith(" " + key):
            out = out[: -len(key)] + value
    return lead + out + trail


def _attr(m: re.Match[str], attr: dict[str, str]) -> str:
    quote = m.group(2)
    return m.group(1) + quote + attr.get(m.group(3), m.group(3)) + quote


def translate(html: str, lang: str) -> str:
    """Translate a finished page into ``lang``; English and unknown languages pass."""
    if lang not in LANGS or lang == "en":
        return html
    text, long, attr, th = _catalog(lang)
    parts = _TAG.split(html)
    skip = 0
    for i, part in enumerate(parts):
        if i % 2:  # a tag
            m = _SKIP.match(part)
            if m:
                skip += -1 if m.group(1) else 1
                skip = max(skip, 0)
            elif "placeholder=" in part:
                parts[i] = _ATTR.sub(lambda a: _attr(a, attr), part)
        elif not skip and part:
            header = i > 0 and parts[i - 1].startswith("<th")
            parts[i] = th.get(part, part) if header and part in th else _text(part, text, long)
    return "".join(parts)
