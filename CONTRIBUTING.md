# Contributing to tradedesk-marketdata

We welcome contributions to `tradedesk-marketdata`. This guide covers the
standards and workflow you need to contribute successfully.

## Scope

This repository is intentionally standalone and does not depend on the
`tradedesk` framework.

## Getting Started

1. Fork the repository and create a feature branch from `main`.
2. Clone locally and install the dev dependencies:

```bash
pip install -e '.[dev]'
```

Or with [`uv`](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
```

### Optional: MCP / code-summary tooling

`llm-tldr` is not a runtime dependency and is not installed by default.
If you use the MCP code-summary tool, install it separately:

```bash
uv sync --extra tooling
```

## Code Standards

### Python Version

Python 3.11+ is required.

### Style and Linting

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
ruff check .
ruff format --check .
```

Rules enforced: `E`, `F`, `I` (import sorting), `B` (bugbear), `UP` (pyupgrade).
Line length is 100 characters.

### Type Checking

We use [mypy](https://mypy.readthedocs.io/):

```bash
mypy .
```

All contributions must pass with zero type errors.

## Conventions

- All timestamps are UTC and written as ISO-8601 strings.
- Exported datasets are canonical and self-describing via `<output>.meta.json`.
- `--price-divisor` is applied once at export time; downstream code must not
  rescale.
- Keep Dukascopy concurrency conservative. One symbol export already uses two
  downloader threads internally, so `--workers 1` caps total download
  concurrency at two requests and is the safest default when reproducing
  failures or filling gaps.
- When exporter, cache-normalization, or probe logic changes, rerun the
  maintainer audit scripts in `scripts/` against a populated cache:
  `dukascopy_audit.py` for local gap/DST/spread/stale checks and
  `dukascopy_cross_provider.py` for cross-provider close-price correlation.

## Testing

We use [pytest](https://docs.pytest.org/):

```bash
pytest --cov=tradedesk_marketdata --cov-fail-under=75
```

- All new and existing tests must pass following any code change.
- Code coverage must remain at or above 75%.
- Include tests for new functionality or bug fixes.

## Credentials and Release Automation

Normal exporter runs, local development, and CI do not require repository
secrets or broker credentials.

Maintainers triggering `.github/workflows/prepare-release.yml` must configure
these repository secrets:

- `RELEASE_APP_ID`
- `RELEASE_APP_PRIVATE_KEY`

The shared reusable release workflow uses those values to mint a GitHub App
token for checkout, version bumping, pushing the release commit, and creating
the GitHub release. `.github/workflows/publish.yml` uses PyPI trusted
publishing via GitHub OIDC (`id-token: write`), so no PyPI API token secret is
expected in this repository.

## Pull Requests

- Keep changes small, well-scoped, and documented in PR descriptions.
- Open PRs against `main`.
- Rebase your branch onto `main` before submitting — we do not use merge commits.
- Provide a brief rationale and expected impact in the PR description.
- Do not include secrets or internal data.

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(cli): add parquet output format
fix(export): handle missing tick hours gracefully
docs: update cache directory guidance
```

Describe the symptom, instrument class, or component in prose. Do **not**
reference downstream-private issue trackers (e.g. `RAD-1234`, `INTERNAL-42`)
in commit messages, code comments, docstrings, README sections, or test
descriptions. External readers cannot resolve those identifiers, so the
prose description must stand on its own. Operator scripts that hardcode
a single user's cache root or one-off remediation dates likewise do not
belong in this repository.

## Reporting Issues

If you find a bug or have a feature request, open an issue describing the goal
and expected behaviour. Include steps to reproduce for bugs.

If unsure about scope or approach, open an issue to discuss before implementing.

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://www.radiusred.uk)
