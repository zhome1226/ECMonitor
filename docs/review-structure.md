# Review Structure

This repository currently uses four branches with different review purposes.

## Branch roles

### `main`

Base scaffold for the six-agent ECMonitor layout:

- repository root
- top-level README
- multi-agent scaffold
- benchmark examples

This branch remains the base for review.

### `codex/fulltext-retrieval-skill`

Focused slice for the retrieval capability only.

Use this branch when you want to review:

- retrieval order
- publisher API harvesting
- PDF integrity checks
- retrieval skill metadata

### `codex/llm-extraction-skill`

Focused slice for the extraction capability only.

Use this branch when you want to review:

- LLM-driven extraction workflow
- output contracts
- benchmark profile notes
- benchmark prompt templates

### `codex/literature-db-builder`

Integration branch and recommended primary review target.

This branch contains:

- the multi-agent scaffold
- both internal skills
- benchmark prompt templates
- benchmark example folders
- review-oriented project structure

## Recommended review order

If you only want one PR to review, review `codex/literature-db-builder` against `main`.

If you want to inspect the capability slices separately:

1. review `codex/fulltext-retrieval-skill`
2. review `codex/llm-extraction-skill`
3. then review `codex/literature-db-builder` as the integrated package

## Why keep the split branches

The split branches are useful because retrieval and extraction evolve at different speeds:

- retrieval is infrastructure-heavy
- extraction is prompt/profile-heavy

Keeping them separate makes future review and cherry-picking easier, while the integration branch remains the best merge target for the current repository state.
