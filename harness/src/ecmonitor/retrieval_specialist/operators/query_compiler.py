"""Compile canonical queries for source-specific metadata APIs."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ecmonitor.retrieval_specialist.models import CanonicalQuery
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso


@dataclass(frozen=True)
class CompiledQuery:
    canonical_query_id: str
    source_name: str
    compiled_query: str
    supported_features: list[str]
    unsupported_features: list[str]
    downgraded_behavior: list[str]
    ignored_fields: list[str]
    warnings: list[str]
    compiler_version: str
    compiled_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class CanonicalQueryCompiler:
    """Phase 1 compiler that records source capability differences."""

    compiler_version = "0.1.1"

    def compile_for_source(self, query: CanonicalQuery, source_name: str) -> CompiledQuery:
        ec_block = " OR ".join(f'"{term}"' for term in query.emerging_contaminant_terms)
        water_block = " OR ".join(f'"{term}"' for term in query.surface_water_terms)
        concentration_block = " OR ".join(
            f'"{term}"' for term in query.monitoring_and_concentration_terms
        )
        compiled = f"({ec_block}) AND ({water_block}) AND ({concentration_block})"
        ignored_fields = []
        if query.optional_context_terms:
            optional_block = " OR ".join(f'"{term}"' for term in query.optional_context_terms)
            compiled = f"{compiled} AND ({optional_block})"
        else:
            ignored_fields.append("optional_context_terms")
        if query.prohibited_or_rejected_terms:
            prohibited_block = " OR ".join(
                f'"{term}"' for term in query.prohibited_or_rejected_terms
            )
            compiled = f"{compiled} NOT ({prohibited_block})"
        else:
            ignored_fields.append("prohibited_or_rejected_terms")
        return CompiledQuery(
            canonical_query_id=query.query_id,
            source_name=source_name,
            compiled_query=compiled,
            supported_features=["boolean_blocks", "date_range", "negative_terms"],
            unsupported_features=["scie_filter"],
            downgraded_behavior=[
                "Phase 1 mock compiler does not execute source-specific API semantics."
            ],
            ignored_fields=ignored_fields,
            warnings=["SCIE status is unknown until a registry snapshot is provided."],
            compiler_version=self.compiler_version,
            compiled_at=utc_now_iso(),
        )
