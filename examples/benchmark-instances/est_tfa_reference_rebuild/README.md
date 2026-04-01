# EST / TFA Reference-Rebuild Example

## Benchmark source

- Benchmark family: `EST / TFA`
- Benchmark paper:
  - `The global threat from the irreversible accumulation of trifluoroacetic acid (TFA)`
  - DOI: `10.1021/acs.est.4c06189`
- Extraction style: benchmark-table rebuild with reference resolution

## Why this example was chosen

This case is structurally different from Nature and Science.

Here, the row-level truth does not come from many PDFs directly. Instead:

- `Table S1` is the row source
- the main paper reference list resolves short citations into fuller references
- a separate mapping document resolves title, DOI, and URL

That makes it an ideal example for showing ECMonitor's reference-resolution mode.

## What the benchmark is about

The benchmark focuses on the environmental monitoring history of trifluoroacetic acid (TFA) across media such as precipitation and waters, while also preserving temporal grouping such as `pre_2010` and `post_2010`.

## Demonstration scope

This example comes from the completed EST / TFA rebuild, which currently contains:

- `169` monitoring rows
- `168` DOI backfills
- `1` unresolved grey-literature record

## Included files

- `prompt.md`
- `sample_rows.json`

## Local source used for the sample

- `/mnt/c/Users/Administrator/codex/projects/tfa_est_rebuild/tfa_literature_database_rebuilt.xlsx`
