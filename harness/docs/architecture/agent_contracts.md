# Agent Contracts

All asynchronous work uses `schemas/common/task_envelope.schema.json`. The envelope supplies stable workflow, document, idempotency, correlation, causation, attempt, policy, and artifact references.

Specialized contracts include:

- Retrieval objects in `schemas/retrieval/`.
- Retrieval-to-download events in `schemas/handoff/`.
- Download validation results in `schemas/download/`.
- Extraction candidates and observations in `schemas/extraction/`.
- Validation terminal outcomes in `schemas/validation/`.
- Workflow snapshots in `schemas/workflow/`.

Payloads must contain references to restricted artifacts rather than embedding article PDFs, authenticated HTML, cookies, or credentials. Schema versions and policy versions are immutable identifiers, not mutable labels such as `latest`.
