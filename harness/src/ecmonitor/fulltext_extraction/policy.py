"""Central validation policy, terminal states, and output-stream routing.

The module deliberately contains only deterministic, document-local policy.  It does not call
models, PubChem, or the network, so it can be reused by the extractor harness, validator, exports,
and regression tests without creating hidden cross-document state.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Literal

from ecmonitor.fulltext_extraction.models import TerminalStatus, ValidationDecision

OutputStream = Literal[
    "accepted_observations",
    "censored_observations",
    "tentative_observations",
    "microplastic_surface_water_observations",
    "rejected_audit",
    "deferred_evidence",
    "run_failures",
    "zero_record_documents",
]

_ACCEPTED = {
    "accepted_main",
    "accepted_censored",
    "accepted_tentative",
    "accepted_microplastic_surface_water",
}
_REJECTED = {
    "rejected_scope",
    "rejected_secondary_source",
    "rejected_treatment_experiment",
    "rejected_non_observation",
    "rejected_non_individual",
    "rejected_source_conflict",
    "rejected_duplicate",
}
_DEFERRED = {
    "deferred_identity_evidence",
    "deferred_source_binding",
    "deferred_matrix_binding",
    "deferred_geographic_conflict",
}

_NUMERIC_LIMIT = re.compile(
    r"^[\s]*(?:<=|≤|<)[\s]*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?)"
)
_MICROPLASTIC_NAME = re.compile(
    r"^(?:microplastics?|nanoplastics?|plastic particles?|microplastic particles?|MPs?)$",
    re.IGNORECASE,
)
_MICROPLASTIC_BOUND = re.compile(
    r"(?:attached|adsorbed|sorbed|bound|associated|accumulated)\s+(?:to\s+)?(?:the\s+)?"
    r"(?:microplastics?|nanoplastics?|plastic particles?)|"
    r"(?:metal|chemical|contaminant)s?\s+(?:on|in)\s+(?:microplastics?|plastic particles?)",
    re.IGNORECASE,
)
_SURFACE_WATER = re.compile(
    r"(?:surface[ _-]*water|river|lake|stream|creek|reservoir|canal|pond|estuar|coastal|"
    r"marine[ _-]*water|sea[ _-]*water|lagoon|bay)",
    re.IGNORECASE,
)
_MICROPLASTIC_ABUNDANCE_UNIT = re.compile(
    r"(?:particles?|items?|pieces?|fragments?|fib(?:ers?|res?)|counts?|number|mg|ug|µg|ng)\s*"
    r"(?:(?:/|per)\s*(?:l|ml|m3|m\^3|km2|km\^2)|[·⋅]?\s*(?:l|ml|m|km)[−-]?[123])",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _candidate_text(candidate: dict[str, Any]) -> str:
    values: list[Any] = []
    for section_name in ("analyte", "sample", "location", "evidence", "result"):
        section = candidate.get(section_name)
        if isinstance(section, dict):
            values.extend(section.values())
    return " ".join(_text(value) for value in values if _text(value))


def materialize_censoring(candidate: dict[str, Any]) -> dict[str, Any]:
    """Preserve censoring without turning a limit or ND marker into an exact concentration."""
    materialized = copy.deepcopy(candidate)
    result = materialized.get("result")
    if not isinstance(result, dict):
        return materialized

    raw_value = result.get("raw_value")
    reported_raw = result.get("reported_raw_value")
    if reported_raw in (None, "") and raw_value not in (None, ""):
        result["reported_raw_value"] = raw_value
    qualifier = _text(result.get("qualifier")).casefold()
    raw_text = _text(raw_value)

    is_nd = qualifier in {"not_detected", "not_quantified"}
    limit_match = _NUMERIC_LIMIT.match(raw_text)
    is_less_than = qualifier in {"less_than", "less_than_or_equal"} or limit_match is not None

    if is_nd:
        result["is_censored"] = True
        result["numeric_value_available"] = False
        result["not_a_zero_concentration"] = True
        result["raw_value"] = None
        result["value_numeric"] = None
        result.setdefault("censoring_limit", None)
        return materialized

    if is_less_than:
        if qualifier not in {"less_than", "less_than_or_equal"}:
            result["qualifier"] = "less_than_or_equal" if raw_text.startswith("<=") or raw_text.startswith("≤") else "less_than"
        limit: float | None = None
        if limit_match is not None:
            try:
                limit = float(limit_match.group(1).replace(",", "."))
            except ValueError:
                limit = None
        if limit is None and isinstance(result.get("censoring_limit"), (int, float)):
            limit = float(result["censoring_limit"])
        result["is_censored"] = True
        result["censoring_limit"] = limit
        result["censoring_limit_unit"] = result.get("censoring_limit_unit") or result.get("raw_unit")
        result["numeric_value_available"] = False
        result["raw_value"] = None
        result["value_numeric"] = None
        result["not_a_zero_concentration"] = True
        return materialized

    result.setdefault("is_censored", False)
    return materialized


def is_left_censored_numeric(candidate: dict[str, Any]) -> bool:
    result = candidate.get("result")
    if not isinstance(result, dict):
        return False
    qualifier = _text(result.get("qualifier")).casefold()
    if qualifier not in {"less_than", "less_than_or_equal"}:
        return False
    if isinstance(result.get("censoring_limit"), (int, float)):
        return True
    return _NUMERIC_LIMIT.match(_text(result.get("reported_raw_value") or result.get("raw_value"))) is not None


def is_qualitative_censored(candidate: dict[str, Any]) -> bool:
    result = candidate.get("result")
    if not isinstance(result, dict):
        return False
    return _text(result.get("qualifier")).casefold() in {"not_detected", "not_quantified"}


def is_microplastic_surface_water_observation(candidate: dict[str, Any]) -> bool:
    """Return true only for abundance/mass in bulk surface water, never particle-bound chemicals."""
    analyte = candidate.get("analyte") or {}
    sample = candidate.get("sample") or {}
    result = candidate.get("result") or {}
    evidence = candidate.get("evidence") or {}
    if not all(isinstance(item, dict) for item in (analyte, sample, result, evidence)):
        return False
    raw_name = _text(analyte.get("reported_name") or analyte.get("raw_name"))
    specificity = _text(analyte.get("specificity_status")).casefold()
    if not (_MICROPLASTIC_NAME.fullmatch(raw_name) or specificity == "polymer_or_particle_category"):
        return False
    relation = " ".join(
        _text(evidence.get(key))
        for key in ("quote", "relation_note", "table_caption", "row_label", "column_label")
    )
    if _MICROPLASTIC_BOUND.search(relation):
        return False
    matrix_text = " ".join(
        _text(sample.get(key))
        for key in ("matrix_raw", "matrix_normalized", "sample_type", "phase_or_fraction")
    )
    if not _SURFACE_WATER.search(matrix_text + " " + relation):
        return False
    raw_unit = _text(result.get("raw_unit"))
    has_value = isinstance(result.get("value_numeric"), (int, float)) or any(
        ch.isdigit() for ch in _text(result.get("raw_value"))
    )
    return has_value and bool(_MICROPLASTIC_ABUNDANCE_UNIT.search(raw_unit))


def is_tentative_observation(candidate: dict[str, Any]) -> bool:
    analyte = candidate.get("analyte") or {}
    result = candidate.get("result") or {}
    if not isinstance(analyte, dict) or not isinstance(result, dict):
        return False
    status_text = " ".join(
        _text(analyte.get(key))
        for key in (
            "identification_level",
            "identity_level",
            "identity_status",
            "confidence_level",
            "resolution_status",
        )
    ).casefold()
    explicit = analyte.get("is_tentative") is True or result.get("semi_quantitative") is True
    level_2_or_3 = bool(re.search(r"(?:level|confidence)\s*[23]\b", status_text))
    return explicit or level_2_or_3 or any(
        marker in status_text for marker in ("tentative", "suspect", "nontarget", "non-target")
    )


def geographic_conflict_reason(candidate: dict[str, Any]) -> str | None:
    location = candidate.get("location")
    if not isinstance(location, dict):
        return None
    if location.get("admin_hierarchy_consistent") is False:
        details = location.get("admin_hierarchy_conflicts") or []
        suffix = ":" + "+".join(str(item) for item in details[:3]) if details else ""
        return "geographic_administrative_hierarchy_conflict" + suffix
    return None


def classify_terminal_status(candidate: dict[str, Any], decision: ValidationDecision) -> TerminalStatus:
    reasons = tuple(str(code) for code in decision.reason_codes)
    reason_text = " ".join(reasons).casefold()

    if decision.action == "accept":
        if is_microplastic_surface_water_observation(candidate):
            return "accepted_microplastic_surface_water"
        if is_left_censored_numeric(candidate):
            return "accepted_censored"
        if is_tentative_observation(candidate):
            return "accepted_tentative"
        return "accepted_main"

    if decision.action == "reject":
        if "duplicate" in reason_text:
            return "rejected_duplicate"
        if any(token in reason_text for token in ("secondary", "cited_study", "literature_summary")):
            return "rejected_secondary_source"
        if any(token in reason_text for token in ("treatment_experiment", "spiked", "synthetic_matrix")):
            return "rejected_treatment_experiment"
        if any(token in reason_text for token in ("not_an_individual", "mixture", "sum_or_total", "class_or_family")):
            return "rejected_non_individual"
        if any(token in reason_text for token in ("source_conflict", "hallucination", "not_in_source", "evidence_conflict")):
            return "rejected_source_conflict"
        if any(token in reason_text for token in ("scope", "not_surface_water", "wastewater", "effluent", "groundwater", "non_surface", "microplastic_bound")):
            return "rejected_scope"
        return "rejected_non_observation"

    if "geographic_administrative_hierarchy_conflict" in reason_text:
        return "deferred_geographic_conflict"
    if any(token in reason_text for token in ("chemical_identity", "chemical_resolution", "registry_alias", "specificity_lost", "ambiguous_product")):
        return "deferred_identity_evidence"
    if any(token in reason_text for token in ("matrix", "surface_water_provenance", "environmental_sample_provenance")):
        return "deferred_matrix_binding"
    return "deferred_source_binding"


def coarse_disposition_for_terminal(status: TerminalStatus) -> Literal["accepted", "rejected", "pending_human_review"]:
    if status in _ACCEPTED:
        return "accepted"
    if status in _REJECTED:
        return "rejected"
    return "pending_human_review"


def output_stream_for_terminal(status: TerminalStatus) -> OutputStream:
    if status == "accepted_main":
        return "accepted_observations"
    if status == "accepted_censored":
        return "censored_observations"
    if status == "accepted_tentative":
        return "tentative_observations"
    if status == "accepted_microplastic_surface_water":
        return "microplastic_surface_water_observations"
    if status in _REJECTED:
        return "rejected_audit"
    if status in _DEFERRED:
        return "deferred_evidence"
    if status == "completed_zero_in_scope_records":
        return "zero_record_documents"
    return "run_failures"


def policy_rule_for_terminal(status: TerminalStatus) -> str:
    return {
        "accepted_main": "OBS-01",
        "accepted_censored": "LC-02",
        "accepted_tentative": "CHEM-TENTATIVE-01",
        "accepted_microplastic_surface_water": "MP-SW-01",
        "rejected_scope": "SCOPE-01",
        "rejected_secondary_source": "SOURCE-SECONDARY-01",
        "rejected_treatment_experiment": "EXPERIMENT-01",
        "rejected_non_observation": "RESULT-NONCONC-01",
        "rejected_non_individual": "CHEM-NONIND-01",
        "rejected_source_conflict": "EVID-01",
        "rejected_duplicate": "DEDUP-SOURCE-01",
        "deferred_identity_evidence": "CHEM-DEFER-01",
        "deferred_source_binding": "SOURCE-DEFER-01",
        "deferred_matrix_binding": "MATRIX-DEFER-01",
        "deferred_geographic_conflict": "GEO-DEFER-01",
        "model_failure": "RUN-MODEL-FAILURE-01",
        "parser_failure": "RUN-PARSER-FAILURE-01",
        "validator_failure": "RUN-VALIDATOR-FAILURE-01",
        "invalid_model_output": "RUN-INVALID-OUTPUT-01",
        "completed_zero_in_scope_records": "RUN-ZERO-01",
    }[status]


def terminal_requires_human_review(status: TerminalStatus, decision: ValidationDecision) -> bool:
    """Identity/SI deferrals are non-blocking evidence queues; true ambiguity remains human work."""
    if status == "deferred_identity_evidence":
        return False
    return status in {
        "deferred_source_binding",
        "deferred_matrix_binding",
        "deferred_geographic_conflict",
    } or decision.human_review_required
