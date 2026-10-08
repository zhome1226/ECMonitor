# Production Readiness Runbook

Version 0.2.0rc1 includes durable queue integration and explicitly authorized local/HTTPS acquisition. Use [the release candidate runbook](release_candidate.md) for the current deployment commands and configuration. Production approval still requires successful container and live staging verification; synthetic tests do not certify provider availability or scientific quality.

## Branch and release

1. Merge only through a reviewed pull request into `main`.
2. Require CI, secret scanning, schema validation, tests, lint, and type checking.
3. Build the container from `harness/`.
4. Test database migrations and rollback on a copy of staging state.
5. Tag releases as `harness-vX.Y.Z` only after the matching checklist is signed.

## Required commands

Run from `harness/`:

`python -m compileall -q src tests scripts`

`python scripts/validate_schemas.py`

`python scripts/release_bundle.py --root .`

`pytest -q`

`ruff check src tests scripts`

`mypy src`

`python -m ecmonitor.orchestration.cli validate-layout`

## Live smoke test

Use a deliberately small, authorized staging corpus. Verify:

- same run ID resumes without duplicate provider pages, downloads, or observations;
- every task has an idempotency key and attempt counter;
- invalid PDFs and HTML challenge pages are rejected;
- one failed document does not stop unrelated documents;
- targeted extraction retry preserves previously valid candidates;
- human-review pause and signed resume retain lineage;
- no credential or restricted artifact appears in logs or Git outputs;
- all child model processes and document resources close after each document.

## v1.0.0 blocker

Do not declare general production readiness until the authorized routes and durable worker have passed the staging smoke test, backups have been restored successfully, and the outbound network policy is approved. Publish only the reviewed source tree or sanitized archive, not the parent research workspace or its Git history. Previously exposed credentials require rotation.
