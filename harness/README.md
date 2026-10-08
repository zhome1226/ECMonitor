# ECMonitor Harness

Release candidate: **0.2.0rc1**, prepared on 2026-10-08. Read [the deployment runbook](docs/runbooks/release_candidate.md) and [security boundary](SECURITY.md) before deployment or publication.

This self-contained source project combines Retrieval, Download, Extraction, and Validation with a deterministic durable workflow worker. The source release is published as the `harness/` directory of the public repository. New archives must be exported using `scripts/release_bundle.py`; do not publish the parent research workspace or its Git history. The [website](https://eco-website.pages.dev/) presents live data and is described separately in the [repository guide](../docs/data-portal.md).

## Architecture

`Protocol -> Retrieval -> Download -> Extraction -> Validation -> Commit`

Validation feedback is routed deterministically:

- missing PDF, supplementary information, or lawful full-text route -> Download;
- recoverable table, OCR, source-binding, or field-binding problem -> targeted Extraction retry;
- evidenced systematic retrieval gap -> versioned Retrieval QueryPatch;
- unresolved scientific ambiguity -> Human Review;
- accepted/rejected terminal decision -> durable commit with lineage.

The Workflow Orchestrator is not a scientific agent. It owns only state transitions, idempotency, retry budgets, checkpoint/resume, pauses, and audit events.

## Layout

- `src/ecmonitor/retrieval_specialist/`: retrieval runtime and control plane.
- `src/ecmonitor/download_specialist/`: download contracts, route plugins, and file validation.
- `src/ecmonitor/fulltext_extraction/`: extraction engine and current validation-policy implementation.
- `src/ecmonitor/validation_specialist/`: stable validation boundary and workflow-facing decision contract.
- `src/ecmonitor/orchestration/`: deterministic cross-agent workflow state machine.
- `schemas/`: versioned JSON contracts.
- `configs/`: agent, workflow, policy, and runtime configuration.
- `prompts/`: versioned model instructions.
- `tests/`: unit, contract, integration, and end-to-end fixtures.
- Private archived handoff snapshots are excluded from the publication artifact.
- `docs/architecture/`, `docs/agents/`, and `docs/runbooks/`: canonical living documentation.

## Install

`python -m venv .venv` (Python 3.12)

Windows PowerShell:

`.venv\Scripts\python -m pip install -e ".[dev,fulltext-pdf]"`

Linux/macOS:

`.venv/bin/python -m pip install -e ".[dev,fulltext-pdf]"`

Chemical structure resolution is optional:

`python -m pip install -e ".[dev,fulltext-pdf,fulltext-chem]"`

## Validate

`python -m compileall -q src tests scripts`

`python scripts/validate_schemas.py`

`python scripts/release_bundle.py --root .`

`pytest -q`

`ruff check src tests scripts`

`mypy src`

## Command-line entry points

- `ecmonitor-retrieval`
- `ecmonitor-download`
- `ecmonitor-fulltext`
- `ecmonitor-workflow`

Useful layout checks:

`ecmonitor-workflow validate-layout`

`ecmonitor-workflow route-validation --disposition deferred_source_binding --reason-code SOURCE-DEFER-01`

`ecmonitor-download validate-file path/to/article.pdf`

Durable execution commands:

- `ecmonitor-workflow preflight`
- `ecmonitor-workflow enqueue --run-id smoke-v1 --manifest private-manifest.json`
- `ecmonitor-workflow worker`
- `ecmonitor-workflow status`
- `ecmonitor-workflow resume --task-id TASK --reason REASON`

All model keys and names are configured through private service environment variables. No live API calls are required by the synthetic tests.

## Data boundary

Git contains code, policies, schemas, prompts, tests, small sanitized fixtures, and reviewed documentation only. Runtime databases, raw run payloads, PDFs, publisher HTML, cookies, browser profiles, local library manifests, and credentials remain outside Git.

## Production readiness

The durable queue and worker are executable, with fenced leases, bounded retries, atomic successor enqueueing, runtime-signature checks, and human-review barriers. Local inventory and authorized public HTTPS acquisition are implemented. Institutional routes and provider/model permissions remain operator responsibilities. Real scientific acceptance, Linux container smoke testing, host security, backups, and publication licensing are release gates; this candidate is not automatically approved for production.
