# Publication API Contract

The starter publication bundle exposes files in a predictable way:

- `api/validated_rows.json`
- `api/analytics_summary.json`
- `api/context_manifest.json` when analytics context was prepared
- `api/enriched_rows.json` when row-level context enrichment exists
- `api/publication_payload.json` as the entrypoint for downstream adapters

Recommended downstream usage:

- treat `release_manifest.json` as the source of truth for bundle contents
- treat `publication_bundle_schema.json` as the minimum contract
- treat `public_api_schema.json` as the API contract
- keep validated and analytics payloads versioned together
