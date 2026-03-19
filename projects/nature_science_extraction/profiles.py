from __future__ import annotations

from copy import deepcopy
from csv import DictWriter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ProfileSpec:
    key: str
    source_block: str
    pdf_root: Path
    output_dir: Path
    merged_filename: str
    merged_csv_filename: str | None
    result_list_field: str
    empty_payload: dict[str, Any]
    extraction_prompt: str
    csv_columns: list[str] | None = None
    evidence_excerpt_field: str = "evidence_snippet"
    evidence_page_field: str = "evidence_page"

    def build_empty_payload(self) -> dict[str, Any]:
        return deepcopy(self.empty_payload)


def _nature_payload() -> dict[str, Any]:
    return {
        "source_pdf": "",
        "paper_title": "",
        "doi": "",
        "journal": "",
        "year": None,
        "record_id_within_paper": "",
        "pollutant_original": "",
        "pollutant_standardized": None,
        "chemical_group": None,
        "matrix": "",
        "matrix_original": "",
        "location": "",
        "location_original": "",
        "country": None,
        "site_name": None,
        "sampling_date": None,
        "sampling_date_original": None,
        "value_original": "",
        "value_numeric": None,
        "value_min": None,
        "value_max": None,
        "unit_original": "",
        "standardized_value": None,
        "standardized_unit": None,
        "statistic_type": "not reported",
        "sample_size": None,
        "detection_flag": "unknown",
        "table_or_text": "main_text",
        "table_figure_id": None,
        "evidence_snippet": "",
        "evidence_page": "",
        "notes": "",
        "extraction_confidence": "low",
    }


def _nature_paper() -> dict[str, Any]:
    return {
        "source_pdf": "",
        "paper_title": "",
        "doi": "",
        "journal": "",
        "year": None,
        "records": [],
        "no_extractable_records": False,
        "notes": "",
    }


def _lancet_stat() -> dict[str, Any]:
    return {
        "pollutant_original": "",
        "matrix": "",
        "location": None,
        "statistic_type": "not reported",
        "value_original": "",
        "value_numeric": None,
        "value_min": None,
        "value_max": None,
        "unit_original": "",
        "table_or_text": "main_text",
        "table_figure_id": None,
        "evidence_snippet": "",
        "evidence_page": "",
        "notes": "",
    }


def _lancet_paper() -> dict[str, Any]:
    return {
        "source_pdf": "",
        "paper_title": "",
        "doi": "",
        "journal": "",
        "year": None,
        "study_region": "",
        "country_or_countries": [],
        "specific_location": None,
        "water_compartment": "",
        "matrix_original": "",
        "target_pollutants": [],
        "pollutant_group": None,
        "sampling_period": None,
        "sampling_period_original": None,
        "sample_size": None,
        "number_of_sites": None,
        "study_design_summary": "",
        "reported_statistics": [],
        "provides_extractable_primary_data": False,
        "data_usability_for_database_matching": "low",
        "reason_if_low_usability": None,
        "notes": "",
    }


def _science_result() -> dict[str, Any]:
    return {
        "pollutant_original": "",
        "pollutant_standardized": None,
        "matrix": "",
        "matrix_original": "",
        "location": None,
        "country": None,
        "site_name": None,
        "value_original": "",
        "value_numeric": None,
        "value_min": None,
        "value_max": None,
        "unit_original": "",
        "statistic_type": "not reported",
        "sample_size": None,
        "detection_flag": "unknown",
        "geo_locatable": "no",
        "table_or_text": "main_text",
        "table_figure_id": None,
        "evidence_snippet": "",
        "evidence_page": "",
        "notes": "",
        "extraction_confidence": "low",
    }


def _science_paper() -> dict[str, Any]:
    return {
        "source_pdf": "",
        "paper_title": "",
        "doi": "",
        "journal": "",
        "year": None,
        "study_country": None,
        "study_region": None,
        "watershed_or_city": None,
        "site_count": None,
        "sampling_period": None,
        "sampling_period_original": None,
        "matrix_types": [],
        "target_pollutants": [],
        "study_scope_summary": "",
        "has_extractable_monitoring_results": False,
        "results": [],
        "notes": "",
    }


def _water_research_row() -> dict[str, Any]:
    return {
        "source_database": "Water Research 2023",
        "benchmark_paper": "Global assessment of chemical quality of drinking water: The case of trihalomethanes",
        "full_title": "",
        "doi": "",
        "first_author": "",
        "year": None,
        "country": "",
        "region": "",
        "location_name": "",
        "water_type": "",
        "sampling_time": "",
        "chemical_name": "",
        "mean_value": None,
        "median_value": None,
        "min_value": None,
        "max_value": None,
        "sd_value": None,
        "range_text": "",
        "unit": "",
        "n_samples": None,
        "instrumentation": "",
        "sample_notes": "",
        "original_excerpt": "",
        "page_or_table": "",
        "extraction_confidence": "low",
    }


def _water_research_paper() -> dict[str, Any]:
    return {
        "source_pdf": "",
        "paper_title": "",
        "doi": "",
        "journal": "",
        "year": None,
        "rows": [],
        "has_extractable_thm_data": False,
        "reason_for_exclusion": None,
        "notes": "",
    }


def _natcomm_row() -> dict[str, Any]:
    return {
        "source_database": "Nature Communications 2017",
        "benchmark_paper": "River plastic emissions to the world’s oceans",
        "full_title": "",
        "doi": "",
        "first_author": "",
        "year": None,
        "river_name": "",
        "country": "",
        "location_name": "",
        "sampling_time": "",
        "metric_type": "other",
        "plastic_type": "",
        "polymer_type": "",
        "mean_value": None,
        "median_value": None,
        "min_value": None,
        "max_value": None,
        "single_value": None,
        "unit": "",
        "n_samples": None,
        "size_range": "",
        "sampling_depth": "",
        "sampling_method": "",
        "mesh_size": "",
        "notes": "",
        "original_excerpt": "",
        "page_or_table": "",
        "extraction_confidence": "low",
    }


def _natcomm_paper() -> dict[str, Any]:
    return {
        "source_pdf": "",
        "paper_title": "",
        "doi": "",
        "journal": "",
        "year": None,
        "rows": [],
        "has_extractable_river_plastic_data": False,
        "reason_for_exclusion": None,
        "notes": "",
    }


NATURE_PROMPT = """Task:
Extract environmental monitoring concentration records from the paper and reconstruct a monitoring-record-style database.

Primary objective:
Recover as many observation-level or table-level environmental concentration records as possible.

Only extract:
- measured concentrations in environmental samples
- records from tables, supplementary tables, main text, or figures only when values are explicitly readable

Do NOT extract:
- laboratory exposure concentrations
- degradation or removal experiment concentrations
- model predictions
- toxicity thresholds
- LOD or LOQ as concentration records
- risk quotients or hazard indices
- purely methodological parameters

Granularity:
- split records into one pollutant + one matrix + one location + one sampling time + one value or range per row whenever reliable
- if multiple pollutants or locations appear in one source row, split them when feasible

Rules:
- every extracted record must include evidence_snippet and evidence_page
- preserve original units
- if a field cannot be determined, use null
- do not hallucinate
- if no extractable environmental concentration record exists, keep records empty and set no_extractable_records to true
"""


LANCET_PROMPT = """Task:
Extract study-level and outcome-level information from the paper to support reconstruction of the reference-literature database behind the Lancet study.

Primary objective:
Capture study metadata and the main environmental concentration results that could support the aggregated database.

Focus on:
- study identity
- geographic scope
- environmental compartment
- target antibiotics or pollutants
- major reported concentration statistics
- sampling period
- sample size
- whether the paper provides usable original occurrence data

Rules:
- prioritize study-level information first
- do not try to reconstruct every single observation unless the paper clearly provides structured tabular records
- every reported statistic must include evidence_snippet and evidence_page
- do not hallucinate missing metadata
- mark provides_extractable_primary_data false when the paper only gives qualitative statements and no usable measured concentration results
"""


SCIENCE_PROMPT = """Task:
Extract monitoring-oriented study information and key measured occurrence results from the paper to support reconstruction of the reference database behind the Science study.

Primary objective:
Recover structured monitoring information including pollutant identity, environmental matrix, geographic context, sampling context, and the main measured concentration results.

Extraction level:
- study-level metadata must always be extracted
- result-level concentration data should be extracted when clearly reported
- full observation-level splitting is optional only when table structure makes it reliable

Rules:
- prioritize concentration results tied to a matrix and a location
- preserve summary statistics faithfully when only summaries are available
- every result-level item must include evidence_snippet and evidence_page
- do not extract laboratory experiments, treatment or removal tests, toxicity thresholds, model outputs, or QA or QC method values as monitoring records
- use null for unknown fields
- do not hallucinate
"""


WATER_RESEARCH_PROMPT = """Task:
Extract source-level THM concentration data from the paper, supplementary materials, or resolved web text for comparison against the benchmark paper 'Global assessment of chemical quality of drinking water: The case of trihalomethanes'.

Primary objective:
Recover drinking-water THM monitoring data that supports country or region level THM concentration summaries.

Rules:
- extract only THM monitoring data and directly related sampling metadata
- prioritize supplementary tables over main-text summaries when both are present
- split rows by location x THM indicator x sampling time whenever reliable
- do not treat guideline values or regulatory limits as monitoring concentrations
- do not invent statistics that are not reported
- if the same data appears in both main text and supplement, prefer supplement values; if they conflict, keep separate rows and explain the conflict in sample_notes
- every extracted row must include original_excerpt and page_or_table
- if no extractable THM concentration data exists, keep rows empty, set has_extractable_thm_data to false, and provide reason_for_exclusion
"""


NATCOMM_PROMPT = """Task:
Extract river plastic observation data from the paper, supplementary materials, or resolved web text for comparison against the benchmark paper 'River plastic emissions to the world’s oceans'.

Primary objective:
Recover observed river plastic concentration, abundance, or flux measurements used as field data, not model outputs.

Rules:
- extract only measured river plastic observation data
- do not extract model predictions or marine-only measurements
- split rows by river x location x time x metric whenever reliable
- keep units exactly as reported
- prefer supplementary tables over narrative summaries when both are present
- if the same value appears in both main text and supplement, prefer supplement; if values conflict, keep both rows and explain the conflict in notes
- every extracted row must include original_excerpt and page_or_table
- if no extractable river plastic observation data exists, keep rows empty, set has_extractable_river_plastic_data to false, and provide reason_for_exclusion
"""


PROFILE_SPECS: dict[str, ProfileSpec] = {
    "Nature2024": ProfileSpec(
        key="nature",
        source_block="Nature2024",
        pdf_root=Path("/mnt/d/paper_data/pdf/Nature"),
        output_dir=Path("/mnt/d/paper_data/pdf/Nature/extracted_json"),
        merged_filename="all_records.json",
        merged_csv_filename=None,
        result_list_field="records",
        empty_payload=_nature_paper(),
        extraction_prompt=NATURE_PROMPT,
    ),
    "Lancet2023": ProfileSpec(
        key="lancet",
        source_block="Lancet2023",
        pdf_root=Path("/mnt/d/paper_data/pdf/Lancet"),
        output_dir=Path("/mnt/d/paper_data/pdf/Lancet/extracted_json"),
        merged_filename="merged_summary.json",
        merged_csv_filename=None,
        result_list_field="reported_statistics",
        empty_payload=_lancet_paper(),
        extraction_prompt=LANCET_PROMPT,
    ),
    "Science2020": ProfileSpec(
        key="science",
        source_block="Science2020",
        pdf_root=Path("/mnt/d/paper_data/pdf/Science"),
        output_dir=Path("/mnt/d/paper_data/pdf/Science/extracted_json"),
        merged_filename="all_records.json",
        merged_csv_filename=None,
        result_list_field="results",
        empty_payload=_science_paper(),
        extraction_prompt=SCIENCE_PROMPT,
    ),
    "Water2023": ProfileSpec(
        key="water",
        source_block="Water2023",
        pdf_root=Path("/mnt/d/paper_data/pdf/Water"),
        output_dir=Path("/mnt/d/paper_data/pdf/Water/extracted_json"),
        merged_filename="merged_records.json",
        merged_csv_filename="merged_records.csv",
        result_list_field="rows",
        empty_payload=_water_research_paper(),
        extraction_prompt=WATER_RESEARCH_PROMPT,
        csv_columns=[
            "source_database",
            "benchmark_paper",
            "full_title",
            "doi",
            "first_author",
            "year",
            "country",
            "region",
            "location_name",
            "water_type",
            "sampling_time",
            "chemical_name",
            "mean_value",
            "median_value",
            "min_value",
            "max_value",
            "sd_value",
            "range_text",
            "unit",
            "n_samples",
            "instrumentation",
            "sample_notes",
            "original_excerpt",
            "page_or_table",
            "extraction_confidence",
            "reason_for_exclusion",
        ],
        evidence_excerpt_field="original_excerpt",
        evidence_page_field="page_or_table",
    ),
    "NatComm2017": ProfileSpec(
        key="natcomm",
        source_block="NatComm2017",
        pdf_root=Path("/mnt/d/paper_data/pdf/NatComm"),
        output_dir=Path("/mnt/d/paper_data/pdf/NatComm/extracted_json"),
        merged_filename="merged_records.json",
        merged_csv_filename="merged_records.csv",
        result_list_field="rows",
        empty_payload=_natcomm_paper(),
        extraction_prompt=NATCOMM_PROMPT,
        csv_columns=[
            "source_database",
            "benchmark_paper",
            "full_title",
            "doi",
            "first_author",
            "year",
            "river_name",
            "country",
            "location_name",
            "sampling_time",
            "metric_type",
            "plastic_type",
            "polymer_type",
            "mean_value",
            "median_value",
            "min_value",
            "max_value",
            "single_value",
            "unit",
            "n_samples",
            "size_range",
            "sampling_depth",
            "sampling_method",
            "mesh_size",
            "notes",
            "original_excerpt",
            "page_or_table",
            "extraction_confidence",
            "reason_for_exclusion",
        ],
        evidence_excerpt_field="original_excerpt",
        evidence_page_field="page_or_table",
    ),
}


RESULT_ITEM_TEMPLATES = {
    "Nature2024": _nature_payload(),
    "Lancet2023": _lancet_stat(),
    "Science2020": _science_result(),
    "Water2023": _water_research_row(),
    "NatComm2017": _natcomm_row(),
}


def get_profile(source_block: str) -> ProfileSpec:
    try:
        return PROFILE_SPECS[source_block]
    except KeyError as exc:
        raise ValueError(f"Unsupported source_block: {source_block}") from exc


def coerce_payload(source_block: str, payload: dict[str, Any]) -> dict[str, Any]:
    profile = get_profile(source_block)
    base = profile.build_empty_payload()
    for key in list(base):
        if key in payload:
            base[key] = payload[key]

    template = RESULT_ITEM_TEMPLATES[source_block]
    items = []
    for raw_item in payload.get(profile.result_list_field, []):
        item = deepcopy(template)
        for key in list(item):
            if key in raw_item:
                item[key] = raw_item[key]
        items.append(item)
    base[profile.result_list_field] = items
    return base


def validate_payload(source_block: str, payload: dict[str, Any]) -> list[str]:
    profile = get_profile(source_block)
    errors: list[str] = []

    for key in profile.empty_payload:
        if key not in payload:
            errors.append(f"missing top-level field: {key}")

    if profile.result_list_field not in payload:
        return errors

    if not isinstance(payload[profile.result_list_field], list):
        errors.append(f"{profile.result_list_field} must be a list")
        return errors

    template = RESULT_ITEM_TEMPLATES[source_block]
    for index, item in enumerate(payload[profile.result_list_field]):
        if not isinstance(item, dict):
            errors.append(f"{profile.result_list_field}[{index}] must be an object")
            continue
        for key in template:
            if key not in item:
                errors.append(f"{profile.result_list_field}[{index}] missing field: {key}")

        evidence_snippet = str(item.get(profile.evidence_excerpt_field, "") or "").strip()
        evidence_page = str(item.get(profile.evidence_page_field, "") or "").strip()
        if evidence_snippet == "":
            errors.append(f"{profile.result_list_field}[{index}] missing {profile.evidence_excerpt_field}")
        if evidence_page == "":
            errors.append(f"{profile.result_list_field}[{index}] missing {profile.evidence_page_field}")

    if source_block == "Nature2024":
        if not payload["records"] and not isinstance(payload.get("no_extractable_records"), bool):
            errors.append("Nature payload requires boolean no_extractable_records")
    if source_block == "Lancet2023":
        if not isinstance(payload.get("provides_extractable_primary_data"), bool):
            errors.append("Lancet payload requires boolean provides_extractable_primary_data")
    if source_block == "Science2020":
        if not isinstance(payload.get("has_extractable_monitoring_results"), bool):
            errors.append("Science payload requires boolean has_extractable_monitoring_results")
    if source_block == "Water2023":
        if not isinstance(payload.get("has_extractable_thm_data"), bool):
            errors.append("Water payload requires boolean has_extractable_thm_data")
    if source_block == "NatComm2017":
        if not isinstance(payload.get("has_extractable_river_plastic_data"), bool):
            errors.append("NatComm payload requires boolean has_extractable_river_plastic_data")

    return errors


def build_model_prompt(
    source_block: str,
    paper_metadata: dict[str, Any],
    source_description: dict[str, Any],
    source_text: str,
) -> str:
    profile = get_profile(source_block)
    payload_template = profile.build_empty_payload()
    result_template = RESULT_ITEM_TEMPLATES[source_block]

    return "\n\n".join(
        [
            profile.extraction_prompt.strip(),
            "Return JSON only. Do not use markdown fences.",
            f"Top-level JSON template:\n{payload_template}",
            f"Result item template for `{profile.result_list_field}`:\n{result_template}",
            f"Paper metadata:\n{paper_metadata}",
            f"Resolved source metadata:\n{source_description}",
            "Source text follows. Use only what is supported by the source text and metadata.",
            source_text,
        ]
    )


def write_profile_csv(profile: ProfileSpec, rows: list[dict[str, Any]], path: Path) -> None:
    if not profile.csv_columns:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = DictWriter(handle, fieldnames=profile.csv_columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in profile.csv_columns})
