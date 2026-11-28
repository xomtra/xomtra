"""Tests of scripts/release.py on a copy of the release-relevant files."""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release", REPO / "scripts" / "release.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["release"] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


release = _load()

CHANGELOG = """# Changelog

Intro with a [link](https://example.org) and `[Unreleased]` mentioned.

## [Unreleased]

### Added

- Something new.

## [0.1.0] - 2026-11-23

### Added

- First.

[Unreleased]: https://github.com/xomtra/xomtra/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/xomtra/xomtra/releases/tag/v0.1.0
"""


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The copies start at 0.1.0 whatever version the repository is at, so the tests do
    # not depend on the last release.
    current = re.search(
        r'^version = "([^"]+)"',
        (REPO / "services/mcp-server/pyproject.toml").read_text(encoding="utf-8"),
        re.M,
    )
    assert current
    paths = (*release.PYPROJECTS, *release.IMAGE_REFERENCES, release.ADDON_CONFIG)
    for path in (*paths, *release.INSTALL_DOCS):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (REPO / path).read_text(encoding="utf-8")
        text = text.replace(f'version = "{current[1]}"', 'version = "0.1.0"')
        text = text.replace(f'version: "{current[1]}"', 'version: "0.1.0"')
        text = re.sub(rf"(xomtra-[a-z]+):{re.escape(current[1])}\b", r"\1:0.1.0", text)
        text = re.sub(r"^VERSION=\S+$", "VERSION=0.1.0", text, flags=re.M)
        target.write_text(text, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("text", "pep440", "tags"),
    [
        ("v0.2.0", "0.2.0", ["0.2.0", "0.2", "latest", "beta"]),
        ("1.4.2", "1.4.2", ["1.4.2", "1.4", "1", "latest", "beta"]),
        ("v0.3.0-rc.2", "0.3.0rc2", ["0.3.0-rc.2", "beta"]),
        ("v1.0.0-beta.1", "1.0.0b1", ["1.0.0-beta.1", "beta"]),
    ],
)
def test_version(text: str, pep440: str, tags: list[str]) -> None:
    version = release.Version.parse(text)
    assert version.pep440 == pep440
    assert version.image_tags() == tags
    assert version.tag == "v" + text.removeprefix("v")


@pytest.mark.parametrize("text", ["v1.2", "01.2.3", "v1.2.3-rc", "v1.2.3-rc.0", "1.2.3+b1", "x"])
def test_invalid_version(text: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.Version.parse(text)


def test_prepare_then_check(tree: Path) -> None:
    version = release.Version.parse("0.2.0")
    with pytest.raises(release.ReleaseError, match="xomtra-mcp"):
        release.check(version)

    release.prepare(version, today=dt.date(2026, 12, 1), lock=False)
    release.check(version)

    assert 'version = "0.2.0"' in (tree / "services/mcp-server/pyproject.toml").read_text()
    compose = (tree / "deploy/compose/compose.yaml").read_text()
    assert "xomtra-ingest:0.2.0}" in compose
    assert ":main}" not in compose
    assert 'version: "0.2.0"' in (tree / release.ADDON_CONFIG).read_text()
    assert "\nVERSION=0.2.0\n" in (tree / "docs/setup/docker.md").read_text()
    changelog = (tree / "CHANGELOG.md").read_text()
    released = "## [Unreleased]\n\n## [0.2.0] - 2026-12-01\n\n### Added\n\n- Something new."
    assert released in changelog
    assert changelog.endswith(
        "[Unreleased]: https://github.com/xomtra/xomtra/compare/v0.2.0...HEAD\n"
        "[0.2.0]: https://github.com/xomtra/xomtra/compare/v0.1.0...v0.2.0\n"
        "[0.1.0]: https://github.com/xomtra/xomtra/releases/tag/v0.1.0\n"
    )
    assert "Intro with a [link](https://example.org)" in changelog
    assert release.notes(version) == "### Added\n\n- Something new."
    assert (tree / release.ADDON_CHANGELOG).read_text() == (
        "# Changelog\n\n"
        "## 0.2.0 - 2026-12-01\n\n### Added\n\n- Something new.\n\n"
        "## 0.1.0 - 2026-11-23\n\n### Added\n\n- First.\n\n"
        "Older versions: https://github.com/xomtra/xomtra/blob/main/CHANGELOG.md\n"
    )
    # A released version cannot be prepared twice, and Unreleased is now empty.
    with pytest.raises(release.ReleaseError):
        release.prepare(version, lock=False)
    with pytest.raises(release.ReleaseError, match="nothing under"):
        release.prepare(release.Version.parse("0.2.1"), lock=False)


def test_prerelease_keeps_changelog(tree: Path) -> None:
    version = release.Version.parse("v0.2.0-rc.1")
    release.prepare(version, lock=False)
    release.check(version)
    assert (tree / "CHANGELOG.md").read_text() == CHANGELOG
    assert 'version = "0.2.0rc1"' in (tree / "packages/hacore/pyproject.toml").read_text()
    assert "xomtra-caddy:0.2.0-rc.1" in (tree / "deploy/compose/.env.example").read_text()
    assert 'version: "0.2.0-rc.1"' in (tree / release.ADDON_CONFIG).read_text()
    assert release.notes(version) == "### Added\n\n- Something new."


def test_prerelease_keeps_the_install_docs(tree: Path) -> None:
    release.prepare(release.Version.parse("0.2.0-rc.1"), lock=False)
    assert "\nVERSION=0.1.0\n" in (tree / "docs/setup/docker.md").read_text()


def test_check_needs_the_install_docs_version(tree: Path) -> None:
    version = release.Version.parse("0.2.0")
    release.prepare(version, today=dt.date(2026, 12, 1), lock=False)
    docs = tree / "docs/setup/docker.md"
    docs.write_text(docs.read_text().replace("VERSION=0.2.0", "VERSION=0.1.0"))
    with pytest.raises(release.ReleaseError, match=r"docker\.md"):
        release.check(version)


def test_check_needs_changelog_section(tree: Path) -> None:
    version = release.Version.parse("0.2.0")
    release.prepare(version, today=dt.date(2026, 12, 1), lock=False)
    (tree / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    with pytest.raises(release.ReleaseError, match=r"no entries under \[0.2.0\]"):
        release.check(version)


def test_main_exit_codes(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert release.main(["image-tags", "v0.2.0-rc.1"]) == 0
    assert capsys.readouterr().out.split() == ["0.2.0-rc.1", "beta"]
    assert release.main(["check", "v9.9.9"]) == 1
    assert "tree is not release 9.9.9" in capsys.readouterr().err


def test_check_needs_the_addon_changelog(tree: Path) -> None:
    version = release.Version.parse("0.2.0")
    release.prepare(version, today=dt.date(2026, 12, 1), lock=False)
    (tree / release.ADDON_CHANGELOG).write_text("# Changelog\n", encoding="utf-8")
    with pytest.raises(release.ReleaseError, match=re.escape("addon/xomtra/CHANGELOG.md")):
        release.check(version)


def test_addon_changelog_keeps_the_last_versions() -> None:
    sections = "".join(f"## [0.{i}.0] - 2026-11-{i:02d}\n\n- v{i}\n\n" for i in range(9, 0, -1))
    text = release.addon_changelog(f"# Changelog\n\n## [Unreleased]\n\n- next\n\n{sections}")
    assert "## 0.9.0 - 2026-11-09\n\n- v9" in text
    assert "## 0.5.0" in text
    assert "## 0.4.0" not in text
    assert "next" not in text


def test_the_addon_changelog_follows_the_changelog() -> None:
    """Home Assistant shows it with an update ("No changelog found" without it)."""
    expected = release.addon_changelog((REPO / release.CHANGELOG).read_text(encoding="utf-8"))
    assert (REPO / release.ADDON_CHANGELOG).read_text(encoding="utf-8") == expected


def test_check_needs_the_addon_version(tree: Path) -> None:
    version = release.Version.parse("0.2.0")
    release.prepare(version, today=dt.date(2026, 12, 1), lock=False)
    config = tree / release.ADDON_CONFIG
    config.write_text(config.read_text().replace('"0.2.0"', '"0.1.0"'), encoding="utf-8")
    with pytest.raises(
        release.ReleaseError, match=re.escape("addon/xomtra/config.yaml: version '0.1.0'")
    ):
        release.check(version)


def test_the_repository_has_one_addon() -> None:
    """The Supervisor takes every config.{yaml,yml,json} outside hidden folders and
    rootfs for an add-on, so no other file may have that name."""
    found = [
        p.relative_to(REPO).as_posix()
        for p in REPO.rglob("config.*")
        if p.suffix in {".yaml", ".yml", ".json"}
        and not any(
            part.startswith(".") or part in {"rootfs", "node_modules"}
            for part in p.relative_to(REPO).parts
        )
    ]
    assert found == [release.ADDON_CONFIG]


def _addon_section(name: str) -> dict[str, str]:
    """Top-level mapping ``name`` of the add-on config (flat ``key: value`` lines)."""
    lines = (REPO / release.ADDON_CONFIG).read_text().splitlines()
    start = lines.index(f"{name}:") + 1
    section: dict[str, str] = {}
    for line in lines[start:]:
        if not line.startswith("  "):
            if line.startswith("#"):
                continue
            break
        key, _, value = line.strip().partition(":")
        section[key] = value.strip()
    return section


def test_addon_defaults_fit_the_schema() -> None:
    """The Supervisor checks the options against the schema before it starts the add-on;
    an optional option (``?``) with an empty default fails that check (``url?`` refuses
    ``""``), so optional options stay out of the defaults."""
    options, schema = _addon_section("options"), _addon_section("schema")
    assert set(options) <= set(schema)
    for key, kind in schema.items():
        if kind.endswith("?"):
            assert key not in options, f"optional option {key} must not have a default"
        else:
            assert key in options, f"option {key} needs a default"
    assert all(value not in {'""', "''"} for value in options.values())
