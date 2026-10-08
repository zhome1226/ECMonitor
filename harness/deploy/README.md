# Deployment Assets

The release candidate provides a durable SQLite workflow worker for four agents and a deterministic harness. It does not certify scientific quality or live provider availability.

Build from `harness/`:

`docker build -f deploy/Dockerfile -t ecmonitor-harness:0.2.0rc1 .`

Run the preflight check after configuring external secrets and the source mount:

`docker compose -f deploy/compose.yaml run --rm worker ecmonitor-workflow preflight`

Start the worker with `docker compose -f deploy/compose.yaml up -d`. The image runs as non-root with read-only source and private durable state. See [the release candidate runbook](../docs/runbooks/release_candidate.md) for environment setup, review gates, backups, rollback, and staging verification. Never publish runtime volumes, private manifests, credentials, or restricted full text.
