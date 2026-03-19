# Benchmark Instances

This folder contains three worked examples for EcoScan's literature-database workflow.

Each subfolder documents:

- where the benchmark came from
- why it was selected
- what the benchmark is trying to reconstruct
- a prompt template
- a small sample of extracted records

## Why these three examples

These three benchmarks were chosen because they represent three different extraction shapes that EcoScan already handled successfully:

1. `Nature`
   - best for record-level environmental monitoring reconstruction
   - many rows need to be split very finely
2. `Science`
   - best for study-level plus monitoring-summary reconstruction
   - preserves context while extracting key quantitative results
3. `EST_TFA`
   - best for benchmark-table rebuilding with reference-resolution logic
   - row-level truth comes from a benchmark source table, while DOI and reference metadata are resolved separately

## Current local source artifacts

These example folders were derived from the following local outputs:

- Nature:
  - `/mnt/d/paper_data/pdf/Nature/extracted_json/all_records.json`
- Science:
  - `/mnt/d/paper_data/pdf/Science/extracted_json/all_records.json`
- EST / TFA:
  - `/mnt/c/Users/Administrator/codex/projects/tfa_est_rebuild/tfa_literature_database_rebuilt.xlsx`

## Current production counts

- Nature: `327` papers, `1055` extracted records
- Science: `82` papers, `141` extracted results
- EST / TFA: `169` monitoring rows, `168` DOI backfills, `1` unresolved grey-literature record
