# Release candidate deployment

Version: 0.2.0rc1. Tested target: Python 3.12. This is a release candidate, not a declaration that a live research deployment has passed acceptance.

## Roles and execution

Retrieval produces included acquisition requests. Download uses a configured local inventory or explicitly authorized public HTTPS route. Extraction and independent evidence validation execute through the existing full-text engine, which retains separate model requests and deterministic scientific gates. The workflow validation step checks the authoritative evidence session and prevents publication while human-review records remain unresolved. The deterministic SQLite worker owns leases, retries, checkpoints, and atomic successor enqueueing; it is not a fifth scientific agent.

## Installation

From the extracted source release:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps -e .
python scripts/validate_schemas.py
python scripts/release_bundle.py
python -m pytest -q
python -m ruff check src tests scripts
python -m mypy src
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1` instead of the `source` command. For non-editable installation, set `ECMONITOR_PROJECT_ROOT` to the extracted source directory so the installed wheel can locate versioned schemas and configs. The lock covers the development and PDF runtime; optional chemistry/layout integrations require separate acceptance.

Copy `.env.example` to a private `.env` and supply credentials through the service environment. The CLI does not load `.env` automatically; Compose uses the configured private env file. Configure both model names explicitly. Use different model families when practical. Keep environment files outside source releases.

## Start with a bounded local-library run

Place authorized source PDFs inside `sources/`. Create a private JSON array:

```json
[{"global_record_id":"doi:10.0000/example","local_path":"study.pdf","screening_decision":"include"}]
```

Only use `include` after your documented screening policy has been applied. This manifest route imports an existing screened retrieval result; it does not pretend to perform model retrieval.

```sh
ecmonitor-workflow preflight --data-root runtime --source-root sources
ecmonitor-workflow enqueue --run-id release-smoke-v1 --manifest private-manifest.json --data-root runtime --source-root sources
ecmonitor-workflow worker --data-root runtime --source-root sources
ecmonitor-workflow status --data-root runtime
```

The worker is a local background service. Stop it using normal process signals; unfinished leases expire and are reclaimed within the retry budget. One worker is the supported scientific deployment setting because the shared chemical registry needs a document-level commit barrier. Concurrent queue claiming is fenced, but a SQLite file must not be shared across network filesystems or Kubernetes nodes.

For live retrieval, omit `--manifest` and set `--date-from`, `--date-to`, and `--max-iterations`. Configure the external ECfinder runner and provider credentials first. Retrieval writes its private snapshot and state beneath the data root. If provider preflight or screening requires operator action, the task blocks rather than silently completing.

For public HTTPS acquisition, add `authorized_url` and `access_authorized: true` to each request, then use the identical `--allowed-host` settings for enqueue and worker. Redirects, signed/query URLs, credentials, and private addresses are deliberately rejected. A stored runtime signature prevents resume with changed policy, prompts, models, paths, or host permissions.

## Human-review barrier

Use `ecmonitor-fulltext signoff` with the private full-text database and signed-examples file under the same data root. Rejections are materialized into a separate rejected stream with resolution lineage. A pilot acceptance is materialized only when the trusted policy wrapper recorded successful deterministic and independent validator gates before the pilot pause. Acceptance signatures alone do not override unresolved evidence or conservative offline validation: correct and version the evidence first. Resume the paused workflow task using `ecmonitor-workflow resume --task-id TASK --reason REASON`. A resume rechecks the database; it never turns an unresolved observation into an accepted one. SQLite is authoritative; original document reports are immutable snapshots taken before signoff.

## Container deployment

```sh
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml run --rm worker ecmonitor-workflow preflight
docker compose -f deploy/compose.yaml up -d
```

The container runs as a non-root user, with read-only code, a read-only source mount, and a persistent data volume. Health checks inspect worker heartbeats and SQLite, not directory existence. Do not publish a network port for this worker. Capture a dependency lock and image digest in your deployment environment before approval.

## Backup, migration, rollback

Use `sqlite3.Connection.backup()` or the SQLite online backup API; do not copy an active WAL database without its transactional state. Schema version 1 is initialized atomically; newer unknown versions fail closed. To roll back, stop the worker, preserve its evidence and databases, restore the matching backup, and deploy the matching immutable source/image version. Do not run old code against a newer database.

## Remaining operator gates

1. Configure model/provider secrets, the ECfinder runner, legal acquisition permissions, and source directories.
2. Run a real bounded retrieval/acquisition/extraction/validation smoke test and review accepted/rejected evidence. Synthetic end-to-end tests do not certify live scientific quality.
3. Build and smoke-test the Linux container on the production host; configure backups, encryption, disk quotas, egress restrictions, and service alerts.
4. Review the sanitized archive before each public source distribution. The published code uses the MIT license. Rotate previously exposed keys. Existing private Git history is not part of the release bundle.

## Publication export

```sh
python scripts/release_bundle.py --output releases/ecmonitor-0.2.0rc1-source.tar.gz
```

The accompanying manifest contains per-file hashes and the archive checksum, not machine usernames or credentials. Publish only the reviewed source tree or this release artifact. Do not upload the parent research workspace.
