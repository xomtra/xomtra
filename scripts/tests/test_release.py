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

[Unreleased]: https://github.com/zettabyte-cz/HAmmond/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zettabyte-cz/HAmmond/releases/tag/v0.1.0
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
    for path in (*release.PYPROJECTS, *release.IMAGE_REFERENCES):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (REPO / path).read_text(encoding="utf-8")
        text = text.replace(f'version = "{current[1]}"', 'version = "0.1.0"')
        text = re.sub(rf"(hammond-[a-z]+):{re.escape(current[1])}\b", r"\1:0.1.0", text)
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
    with pytest.raises(release.ReleaseError, match="hammond-mcp"):
        release.check(version)

    release.prepare(version, today=dt.date(2026, 12, 1), lock=False)
    release.check(version)

    assert 'version = "0.2.0"' in (tree / "services/mcp-server/pyproject.toml").read_text()
    compose = (tree / "deploy/compose/compose.yaml").read_text()
    assert "hammond-ingest:0.2.0}" in compose
    assert ":main}" not in compose
    changelog = (tree / "CHANGELOG.md").read_text()
    released = "## [Unreleased]\n\n## [0.2.0] - 2026-12-01\n\n### Added\n\n- Something new."
    assert released in changelog
    assert changelog.endswith(
        "[Unreleased]: https://github.com/zettabyte-cz/HAmmond/compare/v0.2.0...HEAD\n"
        "[0.2.0]: https://github.com/zettabyte-cz/HAmmond/compare/v0.1.0...v0.2.0\n"
        "[0.1.0]: https://github.com/zettabyte-cz/HAmmond/releases/tag/v0.1.0\n"
    )
    assert "Intro with a [link](https://example.org)" in changelog
    assert release.notes(version) == "### Added\n\n- Something new."
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
    assert "hammond-caddy:0.2.0-rc.1" in (tree / "deploy/compose/.env.example").read_text()
    assert release.notes(version) == "### Added\n\n- Something new."


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
