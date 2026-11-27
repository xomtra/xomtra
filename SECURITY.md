# Security policy

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/xomtra/xomtra/security/advisories/new)
instead. We aim to acknowledge reports within 7 days.

## Supported versions

Before 1.0.0 only the latest release receives fixes (as a new patch or minor release).
Development builds of `main` are not supported for production use.

## Scope notes

- xomtra handles Home Assistant history, which can reveal presence and habits of the
  people in a household. Treat any data exposure as a high severity issue.
- Never attach real Home Assistant data, tokens, tenant/client IDs or hostnames to reports.

## How xomtra is secured

The security principles as the code implements them: [docs/security.md](docs/security.md).

## Supply chain

- GitHub Actions are pinned to commit SHAs; dependencies are updated by Renovate.
- Images are built in CI only, scanned with Trivy and signed with cosign (keyless).
- Releases promote the tested images without rebuilding and sign them again with the
  release tag's identity ([docs/releasing.md](docs/releasing.md)).
