# Workflow State Machine

The canonical happy path is:

`created -> retrieving -> screening -> download_queued -> downloading -> extracting -> validating -> committing -> completed`

Legal feedback loops are explicit:

- Validation to Download: missing full text, supplementary information, or analyte identity evidence.
- Validation to Extraction: bounded table/OCR/source-binding re-extraction.
- Validation to Retrieval: a versioned QueryPatch only when validation evidence demonstrates a systematic retrieval gap.
- Validation to Human Review: unresolved scientific ambiguity, geography conflict, or exhausted targeted retry budget.

Terminal failures and blocked access are distinct. A record without current entitlement may be marked `blocked_access` or permanently skipped without failing unrelated documents.

Every transition carries the previous state, next state, timestamp, actor, reason code, metadata, and optimistic revision. Illegal or stale transitions are rejected.
