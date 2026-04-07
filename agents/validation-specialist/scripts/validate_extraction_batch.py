#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


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


def normalize_identity(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    doi = str(row.get("doi") or "").strip().lower()
    pollutant = str(first_present(row, ["pollutant_group", "pollutant_original", "pollutant"]) or "").strip().lower()
    matrix = str(row.get("matrix") or "").strip().lower()
    geography = str(first_present(row, ["geographic_scope", "location", "country"]) or "").strip().lower()
    window = str(first_present(row, ["temporal_window", "sampling_period", "sampling_period_label"]) or "").strip().lower()
    return doi, pollutant, matrix, geography, window


def validate_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    identities = Counter(normalize_identity(row) for row in rows)
    audited: list[dict[str, Any]] = []
    summary = Counter()

    for index, row in enumerate(rows, start=1):
        notes: list[str] = []
        status = "approved"

        if not row.get("paper_title"):
            status = "rejected"
            notes.append("missing paper_title")
        if not row.get("doi"):
            status = "rejected"
            notes.append("missing doi")

        pollutant = first_present(row, ["pollutant_group", "pollutant_original", "pollutant"])
        if not pollutant:
            status = "needs_review" if status != "rejected" else status
            notes.append("missing pollutant identity")

        if not row.get("matrix"):
            status = "needs_review" if status != "rejected" else status
            notes.append("missing matrix")

        evidence = first_present(row, ["evidence_location", "evidence_page", "table_figure_id"])
        if not evidence:
            status = "needs_review" if status != "rejected" else status
            notes.append("missing evidence location")

        if identities[normalize_identity(row)] > 1:
            status = "needs_review" if status == "approved" else status
            notes.append("possible duplicate row")

        audited_row = dict(row)
        audited_row["row_number"] = index
        audited_row["validation_status"] = status
        audited_row["validation_notes"] = notes
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(args.input)
    audited, summary = validate_rows(rows)

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
        },
    )

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
