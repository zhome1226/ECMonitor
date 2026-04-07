# ValidationSpecialist

ValidationSpecialist reviews extraction outputs before any analytics or publication step.

## Responsibilities

- check completeness and provenance
- normalize or annotate units when defensible
- preserve conflicts rather than silently collapsing them
- reject rows that fail evidence or source rules

## Starter script

- `scripts/validate_extraction_batch.py`
- benchmark rulebooks:
  - `references/rulebooks/science_synthesis_rulebook.json`
  - `references/rulebooks/nature_record_rulebook.json`
  - `references/rulebooks/est_reference_rebuild_rulebook.json`

See `references/quality-gate.md`.
