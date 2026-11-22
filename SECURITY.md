# Security policy

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/zettabyte-cz/hammond/security/advisories/new)
instead. We aim to acknowledge reports within 7 days.

## Scope notes

- HAmmond handles Home Assistant history, which can reveal presence and habits of the
  people in a household. Treat any data exposure as a high severity issue.
- Never attach real Home Assistant data, tokens, tenant/client IDs or hostnames to reports.

## Supply chain

- GitHub Actions are pinned to commit SHAs; dependencies are updated by Renovate.
- Images are built in CI only, scanned with Trivy and signed with cosign (keyless).
