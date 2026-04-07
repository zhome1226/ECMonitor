# Publication API Contract

The starter publication bundle exposes files in a predictable way:

- `api/validated_rows.json`
- `api/analytics_summary.json`
- `api/context_manifest.json` when analytics context was prepared

Recommended downstream usage:

- treat `release_manifest.json` as the source of truth for bundle contents
- treat `publication_bundle_schema.json` as the minimum contract
- keep validated and analytics payloads versioned together
