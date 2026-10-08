"""Export committed/pending observation records from the full-text extraction control DB.

Reads ``control.sqlite3`` and writes a flat CSV plus a JSONL dump with one row per
observation record. DOI is taken from the per-document report's bibliographic metadata
when available and otherwise derived from the PDF filename.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


def _candidate(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("candidate") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def _nested(candidate: dict[str, Any], key: str) -> dict[str, Any]:
    value = candidate.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text if text else ""


def _num(value: object) -> object:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _geocode_fallback(
    location: dict[str, Any],
    *,
    resolver: Any | None = None,
) -> tuple[object, object, str, str]:
    """Fill approximate coordinates for records produced before geocoding was enabled."""
    if location.get("latitude") is not None and location.get("longitude") is not None:
        return (
            location.get("latitude"),
            location.get("longitude"),
            location.get("geocode_source") or "",
            location.get("geocode_matched_name") or "",
        )
    if resolver is None:
        return None, None, "", ""
    try:
        result = resolver.resolve(location)
    except Exception:
        return None, None, "", ""
    if result is None:
        return None, None, "", ""
    return result.latitude, result.longitude, result.source, (result.matched_name or "")


def _doi_from_filename(source_path: str) -> str:
    stem = Path(source_path).stem
    if not stem:
        return ""
    if stem.startswith("10."):
        return stem.replace("_", "/")
    match = re.search(r"(10\.\S+)", stem)
    return match.group(1).replace("_", "/") if match else stem


def _doi_for_session(
    control: sqlite3.Connection, document_session_id: str, source_path: str
) -> str:
    # The per-document report on disk carries bibliographic metadata.
    del control, document_session_id
    return _doi_from_filename(source_path)


def export(run_root: Path, output_stem: Path | None = None) -> dict[str, int]:
    control_path = run_root / "state" / "control.sqlite3"
    if not control_path.is_file():
        raise FileNotFoundError(control_path)
    output_stem = output_stem or run_root / "committed_pending_observations"

    columns = [
        "doi",
        "source_pdf",
        "disposition",
        "reported_name",
        "canonical_name",
        "cas_rn",
        "resolution_status",
        "specificity_status",
        "is_individual_chemical",
        "observation_type",
        "value",
        "value_numeric",
        "unit",
        "statistic",
        "qualifier",
        "range_low",
        "range_high",
        "matrix",
        "phase_or_fraction",
        "sample_type",
        "site_name",
        "waterbody",
        "city",
        "admin1",
        "country",
        "latitude",
        "longitude",
        "geocode_source",
        "geocode_matched_name",
        "sampling_year",
        "sampling_basis",
        "sampling_approximate",
        "sampling_raw_text",
        "method_name",
        "extraction_method",
        "detection_method",
        "instrument",
        "lod_raw",
        "loq_raw",
        "page_start",
        "page_end",
        "quote",
        "reason_codes",
    ]

    try:
        from ecmonitor.fulltext_extraction.geocode import OfflineGeocodeResolver

        resolver = OfflineGeocodeResolver(Path(__file__).resolve().parents[1] / "local_assets" / "geo")
    except Exception:
        resolver = None

    with sqlite3.connect(control_path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT o.record_id, o.document_session_id, o.disposition, o.payload_json,
                   d.source_path
            FROM observation_records o
            JOIN document_sessions s ON s.document_session_id = o.document_session_id
            JOIN document_assets d ON d.document_id = s.document_id
            ORDER BY o.record_id
            """
        ).fetchall()

    csv_path = output_stem.with_suffix(".csv")
    jsonl_path = output_stem.with_suffix(".jsonl")
    output_stem.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_handle, jsonl_path.open(
        "w", encoding="utf-8"
    ) as json_handle:
        writer = csv.DictWriter(csv_handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = json.loads(row["payload_json"])
            cand = _candidate(payload)
            analyte = _nested(cand, "analyte")
            result = _nested(cand, "result")
            sample = _nested(cand, "sample")
            location = _nested(cand, "location")
            sampling = _nested(cand, "sampling_time")
            method = _nested(cand, "analytical_method")
            evidence = _nested(cand, "evidence")
            decision = payload.get("decision") if isinstance(payload, dict) else {}
            reason_codes = decision.get("reason_codes") if isinstance(decision, dict) else []
            _lat, _lng, _geo_src, _geo_name = _geocode_fallback(location, resolver=resolver)
            out = {
                "doi": _doi_for_session(con, row["document_session_id"], row["source_path"]),
                "source_pdf": _text(row["source_path"]),
                "disposition": _text(row["disposition"]),
                "reported_name": _text(analyte.get("reported_name") or analyte.get("raw_name")),
                "canonical_name": _text(analyte.get("canonical_name")),
                "cas_rn": _text(analyte.get("cas_rn")),
                "resolution_status": _text(analyte.get("resolution_status")),
                "specificity_status": _text(analyte.get("specificity_status")),
                "is_individual_chemical": analyte.get("is_individual_chemical"),
                "observation_type": _text(cand.get("observation_type")),
                "value": _text(result.get("raw_value")),
                "value_numeric": _num(result.get("value_numeric")),
                "unit": _text(result.get("raw_unit")),
                "statistic": _text(result.get("statistic")),
                "qualifier": _text(result.get("qualifier")),
                "range_low": _num(result.get("range_low")),
                "range_high": _num(result.get("range_high")),
                "matrix": _text(sample.get("matrix_raw") or sample.get("matrix_normalized")),
                "phase_or_fraction": _text(sample.get("phase_or_fraction")),
                "sample_type": _text(sample.get("sample_type")),
                "site_name": _text(location.get("site_name")),
                "waterbody": _text(location.get("waterbody")),
                "city": _text(location.get("city")),
                "admin1": _text(location.get("admin1")),
                "country": _text(location.get("country")),
                "latitude": _num(_lat),
                "longitude": _num(_lng),
                "geocode_source": _text(_geo_src),
                "geocode_matched_name": _text(_geo_name),
                "sampling_year": _num(sampling.get("year")),
                "sampling_basis": _text(sampling.get("basis")),
                "sampling_approximate": sampling.get("approximate"),
                "sampling_raw_text": _text(sampling.get("raw_text")),
                "method_name": _text(method.get("method_name")),
                "extraction_method": _text(method.get("extraction_method")),
                "detection_method": _text(method.get("detection_method")),
                "instrument": _text(method.get("instrument")),
                "lod_raw": _text(method.get("lod_raw")),
                "loq_raw": _text(method.get("loq_raw")),
                "page_start": _num(evidence.get("page_start")),
                "page_end": _num(evidence.get("page_end")),
                "quote": _text(evidence.get("quote")),
                "reason_codes": ";".join(_text(item) for item in reason_codes),
            }
            writer.writerow(out)
            json_handle.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
            count += 1
    return {"rows": count, "csv": str(csv_path), "jsonl": str(jsonl_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Export observation records to CSV/JSONL")
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = export(args.run_root, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
