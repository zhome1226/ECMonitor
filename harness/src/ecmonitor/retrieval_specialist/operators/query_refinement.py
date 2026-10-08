"""File-based query refinement worker protocol."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import validate

from ecmonitor.retrieval_specialist.models import CanonicalQuery, QueryMetrics
from ecmonitor.retrieval_specialist.operators.query_patch import QueryPatch
from ecmonitor.retrieval_specialist.storage.atomic_io import (
    ensure_dir,
    read_json,
    write_json_atomic,
)

PROMPT_VERSION = "query-refinement-worker-v1.0"

REFINEMENT_INSTRUCTION = (
    "Return at most three QueryPatch objects. Each patch may modify only one "
    "active canonical-query concept block. Optimize for the observed topical-fit "
    "outcome: the next executable query should retrieve more title/abstract "
    "records matching natural-water emerging-contaminant occurrence, monitoring, "
    "or concentration, while reducing repeated off-scope treatment, removal, "
    "laboratory, groundwater, sediment, biota, toxicology, review, or method-only "
    "records. Use the topical_fit_profile first, then the term ledger. Prefer "
    "patches that directly address the dominant false-positive reasons without "
    "risking known includes. Positive expansion terms must be high-frequency in "
    "included title/abstract evidence, scientifically aligned with the protocol, "
    "and absent from the active canonical query. Prefer surface-water occurrence, "
    "monitoring, distribution, sampling, or concentration phrases over broad "
    "pollutant-class expansions when both are available. Phrases such as river "
    "monitoring programs, micropollutant concentrations, occurrence characteristics, "
    "spatiotemporal distribution, and surface-water occurrence are better first "
    "expansion candidates than bare pollutant classes because they keep high recall "
    "anchored to the application scene. Noise-reduction terms must come "
    "from repeated excluded evidence and target prohibited_or_rejected_terms. "
    "For noise reduction, prefer specific phrases such as wastewater treatment, "
    "sediment samples, fish tissues, adsorption, degradation, membrane filtration, "
    "or laboratory-only wording. Avoid broad single-token NOT terms such as "
    "removal, review, sediment, fish, modeling, treatment, or risk when a narrower "
    "candidate phrase is available; broad single-token exclusions are high-loss-risk "
    "and should be proposed only with explicit evidence that they do not occur in "
    "known includes. Use provider document-type filtering for review-like artifacts "
    "when possible rather than a broad text NOT review. "
    "Broad pollutant-class additions such as "
    "antibiotics, pharmaceuticals, PPCPs, PFAS, microplastics, pesticides, or "
    "metals are high-drift candidates: propose them only when the rationale and "
    "expected_effect explicitly preserve natural-water occurrence, monitoring, "
    "or concentration context, and prefer a context-constrained alternative in "
    "surface_water_terms, monitoring_and_concentration_terms, or "
    "optional_context_terms when available. If included evidence supports a "
    "narrower contaminant-occurrence phrase, such as microplastic pollution, "
    "microplastic contamination, PFAS occurrence, pharmaceutical residues, "
    "or pollution load index, propose that phrase instead of a bare class term "
    "such as microplastic* or PFAS. Broad pollutant-class additions must be "
    "supported by at least two independent included documents; a single "
    "included document is not enough. Pair broad class additions with a separate "
    "candidate patch that reduces the dominant noise pattern when evidence supports "
    "one. Prefer phrases that are likely to change "
    "provider executable requests and ranked results; repeated no-effect terms "
    "from previous rejected attempts should be skipped. Do not propose location-specific "
    "basin names unless the evidence supports a reusable waterbody phrase. Do "
    "not stop at the highest-frequency term if it has already failed; try the "
    "next supported term or phrase. Return a diverse set when evidence allows: "
    "one positive expansion, one noise-reduction patch, and one conservative "
    "surface-water or monitoring-context refinement. "
    "not propose duplicate, already-active, or likely no-op terms; if the best "
    "evidence would be a no-op, skip it and use another candidate. Do not decide "
    "which patch is accepted."
)


class QueryRefinementBlocked(RuntimeError):
    """Raised when live query refinement needs Codex worker proposals."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("QueryRefinementWorker result is required.")
        self.payload = payload


class QueryRefinementWorkerExecutor:
    """Durable file protocol for score-guided query-patch proposals."""

    def __init__(self, *, repo_root: Path, run_dir: Path, schema_path: Path) -> None:
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.schema_path = schema_path
        self.schema = read_json(schema_path)

    def propose(
        self,
        *,
        accepted_query: CanonicalQuery,
        metrics: QueryMetrics,
        evidence_refs: dict[str, str],
        previous_changes: list[dict[str, Any]],
    ) -> list[QueryPatch]:
        refinement_key = self._refinement_key(accepted_query.query_id, previous_changes)
        request_ref = self._request_path(refinement_key)
        result_ref = self._result_path(refinement_key)
        if not result_ref.exists():
            ensure_dir(request_ref.parent)
            write_json_atomic(
                request_ref,
                {
                    "worker_name": "QueryRefinementWorker",
                    "instruction": REFINEMENT_INSTRUCTION,
                    "prompt_version": PROMPT_VERSION,
                    "parent_query": accepted_query.to_dict(),
                    "query_metrics": metrics.to_dict(),
                    "evidence_refs": evidence_refs,
                    "candidate_evidence_preview": self._candidate_evidence_preview(
                        evidence_refs
                    ),
                    "previous_accepted_and_rejected_changes": previous_changes,
                    "refinement_key": refinement_key,
                    "result_ref": self._display_path(result_ref),
                },
            )
            raise QueryRefinementBlocked(
                {
                    "status": "paused_query_refinement_worker_required",
                    "worker_name": "QueryRefinementWorker",
                    "request_ref": self._display_path(request_ref),
                    "result_ref": self._display_path(result_ref),
                }
        )
        payload = read_json(result_ref)
        proposals = payload.get("query_patches", payload) if isinstance(payload, dict) else payload
        if not isinstance(proposals, list):
            raise ValueError("QueryRefinementWorker result must be a list or query_patches list")
        patches = []
        for proposal in proposals[:3]:
            validate(instance=proposal, schema=self.schema)
            patches.append(QueryPatch(**proposal))
        return patches

    def prompt_hash(self) -> str:
        return hashlib.sha256(PROMPT_VERSION.encode()).hexdigest()

    def _request_path(self, query_id: str) -> Path:
        return self.run_dir / "query_refinement" / "worker_requests" / f"{query_id}.json"

    def _result_path(self, query_id: str) -> Path:
        return self.run_dir / "query_refinement" / "worker_results" / f"{query_id}.json"

    def _refinement_key(
        self, accepted_query_id: str, previous_changes: list[dict[str, Any]]
    ) -> str:
        attempts = [
            change
            for change in previous_changes
            if change.get("parent_query_id") == accepted_query_id
        ]
        if not attempts:
            return accepted_query_id
        return f"{accepted_query_id}_attempt_{len(attempts) + 1:03d}"

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.repo_root.resolve()))
        except ValueError:
            return str(path)

    def _candidate_evidence_preview(self, evidence_refs: dict[str, str]) -> dict[str, Any]:
        return {
            "positive_term_candidates": self._read_candidate_rows(
                evidence_refs.get("positive_term_candidates"), limit=12
            ),
            "negative_noise_candidates": self._read_candidate_rows(
                evidence_refs.get("negative_noise_candidates"), limit=12
            ),
        }

    def _read_candidate_rows(self, ref: str | None, *, limit: int) -> list[dict[str, Any]]:
        if not ref:
            return []
        path = self._resolve_ref(ref)
        if path is None or not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                positive_support = row.get("supporting_positive_documents", "")
                negative_support = row.get("supporting_negative_documents", "")
                rows.append(
                    {
                        "term": row.get("term", ""),
                        "concept_block": row.get("concept_block", ""),
                        "action": row.get("action", ""),
                        "supporting_positive_documents": positive_support,
                        "supporting_negative_documents": negative_support,
                        "positive_support_count": self._support_count(positive_support),
                        "negative_support_count": self._support_count(negative_support),
                        "discriminative_score": row.get("discriminative_score", ""),
                        "reason": row.get("reason", ""),
                    }
                )
        return sorted(rows, key=self._candidate_preview_sort_key)[:limit]

    def _resolve_ref(self, ref: str) -> Path | None:
        path = Path(ref)
        if path.is_absolute():
            return path
        candidates = [self.repo_root / path, self.run_dir / path]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    @staticmethod
    def _support_count(value: str) -> int:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return 0
        return len(parsed) if isinstance(parsed, list) else 0

    @staticmethod
    def _float_or_zero(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _candidate_preview_sort_key(self, row: dict[str, Any]) -> tuple[Any, ...]:
        action = str(row.get("action") or "")
        if action == "negative_noise_candidate":
            return (
                self._broad_noise_term_penalty(str(row.get("term") or "")),
                -self._specific_noise_phrase_score(str(row.get("term") or "")),
                -int(row.get("negative_support_count") or 0),
                int(row.get("positive_support_count") or 0),
                -self._float_or_zero(row.get("discriminative_score")),
                str(row.get("term") or ""),
            )
        return (
            -self._positive_context_phrase_score(str(row.get("term") or "")),
            self._broad_pollutant_class_penalty(str(row.get("term") or "")),
            -int(row.get("positive_support_count") or 0),
            int(row.get("negative_support_count") or 0),
            -self._float_or_zero(row.get("discriminative_score")),
            str(row.get("term") or ""),
        )

    @staticmethod
    def _broad_noise_term_penalty(term: str) -> int:
        broad_singletons = {
            "fish",
            "modeling",
            "modelling",
            "prediction",
            "removal",
            "review",
            "risk",
            "sediment",
            "sediments",
            "treatment",
        }
        normalized = " ".join(term.lower().replace("-", " ").split())
        return 1 if normalized in broad_singletons else 0

    @staticmethod
    def _specific_noise_phrase_score(term: str) -> int:
        tokens = " ".join(term.lower().replace("-", " ").split()).split()
        phrase_bonus = 2 if len(tokens) >= 2 else 0
        specificity_tokens = {
            "adsorption",
            "biota",
            "coastal",
            "degradation",
            "effluent",
            "filtration",
            "groundwater",
            "laboratory",
            "membrane",
            "outfall",
            "plant",
            "sample",
            "samples",
            "sewage",
            "tissues",
            "treatment",
            "wastewater",
        }
        return phrase_bonus + sum(1 for token in tokens if token in specificity_tokens)

    @staticmethod
    def _positive_context_phrase_score(term: str) -> int:
        normalized = " ".join(term.lower().replace("-", " ").split())
        tokens = normalized.split()
        phrase_bonus = 2 if len(tokens) >= 2 else 0
        context_tokens = {
            "abundance",
            "concentration",
            "concentrations",
            "distribution",
            "field",
            "monitoring",
            "occurrence",
            "program",
            "programs",
            "sample",
            "samples",
            "sampling",
            "spatiotemporal",
            "surface",
            "water",
        }
        return phrase_bonus + sum(1 for token in tokens if token in context_tokens)

    @staticmethod
    def _broad_pollutant_class_penalty(term: str) -> int:
        normalized = " ".join(term.lower().replace("-", " ").split())
        broad_classes = {
            "antibiotic",
            "antibiotics",
            "emerging contaminants",
            "microplastic",
            "microplastics",
            "micropollutant",
            "micropollutants",
            "organic micropollutants",
            "pfas",
            "pharmaceutical",
            "pharmaceuticals",
            "polar organic contaminants",
        }
        return 1 if normalized in broad_classes else 0
