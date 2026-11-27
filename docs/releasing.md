# Releases

xomtra is released as one product: every package, service and container image of a
release carries the same version (ADR 045).

## Versions and channels

- [Semantic Versioning](https://semver.org/): tags `vX.Y.Z`, pre-releases `vX.Y.Z-rc.N`
  (also `-alpha.N`, `-beta.N`). Python packages use the PEP 440 spelling of the same
  version (`0.2.0-rc.1` → `0.2.0rc1`).
- Before 1.0.0 a **minor** release may break things (configuration, schema, API); the
  CHANGELOG lists each such change with an upgrade note. A **patch** release is always a
  drop-in update.
- Image tags in `ghcr.io/xomtra/xomtra-{mcp,ingest,caddy}` and of the Home
  Assistant add-on `xomtra-addon-{amd64,aarch64}` (the Supervisor pulls the tag of the
  `version` in `addon/xomtra/config.yaml`, which `prepare` sets):

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
   `xomtra-X.Y.Z-images.env` (pinned image references) plus `SHA256SUMS`.

## Cutting a release (maintainer)

```bash
git switch -c release-0.2.0 origin/main
uv run python scripts/release.py prepare 0.2.0   # versions, compose defaults, CHANGELOG, uv.lock
# review CHANGELOG.md (upgrade notes!), commit, open PR "Release 0.2.0", squash-merge
git switch main && git pull
# wait until the CI run of the merge commit on main is green (publish jobs included)
git tag -s v0.2.0 -m "xomtra 0.2.0"
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
git clone --branch v0.2.0 --depth 1 https://github.com/xomtra/xomtra.git
cd xomtra/deploy/compose
cp .env.example .env && chmod 600 .env
# fill in .env, then replace the three *_IMAGE lines with the ones from
# xomtra-0.2.0-images.env (release assets) so that the images are pinned by digest
```

Optional: verify the release signature of each image (cosign v3) before the first start:

```bash
cosign verify ghcr.io/xomtra/xomtra-mcp@sha256:<digest> \
  --certificate-identity https://github.com/xomtra/xomtra/.github/workflows/release.yml@refs/tags/v0.2.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Without cosign installed, run the same command in its official container, pinned by
digest (the image is ignored by `docker compose down --rmi all`; remove it with
`docker image rm` when you no longer need it):

```bash
COSIGN=ghcr.io/sigstore/cosign/cosign:v3.1.6@sha256:7d08ec40d6f6fd9ced81a498c367ee6d4dd90d73218f58a791f78661bdbef769
docker run --rm "$COSIGN" verify ghcr.io/xomtra/xomtra-mcp@sha256:<digest> \
  --certificate-identity https://github.com/xomtra/xomtra/.github/workflows/release.yml@refs/tags/v0.2.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Upgrading an installation

1. Read the CHANGELOG entries between your version and the target (upgrade notes, new
   `.env` variables).
2. Back up `.env` and the compose directory.
3. Replace the compose directory with the one of the new tag, keep your `.env`, put in
   the `*_IMAGE` lines of the new release and add any new variables.
4. Optionally verify the signatures, then `docker compose pull && docker compose up -d`.
5. Roll back by restoring the backup and the previous `*_IMAGE` lines (pinned by
   digest, so the old images are pulled again).

**From 0.4.x (code name HAmmond) to xomtra** (ADR 071): after step 3, with the new
`*_IMAGE` lines in `.env` but before starting anything, run
`sh migrate-from-hammond.sh` in the compose directory instead of step 4. It stops the old
stack, rewrites `.env` (`XOMTRA_*`, roles, bucket, compose project `xomtra`), copies
the volumes, renames the Postgres roles, copies the lake bucket, points the DuckLake
catalog to it and starts the new stack. The old volumes, the old bucket and
`.env.bak-hammond` stay as the rollback until you remove them. Afterwards: add the Entra
app roles `xomtra.Admin`/`xomtra.Reader` and assign them, rename the Home Assistant
labels to `xomtra_no_ingest`/`xomtra_no_analysis` and issue new personal tokens.

## Repository settings (one-time, maintainer)

- Ruleset for tags `v*`: restrict updates and deletions; restrict creations with bypass
  only for repository admins.
- Settings → General → Releases: enable release immutability.
- Settings → Environments → `release` (created by the first run): deployment tags `v*`
  only; optionally a required reviewer.
