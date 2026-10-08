"""Build canonical query objects from protocol configuration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ecmonitor.retrieval_specialist.models import CanonicalQuery
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso


class QueryPlanner:
    """Deterministic Phase 1 query planner."""

    def build_initial_query(
        self, protocol: dict[str, object], date_to: str, date_from: str | None = None
    ) -> CanonicalQuery:
        date_range = protocol["date_range"]
        if not isinstance(date_range, dict):
            raise TypeError("date_range must be a mapping")
        publication_type = self._mapping(protocol["publication_type"], "publication_type")
        return CanonicalQuery(
            query_id="Q0001",
            parent_query_id=None,
            iteration=1,
            date_from=date_from or str(date_range["date_from"]),
            date_to=date_to,
            document_types=self._strings(publication_type["prefer"], "publication_type.prefer"),
            emerging_contaminant_terms=self._strings(
                protocol["emerging_contaminant_terms"], "emerging_contaminant_terms"
            ),
            surface_water_terms=self._strings(
                protocol["surface_water_terms"], "surface_water_terms"
            ),
            monitoring_and_concentration_terms=self._strings(
                protocol["monitoring_and_concentration_terms"],
                "monitoring_and_concentration_terms",
            ),
            optional_context_terms=self._strings(
                protocol.get("optional_context_terms", []),
                "optional_context_terms",
            ),
            prohibited_or_rejected_terms=self._strings(
                protocol.get("prohibited_or_rejected_terms", []),
                "prohibited_or_rejected_terms",
            ),
            created_at=utc_now_iso(),
        )

    def _mapping(self, value: object, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError(f"{name} must be a mapping")
        return value

    def _strings(self, value: object, name: str) -> list[str]:
        if not isinstance(value, Sequence) or isinstance(value, str | bytes):
            raise TypeError(f"{name} must be a sequence")
        return [str(item) for item in value]
