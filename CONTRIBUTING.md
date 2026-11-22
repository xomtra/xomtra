# Contributing

- Work on a branch and open a pull request against `main`. `main` requires a PR, green CI
  (`lint`, `test`, `trivy`, `build`), signed commits and linear history.
- Run `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
  before pushing.
- **Never commit installation-specific data**: IP addresses, hostnames, tenant/client IDs,
  secrets, entity IDs or exports from a real Home Assistant. Use `.env` files (ignored by git)
  and the `*.example` templates.
- By contributing you agree that your contribution is licensed under the Apache License 2.0.
