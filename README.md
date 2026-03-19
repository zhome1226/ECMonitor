# EcoScan

EcoScan is a literature-database rebuilding workspace for environmental monitoring studies.

The repository is organized around one orchestration agent and two internal skills:

- `fulltext-retrieval`
  - Acquire full text in a fixed order:
    1. open/open-access PDF
    2. publisher API harvest
    3. Zotero + campus access
    4. browser-assisted manual retrieval
- `llm-extraction`
  - Use an LLM to extract structured records from full text, supplementary files, or source tables.
  - Preserve evidence, provenance, unresolved items, and benchmark-specific schema rules.

## Layout

```text
agents/
  literature-db-builder/
examples/
  benchmark-instances/
skills/
  fulltext-retrieval/
  llm-extraction/
```

## Worked examples

See `examples/benchmark-instances/` for three documented benchmark cases:

- Nature Geoscience PFAS global waters reconstruction
- Science global arsenic groundwater reconstruction
- EST / TFA benchmark-table rebuilding

## Retrieval policy

The retrieval skill always stops at the first successful source:

1. direct open PDF
2. publisher API / harvest pipeline
3. Zotero + institutional access
4. browser-assisted search and capture

Every downloaded file must pass integrity checks before extraction:

- correct article
- not abstract-only
- not one-page junk
- text is extractable
- supplementary files are captured when available

## Extraction policy

Extraction is model-driven, not rule-heavy parsing.

The orchestrator should:

1. gather the best source text and tables
2. choose the benchmark profile
3. ask the model for schema-constrained output
4. validate and merge results
5. record unresolved references and provenance

## Current profiles

- Nature-style monitoring reconstruction
- Science-style monitoring summary reconstruction
- EST / TFA literature rebuild

Water-style workflows are paused and not treated as finished outputs.
