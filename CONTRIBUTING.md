# Contributing

- Work on a branch and open a pull request against `main`. `main` requires a PR, green CI
  (`lint`, `test`, `trivy`, `build`), signed commits and linear history.
- Add a line under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for every user-visible
  change. Releases are cut by the maintainer, see [docs/releasing.md](docs/releasing.md).
- Run `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
  before pushing.
- **Never commit installation-specific data**: IP addresses, hostnames, tenant/client IDs,
  secrets, entity IDs or exports from a real Home Assistant. Use `.env` files (ignored by git)
  and the `*.example` templates.
- **Contributor License Agreement:** before your first pull request is merged, sign the
  [CLA](CLA.md) once; the CLA Assistant bot asks for it in the pull request. You keep the
  copyright in your contribution and grant the maintainer a licence to use it, including
  under other licence terms in future versions. Released versions stay under the Apache
  License 2.0.
