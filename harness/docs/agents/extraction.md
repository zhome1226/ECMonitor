# Extraction Specialist

## Responsibility

Extraction Specialist transforms the current document into evidence-bound candidate observations. A candidate binds analyte, result, unit, statistic, matrix, location, sampling time, method, and evidence. It does not decide final acceptance or write directly to the authoritative observation store.

## Runtime behavior

- Inventory and validate the document.
- Choose direct-PDF, native-text, layout-aware chunk, alternate parser, table-aware, or OCR paths.
- Isolate model processes and use structured JSON contracts.
- Apply deterministic schema, scope, source, result, censoring, and identity gates.
- Persist per-document progress transactionally.
- Accept targeted retry instructions from Validation without reprocessing unrelated evidence.
- Release document/model resources before the next document.

- Code: `src/ecmonitor/fulltext_extraction/`
- Config: `configs/extraction/fulltext_extraction_v1.yaml`
- Schema: `schemas/extraction/model_occurrence_candidate.schema.json`
- Prompt: `prompts/extraction/occurrence_extractor.md`
