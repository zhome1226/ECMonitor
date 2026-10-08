# System Overview

## Components

ECMonitor uses four bounded specialist agents coordinated by a deterministic workflow engine.

1. Retrieval Specialist owns bibliographic discovery, screening, query refinement, saturation, and the transactional download outbox.
2. Download Specialist owns lawful acquisition routes, local-library reuse, content validation, and acquisition manifests.
3. Extraction Specialist owns document parsing and evidence-bound candidate generation.
4. Validation Specialist independently checks evidence and assigns a policy-backed terminal disposition.
5. Workflow Orchestrator owns only operational control: queues, leases, retries, checkpoint/resume, legal state transitions, and audit events.

The orchestrator has no scientific authority and cannot silently modify the protocol, accept an observation, or override a validation decision.

## Trust boundaries

- Retrieval provider implementations remain behind the external metadata discovery boundary.
- Publisher and institution credentials remain outside the repository and outside task payloads.
- Downloaded full text is restricted runtime data, not source code.
- Model output is untrusted until schema and deterministic quality gates pass.
- Validation decisions are versioned events; corrections supersede rather than overwrite prior records.

## Authoritative state

Active workflow state belongs in a transactional SQLite or PostgreSQL control plane. JSON, JSONL, CSV, YAML, and Markdown files are review/export formats and do not replace active state.
