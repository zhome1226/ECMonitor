# Validation Specialist

## Responsibility

Validation Specialist independently checks candidate observations against source evidence and deterministic policy. It verifies chemical identity, units, statistics, matrix, location, sampling time, methods, current-study provenance, table layout, and source anchors.

## Outcomes

The stable workflow boundary emits a terminal status, coarse disposition, output stream, policy rule ID, reason codes, and human-review requirement. Decisions are append-only/superseding events rather than silent edits.

Retry is allowed only when rereading the same or expanded evidence can repair a bounded extraction error. Missing supplementary identity evidence routes to Download; persistent source/matrix ambiguity routes to Human Review; deterministic scope or provenance rejection is committed without pointless re-extraction.

- Policy implementation: `src/ecmonitor/fulltext_extraction/policy.py`
- Stable boundary: `src/ecmonitor/validation_specialist/service.py`
- Policy config: `configs/extraction/fulltext_validation_policy_v1.yaml`
- Schema: `schemas/validation/validation_outcome.schema.json`
- Prompt: `prompts/extraction/evidence_validator.md`
