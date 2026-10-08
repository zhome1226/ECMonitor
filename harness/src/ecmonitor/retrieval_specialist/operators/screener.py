"""Deterministic Phase 1 title/abstract and metadata screening."""

from __future__ import annotations

import hashlib
from typing import TypedDict

from ecmonitor.retrieval_specialist.models import NormalizedRecord, ScreeningDecision
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso

ELIGIBLE_SURFACE_WATER = {
    "ambient surface water",
    "surface water",
    "river",
    "stream",
    "creek",
    "lake",
    "reservoir",
    "wetland",
    "canal",
    "seawater",
    "marine water",
    "coastal water",
    "estuary",
    "bay",
    "lagoon",
    "pond",
}

GROUNDWATER_TERMS = {"groundwater"}
WASTEWATER_TERMS = {
    "wastewater",
    "sewage",
    "sludge",
    "influent",
    "effluent",
}
WATER_PLANT_TERMS = {
    "drinking water",
    "tap water",
    "bottled water",
    "reclaimed water",
    "wastewater treatment plants",
    "sewage treatment plants",
    "drinking-water treatment plants",
    "water treatment plants",
    "industrial treatment facilities",
    "treatment-process units",
    "treatment intermediates",
    "plant process water",
}
EXCLUDED_DOCUMENT_TYPES = {
    "review",
    "systematic review",
    "editorial",
    "commentary",
    "perspective",
    "conference abstract",
    "conference paper",
    "book chapter",
    "correction",
    "letter",
}

REVIEW_ARTIFACT_PHRASES = {
    "a review",
    "a systematic review",
    "an introductory meta-review",
    "bibliometric review",
    "critical review",
    "literature review",
    "meta review",
    "meta-review",
    "mini review",
    "narrative review",
    "preface",
    "scoping review",
    "state-of-the-art review",
    "systematic review",
}

PRIMARY_STUDY_ANCHORS = {
    "baseline concentration",
    "field monitoring",
    "field sample",
    "field sampling",
    "freshwater monitoring",
    "measured concentration",
    "monitoring",
    "occurrence",
    "river surface water",
    "spatial distribution",
    "spatiotemporal distribution",
    "surface water",
    "water samples",
}

SPECIFIC_PRIMARY_STUDY_ANCHORS = {
    "baseline concentration",
    "field sample",
    "field sampling",
    "freshwater monitoring",
    "measured concentration",
    "river surface water",
    "spatial distribution",
    "spatiotemporal distribution",
    "surface water",
    "water samples",
}

HIGH_CONFIDENCE_EC_TERMS = {
    "anthropogenic contaminant",
    "antibiotic",
    "antibiotics",
    "cec",
    "contaminant of emerging concern",
    "contaminants of emerging concern",
    "emerging contaminant",
    "emerging pollutant",
    "microplastic",
    "micropollutant",
    "organic micropollutant",
    "pharmaceutical",
    "pharmaceuticals",
    "trace organic contaminant",
}

HIGH_CONFIDENCE_SURFACE_WATER_TERMS = {
    "bay",
    "coastal water",
    "estuary",
    "freshwater",
    "lake",
    "lagoon",
    "pearl river",
    "reservoir",
    "river",
    "seawater",
    "stream",
    "surface water",
    "wetland",
    "water body",
    "water bodies",
}

HIGH_CONFIDENCE_MONITORING_TERMS = {
    "concentration",
    "concentrations",
    "distribution",
    "field monitoring",
    "field sampling",
    "monitoring",
    "occurrence",
    "quantification",
    "quantitative",
    "semiquantitative",
    "spatial distribution",
    "spatiotemporal distribution",
}

HIGH_CONFIDENCE_NOISE_TERMS = {
    "adsorbent",
    "adsorption",
    "bioremediation",
    "degradation",
    "kinetic study",
    "laboratory",
    "membrane",
    "molecular dynamics",
    "nanofiltration",
    "personal care products",
    "photocatalytic",
    "removal",
    "simulation",
    "simulations",
    "treatment",
    "water reuse",
}

TOXICOLOGY_BIOTA_NOISE_TERMS = {
    "bioassay",
    "biomarker",
    "cell line",
    "carp",
    "dose response",
    "dose-response",
    "exposure group",
    "fish group",
    "immunotoxicity",
    "kidney",
    "organism",
    "organisms",
    "rodlet cell",
    "toxicity",
    "toxicological",
}


class ScreeningContext(TypedDict):
    """Typed context passed through deterministic screening helpers."""

    run_id: str
    query_id: str
    iteration: int
    audit_batch_id: str | None
    prompt_hash: str
    scie_status: str


class TitleAbstractScreener:
    """Rule-backed Phase 1 screener; no real LLM calls are made."""

    def screen(
        self,
        records: list[NormalizedRecord],
        *,
        run_id: str = "",
        query_id: str = "",
        iteration: int = 0,
        audit_batch_id: str | None = None,
        prompt_hash: str = "not_applicable",
        scie_status: str = "registry_unavailable",
    ) -> list[ScreeningDecision]:
        return [
            self.screen_one(
                record,
                run_id=run_id,
                query_id=query_id,
                iteration=iteration,
                audit_batch_id=audit_batch_id,
                prompt_hash=prompt_hash,
                scie_status=scie_status,
            )
            for record in records
        ]

    def screen_one(
        self,
        record: NormalizedRecord,
        *,
        run_id: str = "",
        query_id: str = "",
        iteration: int = 0,
        audit_batch_id: str | None = None,
        prompt_hash: str = "not_applicable",
        scie_status: str = "registry_unavailable",
    ) -> ScreeningDecision:
        evidence = self._evidence(record)
        document_type = (record.document_type or "").strip().lower()
        context = self._context(
            record,
            run_id=run_id,
            query_id=query_id,
            iteration=iteration,
            audit_batch_id=audit_batch_id,
            prompt_hash=prompt_hash,
            scie_status=scie_status,
        )
        if document_type in EXCLUDED_DOCUMENT_TYPES:
            return self._decision(record, "exclude", ["E_REVIEW"], evidence, **context)
        if self._is_review_artifact(record):
            return self._decision(
                record,
                "exclude",
                ["E_REVIEW_OR_ARTIFACT"],
                evidence,
                **context,
            )

        matrices = {matrix.strip().lower() for matrix in record.sampled_matrices}
        eligible = matrices & ELIGIBLE_SURFACE_WATER
        groundwater = matrices & GROUNDWATER_TERMS
        wastewater = matrices & WASTEWATER_TERMS
        plant = matrices & WATER_PLANT_TERMS

        if eligible and (groundwater or wastewater or plant):
            return self._decision(
                record,
                "include",
                ["I_EXTRACTABLE_SURFACE_WATER_IN_MIXED_MATRIX"],
                evidence,
                **context,
            )
        if groundwater:
            return self._decision(record, "exclude", ["E_GROUNDWATER"], evidence, **context)
        if wastewater:
            return self._decision(record, "exclude", ["E_WASTEWATER"], evidence, **context)
        if plant:
            return self._decision(record, "exclude", ["E_WATER_PLANT"], evidence, **context)

        study_type = (record.study_type or "").lower()
        if any(token in study_type for token in ["lab", "degradation", "toxicity", "adsorption"]):
            return self._decision(record, "exclude", ["E_LAB_STUDY"], evidence, **context)

        if record.has_real_field_sample is False:
            return self._decision(record, "exclude", ["E_NO_FIELD_SAMPLE"], evidence, **context)

        if record.has_concentration_evidence is False:
            return self._decision(record, "exclude", ["E_NO_CONCENTRATION"], evidence, **context)

        if record.has_concentration_evidence is None:
            reason = (
                "D_METADATA_MISSING"
                if not record.abstract_original
                else "D_AMBIGUOUS_CONCENTRATION"
            )
            return self._decision(
                record, "defer_metadata", [reason], evidence, concentration_evidence=None, **context
            )

        if not eligible:
            return self._decision(
                record,
                "defer_metadata",
                ["D_METADATA_MISSING"],
                evidence,
                concentration_evidence=None,
                **context,
            )

        return self._decision(
            record,
            "include",
            ["I_SURFACE_WATER_CONCENTRATION"],
            evidence,
            **context,
        )

    def prefilter_one(
        self,
        record: NormalizedRecord,
        *,
        run_id: str = "",
        query_id: str = "",
        iteration: int = 0,
        audit_batch_id: str | None = None,
        prompt_hash: str = "not_applicable",
        scie_status: str = "registry_unavailable",
    ) -> ScreeningDecision | None:
        evidence = self._evidence(record)
        document_type = (record.document_type or "").strip().lower()
        context = self._context(
            record,
            run_id=run_id,
            query_id=query_id,
            iteration=iteration,
            audit_batch_id=audit_batch_id,
            prompt_hash=prompt_hash,
            scie_status=scie_status,
        )
        if document_type in EXCLUDED_DOCUMENT_TYPES:
            return self._decision(record, "exclude", ["E_REVIEW"], evidence, **context)
        if self._is_review_artifact(record):
            return self._decision(
                record,
                "exclude",
                ["E_REVIEW_OR_ARTIFACT"],
                evidence,
                **context,
            )
        if self._is_high_confidence_include(record):
            return self._decision(
                record,
                "include",
                ["I_SURFACE_WATER_CONCENTRATION"],
                evidence,
                **context,
            )
        if self._is_toxicology_biota_noise(record):
            return self._decision(record, "exclude", ["E_LAB_STUDY"], evidence, **context)
        if self._is_high_confidence_title_noise(record):
            return self._decision(record, "exclude", ["E_LAB_STUDY"], evidence, **context)
        if not record.abstract_original:
            return self._decision(
                record,
                "defer_metadata",
                ["D_METADATA_MISSING"],
                evidence,
                concentration_evidence=None,
                **context,
            )
        return None

    def _decision(
        self,
        record: NormalizedRecord,
        decision: str,
        reason_codes: list[str],
        evidence: list[str],
        *,
        run_id: str,
        query_id: str,
        iteration: int,
        audit_batch_id: str | None,
        prompt_hash: str,
        scie_status: str,
        concentration_evidence: str | None = "explicit_quantified",
    ) -> ScreeningDecision:
        matrices = {matrix.strip().lower() for matrix in record.sampled_matrices}
        eligible = sorted(matrices & ELIGIBLE_SURFACE_WATER)
        excluded = sorted(matrices - ELIGIBLE_SURFACE_WATER)
        timestamp = utc_now_iso()
        return ScreeningDecision(
            global_record_id=record.global_record_id,
            screening_decision_id=self._decision_id(
                run_id, query_id, iteration, record.global_record_id, decision
            ),
            run_id=run_id,
            query_id=query_id,
            iteration=iteration,
            decision=decision,
            confidence=1.0 if decision in {"include", "exclude"} else 0.5,
            article_type_ok=(record.document_type or "").lower() not in EXCLUDED_DOCUMENT_TYPES,
            date_ok=True,
            scie_status=scie_status,
            emerging_contaminant_context=record.article_ec_scope == "true",
            surface_water_sample=bool(eligible),
            included_waterbody_types=eligible,
            excluded_sample_matrices_present=bool(excluded),
            excluded_sample_matrices=excluded,
            water_treatment_plant_samples_present=bool(matrices & WATER_PLANT_TERMS),
            mixed_eligible_ineligible_matrices=bool(eligible and excluded),
            field_environmental_samples=record.has_real_field_sample,
            concentration_evidence=self._concentration_evidence(
                record, concentration_evidence
            ),
            study_type=record.study_type,
            reason_codes=reason_codes,
            evidence_spans=evidence,
            prompt_hash=prompt_hash,
            screening_timestamp=timestamp,
            audit_batch_id=audit_batch_id,
            article_ec_scope=record.article_ec_scope,
        )

    def _evidence(self, record: NormalizedRecord) -> list[str]:
        evidence = [record.title_original]
        if record.abstract_original:
            evidence.append(record.abstract_original[:500])
        if record.sampled_matrices:
            evidence.append(f"sampled_matrices={','.join(record.sampled_matrices)}")
        return evidence

    def _is_review_artifact(self, record: NormalizedRecord) -> bool:
        text = self._screening_text(record)
        if not any(phrase in text for phrase in REVIEW_ARTIFACT_PHRASES):
            return False
        if "review" not in text and "preface" not in text:
            return False
        title = (record.title_original or "").lower()
        if "review" not in title and "preface" not in title:
            return False
        anchored_primary_review = (
            "review" in title
            and not any(phrase in title for phrase in REVIEW_ARTIFACT_PHRASES)
            and any(anchor in title for anchor in PRIMARY_STUDY_ANCHORS)
        )
        return not anchored_primary_review

    def _is_high_confidence_include(self, record: NormalizedRecord) -> bool:
        text = self._screening_text(record)
        return (
            any(term in text for term in HIGH_CONFIDENCE_EC_TERMS)
            and any(term in text for term in HIGH_CONFIDENCE_SURFACE_WATER_TERMS)
            and any(term in text for term in HIGH_CONFIDENCE_MONITORING_TERMS)
            and not any(term in text for term in HIGH_CONFIDENCE_NOISE_TERMS)
        )

    def _is_high_confidence_title_noise(self, record: NormalizedRecord) -> bool:
        text = self._screening_text(record)
        has_natural_water_anchor = any(
            term in text for term in HIGH_CONFIDENCE_SURFACE_WATER_TERMS
        )
        return (
            any(term in text for term in HIGH_CONFIDENCE_EC_TERMS)
            and any(term in text for term in HIGH_CONFIDENCE_NOISE_TERMS)
            and not has_natural_water_anchor
        )

    def _is_toxicology_biota_noise(self, record: NormalizedRecord) -> bool:
        text = self._screening_text(record)
        return (
            (
                any(term in text for term in HIGH_CONFIDENCE_EC_TERMS)
                or "pfas" in text
            )
            and any(term in text for term in TOXICOLOGY_BIOTA_NOISE_TERMS)
            and not any(anchor in text for anchor in SPECIFIC_PRIMARY_STUDY_ANCHORS)
        )

    def _screening_text(self, record: NormalizedRecord) -> str:
        parts = [
            record.title_original or "",
            record.abstract_original or "",
            " ".join(record.keywords),
            record.study_type or "",
            record.document_type or "",
        ]
        return " ".join(parts).lower()

    def _context(
        self,
        record: NormalizedRecord,
        *,
        run_id: str,
        query_id: str,
        iteration: int,
        audit_batch_id: str | None,
        prompt_hash: str,
        scie_status: str,
    ) -> ScreeningContext:
        del record
        return {
            "run_id": run_id,
            "query_id": query_id,
            "iteration": iteration,
            "audit_batch_id": audit_batch_id,
            "prompt_hash": prompt_hash,
            "scie_status": scie_status,
        }

    def _decision_id(
        self, run_id: str, query_id: str, iteration: int, global_record_id: str, decision: str
    ) -> str:
        key = f"{run_id}|{query_id}|{iteration}|{global_record_id}|{decision}"
        return f"screen_{hashlib.sha256(key.encode()).hexdigest()[:24]}"

    def _concentration_evidence(
        self, record: NormalizedRecord, override: str | None
    ) -> str:
        if override is None:
            return "likely_but_not_explicit"
        if record.has_concentration_evidence is False:
            return "absent"
        study_type = (record.study_type or "").lower()
        if "semi" in study_type or "nontarget" in study_type:
            return "semi_quantitative"
        return override
