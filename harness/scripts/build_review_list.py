"""Build the human-review list (REVIEW_LIST) from pending review tasks.

Reads ``state/control.sqlite3`` (``human_review_tasks`` joined to ``document_assets`` for the
PDF filename/DOI) and writes a Markdown list grouped by review category, reproducing the
previously hand-generated ``REVIEW_LIST_<date>.md`` format.

Since ``review-standards v1.2`` (left-censored handling), censored / no-value / frequency
records are treated as *non-concentrations*: they are grouped into the qualitative/no-value
category, the human reviewer decides whether to keep them as ``detection`` evidence, and the LOD/LOQ
columns surface the detection-limit context so the downstream censored-statistics path (KM /
MLE / ROS / LB-UB, see ``docs/research/review_standards_research/left_censored_data_handling.md``)
has the numbers it needs.

Usage::

    python scripts/build_review_list.py runtime/review-example \
        --output runtime/review-example/REVIEW_LIST.md
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

_CENSORING_QUALIFIERS = {"not_detected", "not_quantified", "less_than", "less_than_or_equal"}

_CATEGORY_ORDER = [
    "clean",
    "literature",
    "ratio",
    "aggregate",
    "no_value",
    "identity",
    "retry",
    "binding",
    "other",
]

_CATEGORY_META = {
    "clean": (
        "Passed automatic checks; pilot signoff required",
        "Numeric, table, and chemical-identity checks passed",
        "Accept after confirming the source evidence",
    ),
    "literature": (
        "Secondary literature values",
        "A cited comparison or review value rather than a local measurement",
        "Reject unless primary DOI and table evidence are verified; retain secondary-source lineage",
    ),
    "ratio": (
        "Exceedance multiple, not measured concentration",
        "The value is a ratio to a threshold",
        "Reject unless the source separately reports a measured concentration",
    ),
    "aggregate": (
        "Value covers multiple analytes",
        "A shared value, frequency, or sum cannot be assigned to one compound",
        "Bind to a supported individual analyte or reject",
    ),
    "no_value": (
        "Qualitative/no-value/semiquantitative/frequency evidence",
        "No measured numeric concentration is reported",
        "Retain as detection evidence if supported, not concentration; preserve LOD/LOQ",
    ),
    "identity": (
        "Ambiguous chemical identity",
        "The chemical name or identity is ambiguous or unresolved",
        "Resolve using source evidence or reject an unsupported identity",
    ),
    "retry": (
        "Missing fields after retry budget exhaustion",
        "Units, methods, locations, or uncertainty remain unsupported",
        "Resolve from the source, mark not_reported_in_source, or reject",
    ),
    "binding": (
        "Ambiguous observation binding",
        "Value-to-site, matrix, or time relations lack evidence",
        "Accept only if the value can be bound to one supported observation",
    ),
    "other": (
        "Other human-review cases",
        "The case does not match the categories above",
        "Accept or reject using source evidence",
    ),
}

_LITERATURE_CODES = {"comparative_literature_no_value", "literature_summary_value", "secondary_cited_value"}
_RATIO_CODES = {"ratio_to_threshold_not_measurement", "exceedance_ratio_not_concentration"}
_AGGREGATE_CODES = {"aggregate_measurement_for_two_analytes", "sum_or_total_parameter", "chemical_relation_to_frequency_ambiguous"}
_NO_VALUE_CODES = {
    "semiquantitative_ranking_only", "no_numeric_value", "qualitative_no_value",
    "censored_no_measured_concentration", "detection_frequency_not_concentration",
    "no_measurable_concentration",
}
_IDENTITY_CODES = {
    "chemical_identity_ambiguous", "chemical_identity_unresolved", "chemical_identity_conflict",
    "alias_conflict_novel", "resolution_unreliable", "alias_unverified",
}
_RETRY_CODES = {"retry_budget_exhausted"}
_BINDING_CODES = {
    "ambiguous_scope", "missing_relation_binding", "binding_missing", "ambiguous_value_relation",
    "ambiguous_sampling_time", "ambiguous_location_relation", "insufficient_evidence",
    "human_review_trigger", "matrix_binding_unclear", "time_binding_unclear", "location_ambiguous",
}


def _is_censored(candidate: dict[str, Any]) -> bool:
    result = candidate.get("result") or {}
    qualifier = result.get("qualifier")
    if qualifier in _CENSORING_QUALIFIERS:
        return True
    if result.get("statistic") == "frequency":
        return True
    raw = result.get("raw_value")
    if raw is None:
        return True
    if isinstance(raw, str):
        stripped = raw.lstrip()
        if not any(ch.isdigit() for ch in raw) or stripped.startswith("<"):
            return True
    return False


def _categorize(codes: set[str], candidate: dict[str, Any]) -> str:
    if "pilot_human_signoff_required" in codes and not _is_censored(candidate):
        return "clean"
    if codes & _LITERATURE_CODES:
        return "literature"
    if codes & _RATIO_CODES:
        return "ratio"
    if codes & _AGGREGATE_CODES:
        return "aggregate"
    if codes & _NO_VALUE_CODES or _is_censored(candidate):
        return "no_value"
    if codes & _IDENTITY_CODES:
        return "identity"
    if codes & _RETRY_CODES:
        return "retry"
    if codes & _BINDING_CODES:
        return "binding"
    return "other"


def _doi_from_filename(source_path: str) -> str:
    stem = Path(source_path or "").stem
    if not stem:
        return ""
    if stem.startswith("10."):
        return stem.replace("_", "/")
    match = re.search(r"(10\.\S+)", stem)
    return match.group(1).replace("_", "/") if match else stem


def _text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text if text else ""


def _location(candidate: dict[str, Any]) -> str:
    loc = candidate.get("location") or {}
    raw = _text(loc.get("location_raw"))
    parts = [p for p in (loc.get("waterbody"), loc.get("city"), loc.get("admin1"), loc.get("country"), loc.get("site_name")) if _text(p)]
    joined = ", ".join(parts)
    if raw:
        return f"{raw}, {joined}".strip(", ") if joined else raw
    return joined or "laboratory"


def _value_str(candidate: dict[str, Any]) -> str:
    result = candidate.get("result") or {}
    raw = _text(result.get("raw_value"))
    unit = _text(result.get("raw_unit"))
    stat = _text(result.get("statistic"))
    qual = _text(result.get("qualifier"))
    if not raw:
        base = "no-value/unknown"
    else:
        base = raw
        if unit and unit not in base:
            base = f"{base} {unit}"
    suffix = f"({stat})" if stat else ""
    if qual in _CENSORING_QUALIFIERS:
        suffix += f" [censored:{qual}]"
    return f"{base} {suffix}".strip()


def _lod_loq(candidate: dict[str, Any]) -> str:
    method = candidate.get("analytical_method") or {}
    lod = _text(method.get("lod_raw"))
    loq = _text(method.get("loq_raw"))
    bits = []
    if lod:
        bits.append(f"LOD={lod}")
    if loq:
        bits.append(f"LOQ={loq}")
    return "; ".join(bits)


def _quote(candidate: dict[str, Any], max_len: int = 70) -> str:
    evidence = candidate.get("evidence") or {}
    quote = _text(evidence.get("quote"))
    return quote[:max_len] + ("..." if len(quote) > max_len else "")


def _page(candidate: dict[str, Any]) -> str:
    evidence = candidate.get("evidence") or {}
    return _text(evidence.get("page_start")) or _text(evidence.get("page_end"))


def build(run_root: Path) -> tuple[str, dict[str, int]]:
    db_path = run_root / "state" / "control.sqlite3"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT t.task_id, t.candidate_id, t.priority, t.reason_codes_json, t.payload_json,
               s.document_session_id, a.source_path
        FROM human_review_tasks t
        JOIN document_sessions s ON s.document_session_id = t.document_session_id
        LEFT JOIN document_assets a ON a.document_id = s.document_id
        WHERE t.status = 'pending'
        ORDER BY CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                 WHEN 'medium' THEN 2 ELSE 3 END, t.task_id
        """
    ).fetchall()
    con.close()

    groups: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
    for row in rows:
        codes = tuple(sorted(json.loads(row["reason_codes_json"] or "[]")))
        payload = json.loads(row["payload_json"] or "{}")
        candidate = payload.get("candidate") if isinstance(payload, dict) else {}
        if not isinstance(candidate, dict):
            candidate = {}
        category = _categorize(set(codes), candidate)
        key = (category, codes)
        groups.setdefault(key, []).append(
            {
                "task_id": row["task_id"],
                "short": row["task_id"].split("-")[-1][-8:],
                "doi": _doi_from_filename(row["source_path"] or ""),
                "chemical": _text((candidate.get("analyte") or {}).get("raw_name") or (candidate.get("analyte") or {}).get("reported_name")),
                "value": _value_str(candidate),
                "location": _location(candidate),
                "page": _page(candidate),
                "quote": _quote(candidate),
                "lod_loq": _lod_loq(candidate),
                "codes": codes,
            }
        )

    counts: dict[str, int] = dict.fromkeys(_CATEGORY_ORDER, 0)
    for (category, _codes), items in groups.items():
        counts[category] += len(items)

    lines = [
        "# Human review list - batch",
        "",
        f"- Pending review tasks: **{sum(counts.values())}**",
        f"- Database: {run_root}/state/control.sqlite3",
        "- Qualitative censored/no-value/frequency records are **not concentrations**; they may retain detection evidence.",
        "  LOD/LOQ columns preserve reported thresholds for downstream censored statistics.",
        "- Each case requires a human decision beyond deterministic validator rules.",
        "  Decisions are recorded with lineage; future automatic acceptance still requires policy gates.",
        "",
        "## Category summary",
        "",
        "| Category | Tasks | Why review is required | Decision required |",
        "|---|---:|---|---|",
    ]
    for category in _CATEGORY_ORDER:
        if counts.get(category, 0) == 0:
            continue
        label, why, decide = _CATEGORY_META[category]
        lines.append(f"| {label} | {counts[category]} | {why} | {decide} |")

    lines.append("")
    lines.append("## Details by category")
    lines.append("")
    for category in _CATEGORY_ORDER:
        cat_groups = [(k, v) for k, v in groups.items() if k[0] == category]
        if not cat_groups:
            continue
        label, _why, _decide = _CATEGORY_META[category]
        lines.append(f"### {label}")
        lines.append("")
        for (_cat, codes), items in sorted(cat_groups):
            lines.append(f"**{', '.join(codes)}** ({len(items)} tasks)")
            lines.append("")
            lines.append("| DOI | Chemical | Value | Site/city/country | Page | Quote (truncated) | Task | LOD/LOQ |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for item in items:
                lines.append(
                    f"| {item['doi']} | {item['chemical']} | {item['value']} | "
                    f"{item['location']} | {item['page']} | {item['quote']} | "
                    f"{item['short']} | {item['lod_loq']} |"
                )
            lines.append("")
    return "\n".join(lines), counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or (args.run_root / f"REVIEW_LIST_{date.today().isoformat()}.md")
    md, counts = build(args.run_root)
    output.write_text(md, encoding="utf-8")
    print(json.dumps({"output": str(output), "total": sum(counts.values()), "counts": counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
