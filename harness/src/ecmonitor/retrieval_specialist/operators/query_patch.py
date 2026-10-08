"""Structured query refinement patch application."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from jsonschema import validate

from ecmonitor.retrieval_specialist.models import CanonicalQuery
from ecmonitor.retrieval_specialist.operators.query_compiler import CanonicalQueryCompiler
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.storage.atomic_io import read_json

ACTIVE_BLOCKS = {
    "emerging_contaminant_terms",
    "surface_water_terms",
    "monitoring_and_concentration_terms",
    "optional_context_terms",
    "prohibited_or_rejected_terms",
}

BROAD_POLLUTANT_CLASS_TERMS = {
    "antibiotic",
    "antibiotics",
    "pharmaceutical",
    "pharmaceuticals",
    "pharmaceuticals and personal care products",
    "ppcp",
    "ppcps",
    "pfas",
    "per- and polyfluoroalkyl substances",
    "microplastic",
    "microplastics",
    "pesticide",
    "pesticides",
    "metal",
    "metals",
    "heavy metal",
    "heavy metals",
}

CONTEXT_GUARD_TERMS = {
    "surface water",
    "river",
    "lake",
    "stream",
    "reservoir",
    "estuary",
    "coastal water",
    "freshwater",
    "occurrence",
    "monitoring",
    "concentration",
    "measured concentration",
    "detection",
    "field",
    "sample",
    "sampling",
}

MIN_BROAD_POLLUTANT_CLASS_SUPPORTING_DOCUMENTS = 2


@dataclass(frozen=True)
class QueryPatch:
    """One structured query patch proposed by QueryRefinementWorker."""

    query_patch_schema_version: str
    patch_id: str
    parent_query_id: str
    target_concept_block: str
    operation: str
    terms_added: list[str]
    terms_removed: list[str]
    evidence_document_ids: list[str]
    rationale: str
    expected_effect: str
    possible_drift_risk: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any], schema_path: Path) -> QueryPatch:
        validate(instance=payload, schema=read_json(schema_path))
        return cls(**payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_patch_schema_version": self.query_patch_schema_version,
            "patch_id": self.patch_id,
            "parent_query_id": self.parent_query_id,
            "target_concept_block": self.target_concept_block,
            "operation": self.operation,
            "terms_added": self.terms_added,
            "terms_removed": self.terms_removed,
            "evidence_document_ids": self.evidence_document_ids,
            "rationale": self.rationale,
            "expected_effect": self.expected_effect,
            "possible_drift_risk": self.possible_drift_risk,
        }


@dataclass(frozen=True)
class QueryPatchResult:
    """Deterministic patch-application result."""

    status: str
    reason: str
    parent_query: CanonicalQuery
    child_query: CanonicalQuery
    patch: QueryPatch
    query_diff: dict[str, Any]
    parent_compiled: dict[str, str]
    child_compiled: dict[str, str]


class QueryPatchApplier:
    """Apply QueryPatch objects to active canonical-query blocks."""

    def apply(
        self,
        parent: CanonicalQuery,
        patch: QueryPatch,
        *,
        child_query_id: str,
        iteration: int,
    ) -> QueryPatchResult:
        if patch.parent_query_id != parent.query_id:
            return self._reject(parent, parent, patch, "parent_mismatch")
        if patch.target_concept_block not in ACTIVE_BLOCKS:
            return self._reject(parent, parent, patch, "invalid_target_block")
        if patch.operation == "add" and not patch.terms_added:
            return self._reject(parent, parent, patch, "missing_added_terms")
        if patch.operation == "remove" and not patch.terms_removed:
            return self._reject(parent, parent, patch, "missing_removed_terms")
        if patch.operation == "replace" and (
            not patch.terms_added or not patch.terms_removed
        ):
            return self._reject(parent, parent, patch, "replace_requires_add_and_remove")
        if self._is_unguarded_broad_pollutant_class_patch(patch):
            return self._reject(parent, parent, patch, "broad_pollutant_class_without_context")
        if self._is_under_supported_broad_pollutant_class_patch(patch):
            return self._reject(
                parent,
                parent,
                patch,
                "broad_pollutant_class_insufficient_support",
            )
        if self._is_specific_pollutant_emerging_block_patch(patch):
            return self._reject(
                parent,
                parent,
                patch,
                "specific_pollutant_not_allowed_in_emerging_block",
            )

        current = list(getattr(parent, patch.target_concept_block))
        normalized_current = {_norm(term) for term in current}
        duplicate_additions = [
            term for term in patch.terms_added if _norm(term) in normalized_current
        ]
        if duplicate_additions:
            return self._reject(parent, parent, patch, "duplicate_addition")
        missing_removals = [
            term for term in patch.terms_removed if _norm(term) not in normalized_current
        ]
        if missing_removals and patch.operation in {"remove", "replace"}:
            return self._reject(parent, parent, patch, "missing_removed_term")

        removed_terms = {_norm(term) for term in patch.terms_removed}
        updated = [term for term in current if _norm(term) not in removed_terms]
        updated_terms = {_norm(term) for term in updated}
        updated.extend(
            term for term in patch.terms_added if _norm(term) not in updated_terms
        )
        child = self._replace_target_block(
            parent=parent,
            patch=patch,
            updated=updated,
            child_query_id=child_query_id,
            iteration=iteration,
        )
        parent_compiled = self._compiled_by_source(parent)
        child_compiled = self._compiled_by_source(child)
        query_diff = {
            "target_concept_block": patch.target_concept_block,
            "operation": patch.operation,
            "terms_added": patch.terms_added,
            "terms_removed": patch.terms_removed,
            "before": current,
            "after": updated,
            "compiled_changed": parent_compiled != child_compiled,
        }
        if parent_compiled == child_compiled:
            return QueryPatchResult(
                status="rejected",
                reason="no_op",
                parent_query=parent,
                child_query=child,
                patch=patch,
                query_diff=query_diff,
                parent_compiled=parent_compiled,
                child_compiled=child_compiled,
            )
        return QueryPatchResult(
            status="applied",
            reason="compiled_query_changed",
            parent_query=parent,
            child_query=child,
            patch=patch,
            query_diff=query_diff,
            parent_compiled=parent_compiled,
            child_compiled=child_compiled,
        )

    def _reject(
        self,
        parent: CanonicalQuery,
        child: CanonicalQuery,
        patch: QueryPatch,
        reason: str,
    ) -> QueryPatchResult:
        compiled = self._compiled_by_source(parent)
        return QueryPatchResult(
            status="rejected",
            reason=reason,
            parent_query=parent,
            child_query=child,
            patch=patch,
            query_diff={"compiled_changed": False, "reason": reason},
            parent_compiled=compiled,
            child_compiled=compiled,
        )

    def _compiled_by_source(self, query: CanonicalQuery) -> dict[str, str]:
        compiler = CanonicalQueryCompiler()
        return {
            source: compiler.compile_for_source(query, source).compiled_query
            for source in ["crossref", "openalex", "semantic_scholar", "pubmed"]
        }

    def _replace_target_block(
        self,
        *,
        parent: CanonicalQuery,
        patch: QueryPatch,
        updated: list[str],
        child_query_id: str,
        iteration: int,
    ) -> CanonicalQuery:
        kwargs: dict[str, Any] = {
            "query_id": child_query_id,
            "parent_query_id": parent.query_id,
            "iteration": iteration,
            "added_terms": patch.terms_added,
            "removed_terms": patch.terms_removed,
            "modified_concept_blocks": [patch.target_concept_block],
            "candidate_expansion_terms": patch.terms_added,
            "change_rationale": patch.rationale,
            "evidence_for_change": patch.evidence_document_ids,
            "expected_effect": patch.expected_effect,
            "created_at": utc_now_iso(),
        }
        if patch.target_concept_block == "emerging_contaminant_terms":
            return replace(parent, emerging_contaminant_terms=updated, **kwargs)
        if patch.target_concept_block == "surface_water_terms":
            return replace(parent, surface_water_terms=updated, **kwargs)
        if patch.target_concept_block == "monitoring_and_concentration_terms":
            return replace(parent, monitoring_and_concentration_terms=updated, **kwargs)
        if patch.target_concept_block == "optional_context_terms":
            return replace(parent, optional_context_terms=updated, **kwargs)
        if patch.target_concept_block == "prohibited_or_rejected_terms":
            return replace(parent, prohibited_or_rejected_terms=updated, **kwargs)
        raise ValueError(f"Unsupported target concept block: {patch.target_concept_block}")

    def _is_unguarded_broad_pollutant_class_patch(self, patch: QueryPatch) -> bool:
        if patch.target_concept_block != "emerging_contaminant_terms":
            return False
        broad_terms = [
            term
            for term in patch.terms_added
            if _norm(term) in BROAD_POLLUTANT_CLASS_TERMS
        ]
        if not broad_terms:
            return False
        context_text = " ".join(
            [
                patch.rationale,
                patch.expected_effect,
                patch.possible_drift_risk,
                *patch.terms_added,
            ]
        ).lower()
        return not any(term in context_text for term in CONTEXT_GUARD_TERMS)

    def _is_under_supported_broad_pollutant_class_patch(self, patch: QueryPatch) -> bool:
        if patch.target_concept_block != "emerging_contaminant_terms":
            return False
        if not any(_norm(term) in BROAD_POLLUTANT_CLASS_TERMS for term in patch.terms_added):
            return False
        return (
            len(set(patch.evidence_document_ids))
            < MIN_BROAD_POLLUTANT_CLASS_SUPPORTING_DOCUMENTS
        )

    def _is_specific_pollutant_emerging_block_patch(self, patch: QueryPatch) -> bool:
        if patch.target_concept_block != "emerging_contaminant_terms":
            return False
        if patch.operation != "add":
            return False
        if len(patch.terms_added) != 1:
            return False
        term = _norm(patch.terms_added[0])
        if term in BROAD_POLLUTANT_CLASS_TERMS:
            return False
        term_tokens = set(term.split())
        generic_terms = {
            "contaminant",
            "contaminants",
            "emerging",
            "micropollutant",
            "micropollutants",
        }
        return not bool(term_tokens & generic_terms)


def patch_id(payload: dict[str, Any]) -> str:
    return "patch_" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode()
    ).hexdigest()[:16]


def _norm(term: str) -> str:
    return " ".join(term.lower().split())
