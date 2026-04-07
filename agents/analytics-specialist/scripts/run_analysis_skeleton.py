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


def load_context_manifest(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_json_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def normalize_country_to_iso3(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) == 3 and text.isalpha():
        return text.upper()
    mapping = {
        "china": "CHN",
        "united states": "USA",
        "usa": "USA",
        "germany": "DEU",
        "argentina": "ARG",
        "canada": "CAN",
        "japan": "JPN",
        "south korea": "KOR",
        "australia": "AUS",
    }
    return mapping.get(text.lower())


def load_normalized_context(context_manifest: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    if not context_manifest:
        return {"crossref": [], "openalex": [], "world_bank_latest": []}
    outputs = context_manifest.get("normalized_outputs") or {}
    paths = outputs.get("paths") or {}
    return {
        "crossref": load_json_rows(Path(paths["crossref"])) if paths.get("crossref") else [],
        "openalex": load_json_rows(Path(paths["openalex"])) if paths.get("openalex") else [],
        "world_bank_latest": load_json_rows(Path(paths["world_bank_latest"])) if paths.get("world_bank_latest") else [],
    }


def enrich_rows(rows: list[dict[str, Any]], context_rows: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    crossref_index = {str(row.get("doi") or "").lower(): row for row in context_rows["crossref"] if row.get("doi")}
    openalex_index = {str(row.get("doi") or "").lower(): row for row in context_rows["openalex"] if row.get("doi")}
    wb_index = {
        (row.get("country_iso3"), row.get("indicator")): row
        for row in context_rows["world_bank_latest"]
        if row.get("country_iso3") and row.get("indicator")
    }
    wb_by_country: dict[str, dict[str, Any]] = defaultdict(dict)
    for row in context_rows["world_bank_latest"]:
        country_iso3 = row.get("country_iso3")
        indicator = row.get("indicator")
        if country_iso3 and indicator:
            wb_by_country[country_iso3][indicator] = row.get("value")

    publisher_counts = Counter()
    journal_counts = Counter()
    concept_counts = Counter()
    indicator_coverage = Counter()
    enriched: list[dict[str, Any]] = []

    for row in rows:
        out = dict(row)
        doi = str(row.get("doi") or "").strip().lower()
        crossref = crossref_index.get(doi, {})
        openalex = openalex_index.get(doi, {})
        country_iso3 = normalize_country_to_iso3(first_present(row, ["country"]))
        world_bank_context = wb_by_country.get(country_iso3 or "", {})

        if crossref.get("publisher"):
            publisher_counts[str(crossref["publisher"])] += 1
        if crossref.get("journal"):
            journal_counts[str(crossref["journal"])] += 1
        for concept in openalex.get("top_concepts") or []:
            concept_counts[str(concept)] += 1
        for indicator in world_bank_context.keys():
            indicator_coverage[indicator] += 1

        out["context"] = {
            "crossref": {
                "publisher": crossref.get("publisher"),
                "journal": crossref.get("journal"),
                "citation_count": crossref.get("citation_count"),
                "year": crossref.get("year"),
            },
            "openalex": {
                "journal": openalex.get("journal"),
                "cited_by_count": openalex.get("cited_by_count"),
                "is_oa": openalex.get("is_oa"),
                "top_concepts": openalex.get("top_concepts"),
            },
            "world_bank": {
                "country_iso3": country_iso3,
                "indicators": world_bank_context,
            },
        }
        enriched.append(out)

    external_context_summary = {
        "crossref_records": len(crossref_index),
        "openalex_records": len(openalex_index),
        "world_bank_country_indicator_rows": len(context_rows["world_bank_latest"]),
        "top_publishers": publisher_counts.most_common(10),
        "top_journals": journal_counts.most_common(10),
        "top_concepts": concept_counts.most_common(10),
        "world_bank_indicator_coverage": dict(indicator_coverage),
    }
    return enriched, external_context_summary


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
    parser.add_argument("--context-manifest", default=None, type=Path, help="Optional context manifest from fetch_context_adapters.py")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(args.input)
    context_manifest = load_context_manifest(args.context_manifest)
    normalized_context = load_normalized_context(context_manifest)
    enriched_rows, external_context_summary = enrich_rows(rows, normalized_context)
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
                "context_manifest_path": str(args.context_manifest) if args.context_manifest else None,
                "context_overview": {
                    "doi_count": len(context_manifest.get("dois", [])) if context_manifest else 0,
                    "country_count": len(context_manifest.get("countries", [])) if context_manifest else 0,
                    "fetched": bool(context_manifest.get("fetched")) if context_manifest else False,
                },
                "external_context_summary": external_context_summary,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    write_csv(args.out_dir / "pollutant_summary.csv", pollutant_table)
    write_csv(args.out_dir / "matrix_summary.csv", matrix_table)
    write_csv(args.out_dir / "geography_summary.csv", geography_table)
    (args.out_dir / "enriched_rows.json").write_text(
        json.dumps(enriched_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if context_manifest:
        (args.out_dir / "context_manifest.json").write_text(
            json.dumps(context_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
