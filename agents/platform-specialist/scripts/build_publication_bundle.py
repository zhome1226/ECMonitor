#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


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
        "pollutant_summary": copy_if_exists(args.analytics_dir / "pollutant_summary.csv", reports_dir / "pollutant_summary.csv"),
        "matrix_summary": copy_if_exists(args.analytics_dir / "matrix_summary.csv", reports_dir / "matrix_summary.csv"),
        "geography_summary": copy_if_exists(args.analytics_dir / "geography_summary.csv", reports_dir / "geography_summary.csv"),
    }

    manifest = {
        "release_tag": release_tag,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validated_input": str(args.validated),
        "analytics_dir": str(args.analytics_dir),
        "bundle_root": str(args.out_dir),
        "copied_artifacts": copied,
        "notes": [
            "This is a skeleton publication bundle.",
            "Only validated and analytics artifacts should be included.",
            "Downstream database or web adapters can extend this bundle.",
        ],
    }

    (args.out_dir / "release_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
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
