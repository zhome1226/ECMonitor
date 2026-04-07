# ECMonitor Agent Runtime Guide

This document explains how each part of ECMonitor is intended to run, what it takes as input, what it writes as output, and what is assumed to be available inside the current sandbox/runtime.

The current repository state is a **lightweight multi-agent scaffold**:

- the orchestration and contracts are explicit
- the Validation / Analytics / Platform agents already have runnable starter scripts
- Retrieval and Extraction rely on documented skills plus helper scripts
- most starter scripts intentionally use the Python standard library only

---

## 1. End-to-end execution order

The intended execution chain is:

1. `ResearchManager`
2. `RetrievalSpecialist`
3. `ExtractionSpecialist`
4. `ValidationSpecialist`
5. `AnalyticsSpecialist`
6. `PlatformSpecialist`

The integration scaffold is:

- `agents/literature-db-builder/scripts/run_benchmark_workflow.py`

Example:

```bash
python3 agents/literature-db-builder/scripts/run_benchmark_workflow.py \
  --benchmark Science \
  --targets targets.csv \
  --run-root ./runs
```

What it does:

- creates a run directory
- writes a `summary.json`
- records the expected stages and artifacts

What it does **not** do yet:

- it does not automatically call all six agents
- it is a run scaffold, not a full workflow engine

---

## 2. ResearchManager

Location:

- `agents/research-manager/AGENT.md`
- `agents/research-manager/references/task-definition.md`

Purpose:

- define benchmark family
- define row shape
- define inclusion/exclusion rules
- define release criteria

Current runtime form:

- **documentation-only**
- no standalone executable script yet

Expected input:

- benchmark paper or benchmark source table
- project brief
- prior run notes

Expected output:

- `task_brief.md`
- benchmark rulebook
- target selection notes

Sandbox assumptions:

- no special binaries required
- no network required
- currently manual/human-authored

---

## 3. RetrievalSpecialist

Location:

- `agents/retrieval-specialist/AGENT.md`
- `agents/retrieval-specialist/references/source-gate.md`
- `skills/fulltext-retrieval/`

Key runnable helpers:

- `skills/fulltext-retrieval/scripts/check_pdf_integrity.py`
- `skills/fulltext-retrieval/scripts/publisher_api_harvest.py`

### 3.1 Retrieval order

The skill requires this order:

1. open/open-access PDF
2. publisher API harvest
3. Zotero / institutional access
4. browser-assisted retrieval

### 3.2 PDF integrity check

Example:

```bash
python3 skills/fulltext-retrieval/scripts/check_pdf_integrity.py article.pdf
```

Input:

- a local PDF path

Output:

- JSON to stdout with:
  - `pages`
  - `extractable`
  - `integrity_status`

Sandbox/runtime requirements:

- Python 3 standard library only
- system binaries:
  - `pdfinfo`
  - `pdftotext`

If those binaries are missing:

- page count may be `null`
- extractability may fail

### 3.3 Publisher API harvester

Example:

```bash
python3 skills/fulltext-retrieval/scripts/publisher_api_harvest.py \
  --input dois.txt \
  --output-dir ./downloads
```

Input:

- a file containing DOIs

Output:

- downloaded PDFs when publisher API succeeds
- retrieval result rows / manifest

Sandbox/runtime requirements:

- Python 3
- external package:
  - `requests`
- network access required
- optional environment variables depending on publisher:
  - `ELSEVIER_API_KEY`
  - `SPRINGER_API_KEY`
  - `WILEY_TDM_TOKEN`

Current note:

- Retrieval is the most network-sensitive component in the repo
- browser/manual retrieval is described in docs, not fully scripted here

---

## 4. ExtractionSpecialist

Location:

- `agents/extraction-specialist/AGENT.md`
- `agents/extraction-specialist/references/evidence-contract.md`
- `skills/llm-extraction/`

Purpose:

- extract benchmark-shaped rows from validated literature sources

Current runtime form:

- mostly **prompt/profile driven**
- no standalone extraction executor script is bundled in this repo yet

How it is intended to run:

1. choose benchmark profile from:
   - `skills/llm-extraction/references/profiles.md`
2. load matching prompt:
   - `prompt_nature.md`
   - `prompt_science.md`
   - `prompt_est_tfa.md`
3. give validated PDF / HTML / supplement / source table to an LLM
4. emit structured records with provenance

Expected inputs:

- validated source document or benchmark source table
- benchmark profile
- schema contract

Expected outputs:

- structured extracted rows
- evidence page / figure / table location
- unresolved notes

Sandbox assumptions:

- this repo currently treats extraction as an **LLM-facing contract**, not as a local parser
- no mandatory local Python dependencies are required just to read the prompt/profile docs
- a real extraction runtime still needs an LLM caller outside this minimal scaffold

---

## 5. ValidationSpecialist

Location:

- `agents/validation-specialist/AGENT.md`
- `agents/validation-specialist/scripts/validate_extraction_batch.py`
- `agents/validation-specialist/references/rulebooks/*.json`

Purpose:

- audit extracted rows before analytics/publication

### 5.1 What is built into the current sandbox

The ValidationSpecialist starter script uses:

- Python 3 standard library only
  - `argparse`
  - `csv`
  - `json`
  - `collections`
  - `pathlib`

It does **not** require:

- pandas
- numpy
- sklearn
- network access

So in the current repo, Validation runs fully offline.

### 5.2 How to run

Example with explicit rulebook:

```bash
python3 agents/validation-specialist/scripts/validate_extraction_batch.py \
  --input examples/benchmark-instances/science_advances_legacy_pops_global_ocean/sample_results.json \
  --rulebook agents/validation-specialist/references/rulebooks/science_synthesis_rulebook.json \
  --out-dir /tmp/ecmonitor_validation
```

Example with automatic rulebook selection:

```bash
python3 agents/validation-specialist/scripts/validate_extraction_batch.py \
  --input examples/benchmark-instances/science_advances_legacy_pops_global_ocean/sample_results.json \
  --benchmark science \
  --out-dir /tmp/ecmonitor_validation
```

### 5.3 What it reads

- input JSON array of extracted rows

### 5.4 What it writes

- `validation_audit.csv`
- `validated_rows.json`
- `review_rows.json`
- `rejected_rows.json`
- `summary.json`
- `rulebook_manifest.json`

### 5.5 What rules are built in

- required fields
- required-any field groups
- duplicate detection
- forbidden fields
- review vs reject logic

Current rulebooks included:

- `science_synthesis_rulebook.json`
- `nature_record_rulebook.json`
- `est_reference_rebuild_rulebook.json`

---

## 6. AnalyticsSpecialist

Location:

- `agents/analytics-specialist/AGENT.md`
- `agents/analytics-specialist/scripts/fetch_context_adapters.py`
- `agents/analytics-specialist/scripts/run_analysis_skeleton.py`
- `agents/analytics-specialist/references/external-context.md`

Purpose:

- add contextual metadata
- summarize validated rows
- create enriched row payloads

### 6.1 What is built into the current sandbox

The current AnalyticsSpecialist starter scripts use:

- Python 3 standard library only
  - `argparse`
  - `csv`
  - `json`
  - `collections`
  - `pathlib`
  - `statistics`
  - `urllib`
  - `re`
  - `os`

That means:

- the base analytics scaffold is runnable without pandas/sklearn
- context fetching uses built-in `urllib`, not `requests`
- offline mode works without any extra installation

So the **current sandbox-included environment** for Analytics is:

- fully usable in offline mode
- no third-party Python package required by the starter scripts
- optional network only when `--fetch` is used

### 6.2 Context adapter stage

Command:

```bash
python3 agents/analytics-specialist/scripts/fetch_context_adapters.py \
  --input /tmp/ecmonitor_validation/validated_rows.json \
  --out-dir /tmp/ecmonitor_context
```

Optional online fetch:

```bash
python3 agents/analytics-specialist/scripts/fetch_context_adapters.py \
  --input /tmp/ecmonitor_validation/validated_rows.json \
  --out-dir /tmp/ecmonitor_context \
  --fetch
```

What it does:

- extracts DOI list
- extracts country candidates
- prepares request manifest for:
  - Crossref
  - OpenAlex
  - World Bank

When `--fetch` is enabled:

- calls remote APIs
- stores raw payloads
- normalizes them into:
  - `normalized/crossref_works.json`
  - `normalized/openalex_works.json`
  - `normalized/world_bank_records.json`
  - `normalized/world_bank_latest.json`
  - `normalized/context_summary.json`

Optional environment variable:

- `OPENALEX_API_KEY`

Network notes:

- `--fetch` requires internet access
- without `--fetch`, the script is manifest-only and offline-safe

### 6.3 Analysis stage

Command:

```bash
python3 agents/analytics-specialist/scripts/run_analysis_skeleton.py \
  --input /tmp/ecmonitor_validation/validated_rows.json \
  --context-manifest /tmp/ecmonitor_context/context_manifest.json \
  --out-dir /tmp/ecmonitor_analytics
```

What it reads:

- `validated_rows.json`
- optional `context_manifest.json`
- optional normalized context outputs referenced by the manifest

What it writes:

- `analytics_summary.json`
- `pollutant_summary.csv`
- `matrix_summary.csv`
- `geography_summary.csv`
- `enriched_rows.json`
- optional copied `context_manifest.json`

What is currently built in:

- count summaries
- trend summaries
- numeric min/max/mean summaries
- row-level context merge for:
  - Crossref
  - OpenAlex
  - World Bank latest indicators

What is **not yet built in**:

- regression models
- ML pipelines
- SHAP
- database-backed serving

---

## 7. PlatformSpecialist

Location:

- `agents/platform-specialist/AGENT.md`
- `agents/platform-specialist/scripts/build_publication_bundle.py`
- `agents/platform-specialist/references/publication_bundle_schema.json`
- `agents/platform-specialist/references/public_api_schema.json`
- `agents/platform-specialist/references/api-contract.md`

Purpose:

- turn approved validated + analytics outputs into a publishable bundle

### 7.1 What is built into the current sandbox

The PlatformSpecialist starter script uses:

- Python 3 standard library only
  - `argparse`
  - `json`
  - `shutil`
  - `datetime`
  - `pathlib`

It does **not** require:

- network access
- external Python packages

So Platform currently runs fully offline.

### 7.2 How to run

```bash
python3 agents/platform-specialist/scripts/build_publication_bundle.py \
  --validated /tmp/ecmonitor_validation/validated_rows.json \
  --analytics-dir /tmp/ecmonitor_analytics \
  --context-manifest /tmp/ecmonitor_context/context_manifest.json \
  --out-dir /tmp/ecmonitor_publication \
  --release-tag demo-v1
```

### 7.3 What it reads

- `validated_rows.json`
- analytics output directory
- optional `context_manifest.json`

### 7.4 What it writes

- `api/validated_rows.json`
- `api/analytics_summary.json`
- `api/context_manifest.json` if provided
- `api/enriched_rows.json` if available
- `api/publication_payload.json`
- `reports/pollutant_summary.csv`
- `reports/matrix_summary.csv`
- `reports/geography_summary.csv`
- `release_manifest.json`
- `release_notes.md`
- `site_index.json`
- `publication_bundle_schema.json`
- `public_api_schema.json`
- `README.md`

### 7.5 What the bundle is for

The current Platform bundle is designed as a neutral handoff package for:

- future database loading
- website/API adapters
- downstream frontend demos

It is not yet a full web app by itself, but it already defines:

- a bundle contract
- an API contract
- a release payload entrypoint

---

## 8. What is currently “sandbox built-in” vs “optional”

### Built into the current runnable skeleton

- Python 3 standard library for:
  - Validation
  - Analytics
  - Platform
- documented prompt/profile system for Extraction
- documented retrieval ladder and helper scripts for Retrieval

### Optional / external

- `requests` for `publisher_api_harvest.py`
- `pdfinfo` and `pdftotext` for PDF integrity checks
- publisher API keys:
  - Elsevier
  - Springer
  - Wiley
- `OPENALEX_API_KEY` optional for OpenAlex
- network access for:
  - publisher API harvesting
  - Crossref/OpenAlex/World Bank live fetches

### Not yet bundled as first-class local runtime

- a full LLM extraction caller
- automatic retrieval browser runner
- heavy analytics dependencies such as pandas/sklearn
- a production web frontend for publication bundles

---

## 9. Recommended “safe local” run sequence

If you want the current repo to run **entirely offline inside a minimal sandbox**, use:

1. prepare or reuse extracted JSON rows
2. run Validation
3. run Analytics without `--fetch`
4. run Platform

Example:

```bash
python3 agents/validation-specialist/scripts/validate_extraction_batch.py \
  --input examples/benchmark-instances/science_advances_legacy_pops_global_ocean/sample_results.json \
  --benchmark science \
  --out-dir /tmp/ecmonitor_validation

python3 agents/analytics-specialist/scripts/fetch_context_adapters.py \
  --input /tmp/ecmonitor_validation/validated_rows.json \
  --out-dir /tmp/ecmonitor_context

python3 agents/analytics-specialist/scripts/run_analysis_skeleton.py \
  --input /tmp/ecmonitor_validation/validated_rows.json \
  --context-manifest /tmp/ecmonitor_context/context_manifest.json \
  --out-dir /tmp/ecmonitor_analytics

python3 agents/platform-specialist/scripts/build_publication_bundle.py \
  --validated /tmp/ecmonitor_validation/validated_rows.json \
  --analytics-dir /tmp/ecmonitor_analytics \
  --context-manifest /tmp/ecmonitor_context/context_manifest.json \
  --out-dir /tmp/ecmonitor_publication \
  --release-tag demo-v1
```

---

## 10. Current maturity by part

- `ResearchManager`: contract/document stage
- `RetrievalSpecialist`: helper-script stage
- `ExtractionSpecialist`: prompt/profile stage
- `ValidationSpecialist`: runnable skeleton
- `AnalyticsSpecialist`: runnable skeleton with optional live context fetch
- `PlatformSpecialist`: runnable skeleton with bundle/schema output

That means the repo is already useful for:

- benchmark design
- validation
- lightweight analytics
- publication packaging

but still needs further work for:

- full retrieval automation
- full extraction runtime
- production deployment layers
