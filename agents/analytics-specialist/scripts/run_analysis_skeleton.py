#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
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


def maybe_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        writer.writerows(rows)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pollutant_counts = Counter()
    matrix_counts = Counter()
    geography_counts = Counter()
    trend_counts = Counter()
    numeric_values: list[float] = []
    risk_thresholds: list[float] = []

    for row in rows:
        pollutant = first_present(row, ["pollutant_group", "pollutant_original", "pollutant"])
        matrix = row.get("matrix")
        geography = first_present(row, ["geographic_scope", "location", "country"])
        trend = row.get("trend_direction")

        if pollutant:
            pollutant_counts[str(pollutant)] += 1
        if matrix:
            matrix_counts[str(matrix)] += 1
        if geography:
            geography_counts[str(geography)] += 1
        if trend:
            trend_counts[str(trend)] += 1

        for key in ["value_numeric", "value_min", "value_max", "measurement_record_count_min"]:
            number = maybe_number(row.get(key))
            if number is not None:
                numeric_values.append(number)
        threshold = maybe_number(row.get("risk_threshold"))
        if threshold is not None:
            risk_thresholds.append(threshold)

    return {
        "total_rows": len(rows),
        "top_pollutants": pollutant_counts.most_common(10),
        "top_matrices": matrix_counts.most_common(10),
        "top_geographies": geography_counts.most_common(10),
        "trend_direction_counts": dict(trend_counts),
        "numeric_summary": {
            "count": len(numeric_values),
            "mean": mean(numeric_values) if numeric_values else None,
            "min": min(numeric_values) if numeric_values else None,
            "max": max(numeric_values) if numeric_values else None,
        },
        "risk_threshold_summary": {
            "count": len(risk_thresholds),
            "min": min(risk_thresholds) if risk_thresholds else None,
            "max": max(risk_thresholds) if risk_thresholds else None,
        },
    }


def build_group_table(rows: list[dict[str, Any]], field_name: str, aliases: list[str]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"row_count": 0, "trend_notes": set()})
    for row in rows:
        key = first_present(row, aliases)
        if not key:
            continue
        entry = grouped[str(key)]
        entry["row_count"] += 1
        trend = row.get("trend_direction")
        if trend:
            entry["trend_notes"].add(str(trend))

    out: list[dict[str, Any]] = []
    for key, value in grouped.items():
        out.append(
            {
                field_name: key,
                "row_count": value["row_count"],
                "trend_notes": "; ".join(sorted(value["trend_notes"])),
            }
        )
    out.sort(key=lambda row: (-row["row_count"], row[field_name]))
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create lightweight analytics summaries from validated ECMonitor rows.")
    parser.add_argument("--input", required=True, type=Path, help="Validated rows JSON.")
    parser.add_argument("--out-dir", required=True, type=Path, help="Directory for analytics outputs.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(args.input)
    summary = summarize(rows)
    pollutant_table = build_group_table(rows, "pollutant", ["pollutant_group", "pollutant_original", "pollutant"])
    matrix_table = build_group_table(rows, "matrix", ["matrix"])
    geography_table = build_group_table(rows, "geography", ["geographic_scope", "location", "country"])

    (args.out_dir / "analytics_summary.json").write_text(
        json.dumps(
            {
                **summary,
                "input_path": str(args.input),
                "out_dir": str(args.out_dir),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    write_csv(args.out_dir / "pollutant_summary.csv", pollutant_table)
    write_csv(args.out_dir / "matrix_summary.csv", matrix_table)
    write_csv(args.out_dir / "geography_summary.csv", geography_table)

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
