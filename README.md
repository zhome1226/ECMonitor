# ECMonitor

ECMonitor is a multi-agent workspace for rebuilding literature-backed environmental monitoring databases and moving validated evidence all the way to analytics and publication.

The repository is organized around six specialist agents plus reusable retrieval and extraction skills:

- `ResearchManager`
  - define benchmark scope, inclusion rules, release criteria, and handoff order
- `RetrievalSpecialist`
  - obtain validated full text with a fixed retrieval ladder
- `ExtractionSpecialist`
  - extract structured evidence from validated PDFs, HTML, supplements, or benchmark tables
- `ValidationSpecialist`
  - audit field completeness, provenance, duplicates, conflicts, and normalization decisions
- `AnalyticsSpecialist`
  - join validated outputs with external context layers and run benchmark-facing models
- `PlatformSpecialist`
  - package approved outputs for downstream database or website publishing

## Layout

```text
agents/
  research-manager/
  retrieval-specialist/
  extraction-specialist/
  validation-specialist/
  analytics-specialist/
  platform-specialist/
  literature-db-builder/
docs/
  multi-agent-architecture.md
examples/
  benchmark-instances/
skills/
  fulltext-retrieval/
  llm-extraction/
```

## Worked examples

See `examples/benchmark-instances/` for three documented benchmark cases:

- Nature Geoscience PFAS global waters reconstruction
- Science Advances legacy POPs global ocean synthesis reconstruction
- EST / TFA benchmark-table rebuilding

## Agent handoff chain

ECMonitor treats evidence flow as a gated pipeline:

1. `ResearchManager` creates the task brief and benchmark rulebook
2. `RetrievalSpecialist` produces a validated retrieval manifest and source snapshots
3. `ExtractionSpecialist` emits structured candidate records with evidence locations
4. `ValidationSpecialist` approves, corrects, or rejects candidate outputs
5. `AnalyticsSpecialist` generates derived results from validated evidence only
6. `PlatformSpecialist` publishes approved outputs

See `docs/multi-agent-architecture.md` for artifacts and gates.

## Retrieval policy

`RetrievalSpecialist` and the `fulltext-retrieval` skill always stop at the first successful source:

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

`ExtractionSpecialist` is model-driven, not rule-heavy parsing.

The extraction flow should:

1. gather the best source text and tables
2. choose the benchmark profile
3. ask the model for schema-constrained output
4. validate and merge results
5. record unresolved references and provenance

## Current profiles

- Nature-style monitoring reconstruction
- Science-style synthesis and monitoring-summary reconstruction
- EST / TFA literature rebuild

## Agent-to-skill mapping

- `RetrievalSpecialist` uses `skills/fulltext-retrieval/`
- `ExtractionSpecialist` uses `skills/llm-extraction/`
- `ValidationSpecialist`, `AnalyticsSpecialist`, and `PlatformSpecialist` currently ship as operating contracts and review scaffolds in `agents/`

## Starter scripts

- Validation:
  - `agents/validation-specialist/scripts/validate_extraction_batch.py`
  - `agents/validation-specialist/references/rulebooks/*.json`
- Analytics:
  - `agents/analytics-specialist/scripts/run_analysis_skeleton.py`
  - `agents/analytics-specialist/scripts/fetch_context_adapters.py`
- Platform:
  - `agents/platform-specialist/scripts/build_publication_bundle.py`
  - `agents/platform-specialist/references/publication_bundle_schema.json`

Marine and water-style workflows are active, but only validated outputs should be treated as publishable.
