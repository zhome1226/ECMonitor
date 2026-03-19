# Orchestration

## Agent responsibilities

The agent is responsible for sequencing work, not for replacing the two internal skills.

Use this order:

1. load targets and benchmark profile
2. call `fulltext-retrieval`
3. keep only validated sources
4. call `llm-extraction`
5. merge outputs
6. write unresolved and audit artifacts
7. summarize the run

## Run artifacts

Every run should preserve:

- `targets.csv` or equivalent source list
- `retrieval_manifest.csv/json`
- `source_snapshots/`
- `reference_audit.csv`
- extracted outputs
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
