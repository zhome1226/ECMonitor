# ECMonitor Multi-Agent Architecture

ECMonitor uses a gated specialist workflow so benchmark evidence can move from literature retrieval to analysis and publication without losing provenance.

## Specialist roles

### ResearchManager

- defines benchmark scope
- sets inclusion and exclusion rules
- declares the expected row shape
- defines release criteria

### RetrievalSpecialist

- obtains validated PDFs, HTML snapshots, or supplements
- records source type, URL, access path, and integrity status
- stops at the first successful source in the approved retrieval ladder

### ExtractionSpecialist

- extracts schema-constrained records from validated sources
- preserves evidence location, unresolved notes, and provenance
- adapts extraction level to the benchmark profile

### ValidationSpecialist

- checks completeness, units, provenance, conflicts, and duplicates
- approves or rejects candidate outputs
- emits audit and correction artifacts

### AnalyticsSpecialist

- joins validated outputs with external context data
- runs benchmark-appropriate models or summary analytics
- never backfills missing evidence by invention

### PlatformSpecialist

- packages approved outputs for publication
- prepares files for databases, APIs, dashboards, or websites
- tracks release versions and provenance hashes

## Core artifacts

- `task_brief.md`
- `targets.csv`
- `retrieval_manifest.csv/json`
- `source_snapshots/`
- `extraction_outputs/`
- `validation_audit.csv`
- `analytics_outputs/`
- `publication_bundle/`
- `summary.json`

## Gates

1. scope gate — task brief is explicit enough to execute
2. source gate — source is validated before extraction
3. evidence gate — extraction output includes evidence and provenance
4. quality gate — validation approves the row for downstream use
5. analytics gate — derived outputs cite validated evidence only
6. release gate — publication contains only approved artifacts
