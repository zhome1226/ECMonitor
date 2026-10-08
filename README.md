# ECMonitor

ECMonitor is a multi-agent workspace for rebuilding literature-backed environmental monitoring databases and moving validated evidence all the way to analytics and publication.

The retrieval layer now supports [`scansci-pdf`](https://github.com/Rimagination/scansci-pdf) as an optional acquisition backend. ECMonitor still owns source policy, provenance manifests, and integrity checks; `scansci-pdf` is used only to improve DOI/title resolution, batch download, institutional access, caching, and diagnostics when it is explicitly enabled.

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
  agent-runtime-guide.md
  agent-runtime-guide.zh-CN.md
  multi-agent-architecture.md
examples/
  benchmark-instances/
skills/
  fulltext-retrieval/
  llm-extraction/
```

## Worked examples

See [`examples/benchmark-instances/`](examples/benchmark-instances/) for background on three published studies:

- *Nature Geoscience*: global surface-water and groundwater PFAS data
- *Science Advances*: legacy POPs in the global ocean
- *Environmental Science & Technology*: trifluoroacetic acid (TFA) in the environment

ECMonitor has been used to reconstruct literature-linked data from these three sources. The work covers concentration records associated with the PFAS study, marine POP evidence represented in the *Science Advances* study, and monitoring entries with source references in the TFA study's supporting information. The example files illustrate data formats; they are not the complete reconstructed datasets or a quantitative performance report. Detailed evaluation results will be reported with the manuscript.

For a practical runtime guide, see:

- English: `docs/agent-runtime-guide.md`
- 中文: `docs/agent-runtime-guide.zh-CN.md`

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
3. optional `scansci-pdf` backend in legal/OA/institution-first mode
4. Zotero + institutional access
5. browser-assisted search and capture

`scansci-pdf` is a downloader backend, not a source authority. Extended routes such as Sci-Hub, LibGen, or Tor are not default ECMonitor retrieval paths; they require explicit project approval and must be recorded in the retrieval manifest.

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
  - `agents/platform-specialist/references/public_api_schema.json`

Marine and water-style workflows are active, but only validated outputs should be treated as publishable.
