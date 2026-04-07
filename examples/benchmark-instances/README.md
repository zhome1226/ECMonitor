# Benchmark Instances

This folder contains three worked examples for ECMonitor's literature-database workflow.

Each subfolder documents a real benchmark that ECMonitor has already been used to reconstruct.

Each subfolder includes:

- where the benchmark came from
- why it was selected
- what the benchmark is trying to reconstruct
- a prompt template
- a small sample of extracted records

## Why these three examples

These three benchmarks were chosen because they represent three different extraction shapes that ECMonitor already handled successfully:

1. `Nature Geoscience`
   - benchmark: `Underestimated burden of per- and polyfluoroalkyl substances in global surface waters and groundwaters`
   - DOI: `10.1038/s41561-024-01402-8`
   - best for record-level environmental monitoring reconstruction
2. `Science`
   - benchmark: `Exploring global oceanic persistence and ecological effects of legacy persistent organic pollutants across five decades`
   - DOI: `10.1126/sciadv.ado5534`
   - best for synthesis-level region × period × pollutant-group reconstruction with ecological risk notes
3. `EST`
   - benchmark: `The global threat from the irreversible accumulation of trifluoroacetic acid (TFA)`
   - DOI: `10.1021/acs.est.4c06189`
   - best for benchmark-table rebuilding with reference-resolution logic

## Current local source artifacts

These example folders were derived from the following local outputs:

- Nature Geoscience PFAS benchmark:
  - `/mnt/d/paper_data/pdf/Nature/extracted_json/all_records.json`
- Science Advances legacy POPs ocean benchmark:
  - benchmark paper and figure/table evidence summarized into synthesis-style sample outputs
- EST / TFA:
  - `/mnt/c/Users/Administrator/codex/projects/tfa_est_rebuild/tfa_literature_database_rebuilt.xlsx`

## Current production counts

- Nature: `327` papers, `1055` extracted records
- Science Advances benchmark: `>10,000` compiled POP measurements across `1980-2023`, with region-level trend and ecological-risk summaries
- EST / TFA: `169` monitoring rows, `168` DOI backfills, `1` unresolved grey-literature record
