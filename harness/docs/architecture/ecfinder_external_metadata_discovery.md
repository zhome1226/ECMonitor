# ECfinder External Metadata Discovery

Retrieval Specialist reuses the ECfinder `external_metadata_discovery_v1`
contract as the provider-facing metadata discovery layer. The local copy is in
`skills/search/external_metadata_discovery/`.

This contract is not a replacement for the Retrieval Specialist harness. The
harness still owns protocol loading, canonical query compilation, persistence,
deduplication, screening, scoring, handoff, checkpoints, memory release, and
paper exports.

Live discovery invokes the external ECfinder runner configured by
`ECFINDER_METADATA_SKILL_PATH`. ECMonitor validates the request/result schemas,
persists returned file references, imports provider pages into SQLite, and keeps
screening, scoring, query selection, and download handoff inside Retrieval
Specialist. ECMonitor must not import or independently maintain ECfinder
provider adapter modules.
