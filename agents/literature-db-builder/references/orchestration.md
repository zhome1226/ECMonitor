# Orchestration

## Agent responsibilities

The integration agent is responsible for sequencing work, not for replacing the six specialist agents.

Use this order:

1. define task brief and benchmark profile
2. run retrieval over the target list
3. keep only validated sources
4. run extraction over those validated sources
5. perform QA and correction
6. optionally run analytics on approved outputs
7. package publishable outputs
8. summarize the run

## Run artifacts

Every run should preserve:

- `task_brief.md` or equivalent scope definition
- `targets.csv` or equivalent source list
- `retrieval_manifest.csv/json`
- `source_snapshots/`
- `extraction_outputs/`
- `validation_audit.csv`
- `analytics_outputs/` when used
- `publication_bundle/` when used
- unresolved outputs
- `summary.json`

## Human-in-the-loop rule

Manual intervention is allowed only in retrieval, not in data invention.

Valid reasons:

- login
- institution access
- captcha or Cloudflare
- browser confirmation of the correct article page

Invalid reason:

- guessing a DOI
- inventing a value
- using the wrong article because it is easier to access
