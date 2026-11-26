"""Translation of the admin GUI (cs, de, pl, nl)."""

import json

from xomtra_mcp.admin import i18n


def test_pick_prefers_the_cookie_then_accept_language() -> None:
    assert i18n.pick("pl", "de") == "pl"
    assert i18n.pick("xx", "fr-FR,nl;q=0.8") == "nl"
    assert i18n.pick(None, "fr") == "en"
    assert i18n.pick(None, None) == "en"


def test_translate_keeps_values_code_and_english() -> None:
    page = (
        "<p>Less than 5% free on the disk: the retention runs early. Below 2% "
        "collecting pauses.</p><td>Overview</td><code>Overview</code><td>sensor.overview</td>"
    )
    cs = i18n.translate(page, "cs")
    assert "<p>Méně než 5% volného místa na disku: promazávání běží dřív. Pod 2% se sběr" in cs
    assert "<td>Přehled</td><code>Overview</code><td>sensor.overview</td>" in cs
    assert i18n.translate(page, "en") == page


def test_catalog_covers_the_four_languages() -> None:
    data = json.loads(i18n.CATALOG.read_text(encoding="utf-8"))
    assert set(data["text"]) == set(data["attr"]) == {"cs", "de", "pl", "nl"}
    for lang in data["text"]:
        assert all(data["text"][lang].values())
        assert set(data["attr"][lang]) == set(data["attr"]["cs"])
