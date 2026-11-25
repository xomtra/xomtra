# Releases

HAmmond is released as one product: every package, service and container image of a
release carries the same version (ADR 045).

## Versions and channels

- [Semantic Versioning](https://semver.org/): tags `vX.Y.Z`, pre-releases `vX.Y.Z-rc.N`
  (also `-alpha.N`, `-beta.N`). Python packages use the PEP 440 spelling of the same
  version (`0.2.0-rc.1` → `0.2.0rc1`).
- Before 1.0.0 a **minor** release may break things (configuration, schema, API); the
  CHANGELOG lists each such change with an upgrade note. A **patch** release is always a
  drop-in update.
- Image tags in `ghcr.io/zettabyte-cz/hammond-{mcp,ingest,caddy}` and of the Home
  Assistant add-on `hammond-addon-{amd64,aarch64}` (the Supervisor pulls the tag of the
  `version` in `addon/hammond/config.yaml`, which `prepare` sets):

  | Tag | Moves? | Meaning |
  | --- | --- | --- |
  | `X.Y.Z`, `X.Y.Z-rc.N` | never | exactly one release |
  | `X.Y` (and `X` from 1.0) | yes | latest patch of that line |
  | `latest` | yes | stable channel: latest stable release |
  | `beta` | yes | beta channel: latest release or pre-release |
  | `main`, `sha-<commit>` | `main` yes | development builds of every commit of `main` |

  Deploy by digest (see below); moving tags are for discovery only.

## How a release works

Nothing is rebuilt for a release. Every commit of `main` already gets images that passed
`lint`, `test`, `contract`, `trivy` and `build`, tagged `sha-<commit>` and signed by
`ci.yml`. Pushing a signed tag starts [`release.yml`](../.github/workflows/release.yml),
which

1. checks that the tag is annotated, signed (verified by GitHub) and points to a commit
   of `main`, and that the versions, compose defaults and CHANGELOG of that commit match
   the tag (`scripts/release.py check`);
2. waits for the main CI images of that commit and verifies they were signed by `ci.yml`
   on `main` for exactly that commit;
3. adds the release tags to those images (same digests) and signs them again with the
   identity `release.yml@refs/tags/vX.Y.Z`; an existing `X.Y.Z` is never moved;
4. publishes a GitHub release with the CHANGELOG section, the digests and the asset
   `hammond-X.Y.Z-images.env` (pinned image references) plus `SHA256SUMS`.

## Cutting a release (maintainer)

```bash
git switch -c release-0.2.0 origin/main
uv run python scripts/release.py prepare 0.2.0   # versions, compose defaults, CHANGELOG, uv.lock
# review CHANGELOG.md (upgrade notes!), commit, open PR "Release 0.2.0", squash-merge
git switch main && git pull
# wait until the CI run of the merge commit on main is green (publish jobs included)
git tag -s v0.2.0 -m "HAmmond 0.2.0"
git push origin v0.2.0
```

Pre-release: `prepare 0.2.0-rc.1` bumps the versions but leaves the CHANGELOG alone; its
release notes are the `[Unreleased]` section. The final `prepare 0.2.0` moves that
section under the version.

If the release workflow fails before the images were tagged (for example the main CI was
not finished), re-run it. After a release is published it is immutable: fix forward with
a patch release, never move or delete a tag.

Every pull request with a user-visible change adds a line under `[Unreleased]` in
[CHANGELOG.md](../CHANGELOG.md).

## Installing a release

```bash
git clone --branch v0.2.0 --depth 1 https://github.com/zettabyte-cz/HAmmond.git
cd HAmmond/deploy/compose
cp .env.example .env && chmod 600 .env
# fill in .env, then replace the three *_IMAGE lines with the ones from
# hammond-0.2.0-images.env (release assets) so that the images are pinned by digest
```

Verify the release signature of each image (cosign v3) before the first start:

```bash
cosign verify ghcr.io/zettabyte-cz/hammond-mcp@sha256:<digest> \
  --certificate-identity https://github.com/zettabyte-cz/HAmmond/.github/workflows/release.yml@refs/tags/v0.2.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Upgrading an installation

1. Read the CHANGELOG entries between your version and the target (upgrade notes, new
   `.env` variables).
2. Back up `.env` and the compose directory.
3. Replace the compose directory with the one of the new tag, keep your `.env`, put in
   the `*_IMAGE` lines of the new release and add any new variables.
4. Verify the signatures, then `docker compose pull && docker compose up -d`.
5. Roll back by restoring the backup and the previous `*_IMAGE` lines (pinned by
   digest, so the old images are pulled again).

## Repository settings (one-time, maintainer)

- Ruleset for tags `v*`: restrict updates and deletions; restrict creations with bypass
  only for repository admins.
- Settings → General → Releases: enable release immutability.
- Settings → Environments → `release` (created by the first run): deployment tags `v*`
  only; optionally a required reviewer.
