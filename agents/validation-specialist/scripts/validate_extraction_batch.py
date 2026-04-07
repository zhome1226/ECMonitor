#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_RULEBOOK = {
    "name": "generic_monitoring_rulebook",
    "required_fields": ["paper_title", "doi"],
    "required_any_groups": [
        ["pollutant_group", "pollutant_original", "pollutant"],
        ["matrix"],
        ["evidence_location", "evidence_page", "table_figure_id"],
    ],
    "duplicate_identity_fields": [
        "doi",
        "pollutant_group|pollutant_original|pollutant",
        "matrix",
        "geographic_scope|location|country",
        "temporal_window|sampling_period|sampling_period_label",
    ],
    "allowed_statuses": ["approved", "needs_review", "rejected"],
    "review_if_missing": ["matrix"],
    "reject_if_missing": ["paper_title", "doi"],
    "forbidden_fields": ["model_prediction", "toxicity_threshold", "regulatory_limit"],
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    raise ValueError("Input JSON must be a list of row objects.")


def first_present(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", []):
            return value
    return None


def load_rulebook(path: Path | None) -> dict[str, Any]:
    if path is None:
        return dict(DEFAULT_RULEBOOK)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Rulebook JSON must be an object.")
    merged = dict(DEFAULT_RULEBOOK)
    merged.update(payload)
    return merged


def expand_alias_group(group: str) -> list[str]:
    return [part.strip() for part in group.split("|") if part.strip()]


def get_field_value(row: dict[str, Any], field: str) -> Any:
    if "|" in field:
        return first_present(row, expand_alias_group(field))
    return row.get(field)


def normalize_identity(row: dict[str, Any], identity_fields: list[str]) -> tuple[str, ...]:
    return tuple(str(get_field_value(row, field) or "").strip().lower() for field in identity_fields)


def apply_status(current: str, target: str, allowed_statuses: list[str]) -> str:
    order = {status: index for index, status in enumerate(allowed_statuses)}
    if target not in order or current not in order:
        return target
    return current if order[current] >= order[target] else target


def validate_rows(rows: list[dict[str, Any]], rulebook: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    identity_fields = rulebook.get("duplicate_identity_fields", DEFAULT_RULEBOOK["duplicate_identity_fields"])
    allowed_statuses = rulebook.get("allowed_statuses", DEFAULT_RULEBOOK["allowed_statuses"])
    identities = Counter(normalize_identity(row, identity_fields) for row in rows)
    audited: list[dict[str, Any]] = []
    summary = Counter()

    required_fields = rulebook.get("required_fields", [])
    required_any_groups = rulebook.get("required_any_groups", [])
    review_if_missing = set(rulebook.get("review_if_missing", []))
    reject_if_missing = set(rulebook.get("reject_if_missing", []))
    forbidden_fields = set(rulebook.get("forbidden_fields", []))

    for index, row in enumerate(rows, start=1):
        notes: list[str] = []
        status = "approved"

        for field in required_fields:
            if get_field_value(row, field) in (None, "", []):
                target = "rejected" if field in reject_if_missing else "needs_review"
                status = apply_status(status, target, allowed_statuses)
                notes.append(f"missing {field}")

        for group in required_any_groups:
            values = [get_field_value(row, field) for field in group]
            if all(value in (None, "", []) for value in values):
                label = "/".join(group)
                target = "needs_review"
                if any(field in reject_if_missing for field in group):
                    target = "rejected"
                status = apply_status(status, target, allowed_statuses)
                notes.append(f"missing one of {label}")

        for field in review_if_missing:
            if get_field_value(row, field) in (None, "", []):
                status = apply_status(status, "needs_review", allowed_statuses)
                notes.append(f"review required for missing {field}")

        for field in forbidden_fields:
            if get_field_value(row, field) not in (None, "", []):
                status = apply_status(status, "rejected", allowed_statuses)
                notes.append(f"forbidden field present: {field}")

        if identities[normalize_identity(row, identity_fields)] > 1:
            status = apply_status(status, "needs_review", allowed_statuses)
            notes.append("possible duplicate row")

        audited_row = dict(row)
        audited_row["row_number"] = index
        audited_row["validation_status"] = status
        audited_row["validation_notes"] = notes
        audited_row["applied_rulebook"] = rulebook.get("name", DEFAULT_RULEBOOK["name"])
        audited.append(audited_row)
        summary[status] += 1

    return audited, {
        "total_rows": len(rows),
        "approved_rows": summary["approved"],
        "needs_review_rows": summary["needs_review"],
        "rejected_rows": summary["rejected"],
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            serializable = {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                for key, value in row.items()
            }
            writer.writerow(serializable)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate extraction outputs and split them into approved/review/rejected sets.")
    parser.add_argument("--input", required=True, type=Path, help="Input JSON file containing extracted rows.")
    parser.add_argument("--out-dir", required=True, type=Path, help="Directory for validation outputs.")
    parser.add_argument("--rulebook", default=None, type=Path, help="Optional benchmark-specific rulebook JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(args.input)
    rulebook = load_rulebook(args.rulebook)
    audited, summary = validate_rows(rows, rulebook)

    approved = [row for row in audited if row["validation_status"] == "approved"]
    review = [row for row in audited if row["validation_status"] == "needs_review"]
    rejected = [row for row in audited if row["validation_status"] == "rejected"]

    write_csv(args.out_dir / "validation_audit.csv", audited)
    write_json(args.out_dir / "validated_rows.json", approved)
    write_json(args.out_dir / "review_rows.json", review)
    write_json(args.out_dir / "rejected_rows.json", rejected)
    write_json(
        args.out_dir / "summary.json",
        {
            **summary,
            "input_path": str(args.input),
            "out_dir": str(args.out_dir),
            "rulebook_name": rulebook.get("name", DEFAULT_RULEBOOK["name"]),
            "rulebook_path": str(args.rulebook) if args.rulebook else None,
        },
    )

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
