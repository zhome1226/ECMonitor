#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "references" / "publication_bundle_schema.json"
API_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "references" / "public_api_schema.json"


def copy_if_exists(src: Path, dest: Path) -> str | None:
    if not src.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return str(dest)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a lightweight publication bundle from validated and analytics outputs.")
    parser.add_argument("--validated", required=True, type=Path, help="Path to validated_rows.json.")
    parser.add_argument("--analytics-dir", required=True, type=Path, help="Directory containing analytics outputs.")
    parser.add_argument("--out-dir", required=True, type=Path, help="Publication bundle output directory.")
    parser.add_argument("--release-tag", default=None, help="Optional release tag. Defaults to timestamp-based tag.")
    parser.add_argument("--context-manifest", default=None, type=Path, help="Optional context manifest JSON to publish alongside analytics.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    release_tag = args.release_tag or f"release-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    api_dir = args.out_dir / "api"
    reports_dir = args.out_dir / "reports"
    api_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    copied = {
        "validated_rows": copy_if_exists(args.validated, api_dir / "validated_rows.json"),
        "analytics_summary": copy_if_exists(args.analytics_dir / "analytics_summary.json", api_dir / "analytics_summary.json"),
        "context_manifest": copy_if_exists(args.context_manifest, api_dir / "context_manifest.json") if args.context_manifest else None,
        "enriched_rows": copy_if_exists(args.analytics_dir / "enriched_rows.json", api_dir / "enriched_rows.json"),
        "pollutant_summary": copy_if_exists(args.analytics_dir / "pollutant_summary.csv", reports_dir / "pollutant_summary.csv"),
        "matrix_summary": copy_if_exists(args.analytics_dir / "matrix_summary.csv", reports_dir / "matrix_summary.csv"),
        "geography_summary": copy_if_exists(args.analytics_dir / "geography_summary.csv", reports_dir / "geography_summary.csv"),
    }

    schema_payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    (args.out_dir / "publication_bundle_schema.json").write_text(
        json.dumps(schema_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    api_schema_payload = json.loads(API_SCHEMA_PATH.read_text(encoding="utf-8"))
    (args.out_dir / "public_api_schema.json").write_text(
        json.dumps(api_schema_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    manifest = {
        "release_tag": release_tag,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validated_input": str(args.validated),
        "analytics_dir": str(args.analytics_dir),
        "context_manifest_input": str(args.context_manifest) if args.context_manifest else None,
        "bundle_root": str(args.out_dir),
        "copied_artifacts": copied,
        "api_endpoints": {
            "validated_rows": "api/validated_rows.json",
            "analytics_summary": "api/analytics_summary.json",
            "context_manifest": "api/context_manifest.json" if copied["context_manifest"] else None,
            "enriched_rows": "api/enriched_rows.json" if copied["enriched_rows"] else None,
        },
        "notes": [
            "This is a skeleton publication bundle.",
            "Only validated and analytics artifacts should be included.",
            "Downstream database or web adapters can extend this bundle.",
        ],
    }

    (args.out_dir / "release_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out_dir / "site_index.json").write_text(
        json.dumps(
            {
                "release_tag": release_tag,
                "title": "ECMonitor publication bundle",
                "api": manifest["api_endpoints"],
                "schemas": {
                    "bundle": "publication_bundle_schema.json",
                    "public_api": "public_api_schema.json",
                },
                "reports": {
                    "pollutants": "reports/pollutant_summary.csv" if copied["pollutant_summary"] else None,
                    "matrices": "reports/matrix_summary.csv" if copied["matrix_summary"] else None,
                    "geographies": "reports/geography_summary.csv" if copied["geography_summary"] else None,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (api_dir / "publication_payload.json").write_text(
        json.dumps(
            {
                "release_tag": release_tag,
                "generated_at": manifest["generated_at"],
                "assets": manifest["api_endpoints"],
                "schemas": {
                    "bundle": "../publication_bundle_schema.json",
                    "public_api": "../public_api_schema.json",
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (args.out_dir / "release_notes.md").write_text(
        "\n".join(
            [
                f"# Release Notes: {release_tag}",
                "",
                "- validated outputs included",
                "- analytics summary included",
                f"- context manifest included: {'yes' if copied['context_manifest'] else 'no'}",
                "",
                "See `release_manifest.json` and `publication_bundle_schema.json`.",
            ]
        ),
        encoding="utf-8",
    )
    (args.out_dir / "README.md").write_text(
        "\n".join(
            [
                f"# Publication Bundle: {release_tag}",
                "",
                "This bundle was prepared by PlatformSpecialist.",
                "",
                "Included folders:",
                "- `api/` for machine-readable outputs",
                "- `reports/` for human-facing summaries",
                "- `publication_bundle_schema.json` for the minimum contract",
                "- `public_api_schema.json` for the API payload contract",
                "- `site_index.json` for lightweight website adapters",
                "",
                "See `release_manifest.json` for provenance.",
            ]
        ),
        encoding="utf-8",
    )

    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
