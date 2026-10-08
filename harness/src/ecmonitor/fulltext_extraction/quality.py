"""Deterministic chemical-name materialization and conservative validation gates."""

from __future__ import annotations

import copy
import re
from typing import Any, cast

from ecmonitor.fulltext_extraction.adapters.base import EvidenceValidator
from ecmonitor.fulltext_extraction.models import (
    ChemicalResolution,
    EvidenceChunk,
    ValidationDecision,
)
from ecmonitor.fulltext_extraction.policy import (
    geographic_conflict_reason,
    is_left_censored_numeric,
    is_microplastic_surface_water_observation,
    is_qualitative_censored,
)

_EXPLICIT_NON_INDIVIDUAL = {
    "pfas",
    "pfaas",
    "pahs",
    "pcbs",
    "pesticides",
    "herbicides",
    "insecticides",
    "pharmaceuticals",
    "antibiotics",
    "hormones",
    "microplastics",
    "nanoplastics",
    "flame retardants",
    "personal care products",
    "emerging contaminants",
    "contaminants of emerging concern",
}
_NON_INDIVIDUAL_SPECIFICITY = {
    "class_or_family",
    "sum_or_total_parameter",
    "mixture_or_product",
    "polymer_or_particle_category",
    "unspecified_metabolite",
}
_SUM_PREFIX = re.compile(r"^(?:sum\s+of|total\s+|Σ|∑)", re.IGNORECASE)

# --- Evidence-quality gates (industry-standard alignment) ---
# Implement the validation standards synthesized from NORMAN / EFSA / Helsel / EPA DSSTox / USGS
# monitoring practice: censored and semi-quantitative values are not measured concentrations,
# exceedance multiples are not concentrations, literature/secondary values are rejected as
# records of the current paper, and a numeric result must stay row-bound to matrix/time/location.
# Deterministic exclusions skip model calls; genuinely ambiguous value/binding cases still route
# to human review.
_SECONDARY_DATA_MARKERS = ("literature", "secondary", "review", "cited")
_CENSORING_QUALIFIERS = {"not_detected", "not_quantified", "less_than", "less_than_or_equal"}
_FREQUENCY_STATISTICS = {"frequency"}
_EXCEEDANCE_UNIT = re.compile("^(?:fold|times|multiple|\u500d\u6570)$", re.IGNORECASE)
_EXCEEDANCE_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:fold|times))|"
    r"(?:exceed(?:ed|s|ing)?\s+(?:the\s+)?(?:guideline|limit|standard|level)\s+by\s+[\d.]+)",
    re.IGNORECASE,
)
_SECONDARY_CITATION_PATTERN = re.compile(
    r"(?:\b[A-Z][A-Za-z'’-]+\s+et\s+al\.?[, ]+\(?(?:19|20)\d{2}\)?)|"
    r"(?:\b(?:according to|reported by|previous(?:ly)? reported|cited (?:study|literature))\b)",
    re.IGNORECASE,
)
_TREATMENT_EXPERIMENT_PATTERN = re.compile(
    r"\b(?:remove[ds]?|removal|adsor(?:b|bed|ption)|degrad(?:e|ed|ation)|"
    r"treatment experiment|batch experiment|initial concentration|"
    r"at \d+(?:\.\d+)?\s*h(?:ours?)?)\b",
    re.IGNORECASE,
)
_SYNTHETIC_OR_SPIKED_PATTERN = re.compile(
    r"\b(?:spik(?:e|ed|ing)|fortif(?:y|ied|ication)|synthetic water|prepared water|"
    r"laboratory water|artificial water|stock solution)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_PRODUCT_OR_PROCESS_NAMES = {
    "genx",
    "genx chemicals",
    "genx technology",
}
_MOJIBAKE_GREEK = {"Î±": "α", "Î²": "β", "Î³": "γ", "Î´": "δ"}
_GREEK_WORDS = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta"}
_SPECIFICITY_TOKEN_PATTERN = re.compile(
    r"(?<![a-z])(?:alpha|beta|gamma|delta|cis|trans)(?![a-z])", re.IGNORECASE
)
_TABLE_STATISTIC_CAPTION_PATTERNS = (
    (re.compile(r"\b(?:average|averaged|mean)\b", re.IGNORECASE), {"mean"}, "mean"),
    (re.compile(r"\bmedian\b", re.IGNORECASE), {"median"}, "median"),
    (re.compile(r"\b(?:maximum|maxima|max\.)\b", re.IGNORECASE), {"maximum", "max"}, "maximum"),
    (re.compile(r"\b(?:minimum|minima|min\.)\b", re.IGNORECASE), {"minimum", "min"}, "minimum"),
)

_VALIDATION_EQUIVALENT_STATISTICS = {"minimum", "min", "maximum", "max", "mean"}
_VALIDATION_RESULT_VARIANTS = {
    "raw_value",
    "value_numeric",
    "statistic",
    "uncertainty_raw",
    "range_min",
    "range_max",
}
_CANDIDATE_SPECIFIC_RESULT_POINTERS = ("/result", "/evidence")

# The production observation table is intentionally narrower than the wider literature corpus:
# it stores concentrations measured in actual environmental surface-water samples only. Other
# matrices may remain useful as reference literature, but they must not reach the main table.
_SURFACE_WATER_MATRIX_PATTERN = re.compile(
    r"\b(?:surface[ _-]*water|river[ _-]*water|lake[ _-]*water|stream[ _-]*water|"
    r"creek[ _-]*water|reservoir[ _-]*water|canal[ _-]*water|pond[ _-]*water|"
    r"estuar(?:y|ine)[ _-]*water|coastal[ _-]*water|marine[ _-]*water|sea[ _-]*water|"
    r"lagoon[ _-]*water|bay[ _-]*water)\b",
    re.IGNORECASE,
)
_SURFACE_WATERBODY_PATTERN = re.compile(
    r"\b(?:river|lake|stream|creek|reservoir|canal|pond|estuary|lagoon|bay|coast|sea)\b",
    re.IGNORECASE,
)
_GENERIC_WATER_MATRIX_PATTERN = re.compile(
    r"^(?:environmental[ _-]*)?water(?:[ _-]*sample)?s?$", re.IGNORECASE
)
_MICROPLASTIC_BOUND_MATRIX_PATTERN = re.compile(
    r"\b(?:microplastics?|nanoplastics?|plastic particles?|polymer particles?|MPs[- _]?bound|"
    r"microplastic[- ]bound|attached to microplastics?|sorbed to microplastics?|"
    r"metals? (?:on|in|associated with) microplastics?)\b",
    re.IGNORECASE,
)
_MICROPLASTIC_BOUND_EVIDENCE_PATTERN = re.compile(
    r"(?i)(?:attached|adsorbed|sorbed|bound|associated|accumulated|on|in)\s+(?:to\s+)?"
    r"(?:the\s+)?(?:microplastics?|nanoplastics?|plastic particles?)|"
    r"(?:microplastics?|nanoplastics?|plastic particles?)[^\n]{0,80}(?:µg|ug|mg)\s*/\s*(?:g|kg)|"
    r"(?:metal|element|contaminant)[^\n]{0,80}(?:on|in|bound to|associated with)\s+"
    r"(?:microplastics?|nanoplastics?)",
)
_WASTEWATER_MATRIX_PATTERN = re.compile(
    r"\b(?:waste[ _-]*water|sewage|influent|effluent|secondary effluent|treated wastewater|"
    r"WWTP|STP|reclaimed water|reuse water|sludge)\b",
    re.IGNORECASE,
)
_GROUNDWATER_MATRIX_PATTERN = re.compile(
    r"\b(?:ground[ _-]*water|aquifer|well water)\b", re.IGNORECASE
)
_OTHER_NON_SURFACE_MATRIX_PATTERN = re.compile(
    r"\b(?:air|PM\s*2\.5|particulate matter|road dust|dust|sediment|soil|sludge|"
    r"tissue|biota|mussel|fish|urine|blood|serum|consumer product|personal care product|"
    r"deionized water|distilled water|ultrapure water|laboratory water|synthetic water|"
    r"prepared water|drinking water|tap water|ice|rainwater|stormwater)\b",
    re.IGNORECASE,
)


def materialize_chemical_identity(
    candidate: dict[str, Any], resolutions: tuple[ChemicalResolution, ...]
) -> dict[str, Any]:
    """Return a copy enriched with canonical and reported-name fields.

    The reported name is never overwritten. A single validated/resolved identity may populate
    canonical fields, but ambiguous external results remain proposals only.
    """

    materialized = copy.deepcopy(candidate)
    analyte = materialized.get("analyte")
    if not isinstance(analyte, dict):
        return materialized
    raw_name = analyte.get("raw_name")
    reported_name = raw_name.strip() if isinstance(raw_name, str) else None
    analyte.setdefault("reported_name", reported_name)

    usable = [
        item
        for item in resolutions
        if item.status in {"validated_local", "resolved"} and len(item.matches) == 1
    ]
    if len(usable) != 1:
        analyte.setdefault("replacement_name", None)
        return materialized

    resolution = usable[0]
    match = resolution.matches[0]
    analyte["canonical_name"] = match.canonical_name
    analyte["matched_alias"] = match.matched_alias or reported_name
    analyte["replacement_name"] = (
        reported_name
        if reported_name and reported_name.casefold() != match.canonical_name.casefold()
        else None
    )
    if match.pubchem_cid and str(match.pubchem_cid).isdigit():
        analyte.setdefault("pubchem_cid", int(match.pubchem_cid))
    analyte.setdefault("inchikey", match.inchikey)
    analyte.setdefault("canonical_smiles", match.canonical_smiles)
    if len(match.cas_candidates) == 1:
        analyte.setdefault("cas_rn", match.cas_candidates[0])
        analyte.setdefault("cas_source", match.source)
    analyte.setdefault(
        "resolution_status",
        "resolved" if resolution.status == "validated_local" else "provisional",
    )
    return materialized


def chemical_name_fields(candidate: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    analyte = candidate.get("analyte")
    if not isinstance(analyte, dict):
        return None, None, None
    canonical = _string_or_none(analyte.get("canonical_name"))
    reported = _string_or_none(analyte.get("reported_name") or analyte.get("raw_name"))
    replacement = _string_or_none(analyte.get("replacement_name"))
    return canonical, reported, replacement


def looks_non_individual(candidate: dict[str, Any]) -> bool:
    analyte = candidate.get("analyte")
    if not isinstance(analyte, dict):
        return False
    if analyte.get("is_individual_chemical") is False:
        return True
    specificity = _string_or_none(analyte.get("specificity_status"))
    if specificity in _NON_INDIVIDUAL_SPECIFICITY:
        return True
    raw_name = _string_or_none(analyte.get("raw_name"))
    if raw_name is None:
        return False
    normalized = " ".join(raw_name.casefold().split())
    return normalized in _EXPLICIT_NON_INDIVIDUAL or bool(_SUM_PREFIX.match(raw_name.strip()))


# --- Project scope gate: heavy metals and organics are IN scope; only classic ---
# --- water-quality parameters are classic pollutants, not new/emerging contaminants. ---
# --- Domain decision 2026-08: heavy metals AND their transformation products count as ---
# --- new/emerging contaminants, so only unambiguous classic water-quality parameters ---
# --- (major nutrients/ions, dissolved-oxygen/COD/BOD/TOC aggregates, conductivity, and ---
# --- major background cations Na/K/Ca/Mg) are rejected. Organometallic organics ---
# --- (methylmercury, tributyltin) were already in scope as organic substances. ---
_CLASSIC_WATER_CATIONS = {
    "sodium", "potassium", "calcium", "magnesium",
}
_CLASSIC_WATER_CATION_SYMBOLS = {"Na", "K", "Ca", "Mg"}
_CLASSIC_WATER_CATION_SYMBOLS_LOWER = {s.casefold() for s in _CLASSIC_WATER_CATION_SYMBOLS}
_CLASSIC_WATER_PARAMETERS = {
    "nitrate", "nitrite", "ammonia", "ammonium", "phosphate", "orthophosphate",
    "sulfate", "sulphate", "sulfide", "sulphide", "chloride", "fluoride", "bromide",
    "iodide", "silicate", "cyanide", "carbonate", "bicarbonate", "hydroxide",
    "dissolved oxygen", "salinity", "conductivity", "turbidity",
    "total dissolved solids", "total suspended solids",
    "chemical oxygen demand", "biological oxygen demand", "biochemical oxygen demand",
    "total organic carbon", "organic carbon", "oxygen", "dissolved solids", "suspended solids",
    "nitrogen", "phosphorus", "hardness", "alkalinity", "total nitrogen", "total phosphorus",
    "no3", "no2", "so4", "po4", "nh4", "co3", "hco3", "cn", "oh", "cl", "f", "br", "i",
    "o2", "h2s", "nh3", "h2o", "sio2", "no3-n", "no2-n", "nh4-n", "po4-p",
    "ammoniacal nitrogen",
}
_SCOPE_LEADING_PREFIXES = (
    "total ",
    "dissolved ",
    "particulate ",
    "soluble ",
    "labile ",
    "exchangeable ",
    "bioavailable ",
    "ionic ",
)
_SCOPE_CHARGE_SUFFIX_RE = re.compile(
    r"\s*(?:\([IVX]+\)|(?:\d+\s*[+-]\s*)|(?:[+-]\s*))$"
)


_COMPACT_INORGANIC = {
    "nh4+", "nh3", "no3-", "no2-", "no3-n", "no2-n", "nh4-n",
    "so4", "so4-", "so42-", "so4--", "so32-", "so3-",
    "po4", "po4-", "po43-", "po4--", "po4---",
    "co3", "co32-", "co3--", "hco3-",
    "oh-", "cn-", "cl-", "f-", "br-", "i-",
    "h2s", "o2", "co2", "h2o", "sio2",
}


def _scope_core_name(raw_name: str) -> str:
    name = " ".join(raw_name.strip().split())
    lowered = name.casefold()
    for prefix in _SCOPE_LEADING_PREFIXES:
        if lowered.startswith(prefix):
            name = name[len(prefix):].strip()
            break
    match = _SCOPE_CHARGE_SUFFIX_RE.search(name)
    if match and match.end() == len(name):
        base = name[: match.start()].strip()
        suffix = match.group(0).strip()
        # Strip a charge/valence suffix only when the remainder is a classic background
        # cation (Na+, Ca2+) or the suffix is a parenthesised valence (paraquat(2+)).
        # Heavy-metal species (Zn2+, Fe(II), dissolved Cu) are IN scope, so their charge
        # suffix is left on and they simply do not match the classic-parameter sets.
        # Polyatomic ions (NH4+, NO3-, PO4 3-) are left intact for compact-form matching.
        if (
            base.casefold() in _CLASSIC_WATER_CATIONS
            or base.casefold() in _CLASSIC_WATER_CATION_SYMBOLS_LOWER
            or suffix.startswith("(")
        ):
            name = base
    return name


def looks_out_of_scope_water_quality(candidate: dict[str, Any]) -> bool:
    """True when the analyte is a classic water-quality parameter (out of scope).

    Heavy metals and organic emerging contaminants are IN scope (2026-08 domain decision).
    Only unambiguous classic water-quality parameters are excluded: major nutrients/ions
    (nitrate, sulfate, chloride, phosphate, ammonium, ...), dissolved-oxygen / COD / BOD /
    TOC-type aggregates, conductivity/turbidity, and major background cations (Na/K/Ca/Mg).
    """
    analyte = candidate.get("analyte")
    if not isinstance(analyte, dict):
        return False
    for name in (
        analyte.get("raw_name"),
        analyte.get("reported_name"),
        analyte.get("canonical_name"),
        analyte.get("replacement_name"),
    ):
        raw = _string_or_none(name)
        if raw is None:
            continue
        core = _scope_core_name(raw)
        compact = re.sub(r"\s+", "", raw).casefold()
        if (
            core.casefold() in _CLASSIC_WATER_CATIONS
            or core.casefold() in _CLASSIC_WATER_PARAMETERS
        ):
            return True
        if core.casefold() in _CLASSIC_WATER_CATION_SYMBOLS_LOWER:
            return True
        if compact in _COMPACT_INORGANIC:
            return True
    return False

# Review ``retry`` decisions that re-extraction of the same source text cannot plausibly repair.
# Re-prompting a whole chunk for one of these burns a full model call on a problem the model
# cannot influence (for example the name is present but nothing in the registry/PubChem resolves
# it). The harness escalates those to human review immediately instead of looping until the retry
# budget is exhausted. Markers are substring matches against reason-code strings so the model's
# own vocabulary stays compatible.
_NON_REPAIRABLE_RETRY_MARKERS = (
    "chemical_resolution_missing",
    "chemical_identity_unresolved",
    "chemical_identity_conflict",
    "chemical_identity_ambiguous",
    "registry",
    "resolution",
    "alias_conflict",
)


def retry_is_repairable_by_extraction(decision: Any) -> bool:
    """True when a validator ``retry`` decision can benefit from re-extracting the same text.

    Repairable retries (missing/malformed analyte name, missing evidence quote, wrong value
    transcription, ...) justify a bounded targeted re-prompt. Retries caused by registry/identity
    resolution gaps are not: the model cannot change what the source text says or what resolves.
    """
    for reason in decision.reason_codes:
        if any(marker in str(reason).casefold() for marker in _NON_REPAIRABLE_RETRY_MARKERS):
            return False
    return True


def literature_summary_reason(candidate: dict[str, Any]) -> str | None:
    """Return a reason when the candidate is a secondary/literature value, else ``None``."""
    if candidate.get("observation_type") == "literature_summary":
        return "literature_summary_value"
    for flag in candidate.get("quality_flags") or []:
        folded = str(flag).casefold()
        if any(marker in folded for marker in _SECONDARY_DATA_MARKERS):
            return "secondary_cited_value:" + str(flag)[:60]
    return None


def _candidate_evidence_text(candidate: dict[str, Any]) -> str:
    evidence = candidate.get("evidence")
    parts: list[str] = []
    if isinstance(evidence, dict):
        for key in ("quote", "relation_note", "row_label", "column_label", "table_caption"):
            value = _string_or_none(evidence.get(key))
            if value:
                parts.append(value)
    parts.extend(str(flag) for flag in candidate.get("quality_flags") or [])
    return " ".join(parts)


def secondary_primary_source_reasons(candidate: dict[str, Any]) -> tuple[str, ...]:
    """Return deterministic rejection reasons for values copied from another study."""
    literature = literature_summary_reason(candidate)
    text = _candidate_evidence_text(candidate)
    explicit_secondary = bool(_SECONDARY_CITATION_PATTERN.search(text)) and any(
        marker in text.casefold()
        for marker in ("not this study", "cited", "literature", "according to", "reported by")
    )
    if not literature and not explicit_secondary:
        return ()
    reasons = ["secondary_cited_study_not_primary_observation"]
    if literature:
        reasons.append(literature)
    reasons.append("missing_primary_observation_provenance")
    return tuple(dict.fromkeys(reasons))


def treatment_experiment_reasons(candidate: dict[str, Any]) -> tuple[str, ...]:
    """Reject treatment-dose records unless they are traceable field occurrence measurements."""
    text = _candidate_evidence_text(candidate)
    if not _TREATMENT_EXPERIMENT_PATTERN.search(text):
        return ()
    observation_type = candidate.get("observation_type")
    location = candidate.get("location") or {}
    sampling_time = candidate.get("sampling_time") or {}
    method = candidate.get("analytical_method") or {}
    has_location = any(
        _string_or_none(location.get(key))
        for key in ("site_name", "waterbody", "city", "admin1", "country", "location_raw")
    )
    has_time = any(
        (
            sampling_time.get("year") is not None,
            bool(_string_or_none(sampling_time.get("date_start"))),
            bool(_string_or_none(sampling_time.get("date_end"))),
            bool(_string_or_none(sampling_time.get("raw_text"))),
        )
    )
    has_method = (
        any(_string_or_none(value) for value in method.values())
        if isinstance(method, dict)
        else False
    )
    explicit_synthetic = bool(_SYNTHETIC_OR_SPIKED_PATTERN.search(text))
    field_provenance_complete = (
        observation_type == "field_measurement" and has_location and has_time and has_method
    )
    if field_provenance_complete and not explicit_synthetic:
        return ()
    reasons = [
        "treatment_experiment_not_environmental_occurrence",
        "environmental_sample_provenance_unconfirmed",
    ]
    if explicit_synthetic or observation_type in {"laboratory_measurement", "method_validation"}:
        reasons.append("spiked_or_synthetic_matrix_not_excluded")
    return tuple(reasons)


def ambiguous_product_identity_reason(candidate: dict[str, Any]) -> str | None:
    """Return a reason for product/process labels that do not identify one chemical entity."""
    analyte = candidate.get("analyte")
    if not isinstance(analyte, dict):
        return None
    raw = _string_or_none(analyte.get("reported_name") or analyte.get("raw_name"))
    if not raw:
        return None
    normalized = " ".join(raw.casefold().split())
    has_identifier = any(
        analyte.get(key) not in {None, ""}
        for key in ("cas_rn", "pubchem_cid", "inchikey", "canonical_smiles")
    )
    product_like = (
        normalized in _AMBIGUOUS_PRODUCT_OR_PROCESS_NAMES
        or analyte.get("alias_type") == "trade_name"
        or analyte.get("specificity_status") == "mixture_or_product"
    )
    if product_like and not has_identifier:
        return "chemical_identity_ambiguous_product_or_process_name"
    return None


def _specificity_tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    repaired = value
    for broken, greek in _MOJIBAKE_GREEK.items():
        repaired = repaired.replace(broken, greek)
    for greek, word in _GREEK_WORDS.items():
        repaired = repaired.replace(greek, word)
    return {match.group(0).casefold() for match in _SPECIFICITY_TOKEN_PATTERN.finditer(repaired)}


def chemical_identity_specificity_reason(candidate: dict[str, Any]) -> str | None:
    """Prevent canonicalization from collapsing a named isomer into its generic parent."""
    analyte = candidate.get("analyte")
    if not isinstance(analyte, dict):
        return None
    reported = _string_or_none(analyte.get("reported_name") or analyte.get("raw_name"))
    canonical = _string_or_none(analyte.get("canonical_name"))
    reported_tokens = _specificity_tokens(reported)
    if (
        reported_tokens
        and canonical
        and not reported_tokens.issubset(_specificity_tokens(canonical))
    ):
        return "chemical_identity_specificity_lost"
    return None


def exceedance_ratio_reason(candidate: dict[str, Any]) -> str | None:
    """Return a reason when the reported value is an exceedance multiple, not a concentration.

    "Exceeded the limit by N times" and ratio units such as ``fold``/``times`` describe a ratio to a
    guideline value, not an instrument measurement, so they must never enter the concentration
    stream. Detection only inspects structured result fields, never free prose, to avoid
    misclassifying ordinary "N times higher" statements.
    """
    result = candidate.get("result")
    if not isinstance(result, dict):
        return None
    unit = _string_or_none(result.get("raw_unit"))
    if unit and _EXCEEDANCE_UNIT.match(unit.strip()):
        return "exceedance_ratio_not_concentration:" + unit
    raw_value = _string_or_none(result.get("raw_value"))
    if raw_value:
        match = _EXCEEDANCE_PATTERN.search(raw_value)
        if match:
            return "exceedance_ratio_not_concentration:" + match.group(0)[:60]
    return None


def no_measurable_value_reason(candidate: dict[str, Any]) -> str | None:
    """Reject non-observations while retaining numeric left-censored thresholds separately."""
    result = candidate.get("result")
    if not isinstance(result, dict):
        return None
    if is_left_censored_numeric(candidate):
        return None
    if is_qualitative_censored(candidate):
        return "qualitative_censored_without_numeric_observation:" + str(result.get("qualifier"))
    if result.get("statistic") in _FREQUENCY_STATISTICS:
        return "detection_frequency_not_concentration"
    raw_value = _string_or_none(result.get("raw_value"))
    value_numeric = result.get("value_numeric")
    has_numeric = isinstance(value_numeric, (int, float)) or (
        raw_value is not None and any(ch.isdigit() for ch in raw_value)
    )
    if not has_numeric:
        return "no_measurable_concentration"
    return None

def table_statistic_mismatch_reason(candidate: dict[str, Any]) -> str | None:
    """Flag a table result whose statistic contradicts an explicit caption/header.

    Flattened or rotated tables often preserve numbers while dropping the aggregation semantics.
    An explicit caption such as "average concentrations" is authoritative and must override a
    model default of ``single``. Ambiguous captions do not trigger this deterministic gate.
    """
    evidence = candidate.get("evidence")
    result = candidate.get("result")
    if not isinstance(evidence, dict) or not isinstance(result, dict):
        return None
    if not _string_or_none(evidence.get("table_id")):
        return None
    caption_parts = [
        _string_or_none(evidence.get(key))
        for key in ("table_caption", "column_label", "relation_note")
    ]
    caption = " ".join(part for part in caption_parts if part)
    if not caption:
        return None
    statistic = (_string_or_none(result.get("statistic")) or "unknown").casefold()
    for pattern, allowed, expected in _TABLE_STATISTIC_CAPTION_PATTERNS:
        if pattern.search(caption) and statistic not in allowed:
            return f"table_caption_statistic_mismatch:expected_{expected}:got_{statistic}"
    return None


def binding_gap_reason(candidate: dict[str, Any]) -> str | None:
    """Return a reason when a numeric result lacks >=2 of matrix/time/location bindings.

    USGS WDFN / EPA WQP / NORMAN EMPODAT keep one result row = analyte + site + date + matrix +
    value + method; an un-bindable value is not a valid result. Laboratory measurements are
    exempt from the location requirement but still need matrix and time.
    """
    result = candidate.get("result")
    if not isinstance(result, dict):
        return None
    if (
        result.get("raw_value") is None
        and result.get("value_numeric") is None
        and not is_left_censored_numeric(candidate)
    ):
        return None  # qualitative no-value cases are handled by ``no_measurable_value_reason``
    observation_type = candidate.get("observation_type")
    needs_location = observation_type in {None, "unknown", "field_measurement"}
    location = candidate.get("location") or {}
    sample = candidate.get("sample") or {}
    sampling_time = candidate.get("sampling_time") or {}
    location_present = any(
        _string_or_none(location.get(key))
        for key in ("site_name", "waterbody", "city", "admin1", "country", "location_raw")
    )
    matrix_present = bool(
        _string_or_none(sample.get("matrix_raw"))
        or _string_or_none(sample.get("matrix_normalized"))
    )
    time_present = bool(
        sampling_time.get("year") is not None
        or _string_or_none(sampling_time.get("date_start"))
        or _string_or_none(sampling_time.get("date_end"))
        or _string_or_none(sampling_time.get("raw_text"))
    )
    missing: list[str] = []
    if not matrix_present:
        missing.append("matrix")
    if not time_present:
        missing.append("time")
    if needs_location and not location_present:
        missing.append("location")
    if len(missing) >= 2:
        return "binding_missing:" + "+".join(missing)
    return None


def surface_water_scope_reason(candidate: dict[str, Any]) -> str | None:
    """Return a hard-gate reason when a candidate is not a proven water-column observation.

    The main EC_MONITOR table is deliberately limited to concentrations measured in actual
    environmental surface water. This excludes wastewater/effluent, groundwater, air, biota,
    road dust, laboratory water, sediment, and concentrations measured on particles recovered
    from surface water (for example metals attached to microplastics). A generic ``water`` label
    is accepted only when the same evidence binds it to a natural surface-water body.
    """
    sample = candidate.get("sample") or {}
    location = candidate.get("location") or {}
    evidence = candidate.get("evidence") or {}
    values = [
        sample.get("matrix_raw"),
        sample.get("matrix_normalized"),
        sample.get("phase_or_fraction"),
        sample.get("sample_type"),
        location.get("waterbody"),
        location.get("site_name"),
        evidence.get("quote"),
        evidence.get("relation_note"),
        evidence.get("table_caption"),
        evidence.get("row_label"),
        evidence.get("column_label"),
    ]
    text = " ".join(str(value) for value in values if value not in (None, ""))
    matrix_values = [
        str(value)
        for value in (sample.get("matrix_raw"), sample.get("matrix_normalized"), sample.get("phase_or_fraction"), sample.get("sample_type"))
        if value not in (None, "")
    ]
    # Leave a completely missing matrix to the binding gate, which can report the more
    # actionable missing-field escalation instead of classifying it as a proven exclusion.
    if not matrix_values:
        return None
    if any(_MICROPLASTIC_BOUND_MATRIX_PATTERN.search(value) for value in matrix_values):
        return "microplastic_bound_measurement_not_water_column"
    relation_text = " ".join(
        str(value)
        for value in (evidence.get("quote"), evidence.get("relation_note"), evidence.get("table_caption"), evidence.get("row_label"), evidence.get("column_label"))
        if value not in (None, "")
    )
    if _MICROPLASTIC_BOUND_EVIDENCE_PATTERN.search(relation_text):
        return "microplastic_bound_measurement_not_water_column"
    if any(_WASTEWATER_MATRIX_PATTERN.search(value) for value in matrix_values):
        return "wastewater_or_effluent_not_surface_water"
    if any(_GROUNDWATER_MATRIX_PATTERN.search(value) for value in matrix_values):
        return "groundwater_not_surface_water"
    if any(_OTHER_NON_SURFACE_MATRIX_PATTERN.search(value) for value in matrix_values):
        return "non_surface_water_matrix"
    if any(_SURFACE_WATER_MATRIX_PATTERN.search(value) for value in matrix_values):
        return None
    if any(_GENERIC_WATER_MATRIX_PATTERN.fullmatch(value.strip()) for value in matrix_values) and _SURFACE_WATERBODY_PATTERN.search(text):
        return None
    return "surface_water_provenance_unconfirmed"


def evidence_quality_gate(candidate: dict[str, Any]) -> ValidationDecision | None:
    """Apply deterministic scope, provenance, experiment, identity, and value-quality gates."""
    scope_reason = surface_water_scope_reason(candidate)
    rejection_reasons = [scope_reason] if scope_reason else []
    rejection_reasons.extend((*secondary_primary_source_reasons(candidate), *treatment_experiment_reasons(candidate)))
    ambiguous_product = ambiguous_product_identity_reason(candidate)
    if ambiguous_product and rejection_reasons:
        rejection_reasons.append(ambiguous_product)
    if rejection_reasons:
        return ValidationDecision(
            action="reject",
            reason_codes=tuple(dict.fromkeys(rejection_reasons)),
            failed_json_pointers=("/observation_type", "/evidence", "/sample", "/location", "/sampling_time", "/analytical_method", "/analyte"),
            human_review_required=False,
        )
    if ambiguous_product:
        return ValidationDecision(
            action="escalate",
            reason_codes=(ambiguous_product,),
            failed_json_pointers=("/analyte",),
            requested_context=("document_local_analyte_list", "supplementary_information"),
            human_review_required=False,
        )
    specificity = chemical_identity_specificity_reason(candidate)
    if specificity:
        return ValidationDecision(
            action="escalate", reason_codes=(specificity,),
            failed_json_pointers=("/analyte/raw_name", "/analyte/canonical_name"),
            human_review_required=True,
        )
    exceedance = exceedance_ratio_reason(candidate)
    if exceedance:
        return ValidationDecision(
            action="reject", reason_codes=(exceedance,),
            failed_json_pointers=("/result/raw_unit", "/result/raw_value"),
            human_review_required=False,
        )
    no_value = no_measurable_value_reason(candidate)
    if no_value:
        return ValidationDecision(
            action="reject", reason_codes=(no_value,),
            failed_json_pointers=("/result",), human_review_required=False,
        )
    statistic_mismatch = table_statistic_mismatch_reason(candidate)
    if statistic_mismatch:
        return ValidationDecision(
            action="retry",
            reason_codes=(statistic_mismatch, "table_statistic_requires_source_rebind"),
            failed_json_pointers=("/result/statistic", "/evidence/table_caption"),
            requested_context=("table_caption", "table_headers", "table_cell_2d_layout"),
            human_review_required=False,
        )
    geo_conflict = geographic_conflict_reason(candidate)
    if geo_conflict:
        return ValidationDecision(
            action="escalate", reason_codes=(geo_conflict,),
            failed_json_pointers=("/location",),
            requested_context=("location_evidence", "administrative_hierarchy"),
            human_review_required=True,
        )
    sampling_time = candidate.get("sampling_time") or {}
    if (
        isinstance(sampling_time, dict)
        and sampling_time.get("basis") == "publication_year_fallback"
        and sampling_time.get("approximate") is not True
    ):
        return ValidationDecision(
            action="retry",
            reason_codes=("publication_year_fallback_requires_approximate_true",),
            failed_json_pointers=("/sampling_time/approximate",),
            requested_context=("publication_year",),
            human_review_required=False,
        )
    binding = binding_gap_reason(candidate)
    if binding:
        return ValidationDecision(
            action="escalate", reason_codes=(binding,),
            failed_json_pointers=("/sample", "/location", "/sampling_time"),
            human_review_required=True,
        )
    return None

def _freeze_validation_key(value: Any) -> Any:
    """Convert JSON-shaped data into a deterministic hashable validation key."""
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze_validation_key(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_validation_key(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze_validation_key(item) for item in value))
    return value


def _validation_equivalence_key(
    candidate: dict[str, Any], resolutions: tuple[ChemicalResolution, ...]
) -> Any | None:
    """Return a safe key for min/max/mean records sharing one evidence-bound observation.

    Grouping is intentionally narrow: only exact measured minimum, maximum, and mean records may
    share a model decision. Censored values, ranges, single measurements, and unknown qualifiers
    remain independent. All non-value candidate fields and the resolved chemical identity must be
    identical, including matrix, location, time, method, and evidence bindings.
    """
    result = candidate.get("result")
    if not isinstance(result, dict):
        return None
    qualifier = str(result.get("qualifier") or "").casefold()
    statistic = str(result.get("statistic") or "").casefold()
    if qualifier != "exact" or statistic not in _VALIDATION_EQUIVALENT_STATISTICS:
        return None

    candidate_key = {
        key: value
        for key, value in candidate.items()
        if key not in {"candidate_id", "result"}
    }
    candidate_key["result"] = {
        key: value for key, value in result.items() if key not in _VALIDATION_RESULT_VARIANTS
    }
    resolution_key = [
        {
            "raw_name": item.raw_name,
            "normalized_query": item.normalized_query,
            "status": item.status,
            "resolver_name": item.resolver_name,
            "matches": [
                {
                    "source": match.source,
                    "source_record_id": match.source_record_id,
                    "canonical_name": match.canonical_name,
                    "matched_alias": match.matched_alias,
                    "pubchem_cid": match.pubchem_cid,
                    "cas_candidates": match.cas_candidates,
                    "inchikey": match.inchikey,
                }
                for match in item.matches
            ],
        }
        for item in resolutions
    ]
    return _freeze_validation_key((candidate_key, resolution_key))


def _decision_is_candidate_specific(decision: ValidationDecision) -> bool:
    """True when propagating the decision across different values/statistics would be unsafe."""
    return decision.action == "retry" and any(
        pointer == prefix or pointer.startswith(prefix + "/")
        for pointer in decision.failed_json_pointers
        for prefix in _CANDIDATE_SPECIFIC_RESULT_POINTERS
    )


class PolicyGatedEvidenceValidator:
    """Apply deterministic safety gates around an independent model validator.

    The delegate is called only after basic chemical-specificity and resolution checks pass. During
    the pilot, a model ``accept`` becomes a pending human-review decision rather than an automatic
    authoritative acceptance.
    """

    validator_name = "policy_gated_model_evidence_validator_v2_grouped"

    def __init__(
        self, delegate: EvidenceValidator, *, require_pilot_human_signoff: bool = True
    ) -> None:
        if not callable(getattr(delegate, "validate", None)):
            raise TypeError("delegate must implement validate")
        self.delegate = delegate
        self.document_context: dict[str, Any] | None = None
        self.require_pilot_human_signoff = require_pilot_human_signoff

    def _forward_document_context(self) -> None:
        if self.document_context is not None and hasattr(self.delegate, "document_context"):
            self.delegate.document_context = dict(self.document_context)

    def validate(
        self,
        candidate: dict[str, Any],
        *,
        chunk: EvidenceChunk,
        resolutions: tuple[ChemicalResolution, ...],
    ) -> ValidationDecision:
        deterministic = self._deterministic_decision(candidate, resolutions)
        if deterministic is not None:
            return deterministic
        self._forward_document_context()
        decision = self.delegate.validate(candidate, chunk=chunk, resolutions=resolutions)
        return self._apply_pilot_policy(decision)

    def validate_batch(
        self,
        candidates: list[dict[str, Any]],
        *,
        chunk: EvidenceChunk,
        resolutions_by_candidate: list[tuple[ChemicalResolution, ...]],
    ) -> list[ValidationDecision]:
        if len(candidates) != len(resolutions_by_candidate):
            raise ValueError("candidates and resolutions_by_candidate must have equal length")
        decisions: list[ValidationDecision | None] = [None] * len(candidates)
        eligible_indexes: list[int] = []
        for index, (candidate, resolutions) in enumerate(
            zip(candidates, resolutions_by_candidate, strict=True)
        ):
            deterministic = self._deterministic_decision(candidate, resolutions)
            if deterministic is not None:
                decisions[index] = deterministic
            else:
                eligible_indexes.append(index)

        if eligible_indexes:
            groups = self._build_validation_groups(
                eligible_indexes,
                candidates=candidates,
                resolutions_by_candidate=resolutions_by_candidate,
            )
            representative_indexes = [group[0] for group in groups]
            delegated = self._delegate_indexes(
                representative_indexes,
                candidates=candidates,
                chunk=chunk,
                resolutions_by_candidate=resolutions_by_candidate,
            )
            fallback_indexes: list[int] = []
            for group, raw_decision in zip(groups, delegated, strict=True):
                decision = self._apply_pilot_policy(raw_decision)
                representative = group[0]
                decisions[representative] = decision
                if len(group) == 1:
                    continue
                if _decision_is_candidate_specific(raw_decision):
                    fallback_indexes.extend(group[1:])
                    continue
                for index in group[1:]:
                    decisions[index] = decision

            if fallback_indexes:
                fallback_decisions = self._delegate_indexes(
                    fallback_indexes,
                    candidates=candidates,
                    chunk=chunk,
                    resolutions_by_candidate=resolutions_by_candidate,
                )
                for index, decision in zip(fallback_indexes, fallback_decisions, strict=True):
                    decisions[index] = self._apply_pilot_policy(decision)

        if any(item is None for item in decisions):
            raise RuntimeError("validation batch left candidates without decisions")
        return [item for item in decisions if item is not None]

    @staticmethod
    def _build_validation_groups(
        eligible_indexes: list[int],
        *,
        candidates: list[dict[str, Any]],
        resolutions_by_candidate: list[tuple[ChemicalResolution, ...]],
    ) -> list[list[int]]:
        """Group only unique min/max/mean siblings with identical validation-critical context."""
        keyed: dict[Any, list[int]] = {}
        ungrouped: list[list[int]] = []
        for index in eligible_indexes:
            key = _validation_equivalence_key(candidates[index], resolutions_by_candidate[index])
            if key is None:
                ungrouped.append([index])
            else:
                keyed.setdefault(key, []).append(index)

        grouped: list[list[int]] = []
        for indexes in keyed.values():
            statistics = [
                str((candidates[index].get("result") or {}).get("statistic") or "").casefold()
                for index in indexes
            ]
            # Duplicate statistics can indicate collapsed site/column bindings. Do not propagate
            # across such records; send each one to the validator independently.
            if len(statistics) != len(set(statistics)):
                grouped.extend([index] for index in indexes)
            else:
                grouped.append(indexes)

        all_groups = grouped + ungrouped
        all_groups.sort(key=lambda indexes: indexes[0])
        return all_groups

    def _delegate_indexes(
        self,
        indexes: list[int],
        *,
        candidates: list[dict[str, Any]],
        chunk: EvidenceChunk,
        resolutions_by_candidate: list[tuple[ChemicalResolution, ...]],
    ) -> list[ValidationDecision]:
        if not indexes:
            return []
        self._forward_document_context()
        batch_method = getattr(self.delegate, "validate_batch", None)
        if callable(batch_method):
            raw_delegated = batch_method(
                [candidates[index] for index in indexes],
                chunk=chunk,
                resolutions_by_candidate=[resolutions_by_candidate[index] for index in indexes],
            )
            if not isinstance(raw_delegated, list) or not all(
                isinstance(item, ValidationDecision) for item in raw_delegated
            ):
                raise TypeError("delegate batch validator must return list[ValidationDecision]")
            delegated = cast(list[ValidationDecision], raw_delegated)
        else:
            delegated = [
                self.delegate.validate(
                    candidates[index],
                    chunk=chunk,
                    resolutions=resolutions_by_candidate[index],
                )
                for index in indexes
            ]
        if len(delegated) != len(indexes):
            raise RuntimeError("delegate batch validator returned the wrong number of decisions")
        return delegated

    @staticmethod
    def _deterministic_decision(
        candidate: dict[str, Any], resolutions: tuple[ChemicalResolution, ...]
    ) -> ValidationDecision | None:
        analyte = candidate.get("analyte")
        if not isinstance(analyte, dict) or not _string_or_none(analyte.get("raw_name")):
            return ValidationDecision(
                action="retry",
                reason_codes=("analyte_name_missing_or_malformed",),
                failed_json_pointers=("/analyte/raw_name",),
            )
        if looks_non_individual(candidate) and not is_microplastic_surface_water_observation(candidate):
            return ValidationDecision(
                action="reject",
                reason_codes=("not_an_individual_chemical",),
                failed_json_pointers=("/analyte/specificity_status",),
            )
        if looks_out_of_scope_water_quality(candidate):
            return ValidationDecision(
                action="reject",
                reason_codes=("classic_water_quality_parameter_out_of_scope",),
                failed_json_pointers=("/analyte",),
            )
        if is_microplastic_surface_water_observation(candidate):
            return None
        # A completely absent identity is a prerequisite failure: report it before
        # requesting unrelated evidence-binding repair. Once a resolver has produced an
        # outcome, however, deterministic source/scope rejects take precedence over an
        # unresolved-identity escalation.
        if not resolutions:
            return ValidationDecision(
                action="escalate",
                reason_codes=("chemical_resolution_missing",),
                failed_json_pointers=("/analyte",),
                human_review_required=True,
            )
        gate = evidence_quality_gate(candidate)
        if gate is not None:
            return gate
        if any(item.status in {"ambiguous", "not_found", "error"} for item in resolutions):
            return ValidationDecision(
                action="escalate",
                reason_codes=("chemical_identity_unresolved",),
                failed_json_pointers=("/analyte",),
                human_review_required=True,
            )
        return None

    def _apply_pilot_policy(self, decision: ValidationDecision) -> ValidationDecision:
        if not isinstance(decision, ValidationDecision):
            raise TypeError("delegate validator must return ValidationDecision")
        if decision.action == "accept" and self.require_pilot_human_signoff:
            return ValidationDecision(
                action="escalate",
                reason_codes=tuple(
                    dict.fromkeys((*decision.reason_codes, "pilot_human_signoff_required"))
                ),
                failed_json_pointers=decision.failed_json_pointers,
                requested_context=decision.requested_context,
                human_review_required=True,
                pilot_accept_eligible=not decision.failed_json_pointers and not decision.requested_context,
            )
        return decision


class ConservativeEvidenceValidator:
    """Deterministic first gate; production may add an independent model validator after it."""

    validator_name = "conservative_evidence_validator_v2"

    def validate(
        self,
        candidate: dict[str, Any],
        *,
        chunk: EvidenceChunk,
        resolutions: tuple[ChemicalResolution, ...],
    ) -> ValidationDecision:
        del chunk
        analyte = candidate.get("analyte")
        if not isinstance(analyte, dict) or not _string_or_none(analyte.get("raw_name")):
            return ValidationDecision(
                action="retry",
                reason_codes=("analyte_name_missing_or_malformed",),
                failed_json_pointers=("/analyte/raw_name",),
            )
        if looks_non_individual(candidate) and not is_microplastic_surface_water_observation(candidate):
            return ValidationDecision(
                action="reject",
                reason_codes=("not_an_individual_chemical",),
                failed_json_pointers=("/analyte/specificity_status",),
            )
        gate = evidence_quality_gate(candidate)
        if gate is not None:
            return gate
        if is_microplastic_surface_water_observation(candidate):
            return ValidationDecision(
                action="accept",
                reason_codes=("microplastic_surface_water_observation",),
            )
        if not resolutions:
            # Re-extracting the same text cannot conjure a registry/PubChem identity, so this is
            # a human-review escalation, not a repairable retry.
            return ValidationDecision(
                action="escalate",
                reason_codes=("chemical_resolution_missing",),
                failed_json_pointers=("/analyte",),
                human_review_required=True,
            )
        if any(item.status in {"ambiguous", "not_found", "error"} for item in resolutions):
            return ValidationDecision(
                action="escalate",
                reason_codes=("chemical_identity_unresolved",),
                failed_json_pointers=("/analyte",),
                human_review_required=True,
            )
        return ValidationDecision(
            action="escalate",
            reason_codes=("pilot_human_signoff_required",),
            human_review_required=True,
        )


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
