# Retrieval Specialist

## Responsibility

Retrieval Specialist converts a versioned research protocol into an auditable candidate document set. It owns query planning and compilation, external metadata discovery, normalization, deduplication, title/abstract screening, missing-metadata deferral, term evidence, QueryPatch evaluation, acceptance/rollback, saturation, and the transactional download outbox.

## Boundary

Provider-specific retrieval remains behind the external metadata discovery gateway. Retrieval Specialist does not download full text, extract observations, accept scientific records, run Git, read credentials directly, or change the research protocol outside versioned configuration.

## Runtime

- Code: `src/ecmonitor/retrieval_specialist/`
- Config: `configs/retrieval/`
- Schemas: `schemas/retrieval/`
- Prompts: `prompts/retrieval/`
- Handoff: `schemas/handoff/download_request.schema.json`
