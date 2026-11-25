#!/usr/bin/env python3
"""Release helper for Xomtra (see docs/releasing.md). Standard library only.

    python3 scripts/release.py prepare 0.2.0        # release PR: versions, compose, CHANGELOG
    python3 scripts/release.py prepare 0.2.0-rc.1   # pre-release (beta channel)
    python3 scripts/release.py check v0.2.0         # tag matches the tree (release workflow)
    python3 scripts/release.py notes v0.2.0         # release notes from CHANGELOG.md
    python3 scripts/release.py image-tags v0.2.0    # image tags the release publishes

One version for the whole product: every package, service and image of a release
carries the same SemVer version. Tags are ``v<semver>``; Python packages get the
PEP 440 spelling of the same version (``0.2.0-rc.1`` -> ``0.2.0rc1``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PYPROJECTS = (
    "packages/hacore/pyproject.toml",
    "services/ingest/pyproject.toml",
    "services/mcp-server/pyproject.toml",
)
# Files whose default image references follow the release version.
IMAGE_REFERENCES = (
    "deploy/compose/compose.yaml",
    "deploy/compose/compose.lite.yaml",
    "deploy/compose/.env.example",
)
IMAGES = ("xomtra-mcp", "xomtra-ingest", "xomtra-caddy")
# Home Assistant add-on: the Supervisor pulls <image>:<version of config.yaml>.
ADDON_CONFIG = "addon/xomtra/config.yaml"
REGISTRY = "ghcr.io/xomtra"
CHANGELOG = "CHANGELOG.md"
REPO_URL = "https://github.com/xomtra/xomtra"

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.([1-9]\d*))?$")
_PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}
_PYPROJECT_VERSION = re.compile(r'^version = "([^"]+)"$', re.MULTILINE)
_ADDON_VERSION = re.compile(r'^version: "([^"]+)"$', re.MULTILINE)
_IMAGE_REF = re.compile(rf"({re.escape(REGISTRY)}/(?:{'|'.join(IMAGES)})):([A-Za-z0-9._-]+)")
_LINK_DEF = re.compile(r"^\[[^\]]+\]: \S+$\n?", re.MULTILINE)
_SECTION = re.compile(r"^## \[([^\]]+)\](.*)$", re.MULTILINE)


class ReleaseError(Exception):
    """The tree or the requested version is not releasable."""


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre_kind: str | None = None
    pre_number: int | None = None

    @classmethod
    def parse(cls, text: str) -> Version:
        match = _SEMVER.match(text.removeprefix("v"))
        if not match:
            raise ReleaseError(
                f"not a release version: {text!r} (expected X.Y.Z or X.Y.Z-rc.N, "
                "optionally prefixed with v)"
            )
        major, minor, patch, kind, number = match.groups()
        return cls(int(major), int(minor), int(patch), kind, int(number) if number else None)

    @property
    def prerelease(self) -> bool:
        return self.pre_kind is not None

    @property
    def core(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def semver(self) -> str:
        return f"{self.core}-{self.pre_kind}.{self.pre_number}" if self.prerelease else self.core

    @property
    def pep440(self) -> str:
        if self.pre_kind is None:
            return self.core
        return f"{self.core}{_PEP440_PRE[self.pre_kind]}{self.pre_number}"

    @property
    def tag(self) -> str:
        return f"v{self.semver}"

    def image_tags(self) -> list[str]:
        """Image tags of this release; ``beta`` follows every release, ``latest`` only stable."""
        if self.prerelease:
            return [self.semver, "beta"]
        tags = [self.core, f"{self.major}.{self.minor}"]
        if self.major > 0:
            tags.append(str(self.major))
        return [*tags, "latest", "beta"]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def _sections(changelog: str) -> dict[str, str]:
    """Body of every ``## [name]`` section of the changelog, keyed by name."""
    matches = list(_SECTION.finditer(changelog))
    bodies = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(changelog)
        body = changelog[match.end() : end]
        # Link reference definitions at the end of the file are not part of a section.
        body = _LINK_DEF.sub("", body)
        bodies[match.group(1)] = body.strip()
    return bodies


def _has_entries(body: str) -> bool:
    return any(line.lstrip().startswith(("-", "*")) for line in body.splitlines())


def _previous_tag(changelog: str, version: Version) -> str | None:
    for name in _sections(changelog):
        if name not in ("Unreleased", version.semver):
            return f"v{name}"
    return None


def _move_unreleased(changelog: str, version: Version, today: dt.date) -> str:
    sections = _sections(changelog)
    if version.semver in sections:
        raise ReleaseError(f"{CHANGELOG} already has a section for {version.semver}")
    if not _has_entries(sections.get("Unreleased", "")):
        raise ReleaseError(f"{CHANGELOG}: nothing under [Unreleased] to release")
    previous = _previous_tag(changelog, version)
    changelog = changelog.replace(
        "## [Unreleased]",
        f"## [Unreleased]\n\n## [{version.semver}] - {today.isoformat()}",
        1,
    )
    # Compare links (Keep a Changelog) at the end of the file: Unreleased against the new
    # tag, the new version against the previous one, older links unchanged.
    old_links = [
        line.rstrip("\n")
        for line in _LINK_DEF.findall(changelog)
        if not line.startswith("[Unreleased]: ")
    ]
    changelog = _LINK_DEF.sub("", changelog).rstrip()
    links = [f"[Unreleased]: {REPO_URL}/compare/{version.tag}...HEAD"]
    if previous:
        links.append(f"[{version.semver}]: {REPO_URL}/compare/{previous}...{version.tag}")
    else:
        links.append(f"[{version.semver}]: {REPO_URL}/releases/tag/{version.tag}")
    return changelog + "\n\n" + "\n".join([*links, *old_links]) + "\n"


def prepare(version: Version, *, today: dt.date | None = None, lock: bool = True) -> list[str]:
    """Set the release version everywhere; returns the changed files."""
    changed = []
    for path in PYPROJECTS:
        text = _read(path)
        new, count = _PYPROJECT_VERSION.subn(f'version = "{version.pep440}"', text, count=1)
        if count != 1:
            raise ReleaseError(f"{path}: no version line")
        if new != text:
            _write(path, new)
            changed.append(path)
    for path in IMAGE_REFERENCES:
        text = _read(path)
        new = _IMAGE_REF.sub(rf"\1:{version.semver}", text)
        if new != text:
            _write(path, new)
            changed.append(path)
    text = _read(ADDON_CONFIG)
    new, count = _ADDON_VERSION.subn(f'version: "{version.semver}"', text, count=1)
    if count != 1:
        raise ReleaseError(f"{ADDON_CONFIG}: no version line")
    if new != text:
        _write(ADDON_CONFIG, new)
        changed.append(ADDON_CONFIG)
    if not version.prerelease:
        _write(CHANGELOG, _move_unreleased(_read(CHANGELOG), version, today or dt.date.today()))
        changed.append(CHANGELOG)
    if lock:
        uv = shutil.which("uv")
        if uv is None:
            raise ReleaseError("uv not found: run `uv lock` yourself before committing")
        subprocess.run([uv, "lock"], cwd=ROOT, check=True)  # noqa: S603
        changed.append("uv.lock")
    return changed


def check(version: Version) -> None:
    """Fail unless the tree is exactly the release ``version`` (run on the tagged commit)."""
    problems = []
    for path in PYPROJECTS:
        match = _PYPROJECT_VERSION.search(_read(path))
        found = match.group(1) if match else None
        if found != version.pep440:
            problems.append(f"{path}: version {found!r}, expected {version.pep440!r}")
    for path in IMAGE_REFERENCES:
        for match in _IMAGE_REF.finditer(_read(path)):
            if match.group(2) != version.semver:
                problems.append(f"{path}: {match.group(0)} (expected :{version.semver})")
    addon = _ADDON_VERSION.search(_read(ADDON_CONFIG))
    if (addon.group(1) if addon else None) != version.semver:
        found = addon.group(1) if addon else None
        problems.append(f"{ADDON_CONFIG}: version {found!r}, expected {version.semver!r}")
    try:
        notes(version)
    except ReleaseError as err:
        problems.append(str(err))
    if problems:
        raise ReleaseError(
            f"tree is not release {version.semver} "
            "(run `scripts/release.py prepare` in a PR):\n  " + "\n  ".join(problems)
        )


def notes(version: Version) -> str:
    """Release notes: the version's CHANGELOG section (pre-releases: [Unreleased])."""
    sections = _sections(_read(CHANGELOG))
    name = "Unreleased" if version.prerelease else version.semver
    body = sections.get(name, "")
    if not _has_entries(body):
        raise ReleaseError(f"{CHANGELOG}: no entries under [{name}]")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_prepare = sub.add_parser("prepare", help="set the version for a release PR")
    p_prepare.add_argument("version")
    p_prepare.add_argument("--no-lock", action="store_true", help="do not run `uv lock`")
    for name in ("check", "notes", "image-tags"):
        sub.add_parser(name).add_argument("version")
    args = parser.parse_args(argv)
    try:
        version = Version.parse(args.version)
        if args.command == "prepare":
            for path in prepare(version, lock=not args.no_lock):
                print(f"updated {path}")
            print(
                f"\nNext: review CHANGELOG.md, open a PR 'Release {version.semver}', "
                f"squash-merge it, wait for the main CI to publish the images, then\n"
                f"  git switch main && git pull && "
                f"git tag -s {version.tag} -m 'Xomtra {version.semver}' && "
                f"git push origin {version.tag}"
            )
        elif args.command == "check":
            check(version)
            print(f"tree is release {version.semver}")
        elif args.command == "notes":
            print(notes(version))
        else:
            print("\n".join(version.image_tags()))
    except ReleaseError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
