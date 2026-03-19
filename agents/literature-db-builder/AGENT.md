# Literature DB Builder

This agent rebuilds literature-backed environmental monitoring databases.

It delegates work to two internal skills:

1. `fulltext-retrieval`
2. `llm-extraction`

## Operating contract

Always run retrieval first. Never extract from a paper that has not passed source validation.

## Workflow

1. Load the benchmark profile and target list.
2. Call `fulltext-retrieval` to locate or download full text.
3. Filter to sources with `fulltext_ready` or equivalent validated status.
4. Call `llm-extraction` with the benchmark prompt/schema.
5. Merge outputs into the benchmark's final table.
6. Emit:
   - retrieval manifest
   - extraction outputs
   - unresolved references
   - summary

## Non-negotiable rules

- Do not invent DOIs.
- Do not treat abstract-only landing pages as full text.
- Do not treat regulatory limits or model predictions as monitoring observations.
- Prefer supplementary tables over narrative summaries when both report the same data.
- If main text and supplement conflict, keep both with conflict notes.
