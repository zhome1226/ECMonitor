"""SQLite-backed Phase 1.1 mock harness for Retrieval Specialist."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from jsonschema import validate

from ecmonitor.retrieval_specialist.adapters.external_metadata_gateway import (
    ExternalMetadataDiscoveryGateway,
)
from ecmonitor.retrieval_specialist.models import (
    CanonicalQuery,
    NormalizedRecord,
    QueryMetrics,
    RawRecord,
    ScreeningDecision,
    SourceExecutionStatus,
)
from ecmonitor.retrieval_specialist.operators.audit_pool import AuditPoolEvaluator
from ecmonitor.retrieval_specialist.operators.evaluator import clamp
from ecmonitor.retrieval_specialist.operators.gpt_screening import (
    ScreeningWorkerBlocked,
    TitleAbstractScreeningWorkerExecutor,
)
from ecmonitor.retrieval_specialist.operators.metadata_normalizer import MetadataNormalizer
from ecmonitor.retrieval_specialist.operators.protocol_loader import LoadedConfig, ProtocolLoader
from ecmonitor.retrieval_specialist.operators.query_compiler import CanonicalQueryCompiler
from ecmonitor.retrieval_specialist.operators.query_patch import (
    QueryPatchApplier,
)
from ecmonitor.retrieval_specialist.operators.query_planner import QueryPlanner
from ecmonitor.retrieval_specialist.operators.query_refinement import (
    QueryRefinementBlocked,
    QueryRefinementWorkerExecutor,
)
from ecmonitor.retrieval_specialist.operators.screener import TitleAbstractScreener
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.orchestration.checkpoints import CheckpointManager
from ecmonitor.retrieval_specialist.orchestration.memory import (
    MemoryTelemetry,
    process_rss_mb,
    release_iteration_memory,
)
from ecmonitor.retrieval_specialist.orchestration.state_machine import RetrievalState, StateMachine
from ecmonitor.retrieval_specialist.storage.atomic_io import (
    append_jsonl,
    ensure_dir,
    read_json,
    read_yaml,
    sha256_file,
    write_csv_atomic,
    write_json_atomic,
    write_text_atomic,
    write_yaml_atomic,
)
from ecmonitor.retrieval_specialist.storage.control_plane import SCHEMA_VERSION, ControlPlane
from ecmonitor.security import redact

SOURCE_NAMES = ["crossref", "openalex", "semantic_scholar", "pubmed"]
DOCUMENT_VERSION = "metadata-v1"
METRIC_VERSION = "phase1.1.0"
DEFAULT_CANDIDATE_EVALUATION_TARGET_NOVEL_RECORDS = 20
DEFAULT_CANDIDATE_EVALUATION_LOSS_AUDIT_RECORDS = 50
CANDIDATE_POOL_CALIBRATION_POLICY_VERSION = "candidate-pool-audit-calibration-v1.0"
METADATA_ENRICHMENT_MAX_RETRYABLE_ATTEMPTS = 3
METADATA_ENRICHMENT_TERMINAL_PROVIDER_STATUSES = {
    "success",
    "no-results",
    "no_results",
    "source_success",
    "source_no_results",
}
METADATA_ENRICHMENT_RETRYABLE_PROVIDER_STATUSES = {
    "configuration-required",
    "configuration_required",
    "failed",
    "not-run",
    "not_run",
    "partial",
    "rate-limited",
    "rate_limited",
    "source_failed",
    "source_not_run",
    "source_partial",
    "source_rate_limited",
}
PENDING_DOWNLOAD_STATES = {
    "pending",
    "claimed",
    "checking_local_inventory",
    "downloading",
    "failed_retryable",
}
TERMINAL_DOWNLOAD_STATES = {
    "succeeded",
    "skipped_existing",
    "failed_terminal",
    "cancelled",
    "eligibility_revoked",
    "dead_letter",
}
TERM_MINING_STOPWORDS = {
    "about",
    "after",
    "all",
    "also",
    "analysis",
    "and",
    "approach",
    "are",
    "article",
    "associated",
    "based",
    "between",
    "concern",
    "data",
    "different",
    "detected",
    "determination",
    "deterministic",
    "during",
    "effect",
    "effects",
    "estimates",
    "for",
    "from",
    "friendly",
    "has",
    "have",
    "impact",
    "improving",
    "into",
    "iteration",
    "its",
    "may",
    "mock",
    "need",
    "nontarget",
    "not",
    "of",
    "on",
    "or",
    "our",
    "potential",
    "present",
    "process",
    "program",
    "programs",
    "real",
    "reconciling",
    "retrieval",
    "results",
    "screening",
    "study",
    "the",
    "their",
    "these",
    "this",
    "using",
    "was",
    "were",
    "whole",
    "with",
}


@dataclass(frozen=True)
class IterationPlan:
    """Deterministic mock query-variant behavior for Phase 1.1."""

    query_id: str
    parent_query_id: str | None
    branch_id: str
    acceptance_status: str
    decision: str
    decision_reason: str
    added_terms: list[str]
    removed_terms: list[str]
    modified_blocks: list[str]
    expected_effect: str
    saturation_override: str | None = None


@dataclass(frozen=True)
class IterationResult:
    """Small result returned after each iteration scope exits."""

    query_id: str
    iteration: int
    total_score: float | None
    score_delta: float | None
    decision: str
    saturation_status: str
    row_counts: dict[str, int]


@dataclass(frozen=True)
class LiveAcceptanceReference:
    """Parent metrics used by deterministic live query acceptance."""

    score: float
    novel_precision_at_20: float
    defer_rate: float
    excluded_matrix_rate: float
    source_completeness: str


@dataclass(frozen=True)
class CandidateQueryPlan:
    """Applied live query candidate awaiting limited novelty evaluation."""

    query: CanonicalQuery
    plan: IterationPlan
    patch_result: Any
    limited_evaluation: dict[str, Any] | None = None


@dataclass(frozen=True)
class CandidateEvaluationPlan:
    """Sampling policy for a live QueryPatch candidate."""

    patch_type: str
    gain_target_records: int
    loss_audit_target_records: int


class MockQueryVariantGenerator:
    """Deterministic variant generator with accept/reject/rollback/saturation branches."""

    def plan(self, iteration: int) -> IterationPlan:
        plans = {
            1: IterationPlan(
                query_id="Q0001",
                parent_query_id=None,
                branch_id="main",
                acceptance_status="accepted",
                decision="accept",
                decision_reason="Initial protocol-derived query accepted as baseline.",
                added_terms=[],
                removed_terms=[],
                modified_blocks=[],
                expected_effect="Establish the baseline novelty frontier.",
            ),
            2: IterationPlan(
                query_id="Q0002",
                parent_query_id="Q0001",
                branch_id="main",
                acceptance_status="accepted",
                decision="accept",
                decision_reason="Accepted deterministic expansion with improved mock yield.",
                added_terms=["estuary"],
                removed_terms=[],
                modified_blocks=["surface_water_terms"],
                expected_effect="Broaden surface-water scope without adding matrix exclusions.",
            ),
            3: IterationPlan(
                query_id="Q0003",
                parent_query_id="Q0002",
                branch_id="candidate-noise",
                acceptance_status="rejected",
                decision="reject",
                decision_reason="Rejected candidate branch because ineligible overlap increased.",
                added_terms=["risk assessment"],
                removed_terms=[],
                modified_blocks=["monitoring_and_concentration_terms"],
                expected_effect="Test whether risk wording discovers eligible monitoring papers.",
            ),
            4: IterationPlan(
                query_id="Q0004",
                parent_query_id="Q0002",
                branch_id="rollback-to-accepted",
                acceptance_status="rollback",
                decision="rollback",
                decision_reason="Rolled back to Q0002 and continued after rejected Q0003.",
                added_terms=[],
                removed_terms=["risk assessment"],
                modified_blocks=["monitoring_and_concentration_terms"],
                expected_effect="Restore the last accepted query frontier.",
            ),
            5: IterationPlan(
                query_id="Q0005",
                parent_query_id="Q0002",
                branch_id="saturated-narrow",
                acceptance_status="accepted",
                decision="accept",
                decision_reason="Accepted narrow branch, then stopped by saturation detector.",
                added_terms=["reservoir"],
                removed_terms=[],
                modified_blocks=["surface_water_terms"],
                expected_effect="Probe remaining reservoir-specific novelty.",
                saturation_override="saturated_narrow",
            ),
        }
        if iteration in plans:
            return plans[iteration]
        parent = f"Q{iteration - 1:04d}" if iteration > 1 else None
        return IterationPlan(
            query_id=f"Q{iteration:04d}",
            parent_query_id=parent,
            branch_id="stress",
            acceptance_status="accepted",
            decision="accept",
            decision_reason="Accepted deterministic stress-test continuation.",
            added_terms=[f"stress-term-{iteration}"],
            removed_terms=[],
            modified_blocks=["surface_water_terms"],
            expected_effect="Exercise bounded-memory multi-iteration behavior.",
        )

    def build_query(
        self,
        protocol: dict[str, Any],
        date_to: str,
        iteration: int,
        date_from: str | None = None,
    ) -> CanonicalQuery:
        base = QueryPlanner().build_initial_query(
            protocol, date_to, date_from=date_from
        )
        plan = self.plan(iteration)
        return replace(
            base,
            query_id=plan.query_id,
            parent_query_id=plan.parent_query_id,
            iteration=iteration,
            added_terms=plan.added_terms,
            removed_terms=plan.removed_terms,
            modified_concept_blocks=plan.modified_blocks,
            candidate_expansion_terms=plan.added_terms,
            prohibited_or_rejected_terms=plan.removed_terms,
            change_rationale=plan.decision_reason,
            expected_effect=plan.expected_effect,
            created_at=utc_now_iso(),
        )


class Phase11Runner:
    """Persistent bounded-memory runner used by RunManager for Phase 1.1."""

    def __init__(
        self,
        *,
        repo_root: Path,
        config_dir: Path,
        runs_dir: Path,
        paper_exports_dir: Path,
        db_path: Path,
    ) -> None:
        self.repo_root = repo_root
        self.config_dir = config_dir
        self.runs_dir = runs_dir
        self.paper_exports_dir = paper_exports_dir
        self.db_path = db_path
        self.variant_generator = MockQueryVariantGenerator()
        self.normalizer = MetadataNormalizer()
        self.screener = TitleAbstractScreener()
        self._git_sha_value = self._git(["rev-parse", "HEAD"], default="unknown")
        self._git_branch_value = self._git(["branch", "--show-current"], default="unknown")
        git_status = self._git(["status", "--porcelain"], default="")
        self._git_dirty_value = "dirty" if git_status.strip() else "clean"
        self._prompt_hash_value: str | None = None
        self._screening_schema: dict[str, Any] | None = None
        self._download_schema: dict[str, Any] | None = None
        self._failure_operator_count = 0
        self._failure_batch_count = 0
        self._failure_source_page_count = 0

    def run(
        self,
        *,
        date_from: str | None = None,
        date_to: str,
        run_id: str | None = None,
        target_novel_records: int | None = None,
        max_scan_depth: int | None = None,
        max_iterations: int | None = None,
        resume: bool = False,
        fail_after_operator: str | None = None,
        fail_after_batch: int | None = None,
        fail_after_source_page: int | None = None,
        stress_records_per_source: int | None = None,
        live_mode: bool = False,
        providers: list[str] | None = None,
        force_max_iterations: bool = False,
    ) -> dict[str, Any]:
        loaded = ProtocolLoader(self.config_dir).load()
        runtime = self._runtime_settings(loaded)
        if max_iterations is None:
            max_iterations = int(loaded.stopping["iterations"]["max_iterations"])
        if target_novel_records is None:
            target_novel_records = int(
                loaded.stopping["evaluation"]["target_novel_records_total"]
            )
        if max_scan_depth is None:
            max_scan_depth = int(loaded.stopping["evaluation"]["max_scan_depth_per_source"])

        run_id = run_id or self._new_run_id()
        run_dir = self._prepare_run_dir(run_id)
        checkpoints = CheckpointManager(run_dir)
        plane = ControlPlane(self.db_path, self._git_sha())
        plane.migrate()
        self._write_snapshots(run_dir, loaded)
        manifest = self._manifest(run_id, date_to, loaded, runtime, date_from=date_from)
        if live_mode:
            manifest["run_mode"] = "live_retrieval"
            manifest["live_providers"] = providers or SOURCE_NAMES
            manifest["force_max_iterations"] = force_max_iterations
            manifest["adapter_versions"][
                "external_metadata_discovery_v1"
            ] = "file_based_skill_boundary"
            manifest["model_names"] = ["codex-gpt"]
            manifest["model_parameters"] = {
                "llm_enabled": True,
                "screening_execution": "durable_file_based_worker_request",
                "screening_isolation": "one_document_per_conversation",
            }
            manifest["runtime_source_health_mode"] = "external_metadata_discovery_v1"
        if resume and (run_dir / "manifest.json").exists():
            manifest = manifest | dict(read_json(run_dir / "manifest.json")) | {
                "run_status": "running",
                "resume_requested_at": utc_now_iso(),
            }
        write_json_atomic(run_dir / "manifest.json", manifest)
        manifest["configured_max_iterations"] = max_iterations
        manifest["configured_target_novel_records"] = target_novel_records
        manifest["configured_max_scan_depth"] = max_scan_depth
        write_json_atomic(run_dir / "manifest.json", manifest)
        self._upsert_run(run_id, date_to, loaded, manifest)

        first_iteration = self._next_iteration(run_id)
        if first_iteration > max_iterations:
            return {"run_id": run_id, "status": "already_completed", "run_dir": str(run_dir)}

        machine = StateMachine(run_dir)
        if not resume:
            machine.transition(RetrievalState.LOAD_PROTOCOL)
            checkpoints.save("LOAD_PROTOCOL", {"config_hash": loaded.config_hash})
        else:
            append_jsonl(
                run_dir / "logs" / "decision_log.jsonl",
                {"timestamp": utc_now_iso(), "decision": "resume", "run_id": run_id},
            )
            machine.transition(RetrievalState.LOAD_PROTOCOL)
            checkpoints.save("LOAD_PROTOCOL", {"config_hash": loaded.config_hash, "resume": True})

        latest_result: IterationResult | None = None
        for iteration in range(first_iteration, max_iterations + 1):
            pause = self._memory_pause_if_needed(run_id, runtime)
            if pause is not None:
                checkpoints.save("PAUSED_MEMORY_LIMIT", pause)
                self._write_manifest_update(
                    run_dir,
                    manifest,
                    self._paused_manifest_update(run_id, "paused_memory_limit"),
                )
                self._snapshot_run_db(run_dir)
                return {"run_id": run_id, "run_dir": str(run_dir), **pause}

            try:
                query, plan = self._query_for_iteration(
                    run_id=run_id,
                    run_dir=run_dir,
                    loaded=loaded,
                    date_from=date_from,
                    date_to=date_to,
                    iteration=iteration,
                    live_mode=live_mode,
                )
            except QueryRefinementBlocked as exc:
                checkpoints.save("PROPOSE_VARIANTS", exc.payload)
                status = str(exc.payload["status"])
                if status in {"saturated_narrow", "saturated_noise", "saturated_success"}:
                    self._write_manifest_update(
                        run_dir,
                        manifest,
                        {"run_status": status, "run_completeness": "complete"},
                    )
                    self._rebuild_audit_files(run_id, run_dir)
                    self._snapshot_run_db(run_dir)
                    return {
                        "run_id": run_id,
                        "run_dir": str(run_dir),
                        **exc.payload,
                    }
                manifest_update = (
                    self._paused_manifest_update(run_id, status)
                    if status.startswith("paused_")
                    else {"run_status": status}
                )
                self._write_manifest_update(
                    run_dir,
                    manifest,
                    manifest_update,
                )
                self._snapshot_run_db(run_dir)
                return {
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    **exc.payload,
                }
            result = self._execute_iteration(
                run_id=run_id,
                run_dir=run_dir,
                query=query,
                plan=plan,
                loaded=loaded,
                runtime=runtime,
                target_novel_records=target_novel_records,
                max_scan_depth=max_scan_depth,
                machine=machine,
                checkpoints=checkpoints,
                fail_after_operator=fail_after_operator,
                fail_after_batch=fail_after_batch,
                fail_after_source_page=fail_after_source_page,
                stress_records_per_source=stress_records_per_source,
                stress_mode=stress_records_per_source is not None,
                live_mode=live_mode,
                providers=providers,
            )
            if result.saturation_status == "interrupted_injected":
                self._write_manifest_update(
                    run_dir, manifest, {"run_status": "interrupted_injected"}
                )
                self._snapshot_run_db(run_dir)
                return {
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "status": "interrupted_injected",
                    "query_id": query.query_id,
                    "iteration": iteration,
                }
            if result.saturation_status == "paused_downstream_backpressure":
                self._write_manifest_update(
                    run_dir,
                    manifest,
                    self._paused_manifest_update(
                        run_id, "paused_downstream_backpressure"
                    ),
                )
                self._snapshot_run_db(run_dir)
                return {
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "status": "paused_downstream_backpressure",
                    "query_id": query.query_id,
                    "iteration": iteration,
                }
            if result.saturation_status.startswith("paused_"):
                self._write_manifest_update(
                    run_dir,
                    manifest,
                    self._paused_manifest_update(run_id, result.saturation_status),
                )
                self._snapshot_run_db(run_dir)
                return {
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "status": result.saturation_status,
                    "query_id": query.query_id,
                    "iteration": iteration,
                }
            latest_result = result
            release_iteration_memory(result)
            saturated = result.saturation_status in {
                "saturated_success",
                "saturated_narrow",
                "saturated_noise",
            }
            if iteration < max_iterations and (
                not saturated
                or stress_records_per_source is not None
                or force_max_iterations
            ):
                machine.transition(RetrievalState.BUILD_QUERY)
                continue
            break

        if latest_result is None:
            raise RuntimeError("No iteration result was produced.")
        machine.transition(RetrievalState.STOP)
        checkpoints.save("STOP", {"run_status": "completed", "run_id": run_id})
        final_completeness = (
            self._latest_source_completeness(run_id) if live_mode else "complete"
        )
        if final_completeness == "unknown":
            final_completeness = "failed" if live_mode else "complete"
        self._complete_run(run_id, latest_result, completeness=final_completeness)
        if stress_records_per_source is None or self._path_is_relative_to(
            self.runs_dir, self.repo_root
        ):
            self._rebuild_audit_files(run_id, run_dir)
            export_result = self.export_paper_data(run_id)
            self._write_run_summary(run_dir, latest_result)
            self._update_status_files(run_id, latest_result, export_result)
            self._snapshot_run_db(run_dir)
        self._write_manifest_update(
            run_dir,
            manifest,
            {
                "end_time": utc_now_iso(),
                "run_status": "completed",
                "run_completeness": final_completeness,
                "final_query_id": latest_result.query_id,
                "final_iteration": latest_result.iteration,
            },
        )
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "status": "completed",
            "query_id": latest_result.query_id,
            "iterations": latest_result.iteration,
            "total_score": latest_result.total_score,
            "saturation_status": latest_result.saturation_status,
        }

    def discovery_dry_run(
        self,
        *,
        date_to: str,
        date_from: str | None = None,
        max_records_per_provider: int = 5,
        run_id: str | None = None,
        providers: list[str] | None = None,
    ) -> dict[str, Any]:
        loaded = ProtocolLoader(self.config_dir).load()
        runtime = self._runtime_settings(loaded)
        run_id = run_id or self._new_run_id(prefix="retrieval_live_discovery")
        run_dir = self._prepare_run_dir(run_id)
        plane = ControlPlane(self.db_path, self._git_sha())
        plane.migrate()
        self._write_snapshots(run_dir, loaded)
        manifest = self._manifest(run_id, date_to, loaded, runtime, date_from=date_from)
        manifest["run_mode"] = "live_discovery_dry_run"
        manifest["runtime_source_health_mode"] = "external_metadata_discovery_v1"
        manifest["live_providers"] = providers or SOURCE_NAMES
        manifest["model_parameters"] = {"llm_enabled": False}
        manifest["adapter_versions"][
            "external_metadata_discovery_v1"
        ] = "file_based_skill_boundary"
        write_json_atomic(run_dir / "manifest.json", manifest)
        self._upsert_run(run_id, date_to, loaded, manifest)

        query = QueryPlanner().build_initial_query(
            loaded.protocol, date_to, date_from=date_from
        )
        plan = IterationPlan(
            query_id=query.query_id,
            parent_query_id=None,
            branch_id="live-discovery-dry-run",
            acceptance_status="accepted",
            decision="accept",
            decision_reason="Discovery dry run baseline query.",
            added_terms=[],
            removed_terms=[],
            modified_blocks=[],
            expected_effect="Validate ECfinder external metadata gateway.",
        )
        query_dir = ensure_dir(run_dir / "queries" / query.query_id)
        self._start_query_iteration(run_id, query, plan, loaded)
        self._write_query_artifacts(run_id, query_dir, query, plan, None)
        self._compile_query(query_dir, query)
        page_size = min(
            int(runtime["retrieval_page_size"]),
            max(1, max_records_per_provider),
        )
        max_scan_depth = max(
            1, (max_records_per_provider + page_size - 1) // page_size
        )
        bounded_runtime = dict(runtime)
        bounded_runtime["retrieval_page_size"] = page_size
        summary = self._search_sources_external(
            run_id=run_id,
            run_dir=run_dir,
            query=query,
            runtime=bounded_runtime,
            max_scan_depth=max_scan_depth,
            providers=providers or SOURCE_NAMES,
            max_records_per_provider=max_records_per_provider,
        )
        normalized_count = self._normalize_and_register(
            run_id=run_id,
            run_dir=run_dir,
            query=query,
            batch_size=int(runtime["normalization_batch_size"]),
            target_novel_records=max_records_per_provider * len(providers or SOURCE_NAMES),
            fail_after_batch=None,
        )
        duplicate_count = self._duplicate_count(run_id, query.query_id)
        manifest["run_status"] = "completed"
        manifest["run_completeness"] = self._source_completeness_from_statuses(summary)
        manifest["source_health_status"] = summary["source_statuses"]
        manifest["normalized_record_count"] = normalized_count
        manifest["duplicate_record_count"] = duplicate_count
        manifest["end_time"] = utc_now_iso()
        write_json_atomic(run_dir / "manifest.json", manifest)
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = 'completed',
                    completeness = ?,
                    completed_at = ?,
                    source_status_json = ?
                WHERE run_id = ?
                """,
                (
                    str(manifest["run_completeness"]),
                    utc_now_iso(),
                    json.dumps(summary["source_statuses"], sort_keys=True),
                    run_id,
                ),
            )
        self._rebuild_audit_files(run_id, run_dir)
        self._snapshot_run_db(run_dir)
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "status": "completed",
            "mode": "live_discovery_dry_run",
            "query_id": query.query_id,
            "date_from": query.date_from,
            "normalized_record_count": normalized_count,
            "duplicate_record_count": duplicate_count,
            **summary,
        }

    def resume(self, run_id: str) -> dict[str, Any]:
        run_dir = self.runs_dir / run_id
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Missing run manifest for resume: {run_id}")
        manifest = read_json(manifest_path)
        if manifest.get("run_status") == "completed":
            return {"run_id": run_id, "status": "already_completed", "run_dir": str(run_dir)}
        providers = manifest.get("live_providers")
        return self.run(
            date_from=str(manifest["date_from"]) if manifest.get("date_from") else None,
            date_to=str(manifest["date_to"]),
            run_id=run_id,
            resume=True,
            max_iterations=int(manifest.get("configured_max_iterations", 5)),
            target_novel_records=int(
                manifest.get("configured_target_novel_records", 20)
            ),
            max_scan_depth=int(manifest.get("configured_max_scan_depth", 1)),
            live_mode=self._manifest_is_live(manifest),
            providers=providers if isinstance(providers, list) else None,
            force_max_iterations=bool(manifest.get("force_max_iterations")),
        )

    def _manifest_is_live(self, manifest: dict[str, Any]) -> bool:
        if manifest.get("runtime_source_health_mode") == "mock_no_external_api_calls":
            return False
        if manifest.get("run_mode") == "live_retrieval":
            return True
        if manifest.get("runtime_source_health_mode") == "external_metadata_discovery_v1":
            return True
        adapter_versions = dict(manifest.get("adapter_versions") or {})
        if adapter_versions.get("external_metadata_discovery_v1") in {
            "ecfinder_live",
            "file_based_skill_boundary",
        }:
            return True
        model_parameters = dict(manifest.get("model_parameters") or {})
        return bool(model_parameters.get("llm_enabled"))

    def inspect(self, run_id: str) -> dict[str, Any]:
        run_dir = self.runs_dir / run_id
        plane = ControlPlane(self.db_path, self._git_sha())
        status = plane.status() if self.db_path.exists() else {"status": "missing"}
        latest = CheckpointManager(run_dir).latest()
        with plane.connect() as connection:
            iterations = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT iteration, query_id, parent_query_id, acceptance_status,
                           score, score_delta, saturation_status, finalized_at
                    FROM query_iterations
                    WHERE run_id = ?
                    ORDER BY iteration
                    """,
                    (run_id,),
                )
            ]
        return {
            "manifest": read_json(run_dir / "manifest.json"),
            "latest_checkpoint": latest,
            "database": status,
            "iterations": iterations,
        }

    def rollback(self, run_id: str, query_id: str) -> dict[str, Any]:
        run_dir = self.runs_dir / run_id
        rollback = CheckpointManager(run_dir).rollback_to_query(query_id)
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO audit_events (
                    audit_event_id, run_id, query_id, global_record_id, event_type,
                    payload_json, actor, created_at
                )
                VALUES (?, ?, ?, NULL, 'query_rollback', ?, 'Retrieval Specialist', ?)
                """,
                (
                    f"rollback:{run_id}:{query_id}",
                    run_id,
                    query_id,
                    json.dumps(rollback, sort_keys=True, ensure_ascii=True),
                    utc_now_iso(),
                ),
            )
        return rollback

    def export_paper_data(self, run_id: str) -> dict[str, Any]:
        run_dir = self.runs_dir / run_id
        self._rebuild_audit_files(run_id, run_dir)
        export_dirs = [self.paper_exports_dir, run_dir / "exports"]
        rows = self._export_rows(run_id)
        for export_dir in export_dirs:
            ensure_dir(export_dir)
            self._write_export_tables(export_dir, rows, run_id)
        manifest = self._write_export_manifest(run_id, run_dir / "exports")
        self._write_export_manifest(run_id, self.paper_exports_dir)
        return {
            "run_id": run_id,
            "rebuilt_exports": str(run_dir / "exports"),
            "export_manifest": str(manifest),
            "row_counts": {name: len(value) for name, value in rows.items()},
        }

    def evaluate_audit_pool(
        self, run_id: str, audit_pool_path: Path | None = None
    ) -> dict[str, Any]:
        run_dir = self.runs_dir / run_id
        path = audit_pool_path or self.config_dir / "audit_pool.yaml"
        evaluator = AuditPoolEvaluator.from_yaml(path)
        rows = evaluator.evaluate(self._audit_pool_document_rows(run_id))
        for export_dir in [self.paper_exports_dir, run_dir / "exports"]:
            ensure_dir(export_dir)
            self._write_audit_pool_exports(export_dir, rows)
        manifest = self._write_export_manifest(run_id, run_dir / "exports")
        self._write_export_manifest(run_id, self.paper_exports_dir)
        summary = rows["audit_pool_recall_summary"]
        overall = next((row for row in summary if row["role"] == "all"), {})
        return {
            "run_id": run_id,
            "audit_pool": self._display_path(path),
            "exports": self._display_path(run_dir / "exports"),
            "export_manifest": self._display_path(manifest),
            "total": overall.get("total", 0),
            "retrieved": overall.get("retrieved", 0),
            "missing": overall.get("missing", 0),
            "recall": overall.get("recall", ""),
        }

    def evaluate_candidate_pool_audit(
        self, *, pool_id: str, audit_pool_path: Path | None = None
    ) -> dict[str, Any]:
        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        records_ref = pool_root / "records.jsonl"
        if not records_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool records: {records_ref}")
        path = audit_pool_path or self.config_dir / "audit_pool.yaml"
        evaluator = AuditPoolEvaluator.from_yaml(path)
        rows = evaluator.evaluate(self._candidate_pool_audit_document_rows(pool_id))
        export_dir = ensure_dir(self.paper_exports_dir / pool_id)
        self._write_audit_pool_exports(export_dir, rows)
        write_json_atomic(export_dir / "candidate_pool_audit_recall.json", rows)
        self._write_candidate_pool_audit_summary_markdown(
            export_dir=export_dir,
            pool_id=pool_id,
            audit_pool_path=path,
            rows=rows,
        )
        summary = rows["audit_pool_recall_summary"]
        overall = next((row for row in summary if row["role"] == "all"), {})
        return {
            "pool_id": pool_id,
            "status": "completed",
            "audit_pool": self._display_path(path),
            "total": overall.get("total", 0),
            "retrieved": overall.get("retrieved", 0),
            "missing": overall.get("missing", 0),
            "recall": overall.get("recall", 0.0),
            "export_dir": self._display_path(export_dir),
            "recall_ref": self._display_path(export_dir / "audit_pool_recall.csv"),
            "summary_ref": self._display_path(export_dir / "audit_pool_recall_summary.csv"),
            "markdown_ref": self._display_path(
                export_dir / "CANDIDATE_POOL_AUDIT_RECALL.md"
            ),
        }

    def build_candidate_pool(
        self, *, source_run_ids: list[str], pool_id: str | None = None
    ) -> dict[str, Any]:
        if not source_run_ids:
            raise ValueError("At least one --source-run-id is required")
        pool_id = pool_id or self._new_run_id("retrieval_candidate_pool")
        pool_dir = self._prepare_run_dir(pool_id)
        ensure_dir(pool_dir / "candidate_pool")
        source_runs = [self._candidate_pool_source_run(run_id) for run_id in source_run_ids]
        records = self._candidate_pool_records(source_runs)
        unique_records, duplicate_links = self._deduplicate_candidate_pool_records(records)
        provider_counts = self._candidate_pool_counter(records, "source_provider")
        family_counts = self._candidate_pool_counter(records, "query_family")
        write_text_atomic(
            pool_dir / "candidate_pool" / "records.jsonl",
            "\n".join(
                json.dumps(record, ensure_ascii=True, sort_keys=True)
                for record in unique_records
            )
            + ("\n" if unique_records else ""),
        )
        write_text_atomic(
            pool_dir / "candidate_pool" / "duplicate_links.jsonl",
            "\n".join(
                json.dumps(link, ensure_ascii=True, sort_keys=True)
                for link in duplicate_links
            )
            + ("\n" if duplicate_links else ""),
        )
        manifest = {
            "run_id": pool_id,
            "run_mode": "candidate_pool_union",
            "run_status": "completed",
            "run_completeness": "complete",
            "source_run_ids": source_run_ids,
            "source_runs": source_runs,
            "raw_candidate_count": len(records),
            "deduplicated_candidate_count": len(unique_records),
            "duplicate_link_count": len(duplicate_links),
            "provider_counts": provider_counts,
            "query_family_counts": family_counts,
            "screening_status": "not_started",
            "query_refinement_status": "not_started",
            "pdf_download_status": "not_started",
            "created_at": utc_now_iso(),
            "code_commit_sha": self._git_sha(),
            "git_branch": self._git_branch(),
            "dirty_worktree_status": self._git_dirty_status(),
        }
        write_json_atomic(pool_dir / "candidate_pool" / "manifest.json", manifest)
        self._write_candidate_pool_summary(pool_dir, manifest, unique_records)
        return {
            "pool_id": pool_id,
            "status": "completed",
            "run_dir": str(pool_dir),
            "raw_candidate_count": len(records),
            "deduplicated_candidate_count": len(unique_records),
            "duplicate_link_count": len(duplicate_links),
            "provider_counts": provider_counts,
            "query_family_counts": family_counts,
            "records_ref": self._display_path(pool_dir / "candidate_pool" / "records.jsonl"),
            "manifest_ref": self._display_path(pool_dir / "candidate_pool" / "manifest.json"),
        }

    def screen_candidate_pool(
        self,
        *,
        pool_id: str,
        max_records: int | None = None,
        providers: list[str] | None = None,
        query_families: list[str] | None = None,
    ) -> dict[str, Any]:
        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        records_ref = pool_root / "records.jsonl"
        if not records_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool records: {records_ref}")
        manifest_ref = pool_root / "manifest.json"
        manifest = dict(read_json(manifest_ref)) if manifest_ref.exists() else {}
        protocol = self._candidate_pool_screening_protocol()
        executor = TitleAbstractScreeningWorkerExecutor(
            repo_root=self.repo_root,
            run_dir=pool_dir,
            schema_path=self._schema_path("retrieval", "screening_decision.schema.json"),
        )
        already_screened_ids = {
            str(row["global_record_id"])
            for row in self._read_jsonl_records(pool_root / "screening_decisions.jsonl")
            if row.get("global_record_id")
        }
        already_requested_ids = {
            str(row["global_record_id"])
            for row in self._read_jsonl_records(pool_root / "screening_worker_requests.jsonl")
            if row.get("global_record_id")
        }
        screened = 0
        requested = 0
        pending_worker_results = 0
        decisions: list[dict[str, Any]] = []
        provider_filter = {str(provider) for provider in providers or []}
        family_filter = {str(family) for family in query_families or []}
        candidate_records = [
            record_payload
            for record_payload in self._candidate_pool_records_with_metadata_enrichment(
                pool_root=pool_root,
                records=self._read_jsonl_records(records_ref),
            )
            if self._candidate_pool_record_matches_filters(
                record_payload,
                provider_filter=provider_filter,
                family_filter=family_filter,
            )
        ]
        for record_payload in self._prioritized_candidate_pool_screening_records(
            candidate_records
        ):
            if max_records is not None and screened + pending_worker_results >= max_records:
                break
            record = self._candidate_pool_normalized_record(record_payload)
            if record.global_record_id in already_screened_ids:
                continue
            try:
                decision = executor.screen_one(
                    record,
                    run_id=pool_id,
                    query_id="CANDIDATE_POOL",
                    iteration=0,
                    audit_batch_id=(
                        f"pool_screening_{screened + pending_worker_results + 1:05d}"
                    ),
                    protocol=protocol,
                    allowed_reason_codes=self._allowed_reason_codes(),
                    scie_status="unknown",
                )
            except ScreeningWorkerBlocked as exc:
                pending_worker_results += 1
                if record.global_record_id not in already_requested_ids:
                    append_jsonl(
                        pool_root / "screening_worker_requests.jsonl",
                        {
                            "global_record_id": record.global_record_id,
                            "request_ref": exc.payload.get("request_ref"),
                            "result_ref": exc.payload.get("result_ref"),
                            "status": exc.payload.get("status"),
                            "query_families": record_payload.get("query_families", []),
                            "source_providers": record_payload.get("source_providers", []),
                        },
                    )
                    already_requested_ids.add(record.global_record_id)
                    requested += 1
                continue
            self._validate_screening_decision(decision)
            decision_row = decision.to_dict() | {
                "query_families": record_payload.get("query_families", []),
                "source_providers": record_payload.get("source_providers", []),
                "metadata_enrichment_applied": bool(
                    record_payload.get("_metadata_enrichment_applied")
                ),
                "metadata_enrichment_provider": record_payload.get(
                    "_metadata_enrichment_provider"
                ),
            }
            append_jsonl(pool_root / "screening_decisions.jsonl", decision_row)
            already_screened_ids.add(record.global_record_id)
            decisions.append(decision_row)
            screened += 1
        self._write_candidate_pool_screening_summary(
            pool_root=pool_root,
            manifest=manifest,
            new_decisions=decisions,
        )
        return {
            "pool_id": pool_id,
            "status": (
                "paused_screening_worker_required"
                if pending_worker_results
                else "completed"
            ),
            "screened_decisions": screened,
            "worker_requests_created": requested,
            "pending_worker_results": pending_worker_results,
            "screening_requests_ref": self._display_path(
                pool_root / "screening_worker_requests.jsonl"
            ),
            "screening_decisions_ref": self._display_path(
                pool_root / "screening_decisions.jsonl"
            ),
            "summary_ref": self._display_path(pool_root / "screening_summary.json"),
            "isolation": "one_document_per_conversation",
            "provider_filter": sorted(provider_filter),
            "query_family_filter": sorted(family_filter),
        }

    def reuse_candidate_pool_screening(
        self, *, pool_id: str, source_pool_ids: list[str]
    ) -> dict[str, Any]:
        """Copy prior candidate-pool decisions by stable candidate-pool key."""

        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        records_ref = pool_root / "records.jsonl"
        if not records_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool records: {records_ref}")
        if not source_pool_ids:
            raise ValueError("At least one source pool id is required")
        records_by_key = {
            str(record.get("candidate_pool_key")): record
            for record in self._read_jsonl_records(records_ref)
            if record.get("candidate_pool_key")
        }
        already_screened = {
            str(row.get("global_record_id"))
            for row in self._read_jsonl_records(pool_root / "screening_decisions.jsonl")
            if row.get("global_record_id")
        }
        reused = 0
        missing_source_decision_files: list[str] = []
        source_counts: dict[str, int] = {}
        for source_pool_id in source_pool_ids:
            source_ref = (
                self.runs_dir
                / source_pool_id
                / "candidate_pool"
                / "screening_decisions.jsonl"
            )
            if not source_ref.exists():
                missing_source_decision_files.append(self._display_path(source_ref))
                continue
            source_count = 0
            for decision in self._read_jsonl_records(source_ref):
                key = str(decision.get("global_record_id") or "")
                if key not in records_by_key or key in already_screened:
                    continue
                record = records_by_key[key]
                decision_row = dict(decision) | {
                    "run_id": pool_id,
                    "query_id": "CANDIDATE_POOL",
                    "query_families": record.get("query_families", []),
                    "source_providers": record.get("source_providers", []),
                    "candidate_pool_reuse_source": source_pool_id,
                    "candidate_pool_reuse_policy": "candidate_pool_key_exact_match",
                }
                append_jsonl(pool_root / "screening_decisions.jsonl", decision_row)
                already_screened.add(key)
                reused += 1
                source_count += 1
            source_counts[source_pool_id] = source_count
        missing_keys = sorted(set(records_by_key) - already_screened)
        summary = {
            "pool_id": pool_id,
            "status": "completed",
            "records": len(records_by_key),
            "reused_decisions": reused,
            "missing_decisions": len(missing_keys),
            "source_counts": source_counts,
            "missing_source_decision_files": missing_source_decision_files,
            "missing_decision_keys_ref": self._display_path(
                pool_root / "screening_reuse_missing_keys.json"
            ),
        }
        write_json_atomic(pool_root / "screening_reuse_missing_keys.json", missing_keys)
        write_json_atomic(pool_root / "screening_reuse_summary.json", summary)
        return summary

    def safe_defer_candidate_pool_screening(
        self, *, pool_id: str, max_records: int | None = None
    ) -> dict[str, Any]:
        """Create schema-valid safe-defer worker results for unscreened records."""

        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        records_ref = pool_root / "records.jsonl"
        if not records_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool records: {records_ref}")
        manifest_ref = pool_root / "manifest.json"
        manifest = dict(read_json(manifest_ref)) if manifest_ref.exists() else {}
        executor = TitleAbstractScreeningWorkerExecutor(
            repo_root=self.repo_root,
            run_dir=pool_dir,
            schema_path=self._schema_path("retrieval", "screening_decision.schema.json"),
        )
        already_screened = {
            str(row.get("global_record_id"))
            for row in self._read_jsonl_records(pool_root / "screening_decisions.jsonl")
            if row.get("global_record_id")
        }
        created_results = 0
        completed_decisions = 0
        for record_payload in self._candidate_pool_records_with_metadata_enrichment(
            pool_root=pool_root,
            records=self._read_jsonl_records(records_ref),
        ):
            if max_records is not None and created_results >= max_records:
                break
            key = str(record_payload.get("candidate_pool_key") or "")
            if not key or key in already_screened:
                continue
            record = self._candidate_pool_normalized_record(record_payload)
            try:
                decision = executor.screen_one(
                    record,
                    run_id=pool_id,
                    query_id="CANDIDATE_POOL",
                    iteration=0,
                    audit_batch_id=f"pool_safe_defer_{created_results + 1:05d}",
                    protocol=self._candidate_pool_screening_protocol(),
                    allowed_reason_codes=self._allowed_reason_codes(),
                    scie_status="unknown",
                )
            except ScreeningWorkerBlocked as exc:
                result_ref = self.repo_root / str(exc.payload.get("result_ref", ""))
                request_ref = self.repo_root / str(exc.payload.get("request_ref", ""))
                raw_ref = (
                    pool_dir
                    / "screening"
                    / "raw_responses"
                    / "CANDIDATE_POOL"
                    / f"{hashlib.sha256(record.global_record_id.encode()).hexdigest()[:24]}"
                    ".safe_defer.json"
                )
                payload = executor._safe_defer_payload(  # noqa: SLF001
                    record=record,
                    run_id=pool_id,
                    query_id="CANDIDATE_POOL",
                    iteration=0,
                    audit_batch_id=f"pool_safe_defer_{created_results + 1:05d}",
                    scie_status="unknown",
                    raw_ref=raw_ref,
                    schema_error="safe_defer_requested_for_unscreened_candidate_pool_record",
                )
                ensure_dir(result_ref.parent)
                write_json_atomic(result_ref, payload)
                append_jsonl(
                    pool_root / "screening_worker_requests.jsonl",
                    {
                        "global_record_id": record.global_record_id,
                        "request_ref": exc.payload.get("request_ref"),
                        "result_ref": exc.payload.get("result_ref"),
                        "status": "safe_defer_result_created",
                        "query_families": record_payload.get("query_families", []),
                        "source_providers": record_payload.get("source_providers", []),
                    },
                )
                created_results += 1
                if request_ref.exists():
                    decision = executor.screen_one(
                        record,
                        run_id=pool_id,
                        query_id="CANDIDATE_POOL",
                        iteration=0,
                        audit_batch_id=f"pool_safe_defer_{created_results:05d}",
                        protocol=self._candidate_pool_screening_protocol(),
                        allowed_reason_codes=self._allowed_reason_codes(),
                        scie_status="unknown",
                    )
                else:
                    continue
            self._validate_screening_decision(decision)
            decision_row = decision.to_dict() | {
                "query_families": record_payload.get("query_families", []),
                "source_providers": record_payload.get("source_providers", []),
                "candidate_pool_safe_defer": True,
                "metadata_enrichment_applied": bool(
                    record_payload.get("_metadata_enrichment_applied")
                ),
                "metadata_enrichment_provider": record_payload.get(
                    "_metadata_enrichment_provider"
                ),
            }
            append_jsonl(pool_root / "screening_decisions.jsonl", decision_row)
            already_screened.add(record.global_record_id)
            completed_decisions += 1
        self._write_candidate_pool_screening_summary(
            pool_root=pool_root,
            manifest=manifest,
            new_decisions=[],
        )
        return {
            "pool_id": pool_id,
            "status": "completed",
            "safe_defer_results_created": created_results,
            "screening_decisions_completed": completed_decisions,
            "screening_decisions_ref": self._display_path(
                pool_root / "screening_decisions.jsonl"
            ),
            "summary_ref": self._display_path(pool_root / "screening_summary.json"),
            "isolation": "one_document_per_conversation",
        }

    def analyze_candidate_pool(self, *, pool_id: str) -> dict[str, Any]:
        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        records_ref = pool_root / "records.jsonl"
        decisions_ref = pool_root / "screening_decisions.jsonl"
        if not records_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool records: {records_ref}")
        manifest_ref = pool_root / "manifest.json"
        manifest = dict(read_json(manifest_ref)) if manifest_ref.exists() else {}
        raw_records = self._read_jsonl_records(records_ref)
        records = self._candidate_pool_records_with_metadata_enrichment(
            pool_root=pool_root,
            records=raw_records,
        )
        decisions = self._read_jsonl_records(decisions_ref)
        analysis = self._candidate_pool_analysis(
            pool_id=pool_id,
            manifest=manifest,
            records=records,
            decisions=decisions,
            original_records=raw_records,
        )
        export_dir = self.repo_root / "docs" / "retrieval_runs" / "exports" / pool_id
        enrichment_plan = self._candidate_pool_metadata_enrichment_plan(
            pool_id=pool_id,
            records=raw_records,
            decisions=decisions,
            enriched_records=self._read_jsonl_records(
                pool_root / "metadata_enrichment" / "enriched_records.jsonl"
            ),
            enrichment_attempts=self._read_jsonl_records(
                pool_root / "metadata_enrichment" / "enrichment_attempts.jsonl"
            ),
            priority_rows=self._candidate_pool_existing_priority_rows(export_dir),
            limit=len(records),
        )
        completion_assessment = self._candidate_pool_completion_assessment(
            pool_root=pool_root,
            export_dir=export_dir,
            manifest=manifest,
            analysis=analysis,
            enrichment_plan=enrichment_plan,
        )
        analysis["completion_assessment"] = completion_assessment
        analysis["recommended_next_action"] = (
            self._candidate_pool_acceptance_next_action(completion_assessment)
        )
        write_json_atomic(pool_root / "candidate_pool_analysis.json", analysis)
        self._write_candidate_pool_analysis_markdown(pool_root, analysis)
        return {
            "pool_id": pool_id,
            "status": "completed",
            "screened_total": analysis["screening"]["screened_total"],
            "deduplicated_candidate_count": analysis["candidate_pool"][
                "deduplicated_candidate_count"
            ],
            "recommended_next_action": analysis["recommended_next_action"],
            "acceptance_status": analysis["completion_assessment"]["status"],
            "blocking_gates": analysis["completion_assessment"]["blocking_gates"],
            "analysis_ref": self._display_path(pool_root / "candidate_pool_analysis.json"),
            "summary_ref": self._display_path(pool_root / "CANDIDATE_POOL_ANALYSIS.md"),
        }

    def _candidate_pool_completion_assessment(
        self,
        *,
        pool_root: Path,
        export_dir: Path,
        manifest: dict[str, Any],
        analysis: dict[str, Any],
        enrichment_plan: dict[str, Any],
    ) -> dict[str, Any]:
        min_exclude_audit = 60
        min_saturation_family_screened = 20
        required_zero_unique_streak = 3
        audit_rows = self._read_jsonl_records(
            pool_root / "audit_review_decisions.jsonl"
        )
        risky_exclude_rows = [
            row
            for row in audit_rows
            if row.get("audit_category") == "risky_exclude_false_negative"
        ]
        false_negative_rows = [
            row
            for row in risky_exclude_rows
            if row.get("review_outcome") == "false_negative_risk"
            or (
                row.get("provisional_decision") == "exclude"
                and row.get("decision") == "include"
            )
        ]
        family_metrics = dict(analysis.get("query_family_metrics") or {})
        family_order = list(dict(manifest.get("query_family_counts") or {}).keys())
        zero_unique_streak = 0
        saturation_families: list[str] = []
        for family in reversed(family_order):
            metric = dict(family_metrics.get(family) or {})
            if (
                int(metric.get("screened_count") or 0)
                >= min_saturation_family_screened
                and int(metric.get("unique_include_count") or 0) == 0
            ):
                zero_unique_streak += 1
                saturation_families.append(family)
                continue
            break
        build_summary_ref = pool_root / "high_recall_build_summary.json"
        build_summary = (
            dict(read_json(build_summary_ref)) if build_summary_ref.exists() else {}
        )
        pool_build_complete = (
            str(manifest.get("run_status") or build_summary.get("status") or "")
            == "completed"
            and not build_summary.get("failed_source_runs")
        )
        screening = dict(analysis.get("screening") or {})
        enrichment_summary = dict(enrichment_plan.get("summary") or {})
        metadata_outcome_complete = bool(
            enrichment_summary.get("metadata_lookup_outcome_complete")
        )
        export_state = self._candidate_pool_final_export_state(
            pool_id=str(analysis.get("pool_id") or manifest.get("run_id") or ""),
            export_dir=export_dir,
            analysis=analysis,
            audit_review_rows=audit_rows,
        )
        gates = {
            "pool_build_complete": pool_build_complete,
            "screening_complete": int(screening.get("unscreened_total") or 0) == 0,
            "metadata_lookup_complete": metadata_outcome_complete,
            "exclude_audit_complete": len(risky_exclude_rows) >= min_exclude_audit,
            "false_negative_audit_clear": (
                len(risky_exclude_rows) >= min_exclude_audit
                and not false_negative_rows
            ),
            "query_family_saturation_complete": (
                zero_unique_streak >= required_zero_unique_streak
            ),
            "final_export_present": bool(export_state["present"]),
            "final_export_current": bool(export_state["current"]),
        }
        blocking_gates = [name for name, passed in gates.items() if not passed]
        return {
            "policy_version": "high-recall-candidate-pool-acceptance-v3",
            "status": "accepted" if not blocking_gates else "not_ready",
            "gates": gates,
            "blocking_gates": blocking_gates,
            "evidence": {
                "unscreened_records": int(screening.get("unscreened_total") or 0),
                "missing_abstract_records": int(
                    enrichment_summary.get("records_missing_abstract") or 0
                ),
                "pending_identifier_lookups": int(
                    enrichment_summary.get("enrichment_queue_total") or 0
                ),
                "metadata_terminal_outcomes": int(
                    enrichment_summary.get("terminal_missing_abstract") or 0
                ),
                "metadata_retryable_records": int(
                    enrichment_summary.get("retryable_missing_abstract") or 0
                ),
                "metadata_outcomes_accounted": int(
                    enrichment_summary.get("metadata_outcomes_accounted") or 0
                ),
                "metadata_lookup_outcome_complete": metadata_outcome_complete,
                "review_only_missing_abstracts": int(
                    enrichment_summary.get("review_only_missing_abstract") or 0
                ),
                "risky_exclude_audited": len(risky_exclude_rows),
                "false_negative_risks": len(false_negative_rows),
                "consecutive_zero_unique_families": zero_unique_streak,
                "saturation_families": list(reversed(saturation_families)),
                "final_export": export_state,
            },
            "thresholds": {
                "minimum_risky_exclude_audit": min_exclude_audit,
                "minimum_screened_per_saturation_family": (
                    min_saturation_family_screened
                ),
                "required_consecutive_zero_unique_families": (
                    required_zero_unique_streak
                ),
                "metadata_lookup_completion": (
                    "every missing-abstract record has a recovered abstract, a terminal "
                    "provider outcome, or an explicit review-only outcome; no retryable "
                    "records remain"
                ),
                "final_export_completion": (
                    "all required review-export files exist and their source-state counts "
                    "match the current records, screening decisions, audits, and enriched "
                    "metadata"
                ),
            },
            "interpretation": (
                "Acceptance means retrieval construction is complete enough for a final "
                "audited title/abstract candidate corpus. Missing abstracts do not need "
                "to be fabricated, but every such record must have a recorded terminal "
                "metadata outcome before acceptance. PDF review is not included."
            ),
        }

    def _candidate_pool_final_export_state(
        self,
        *,
        pool_id: str,
        export_dir: Path,
        analysis: dict[str, Any],
        audit_review_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        manifest_ref = export_dir / "audit_review_export_manifest.json"
        if not manifest_ref.exists():
            return {
                "present": False,
                "current": False,
                "reason": "manifest_missing",
                "expected": {},
                "actual": {},
            }
        try:
            manifest = dict(read_json(manifest_ref))
        except (OSError, TypeError, ValueError):
            return {
                "present": True,
                "current": False,
                "reason": "manifest_invalid",
                "expected": {},
                "actual": {},
            }
        required_files = {
            "final_included_candidates.csv",
            "include_candidate_needs_review.csv",
            "records_needing_review.csv",
            "resolved_exclude_or_defer.csv",
            "false_negative_risk_review.csv",
        }
        file_refs = dict(manifest.get("files") or {})
        missing_files: list[str] = []
        for filename in sorted(required_files):
            raw_ref = str(file_refs.get(filename) or "")
            path = Path(raw_ref) if raw_ref else export_dir / filename
            if not path.is_absolute():
                path = self.repo_root / path
            if not path.exists():
                missing_files.append(filename)
        if missing_files:
            return {
                "present": False,
                "current": False,
                "reason": "required_export_files_missing",
                "missing_files": missing_files,
                "expected": {},
                "actual": dict(manifest.get("source_state") or {}),
            }
        screening = dict(analysis.get("screening") or {})
        candidate_pool = dict(analysis.get("candidate_pool") or {})
        expected = {
            "screening_decision_rows": int(screening.get("decision_rows_total") or 0),
            "screened_records": int(screening.get("screened_total") or 0),
            "audit_review_decision_rows": len(audit_review_rows),
            "missing_abstracts_after_enrichment": int(
                candidate_pool.get("missing_abstracts") or 0
            ),
            "records_with_metadata_enrichment": int(
                candidate_pool.get("records_with_metadata_enrichment") or 0
            ),
        }
        actual = dict(manifest.get("source_state") or {})
        current = (
            str(manifest.get("pool_id") or "") == str(pool_id)
            and actual == expected
        )
        return {
            "present": True,
            "current": current,
            "reason": "ok" if current else "source_state_mismatch",
            "missing_files": [],
            "expected": expected,
            "actual": actual,
        }

    @staticmethod
    def _candidate_pool_acceptance_next_action(
        completion_assessment: dict[str, Any],
    ) -> str:
        gates = dict(completion_assessment.get("gates") or {})
        ordered_actions = (
            ("pool_build_complete", "repair_or_complete_candidate_pool_build"),
            ("metadata_lookup_complete", "exhaust_identifier_backed_metadata_lookup_queue"),
            ("screening_complete", "screen_all_remaining_candidate_pool_records"),
            ("exclude_audit_complete", "audit_at_least_60_risky_exclude_decisions"),
            (
                "false_negative_audit_clear",
                "resolve_false_negative_findings_and_repeat_exclude_audit",
            ),
            (
                "query_family_saturation_complete",
                "evaluate_additional_query_families_until_saturation",
            ),
            ("final_export_present", "write_final_audited_candidate_export"),
            ("final_export_current", "refresh_final_audited_candidate_export"),
        )
        for gate, action in ordered_actions:
            if not bool(gates.get(gate)):
                return action
        return "candidate_pool_acceptance_complete"

    def plan_query_family_construction(self, *, pool_id: str) -> dict[str, Any]:
        pool_root = self.runs_dir / pool_id / "candidate_pool"
        analysis_ref = pool_root / "candidate_pool_analysis.json"
        if not analysis_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool analysis: {analysis_ref}")
        analysis = dict(read_json(analysis_ref))
        plan = self._query_family_construction_plan(pool_id=pool_id, analysis=analysis)
        export_dir = ensure_dir(
            self.repo_root / "docs" / "retrieval_runs" / "exports" / pool_id
        )
        plan_ref = export_dir / "next_query_family_construction_plan.json"
        summary_ref = export_dir / "NEXT_QUERY_FAMILY_CONSTRUCTION_PLAN.md"
        write_json_atomic(plan_ref, plan)
        self._write_query_family_construction_plan_markdown(summary_ref, plan)
        return {
            "pool_id": pool_id,
            "status": "completed",
            "plan_ref": self._display_path(plan_ref),
            "summary_ref": self._display_path(summary_ref),
            "recommended_next_pool_id": plan["recommended_next_pool_id"],
            "recommended_family_count": len(plan["recommended_next_family_config_dirs"]),
        }

    def plan_candidate_pool_post_review(self, *, pool_id: str) -> dict[str, Any]:
        pool_root = self.runs_dir / pool_id / "candidate_pool"
        export_dir = ensure_dir(
            self.repo_root / "docs" / "retrieval_runs" / "exports" / pool_id
        )
        analysis_ref = pool_root / "candidate_pool_analysis.json"
        review_ref = pool_root / "audit_review_summary.json"
        priority_ref = export_dir / "review_priority" / "review_priority_summary.json"
        if not analysis_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool analysis: {analysis_ref}")
        analysis = dict(read_json(analysis_ref))
        review = dict(read_json(review_ref)) if review_ref.exists() else {}
        priority = dict(read_json(priority_ref)) if priority_ref.exists() else {}
        plan = self._candidate_pool_post_review_plan(
            pool_id=pool_id,
            analysis=analysis,
            review=review,
            priority=priority,
        )
        plan_ref = export_dir / "post_review_action_plan.json"
        summary_ref = export_dir / "POST_REVIEW_ACTION_PLAN.md"
        write_json_atomic(plan_ref, plan)
        self._write_candidate_pool_post_review_plan_markdown(summary_ref, plan)
        return {
            "pool_id": pool_id,
            "status": "completed",
            "plan_ref": self._display_path(plan_ref),
            "summary_ref": self._display_path(summary_ref),
            "recommended_next_action": plan["recommended_next_action"],
        }

    def plan_candidate_pool_metadata_enrichment(
        self, *, pool_id: str, limit: int = 500
    ) -> dict[str, Any]:
        pool_root = self.runs_dir / pool_id / "candidate_pool"
        records_ref = pool_root / "records.jsonl"
        if not records_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool records: {records_ref}")
        export_dir = ensure_dir(
            self.repo_root / "docs" / "retrieval_runs" / "exports" / pool_id
        )
        records = self._read_jsonl_records(records_ref)
        decisions = self._read_jsonl_records(pool_root / "screening_decisions.jsonl")
        enriched_records = self._read_jsonl_records(
            pool_root / "metadata_enrichment" / "enriched_records.jsonl"
        )
        enrichment_attempts = self._read_jsonl_records(
            pool_root / "metadata_enrichment" / "enrichment_attempts.jsonl"
        )
        priority_rows = self._candidate_pool_existing_priority_rows(export_dir)
        plan = self._candidate_pool_metadata_enrichment_plan(
            pool_id=pool_id,
            records=records,
            decisions=decisions,
            enriched_records=enriched_records,
            enrichment_attempts=enrichment_attempts,
            priority_rows=priority_rows,
            limit=limit,
        )
        plan_ref = export_dir / "metadata_enrichment_plan.json"
        summary_ref = export_dir / "METADATA_ENRICHMENT_PLAN.md"
        write_json_atomic(plan_ref, plan)
        self._write_candidate_pool_metadata_enrichment_markdown(summary_ref, plan)
        return {
            "pool_id": pool_id,
            "status": "completed",
            "recommended_next_action": plan["recommended_next_action"],
            "records_missing_abstract": plan["summary"]["records_missing_abstract"],
            "enrichment_queue_size": plan["summary"]["enrichment_queue_total"],
            "enrichment_queue_exported": plan["summary"]["enrichment_queue_exported"],
            "terminal_missing_abstract": plan["summary"]["terminal_missing_abstract"],
            "retryable_missing_abstract": plan["summary"]["retryable_missing_abstract"],
            "metadata_outcomes_accounted": plan["summary"][
                "metadata_outcomes_accounted"
            ],
            "metadata_lookup_outcome_complete": plan["summary"][
                "metadata_lookup_outcome_complete"
            ],
            "plan_ref": self._display_path(plan_ref),
            "summary_ref": self._display_path(summary_ref),
        }

    def enrich_candidate_pool_metadata(
        self, *, pool_id: str, limit: int = 25, batch_size: int = 10
    ) -> dict[str, Any]:
        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        records_ref = pool_root / "records.jsonl"
        if not records_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool records: {records_ref}")
        export_dir = ensure_dir(
            self.repo_root / "docs" / "retrieval_runs" / "exports" / pool_id
        )
        plan_ref = export_dir / "metadata_enrichment_plan.json"
        # Rebuild the queue before every batch so completed attempts from an earlier
        # invocation cannot be repeated from a stale plan.
        self.plan_candidate_pool_metadata_enrichment(pool_id=pool_id, limit=max(limit, 1))
        plan = dict(read_json(plan_ref))
        queue = [
            row
            for row in plan.get("enrichment_queue", [])
            if isinstance(row, dict)
            and row.get("preferred_enrichment_route")
            != "insufficient_identifier_metadata_review_only"
        ][: max(0, limit)]
        if not queue:
            return {
                "pool_id": pool_id,
                "status": "completed",
                "requested_records": 0,
                "enriched_records": 0,
                "reason": "no_identifier_backed_missing_abstract_records",
                "remaining_enrichment_queue": int(
                    plan["summary"].get("enrichment_queue_total") or 0
                ),
                "remaining_missing_abstract": int(
                    plan["summary"].get("records_missing_abstract") or 0
                ),
                "terminal_missing_abstract": int(
                    plan["summary"].get("terminal_missing_abstract") or 0
                ),
                "retryable_missing_abstract": int(
                    plan["summary"].get("retryable_missing_abstract") or 0
                ),
                "metadata_outcomes_accounted": int(
                    plan["summary"].get("metadata_outcomes_accounted") or 0
                ),
                "metadata_lookup_outcome_complete": bool(
                    plan["summary"].get("metadata_lookup_outcome_complete")
                ),
            }
        records_by_key = {
            str(record.get("candidate_pool_key") or ""): record
            for record in self._read_jsonl_records(records_ref)
            if record.get("candidate_pool_key")
        }
        requested_records = [
            records_by_key[str(row["candidate_pool_key"])]
            for row in queue
            if str(row.get("candidate_pool_key") or "") in records_by_key
        ]
        run_root = ensure_dir(pool_root / "metadata_enrichment")
        prior_enrichment_attempts = self._read_jsonl_records(
            run_root / "enrichment_attempts.jsonl"
        )
        gateway = ExternalMetadataDiscoveryGateway(
            repo_root=self.repo_root,
            db_path=self.db_path,
            code_commit_sha=self._git_sha(),
        )
        invocation_summaries: list[dict[str, Any]] = []
        enriched_records: list[dict[str, Any]] = []
        provider_attempts: set[str] = set()
        failed_batches = 0
        retryable_records = 0
        retry_exhausted_records = 0
        reused_completed_batches = 0
        grouped_records: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for record in requested_records:
            provider_key = tuple(
                self._candidate_pool_metadata_enrichment_remaining_providers(
                    record=record,
                    enrichment_attempts=prior_enrichment_attempts,
                )
            )
            if not provider_key:
                append_jsonl(
                    run_root / "enrichment_attempts.jsonl",
                    {
                        "candidate_pool_key": record.get("candidate_pool_key"),
                        "providers": [],
                        "status": "completed_no_remaining_provider",
                        "attempted_at": utc_now_iso(),
                    },
                )
                continue
            grouped_records.setdefault(provider_key, []).append(record)
        batches: list[tuple[tuple[str, ...], list[dict[str, Any]]]] = []
        bounded_batch_size = max(1, batch_size)
        for provider_key, group in grouped_records.items():
            for start in range(0, len(group), bounded_batch_size):
                batches.append((provider_key, group[start : start + bounded_batch_size]))
        for providers_key, batch_records in batches:
            providers = list(providers_key)
            batch_keys = sorted(
                self._normalize_candidate_pool_key(
                    str(record.get("candidate_pool_key") or "")
                )
                for record in batch_records
            )
            batch_id = hashlib.sha256(
                "|".join([*providers, *batch_keys]).encode("utf-8")
            ).hexdigest()[:16]
            output_root = ensure_dir(
                run_root
                / "external_metadata_discovery_v1"
                / f"batch_{batch_id}"
            )
            provider_attempts.update(providers)
            query_id = f"CANDIDATE_POOL_METADATA_ENRICHMENT_{batch_id.upper()}"
            try:
                output = gateway.invoke_metadata_enrichment(
                    run_id=pool_id,
                    run_dir=pool_dir,
                    query_id=query_id,
                    records=batch_records,
                    output_root=output_root,
                    providers=providers,
                    page_size=len(batch_records),
                    config_ref=self.config_dir / "sources.yaml",
                )
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                failed_batches += 1
                source_statuses = dict.fromkeys(providers, "failed")
                invocation_summaries.append(
                    {
                        "batch_id": batch_id,
                        "candidate_pool_keys": batch_keys,
                        "providers": providers,
                        "status": "retryable_failed",
                        "source_statuses": source_statuses,
                        "error_class": type(exc).__name__,
                        "error_message": redact(str(exc)),
                    }
                )
                for record in batch_records:
                    attempt_status = self._metadata_enrichment_attempt_status(
                        record=record,
                        attempted_providers=providers,
                        source_statuses=source_statuses,
                        prior_attempts=prior_enrichment_attempts,
                    )
                    if attempt_status == "retryable_partial":
                        retryable_records += 1
                    elif attempt_status == "completed_retry_exhausted":
                        retry_exhausted_records += 1
                    append_jsonl(
                        run_root / "enrichment_attempts.jsonl",
                        {
                            "candidate_pool_key": record.get("candidate_pool_key"),
                            "query_id": query_id,
                            "batch_id": batch_id,
                            "providers": providers,
                            "provider_statuses": source_statuses,
                            "status": attempt_status,
                            "attempted_at": utc_now_iso(),
                            "error_class": type(exc).__name__,
                        },
                    )
                continue
            if output.get("reused_completed_output"):
                reused_completed_batches += 1
            batch_matches = self._candidate_pool_enrichment_matches(
                requested_records=batch_records,
                candidates_ref=Path(str(output["candidates_ref"])),
            )
            source_statuses = self._metadata_enrichment_source_statuses_from_output(output)
            batch_has_retryable_provider = any(
                self._metadata_enrichment_provider_status_is_retryable(status)
                for status in source_statuses.values()
            )
            matches_by_key = {
                self._normalize_candidate_pool_key(
                    str(row.get("candidate_pool_key") or "")
                ): row
                for row in batch_matches
            }
            invocation_summaries.append(
                {
                    "batch_id": batch_id,
                    "candidate_pool_keys": batch_keys,
                    "providers": providers,
                    "status": "partial" if batch_has_retryable_provider else "completed",
                    "source_statuses": source_statuses,
                    "reused_completed_output": bool(
                        output.get("reused_completed_output")
                    ),
                    "input_ref": output.get("input_ref"),
                    "output_ref": output.get("output_ref"),
                    "queries_ref": self._display_path(output_root / "queries_ref.json"),
                    "source_status_ref": output.get("source_status_ref"),
                    "provider_states_ref": output.get("provider_states_ref"),
                }
            )
            enriched_records.extend(batch_matches)
            for record in batch_records:
                normalized_key = self._normalize_candidate_pool_key(
                    str(record.get("candidate_pool_key") or "")
                )
                attempt_status = "completed_with_abstract"
                if normalized_key not in matches_by_key:
                    attempt_status = self._metadata_enrichment_attempt_status(
                        record=record,
                        attempted_providers=providers,
                        source_statuses=source_statuses,
                        prior_attempts=prior_enrichment_attempts,
                    )
                    if attempt_status == "retryable_partial":
                        retryable_records += 1
                    elif attempt_status == "completed_retry_exhausted":
                        retry_exhausted_records += 1
                append_jsonl(
                    run_root / "enrichment_attempts.jsonl",
                    {
                        "candidate_pool_key": record.get("candidate_pool_key"),
                        "query_id": query_id,
                        "batch_id": batch_id,
                        "providers": providers,
                        "provider_statuses": source_statuses,
                        "status": attempt_status,
                        "attempted_at": utc_now_iso(),
                        "output_ref": output.get("output_ref"),
                        "source_status_ref": output.get("source_status_ref"),
                        "provider_states_ref": output.get("provider_states_ref"),
                    },
                )
        enriched_ref = run_root / "enriched_records.jsonl"
        append_mode_existing = self._read_jsonl_records(enriched_ref)
        existing_keys = {
            str(row.get("candidate_pool_key") or "") for row in append_mode_existing
        }
        for row in enriched_records:
            if str(row.get("candidate_pool_key") or "") not in existing_keys:
                append_jsonl(enriched_ref, row)
                existing_keys.add(str(row.get("candidate_pool_key") or ""))
        reference_invocation = next(
            (
                row
                for row in invocation_summaries
                if row.get("input_ref") and row.get("output_ref")
            ),
            {},
        )
        summary = {
            "pool_id": pool_id,
            "status": (
                "partial" if failed_batches or retryable_records else "completed"
            ),
            "requested_records": len(requested_records),
            "batch_size": bounded_batch_size,
            "batch_count": len(batches),
            "failed_batches": failed_batches,
            "retryable_records": retryable_records,
            "retry_exhausted_records": retry_exhausted_records,
            "max_retryable_attempts_per_provider": (
                METADATA_ENRICHMENT_MAX_RETRYABLE_ATTEMPTS
            ),
            "reused_completed_batches": reused_completed_batches,
            "enriched_records": len(enriched_records),
            "records_with_abstract": sum(1 for row in enriched_records if row.get("abstract")),
            "cumulative_enriched_records": len(existing_keys),
            "cumulative_records_with_abstract": sum(
                1
                for row in self._read_jsonl_records(enriched_ref)
                if row.get("abstract")
            ),
            "providers_attempted": sorted(provider_attempts),
            "invocations": invocation_summaries,
            "input_ref": reference_invocation.get("input_ref", ""),
            "output_ref": reference_invocation.get("output_ref", ""),
            "queries_ref": reference_invocation.get("queries_ref", ""),
            "enriched_records_ref": self._display_path(enriched_ref),
            "source_status_ref": (
                reference_invocation.get("source_status_ref", "")
            ),
            "provider_states_ref": (
                reference_invocation.get("provider_states_ref", "")
            ),
            "guardrails": [
                "External metadata discovery skill boundary was used.",
                "No PDF download or Download Specialist handoff was started.",
                "Candidate-pool records were not overwritten in place.",
            ],
        }
        refreshed_plan = self.plan_candidate_pool_metadata_enrichment(
            pool_id=pool_id,
            limit=max(limit, 1),
        )
        refreshed_summary = refreshed_plan
        summary["remaining_enrichment_queue"] = refreshed_plan["enrichment_queue_size"]
        summary["remaining_missing_abstract"] = refreshed_plan[
            "records_missing_abstract"
        ]
        summary["terminal_missing_abstract"] = int(
            refreshed_summary.get("terminal_missing_abstract") or 0
        )
        summary["retryable_missing_abstract"] = int(
            refreshed_summary.get("retryable_missing_abstract") or 0
        )
        summary["metadata_outcomes_accounted"] = int(
            refreshed_summary.get("metadata_outcomes_accounted") or 0
        )
        summary["metadata_lookup_outcome_complete"] = bool(
            refreshed_summary.get("metadata_lookup_outcome_complete")
        )
        write_json_atomic(run_root / "metadata_enrichment_summary.json", summary)
        self._write_candidate_pool_metadata_enrichment_summary_markdown(
            run_root / "METADATA_ENRICHMENT_SUMMARY.md",
            summary,
            enriched_records,
        )
        return summary | {
            "summary_ref": self._display_path(run_root / "metadata_enrichment_summary.json"),
            "summary_markdown_ref": self._display_path(
                run_root / "METADATA_ENRICHMENT_SUMMARY.md"
            ),
        }

    def rescreen_enriched_candidate_pool_metadata(
        self, *, pool_id: str, max_records: int | None = None
    ) -> dict[str, Any]:
        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        enriched_ref = pool_root / "metadata_enrichment" / "enriched_records.jsonl"
        if not enriched_ref.exists():
            raise FileNotFoundError(f"Missing enriched records: {enriched_ref}")
        manifest_ref = pool_root / "manifest.json"
        manifest = dict(read_json(manifest_ref)) if manifest_ref.exists() else {}
        records_by_key = {
            str(record.get("candidate_pool_key") or ""): record
            for record in self._read_jsonl_records(pool_root / "records.jsonl")
            if record.get("candidate_pool_key")
        }
        prior_decisions = self._read_jsonl_records(pool_root / "screening_decisions.jsonl")
        prior_by_key = {
            str(row.get("global_record_id") or ""): row
            for row in prior_decisions
            if row.get("global_record_id")
        }
        already_rescreened = {
            str(row.get("global_record_id") or "")
            for row in self._read_jsonl_records(
                pool_root / "metadata_enrichment" / "rescreening_decisions.jsonl"
            )
            if row.get("global_record_id")
        }
        executor = TitleAbstractScreeningWorkerExecutor(
            repo_root=self.repo_root,
            run_dir=pool_dir,
            schema_path=self._schema_path("retrieval", "screening_decision.schema.json"),
        )
        protocol = self._candidate_pool_screening_protocol() | {
            "screening_context": "metadata_enriched_candidate_pool_rescreening",
            "metadata_enrichment_note": (
                "This record previously lacked enough title/abstract metadata. "
                "Use the enriched title/abstract only; do not use PDF-only facts."
            ),
        }
        rescreened = 0
        requested = 0
        decisions: list[dict[str, Any]] = []
        decision_changed = 0
        for enriched in self._read_jsonl_records(enriched_ref):
            key = str(enriched.get("candidate_pool_key") or "")
            if not key or key in already_rescreened:
                continue
            if max_records is not None and rescreened + requested >= max_records:
                break
            source_record = records_by_key.get(key, {})
            record_payload = dict(source_record) | {
                key_name: value
                for key_name, value in enriched.items()
                if value not in ("", None, [])
            }
            record = self._candidate_pool_normalized_record(record_payload)
            audit_batch_id = (
                "pool_metadata_enriched_rescreen_"
                f"{rescreened + requested + 1:05d}"
            )
            try:
                decision = executor.screen_one(
                    record,
                    run_id=pool_id,
                    query_id="CANDIDATE_POOL_METADATA_ENRICHED",
                    iteration=0,
                    audit_batch_id=audit_batch_id,
                    protocol=protocol,
                    allowed_reason_codes=self._allowed_reason_codes(),
                    scie_status="unknown",
                )
            except ScreeningWorkerBlocked as exc:
                append_jsonl(
                    pool_root / "metadata_enrichment" / "rescreening_worker_requests.jsonl",
                    {
                        "global_record_id": key,
                        "request_ref": exc.payload.get("request_ref"),
                        "result_ref": exc.payload.get("result_ref"),
                        "status": exc.payload.get("status"),
                        "metadata_enrichment_ref": self._display_path(enriched_ref),
                    },
                )
                requested += 1
                continue
            self._validate_screening_decision(decision)
            prior = prior_by_key.get(key, {})
            decision_row = decision.to_dict() | {
                "query_families": source_record.get("query_families", []),
                "source_providers": source_record.get("source_providers", []),
                "metadata_enrichment_rescreen": True,
                "metadata_enrichment_provider": enriched.get("enrichment_provider"),
                "previous_decision": prior.get("decision"),
                "previous_reason_codes": prior.get("reason_codes", []),
            }
            append_jsonl(
                pool_root / "metadata_enrichment" / "rescreening_decisions.jsonl",
                decision_row,
            )
            append_jsonl(pool_root / "screening_decisions.jsonl", decision_row)
            decisions.append(decision_row)
            already_rescreened.add(key)
            rescreened += 1
            if prior.get("decision") and prior.get("decision") != decision_row["decision"]:
                decision_changed += 1
        self._write_candidate_pool_screening_summary(
            pool_root=pool_root,
            manifest=manifest,
            new_decisions=decisions,
        )
        summary = {
            "pool_id": pool_id,
            "status": "paused_screening_worker_required" if requested else "completed",
            "enriched_records_ref": self._display_path(enriched_ref),
            "rescreened_decisions": rescreened,
            "worker_requests_created": requested,
            "decision_changed_count": decision_changed,
            "decision_counts": self._candidate_pool_decision_counts(decisions),
            "rescreening_decisions_ref": self._display_path(
                pool_root / "metadata_enrichment" / "rescreening_decisions.jsonl"
            ),
            "rescreening_requests_ref": self._display_path(
                pool_root / "metadata_enrichment" / "rescreening_worker_requests.jsonl"
            ),
            "screening_summary_ref": self._display_path(pool_root / "screening_summary.json"),
        }
        write_json_atomic(
            pool_root / "metadata_enrichment" / "rescreening_summary.json",
            summary,
        )
        return summary

    @staticmethod
    def _candidate_pool_metadata_enrichment_providers(
        records: list[dict[str, Any]],
    ) -> list[str]:
        providers: list[str] = []
        source_providers: set[str] = set()
        for record in records:
            record_sources = record.get("source_providers")
            values = (
                record_sources
                if isinstance(record_sources, list)
                else [record.get("source_provider")]
            )
            source_providers.update(
                str(provider).strip().lower() for provider in values if provider
            )
        if any(record.get("doi") for record in records):
            # Repeating the provider that supplied the sparse record rarely adds an
            # abstract. Prefer independent identifier-backed metadata sources.
            if "crossref" not in source_providers:
                providers.append("crossref")
            if "openalex" not in source_providers:
                providers.append("openalex")
            if "semantic_scholar" not in source_providers:
                providers.append("semantic_scholar")
            if "pubmed" not in source_providers:
                providers.append("pubmed")
        if (
            any(record.get("openalex_id") for record in records)
            and "openalex" not in source_providers
            and "openalex" not in providers
        ):
            providers.append("openalex")
        if any(record.get("pmid") for record in records):
            providers.append("pubmed")
        return providers

    def _candidate_pool_metadata_enrichment_remaining_providers(
        self,
        *,
        record: dict[str, Any],
        enrichment_attempts: list[dict[str, Any]],
    ) -> list[str]:
        candidate_key = self._normalize_candidate_pool_key(
            str(record.get("candidate_pool_key") or "")
        )
        attempts = [
            row
            for row in enrichment_attempts
            if self._normalize_candidate_pool_key(
                str(row.get("candidate_pool_key") or "")
            )
            == candidate_key
        ]
        remaining: list[str] = []
        for provider in self._candidate_pool_metadata_enrichment_providers([record]):
            statuses = [
                status
                for attempt in attempts
                if (
                    status := self._metadata_enrichment_attempt_provider_status(
                        attempt, provider
                    )
                )
            ]
            if any(
                self._metadata_enrichment_provider_status_is_terminal(status)
                for status in statuses
            ):
                continue
            retryable_attempts = sum(
                1
                for status in statuses
                if self._metadata_enrichment_provider_status_is_retryable(status)
            )
            if retryable_attempts >= METADATA_ENRICHMENT_MAX_RETRYABLE_ATTEMPTS:
                continue
            remaining.append(provider)
        return remaining

    def _metadata_enrichment_attempt_status(
        self,
        *,
        record: dict[str, Any],
        attempted_providers: list[str],
        source_statuses: dict[str, str],
        prior_attempts: list[dict[str, Any]],
    ) -> str:
        retryable_providers = [
            provider
            for provider in attempted_providers
            if self._metadata_enrichment_provider_status_is_retryable(
                source_statuses.get(provider, "")
            )
        ]
        if not retryable_providers:
            return "completed_no_abstract"
        candidate_key = self._normalize_candidate_pool_key(
            str(record.get("candidate_pool_key") or "")
        )
        for provider in retryable_providers:
            prior_retryable_count = sum(
                1
                for attempt in prior_attempts
                if self._normalize_candidate_pool_key(
                    str(attempt.get("candidate_pool_key") or "")
                )
                == candidate_key
                and self._metadata_enrichment_provider_status_is_retryable(
                    self._metadata_enrichment_attempt_provider_status(
                        attempt, provider
                    )
                )
            )
            if prior_retryable_count + 1 < METADATA_ENRICHMENT_MAX_RETRYABLE_ATTEMPTS:
                return "retryable_partial"
        return "completed_retry_exhausted"

    def _metadata_enrichment_attempt_is_terminal(
        self, attempt: dict[str, Any]
    ) -> bool:
        status = str(attempt.get("status") or "")
        if not status:
            provider_statuses = self._metadata_enrichment_attempt_provider_statuses(
                attempt
            )
            if not provider_statuses:
                return True
            return not any(
                self._metadata_enrichment_provider_status_is_retryable(
                    provider_status
                )
                for provider_status in provider_statuses.values()
            )
        if status in {
            "completed",
            "completed_with_abstract",
            "completed_no_remaining_provider",
            "completed_retry_exhausted",
        }:
            return True
        if status != "completed_no_abstract":
            return False
        provider_statuses = self._metadata_enrichment_attempt_provider_statuses(attempt)
        if not provider_statuses:
            return True
        return not any(
            self._metadata_enrichment_provider_status_is_retryable(provider_status)
            for provider_status in provider_statuses.values()
        )

    def _metadata_enrichment_attempt_provider_status(
        self, attempt: dict[str, Any], provider: str
    ) -> str:
        return self._metadata_enrichment_attempt_provider_statuses(attempt).get(
            provider, ""
        )

    def _metadata_enrichment_attempt_provider_statuses(
        self, attempt: dict[str, Any]
    ) -> dict[str, str]:
        embedded = attempt.get("provider_statuses")
        if isinstance(embedded, dict):
            return {str(key): str(value) for key, value in embedded.items()}
        source_status_ref = str(attempt.get("source_status_ref") or "").strip()
        if not source_status_ref:
            return {}
        path = Path(source_status_ref)
        if not path.is_absolute():
            path = self.repo_root / path
        try:
            payload = read_json(path)
        except (OSError, ValueError, TypeError):
            return {}
        return (
            {str(key): str(value) for key, value in payload.items()}
            if isinstance(payload, dict)
            else {}
        )

    def _metadata_enrichment_source_statuses_from_output(
        self, output: dict[str, Any]
    ) -> dict[str, str]:
        return self._metadata_enrichment_attempt_provider_statuses(
            {"source_status_ref": output.get("source_status_ref")}
        )

    @staticmethod
    def _metadata_enrichment_provider_status_is_terminal(status: str) -> bool:
        return status.strip().lower() in METADATA_ENRICHMENT_TERMINAL_PROVIDER_STATUSES

    @staticmethod
    def _metadata_enrichment_provider_status_is_retryable(status: str) -> bool:
        return status.strip().lower() in METADATA_ENRICHMENT_RETRYABLE_PROVIDER_STATUSES

    def audit_candidate_pool(self, *, pool_id: str, sample_size: int = 40) -> dict[str, Any]:
        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        records_ref = pool_root / "records.jsonl"
        decisions_ref = pool_root / "screening_decisions.jsonl"
        if not records_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool records: {records_ref}")
        if not decisions_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool screening decisions: {decisions_ref}")
        records = self._candidate_pool_records_with_metadata_enrichment(
            pool_root=pool_root,
            records=self._read_jsonl_records(records_ref),
        )
        decisions = self._read_jsonl_records(decisions_ref)
        audit = self._candidate_pool_targeted_audit(
            pool_id=pool_id,
            records=records,
            decisions=decisions,
            sample_size=sample_size,
        )
        audit_root = ensure_dir(pool_root / "targeted_audit")
        write_json_atomic(audit_root / "targeted_audit_summary.json", audit["summary"])
        for sample_name, rows in audit["samples"].items():
            write_json_atomic(audit_root / f"{sample_name}.json", rows)
            write_text_atomic(
                audit_root / f"{sample_name}.jsonl",
                "\n".join(
                    json.dumps(row, ensure_ascii=True, sort_keys=True) for row in rows
                )
                + ("\n" if rows else ""),
            )
        self._write_candidate_pool_targeted_audit_markdown(audit_root, audit["summary"])
        return {
            "pool_id": pool_id,
            "status": "completed",
            "sample_counts": audit["summary"]["sample_counts"],
            "summary_ref": self._display_path(audit_root / "TARGETED_AUDIT_SUMMARY.md"),
            "audit_ref": self._display_path(audit_root / "targeted_audit_summary.json"),
        }

    def review_candidate_pool_audit(
        self, *, pool_id: str, max_records: int | None = None
    ) -> dict[str, Any]:
        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        records_ref = pool_root / "records.jsonl"
        decisions_ref = pool_root / "screening_decisions.jsonl"
        audit_root = pool_root / "targeted_audit"
        if not records_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool records: {records_ref}")
        if not decisions_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool screening decisions: {decisions_ref}")
        review_queue = self._candidate_pool_review_queue(
            records=self._candidate_pool_records_with_metadata_enrichment(
                pool_root=pool_root,
                records=self._read_jsonl_records(records_ref),
            ),
            decisions=self._read_jsonl_records(decisions_ref),
            audit_root=audit_root,
        )
        executor = TitleAbstractScreeningWorkerExecutor(
            repo_root=self.repo_root,
            run_dir=pool_dir,
            schema_path=self._schema_path("retrieval", "screening_decision.schema.json"),
        )
        protocol = self._candidate_pool_screening_protocol() | {
            "review_context": (
                "Targeted audit review for high-recall candidate-pool calibration. "
                "Review rows are sampled from high-risk provisional includes, defers, "
                "and risky excludes. The result may confirm or override the provisional "
                "decision, but must still use title/abstract/keywords only."
            )
        }
        reviewed = 0
        requested = 0
        already_reviewed = {
            str(row.get("global_record_id") or "")
            for row in self._read_jsonl_records(pool_root / "audit_review_decisions.jsonl")
        }
        already_requested = {
            str(row.get("global_record_id") or "")
            for row in self._read_jsonl_records(pool_root / "audit_review_worker_requests.jsonl")
        }
        for item in review_queue:
            if max_records is not None and reviewed + requested >= max_records:
                break
            record_id = str(item.get("global_record_id") or "")
            if not record_id or record_id in already_reviewed:
                continue
            record = self._candidate_pool_normalized_record(cast(dict[str, Any], item["record"]))
            try:
                decision = executor.screen_one(
                    record,
                    run_id=pool_id,
                    query_id="CANDIDATE_POOL_AUDIT",
                    iteration=0,
                    audit_batch_id=str(item.get("audit_category") or "candidate_pool_audit"),
                    protocol=protocol | {"audit_queue_item": item.get("audit_summary")},
                    allowed_reason_codes=self._allowed_reason_codes(),
                    scie_status="unknown",
                )
            except ScreeningWorkerBlocked as exc:
                if record_id not in already_requested:
                    append_jsonl(
                        pool_root / "audit_review_worker_requests.jsonl",
                        {
                            "global_record_id": record_id,
                            "audit_category": item.get("audit_category"),
                            "provisional_decision": item.get("provisional_decision"),
                            "request_ref": exc.payload.get("request_ref"),
                            "result_ref": exc.payload.get("result_ref"),
                            "status": exc.payload.get("status"),
                        },
                    )
                    already_requested.add(record_id)
                requested += 1
                continue
            self._validate_screening_decision(decision)
            decision_row = decision.to_dict() | {
                "audit_category": item.get("audit_category"),
                "provisional_decision": item.get("provisional_decision"),
                "provisional_reason_codes": item.get("provisional_reason_codes", []),
                "query_families": item.get("query_families", []),
                "source_providers": item.get("source_providers", []),
                "review_outcome": self._candidate_pool_review_outcome(
                    provisional=str(item.get("provisional_decision") or ""),
                    reviewed=decision.decision,
                ),
            }
            append_jsonl(pool_root / "audit_review_decisions.jsonl", decision_row)
            already_reviewed.add(record_id)
            reviewed += 1
        summary = self._candidate_pool_review_summary(
            pool_id=pool_id,
            review_queue=review_queue,
            review_decisions=self._read_jsonl_records(pool_root / "audit_review_decisions.jsonl"),
        )
        write_json_atomic(pool_root / "audit_review_summary.json", summary)
        self._write_candidate_pool_review_summary_markdown(pool_root, summary)
        return {
            "pool_id": pool_id,
            "status": "paused_screening_worker_required" if requested else "completed",
            "review_queue_size": len(review_queue),
            "reviewed_decisions": reviewed,
            "worker_requests_created": requested,
            "review_requests_ref": self._display_path(
                pool_root / "audit_review_worker_requests.jsonl"
            ),
            "review_decisions_ref": self._display_path(
                pool_root / "audit_review_decisions.jsonl"
            ),
            "summary_ref": self._display_path(pool_root / "AUDIT_REVIEW_SUMMARY.md"),
            "isolation": "one_document_per_conversation",
        }

    def safe_defer_candidate_pool_audit_review(
        self, *, pool_id: str, max_records: int | None = None
    ) -> dict[str, Any]:
        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        requests_ref = pool_root / "audit_review_worker_requests.jsonl"
        if not requests_ref.exists():
            raise FileNotFoundError(f"Missing audit review requests: {requests_ref}")
        records_by_id = {
            str(record.get("candidate_pool_key") or ""): record
            for record in self._read_jsonl_records(pool_root / "records.jsonl")
            if record.get("candidate_pool_key")
        }
        existing_decisions = {
            str(row.get("global_record_id") or "")
            for row in self._read_jsonl_records(pool_root / "audit_review_decisions.jsonl")
        }
        created = 0
        appended = 0
        for request_row in self._read_jsonl_records(requests_ref):
            if max_records is not None and created >= max_records:
                break
            record_id = str(request_row.get("global_record_id") or "")
            if not record_id or record_id in existing_decisions:
                continue
            record_payload = records_by_id.get(record_id)
            if not record_payload:
                continue
            record = self._candidate_pool_normalized_record(record_payload)
            audit_category = str(
                request_row.get("audit_category") or "candidate_pool_audit_safe_defer"
            )
            query_id = "CANDIDATE_POOL_AUDIT"
            digest = hashlib.sha256(record.global_record_id.encode()).hexdigest()[:24]
            raw_ref = (
                pool_dir
                / "screening"
                / "raw_responses"
                / query_id
                / f"{digest}.audit_safe_defer.json"
            )
            result_ref = self.repo_root / str(request_row.get("result_ref") or "")
            payload = TitleAbstractScreeningWorkerExecutor(
                repo_root=self.repo_root,
                run_dir=pool_dir,
                schema_path=self._schema_path("retrieval", "screening_decision.schema.json"),
            )._safe_defer_payload(  # noqa: SLF001
                record=record,
                run_id=pool_id,
                query_id=query_id,
                iteration=0,
                audit_batch_id=audit_category,
                scie_status="unknown",
                raw_ref=raw_ref,
                schema_error="safe_defer_requested_for_unreviewed_candidate_pool_audit",
            )
            ensure_dir(result_ref.parent)
            write_json_atomic(result_ref, payload)
            decision = ScreeningDecision(**payload)
            self._validate_screening_decision(decision)
            decision_row = decision.to_dict() | {
                "audit_category": audit_category,
                "provisional_decision": request_row.get("provisional_decision"),
                "provisional_reason_codes": [],
                "query_families": record_payload.get("query_families", []),
                "source_providers": record_payload.get("source_providers", []),
                "review_outcome": self._candidate_pool_review_outcome(
                    provisional=str(request_row.get("provisional_decision") or ""),
                    reviewed=decision.decision,
                ),
                "candidate_pool_audit_safe_defer": True,
            }
            append_jsonl(pool_root / "audit_review_decisions.jsonl", decision_row)
            existing_decisions.add(record_id)
            created += 1
            appended += 1
        review_queue = self._candidate_pool_review_queue(
            records=self._candidate_pool_records_with_metadata_enrichment(
                pool_root=pool_root,
                records=self._read_jsonl_records(pool_root / "records.jsonl"),
            ),
            decisions=self._read_jsonl_records(pool_root / "screening_decisions.jsonl"),
            audit_root=pool_root / "targeted_audit",
        )
        summary = self._candidate_pool_review_summary(
            pool_id=pool_id,
            review_queue=review_queue,
            review_decisions=self._read_jsonl_records(pool_root / "audit_review_decisions.jsonl"),
        )
        write_json_atomic(pool_root / "audit_review_summary.json", summary)
        self._write_candidate_pool_review_summary_markdown(pool_root, summary)
        return {
            "pool_id": pool_id,
            "status": "completed",
            "safe_defer_results_created": created,
            "review_decisions_appended": appended,
            "review_decisions_ref": self._display_path(
                pool_root / "audit_review_decisions.jsonl"
            ),
            "summary_ref": self._display_path(pool_root / "AUDIT_REVIEW_SUMMARY.md"),
            "isolation": "one_document_per_conversation",
        }

    def export_candidate_pool_review(self, *, pool_id: str) -> dict[str, Any]:
        pool_dir = self.runs_dir / pool_id
        pool_root = pool_dir / "candidate_pool"
        records_ref = pool_root / "records.jsonl"
        decisions_ref = pool_root / "screening_decisions.jsonl"
        if not records_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool records: {records_ref}")
        if not decisions_ref.exists():
            raise FileNotFoundError(f"Missing candidate pool screening decisions: {decisions_ref}")
        records = self._candidate_pool_records_with_metadata_enrichment(
            pool_root=pool_root,
            records=self._read_jsonl_records(records_ref),
        )
        decisions = self._read_jsonl_records(decisions_ref)
        review_decisions = self._read_jsonl_records(pool_root / "audit_review_decisions.jsonl")
        export_dir = ensure_dir(
            self.repo_root
            / "docs"
            / "retrieval_runs"
            / "exports"
            / pool_id
        )
        exported = self._write_candidate_pool_review_exports(
            export_dir=export_dir,
            pool_id=pool_id,
            records=records,
            decisions=decisions,
            review_decisions=review_decisions,
        )
        return {
            "pool_id": pool_id,
            "status": "completed",
            "export_dir": self._display_path(export_dir),
            "files": exported,
        }

    def build_candidate_pool_review_priority(
        self, *, pool_id: str, limit: int = 300
    ) -> dict[str, Any]:
        export_dir = ensure_dir(
            self.repo_root / "docs" / "retrieval_runs" / "exports" / pool_id
        )
        required_exports = [
            "include_candidate_needs_review.csv",
            "records_needing_review.csv",
            "resolved_exclude_or_defer.csv",
            "final_included_candidates.csv",
        ]
        missing_exports = [
            self._display_path(export_dir / name)
            for name in required_exports
            if not (export_dir / name).exists()
        ]
        if missing_exports:
            raise FileNotFoundError(
                "Missing candidate-pool review export files: "
                + ", ".join(missing_exports)
            )
        rows_by_file = {
            name: self._read_csv_dicts(export_dir / name)
            for name in required_exports
        }
        final_include_review_rows = [
            row
            for row in rows_by_file["final_included_candidates.csv"]
            if str(row.get("final_review_status") or "") != "reviewed"
        ]
        priority_rows = self._candidate_pool_review_priority_rows(
            include_candidates=(
                rows_by_file["include_candidate_needs_review.csv"]
                + final_include_review_rows
            ),
            needs_review=rows_by_file["records_needing_review.csv"],
        )
        priority_rows.sort(
            key=lambda row: (
                -int(row["review_rank_score"]),
                str(row["global_record_id"]),
            )
        )
        exported_rows = priority_rows[: max(0, limit)]
        priority_dir = ensure_dir(export_dir / "review_priority")
        queue_ref = priority_dir / (
            "review_priority_queue_top300.csv"
            if limit == 300
            else f"review_priority_queue_top{limit}.csv"
        )
        fieldnames = (
            list(exported_rows[0])
            if exported_rows
            else ["review_rank_score", "review_bucket", "global_record_id", "title"]
        )
        write_csv_atomic(queue_ref, exported_rows, fieldnames)
        summary = self._candidate_pool_review_priority_summary(
            pool_id=pool_id,
            priority_rows=priority_rows,
            exported_rows=exported_rows,
            rows_by_file=rows_by_file,
            queue_ref=queue_ref,
        )
        summary_ref = priority_dir / "review_priority_summary.json"
        markdown_ref = priority_dir / "REVIEW_PRIORITY_QUEUE.md"
        write_json_atomic(summary_ref, summary)
        self._write_candidate_pool_review_priority_markdown(markdown_ref, summary)
        return {
            "pool_id": pool_id,
            "status": "completed",
            "priority_queue_total": len(priority_rows),
            "priority_queue_exported": len(exported_rows),
            "priority_queue_ref": self._display_path(queue_ref),
            "summary_ref": self._display_path(summary_ref),
            "markdown_ref": self._display_path(markdown_ref),
        }

    def export_all_runs(self) -> dict[str, Any]:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            run_ids = [
                str(row["run_id"])
                for row in connection.execute(
                    "SELECT run_id FROM runs WHERE status = 'completed' ORDER BY started_at"
                )
            ]
        return {"runs": [self.export_paper_data(run_id) for run_id in run_ids]}

    def mock_download_claim(
        self, *, worker_id: str, lease_seconds: int = 300
    ) -> dict[str, Any]:
        now = utc_now_iso()
        lease = self._utc_plus_seconds(lease_seconds)
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM download_jobs
                WHERE job_state IN ('pending', 'failed_retryable')
                   OR (job_state = 'claimed' AND lease_expires_at < ?)
                ORDER BY last_attempt_at IS NOT NULL, last_attempt_at, download_job_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return {"status": "empty", "claimed": None}
            connection.execute(
                """
                UPDATE download_jobs
                SET job_state = 'claimed',
                    claimed_by = ?,
                    claimed_at = ?,
                    lease_expires_at = ?,
                    attempt_count = attempt_count + 1,
                    last_attempt_at = ?
                WHERE download_job_id = ?
                """,
                (worker_id, now, lease, now, row["download_job_id"]),
            )
            claimed = dict(row) | {
                "job_state": "claimed",
                "claimed_by": worker_id,
                "claimed_at": now,
                "lease_expires_at": lease,
            }
        self._rebuild_global_handoff_logs()
        return {"status": "claimed", "claimed": claimed}

    def mock_download_complete(
        self, *, idempotency_key: str, final_status: str = "succeeded"
    ) -> dict[str, Any]:
        if final_status not in {"succeeded", "skipped_existing"}:
            raise ValueError("final_status must be succeeded or skipped_existing")
        now = utc_now_iso()
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            row = connection.execute(
                "SELECT * FROM download_jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown download job: {idempotency_key}")
            result_reference = f"mock-result:{row['download_job_id']}:{final_status}"
            connection.execute(
                """
                UPDATE download_jobs
                SET job_state = ?, completed_at = ?, result_reference = ?, failure_reason = NULL
                WHERE idempotency_key = ?
                """,
                (final_status, now, result_reference, idempotency_key),
            )
        self._rebuild_global_handoff_logs()
        return {
            "status": final_status,
            "idempotency_key": idempotency_key,
            "pending_download_jobs": self.download_queue_status()["pending_download_jobs"],
        }

    def mock_download_fail(
        self,
        *,
        idempotency_key: str,
        retryable: bool = True,
        failure_reason: str = "mock_failure",
    ) -> dict[str, Any]:
        state = "failed_retryable" if retryable else "failed_terminal"
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            row = connection.execute(
                "SELECT * FROM download_jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown download job: {idempotency_key}")
            connection.execute(
                """
                UPDATE download_jobs
                SET job_state = ?, failure_reason = ?, last_attempt_at = ?
                WHERE idempotency_key = ?
                """,
                (state, failure_reason, utc_now_iso(), idempotency_key),
            )
        self._rebuild_global_handoff_logs()
        return {
            "status": state,
            "idempotency_key": idempotency_key,
            "pending_download_jobs": self.download_queue_status()["pending_download_jobs"],
        }

    def download_queue_status(self) -> dict[str, Any]:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            state_counts = {
                str(row["job_state"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT job_state, COUNT(*) AS count
                    FROM download_jobs
                    GROUP BY job_state
                    ORDER BY job_state
                    """
                )
            }
            pending = sum(state_counts.get(state, 0) for state in PENDING_DOWNLOAD_STATES)
            terminal = sum(state_counts.get(state, 0) for state in TERMINAL_DOWNLOAD_STATES)
        return {
            "pending_download_jobs": pending,
            "terminal_download_jobs": terminal,
            "state_counts": state_counts,
            "pending_states": sorted(PENDING_DOWNLOAD_STATES),
            "terminal_states": sorted(TERMINAL_DOWNLOAD_STATES),
        }

    def _query_for_iteration(
        self,
        *,
        run_id: str,
        run_dir: Path,
        loaded: LoadedConfig,
        date_from: str | None,
        date_to: str,
        iteration: int,
        live_mode: bool,
    ) -> tuple[CanonicalQuery, IterationPlan]:
        if not live_mode:
            return (
                self.variant_generator.build_query(
                    loaded.protocol, date_to, iteration, date_from=date_from
                ),
                self.variant_generator.plan(iteration),
            )
        if iteration == 1:
            query = QueryPlanner().build_initial_query(
                loaded.protocol, date_to, date_from=date_from
            )
            plan = IterationPlan(
                query_id=query.query_id,
                parent_query_id=None,
                branch_id="live-main",
                acceptance_status="accepted",
                decision="accept",
                decision_reason="Initial protocol-derived live query accepted as baseline.",
                added_terms=[],
                removed_terms=[],
                modified_blocks=[],
                expected_effect="Establish live metadata discovery baseline.",
            )
            return query, plan

        incomplete = self._incomplete_query_for_iteration(run_id, run_dir, iteration)
        if incomplete is not None:
            return incomplete

        parent = self._latest_accepted_query(run_id, run_dir)
        if parent is None:
            parent = QueryPlanner().build_initial_query(
                loaded.protocol, date_to, date_from=date_from
            )
        metrics = self._latest_metrics(run_dir, parent.query_id)
        if metrics is None:
            raise QueryRefinementBlocked(
                {
                    "status": "paused_query_refinement_worker_required",
                    "worker_name": "QueryRefinementWorker",
                    "reason": "missing_parent_metrics",
                    "parent_query_id": parent.query_id,
                }
            )
        if self._completed_refinement_attempt_count(run_dir, parent_query_id=parent.query_id) >= 6:
            raise QueryRefinementBlocked(
                {
                    "status": "saturated_narrow",
                    "worker_name": "Retrieval Specialist",
                    "reason": "no_candidate_passed_live_acceptance_thresholds",
                    "parent_query_id": parent.query_id,
                    "candidate_evaluations": self._completed_refinement_evaluations(
                        run_dir, parent_query_id=parent.query_id
                    ),
                    "rejected_patch_refs": [],
                }
            )
        executor = QueryRefinementWorkerExecutor(
            repo_root=self.repo_root,
            run_dir=run_dir,
            schema_path=self.repo_root / "schemas" / "retrieval" / "query_patch.schema.json",
        )
        patches = executor.propose(
            accepted_query=parent,
            metrics=metrics,
            evidence_refs=self._query_refinement_evidence_refs(run_dir, run_id),
            previous_changes=self._previous_query_changes(run_dir),
        )
        if not patches:
            raise QueryRefinementBlocked(
                {
                    "status": "paused_query_refinement_worker_required",
                    "worker_name": "QueryRefinementWorker",
                    "reason": "no_query_patches_returned",
                    "parent_query_id": parent.query_id,
                }
            )
        candidates: list[CandidateQueryPlan] = []
        rejected_refs: list[str] = []
        applier = QueryPatchApplier()
        for index, patch in enumerate(patches[:3], start=1):
            child_query_id = f"Q{iteration:04d}"
            if index > 1:
                child_query_id = f"Q{iteration:04d}_C{index}"
            result = applier.apply(
                parent, patch, child_query_id=child_query_id, iteration=iteration
            )
            patch_dir = self._write_query_patch_result(run_dir, parent, result)
            if result.status == "applied":
                candidate = CandidateQueryPlan(
                    query=result.child_query,
                    plan=IterationPlan(
                        query_id=result.child_query.query_id,
                        parent_query_id=parent.query_id,
                        branch_id=f"live-{result.patch.patch_id}",
                        acceptance_status="candidate",
                        decision="accept",
                        decision_reason=(
                            "Deterministic selector will evaluate this query "
                            "against configured live acceptance thresholds."
                        ),
                        added_terms=result.patch.terms_added,
                        removed_terms=result.patch.terms_removed,
                        modified_blocks=[result.patch.target_concept_block],
                        expected_effect=result.patch.expected_effect,
                    ),
                    patch_result=result,
                )
                candidates.append(
                    self._attach_limited_candidate_evaluation(
                        run_dir,
                        candidate,
                        run_id=run_id,
                        loaded=loaded,
                        runtime=self._runtime_settings(loaded),
                    )
                )
            else:
                rejected_refs.append(self._display_path(patch_dir / "query_patch.json"))
        if not candidates:
            raise QueryRefinementBlocked(
                {
                    "status": "paused_query_refinement_worker_required",
                    "worker_name": "QueryRefinementWorker",
                    "reason": "no_valid_non_no_op_query_patches",
                    "parent_query_id": parent.query_id,
                    "rejected_patch_refs": rejected_refs,
                }
            )
        pending_evaluations = [
            candidate.limited_evaluation or {}
            for candidate in candidates
            if not candidate.limited_evaluation
            or candidate.limited_evaluation.get("evaluation_status") != "completed"
        ]
        if pending_evaluations:
            raise QueryRefinementBlocked(
                {
                    "status": "paused_query_candidate_evaluation_required",
                    "worker_name": "Retrieval Specialist",
                    "reason": "all_valid_non_no_op_patches_require_limited_evaluation",
                    "parent_query_id": parent.query_id,
                    "candidate_evaluations": pending_evaluations,
                    "rejected_patch_refs": rejected_refs,
                }
            )
        selected = self._select_query_candidate(candidates, parent_metrics=metrics)
        self._write_query_refinement_decision(
            run_dir=run_dir,
            parent=parent,
            selected=selected,
            candidates=candidates,
            rejected_refs=rejected_refs,
            metrics=metrics,
        )
        if selected is None:
            if self._should_continue_after_unaccepted_candidates(
                candidates,
                parent_metrics=metrics,
                run_dir=run_dir,
                parent_query_id=parent.query_id,
            ):
                raise QueryRefinementBlocked(
                    {
                        "status": "paused_query_refinement_worker_required",
                        "worker_name": "QueryRefinementWorker",
                        "reason": (
                            "no_candidate_passed_live_acceptance_thresholds_"
                            "try_alternative_terms"
                        ),
                        "parent_query_id": parent.query_id,
                        "candidate_evaluations": [
                            candidate.limited_evaluation or {} for candidate in candidates
                        ],
                        "rejected_patch_refs": rejected_refs,
                        "request_ref": self._display_path(
                            self._write_query_refinement_retry_request(
                                run_dir=run_dir,
                                parent=parent,
                                metrics=metrics,
                                candidates=candidates,
                                rejected_refs=rejected_refs,
                            )
                        ),
                    }
                )
            raise QueryRefinementBlocked(
                {
                    "status": "saturated_narrow",
                    "worker_name": "Retrieval Specialist",
                    "reason": "no_candidate_passed_live_acceptance_thresholds",
                    "parent_query_id": parent.query_id,
                    "candidate_evaluations": [
                        candidate.limited_evaluation or {} for candidate in candidates
                    ],
                    "rejected_patch_refs": rejected_refs,
                }
            )
        return selected.query, selected.plan

    def _write_query_patch_result(
        self, run_dir: Path, parent: CanonicalQuery, result: Any
    ) -> Path:
        patch_dir = ensure_dir(
            self._candidate_artifact_dir(
                run_dir,
                query_id=result.child_query.query_id,
                patch_id=result.patch.patch_id,
            )
        )
        write_yaml_atomic(patch_dir / "parent_canonical_query.yaml", parent.to_dict())
        write_json_atomic(patch_dir / "query_patch.json", result.patch.to_dict())
        write_yaml_atomic(patch_dir / "child_canonical_query.yaml", result.child_query.to_dict())
        write_json_atomic(patch_dir / "query_diff.json", result.query_diff)
        write_json_atomic(patch_dir / "parent_executable_queries.json", result.parent_compiled)
        write_json_atomic(patch_dir / "child_executable_queries.json", result.child_compiled)
        write_json_atomic(
            patch_dir / "patch_application_status.json",
            {"status": result.status, "reason": result.reason},
        )
        return patch_dir

    def _should_continue_after_unaccepted_candidates(
        self,
        candidates: list[CandidateQueryPlan],
        *,
        parent_metrics: Any,
        run_dir: Path | None = None,
        parent_query_id: str | None = None,
    ) -> bool:
        del parent_metrics
        completed = [candidate.limited_evaluation or {} for candidate in candidates]
        if not completed:
            return False
        no_positive_utility = all(
            float(item.get("conservative_utility", 0.0)) <= 0.0 for item in completed
        )
        no_gain_includes = all(int(item.get("gain_include_count", 0)) == 0 for item in completed)
        any_loss_includes = any(
            int(item.get("known_loss_include_count", 0)) > 0
            or int(item.get("loss_audit_include_count", 0)) > 0
            for item in completed
        )
        tried_noise_reduction = any(
            str(item.get("patch_type") or "") == "noise_reduction" for item in completed
        )
        # The worker may return fewer than three evaluable candidates when a
        # proposal is rejected as duplicate/no-op by the deterministic applier.
        # Do not treat that as evidence that the parent query is saturated.
        tested_enough_candidates = len(completed) >= 2
        completed_attempts = len(completed)
        if run_dir is not None and parent_query_id:
            completed_attempts = self._completed_refinement_attempt_count(
                run_dir, parent_query_id=parent_query_id
            )
        return bool(
            tested_enough_candidates
            and no_positive_utility
            and (no_gain_includes or any_loss_includes)
            and self._remaining_refinement_attempts(completed, completed_attempts) > 0
            and (not tried_noise_reduction or no_gain_includes)
        )

    def _remaining_refinement_attempts(
        self, completed: list[dict[str, Any]], completed_attempts: int | None = None
    ) -> int:
        current_attempts = {
            str(item.get("patch_id") or item.get("query_id") or "")
            for item in completed
            if str(item.get("patch_id") or item.get("query_id") or "")
        }
        tested_count = (
            completed_attempts if completed_attempts is not None else len(current_attempts)
        )
        # Two rounds of three rejected candidates are enough evidence for this
        # high-recall pilot that the current parent query is locally saturated.
        return max(0, 6 - tested_count)

    def _write_query_refinement_retry_request(
        self,
        *,
        run_dir: Path,
        parent: CanonicalQuery,
        metrics: Any,
        candidates: list[CandidateQueryPlan],
        rejected_refs: list[str],
    ) -> Path:
        request_dir = ensure_dir(run_dir / "query_refinement" / "worker_requests")
        next_attempt = self._completed_refinement_attempt_count(
            run_dir, parent_query_id=parent.query_id
        ) + 1
        refinement_key = f"{parent.query_id}_attempt_{next_attempt:03d}"
        request_ref = request_dir / f"{refinement_key}.json"
        failed_evaluations = [candidate.limited_evaluation or {} for candidate in candidates]
        payload = {
            "worker_name": "QueryRefinementWorker",
            "prompt_version": "query-refinement-worker-v1.0",
            "refinement_key": refinement_key,
            "parent_query": parent.to_dict(),
            "query_metrics": metrics.to_dict() if hasattr(metrics, "to_dict") else {},
            "failed_candidate_evaluations": failed_evaluations,
            "rejected_patch_refs": rejected_refs,
            "instruction": (
                "The previous valid patches changed the executable query but failed "
                "high-recall gain/loss acceptance. Do not stop at the first failed "
                "term set. Do not repeat those patches or near synonyms. Return at "
                "most three new QueryPatch objects using lower-ranked but still "
                "evidence-supported terms. Prioritize one positive expansion that "
                "can plausibly recover additional natural-water occurrence, monitoring, "
                "sampling, or concentration records, and one narrow noise_reduction "
                "patch only when excluded evidence supports it without overlapping "
                "known include evidence. Avoid broad pollutant-class additions unless "
                "two or more independent included documents support the exact concept."
            ),
            "result_ref": self._display_path(
                run_dir
                / "query_refinement"
                / "worker_results"
                / f"{refinement_key}.json"
            ),
        }
        write_json_atomic(request_ref, payload)
        return request_ref

    def _candidate_selection_fingerprint(self, candidates: list[CandidateQueryPlan]) -> str:
        basis = "|".join(
            sorted(
                str(candidate.patch_result.patch.patch_id)
                for candidate in candidates
                if candidate.patch_result and candidate.patch_result.patch.patch_id
            )
        )
        return hashlib.sha256(basis.encode()).hexdigest()[:12] if basis else "empty"

    def _completed_refinement_attempt_count(
        self, run_dir: Path, *, parent_query_id: str
    ) -> int:
        return len(self._completed_refinement_patch_ids(run_dir, parent_query_id=parent_query_id))

    def _completed_refinement_patch_ids(
        self, run_dir: Path, *, parent_query_id: str
    ) -> set[str]:
        patch_ids: set[str] = set()
        for path in sorted(
            (run_dir / "query_refinement" / "candidate_evaluations").glob("*.json")
        ):
            try:
                payload = read_json(path)
            except (OSError, ValueError):
                continue
            if payload.get("parent_query_id") != parent_query_id:
                continue
            patch_id = str(payload.get("patch_id") or "")
            if patch_id:
                patch_ids.add(patch_id)
        for path in sorted((run_dir / "query_refinement").glob("*_candidate_selection.json")):
            try:
                payload = read_json(path)
            except (OSError, ValueError):
                continue
            if payload.get("parent_query_id") != parent_query_id:
                continue
            for evaluation in payload.get("candidate_evaluations", []):
                if not isinstance(evaluation, dict):
                    continue
                patch_id = str(evaluation.get("patch_id") or "")
                if patch_id:
                    patch_ids.add(patch_id)
        return patch_ids

    def _completed_refinement_evaluations(
        self, run_dir: Path, *, parent_query_id: str
    ) -> list[dict[str, Any]]:
        seen: set[str] = set()
        evaluations: list[dict[str, Any]] = []
        for path in sorted(
            (run_dir / "query_refinement" / "candidate_evaluations").glob("*.json")
        ):
            try:
                payload = read_json(path)
            except (OSError, ValueError):
                continue
            if payload.get("parent_query_id") != parent_query_id:
                continue
            patch_id = str(payload.get("patch_id") or "")
            if not patch_id or patch_id in seen:
                continue
            seen.add(patch_id)
            evaluations.append(dict(payload))
        return evaluations

    def _select_query_candidate(
        self, candidates: list[CandidateQueryPlan], *, parent_metrics: QueryMetrics
    ) -> CandidateQueryPlan | None:
        observed = [
            candidate
            for candidate in candidates
            if candidate.limited_evaluation
            and candidate.limited_evaluation.get("evaluation_status") == "completed"
        ]
        if observed:
            eligible = [
                candidate
                for candidate in observed
                if self._candidate_passes_live_acceptance(
                    candidate.limited_evaluation or {},
                    parent_metrics=parent_metrics,
                )
            ]
            if eligible:
                return max(
                    eligible,
                    key=lambda candidate: self._candidate_metric_key(
                        candidate.limited_evaluation or {},
                        parent_metrics=parent_metrics,
                    ),
                )
            return None
        return max(
            candidates,
            key=lambda candidate: (
                len(candidate.patch_result.patch.terms_removed),
                len(candidate.patch_result.patch.terms_added),
                -len(candidate.query.optional_context_terms),
            ),
        )

    def _candidate_passes_live_acceptance(
        self, evaluation: dict[str, Any], *, parent_metrics: Any
    ) -> bool:
        if evaluation.get("source_completeness") != "complete":
            return False
        if (
            int(evaluation.get("gain_count", 0)) == 0
            and int(evaluation.get("loss_count", 0)) == 0
        ):
            return False
        patch_type = str(evaluation.get("patch_type") or "")
        if evaluation.get("pairwise_safety_gate") == "fail":
            return False
        if patch_type == "noise_reduction":
            if float(evaluation.get("conservative_utility", 0.0)) <= 0.0:
                return False
            if float(evaluation.get("noise_reduction_utility", 0.0)) <= 0.0:
                return False
            if int(evaluation.get("loss_audit_include_count", 0)) > 0:
                return False
            if int(evaluation.get("known_loss_include_count", 0)) > 0:
                return False
            if int(evaluation.get("loss_audit_screened_count", 0)) < min(
                int(evaluation.get("loss_count", 0)),
                max(1, int(evaluation.get("loss_audit_target_records", 1))),
            ):
                return False
            excluded_matrix_rate = float(evaluation.get("excluded_matrix_rate", 1.0))
            return bool(excluded_matrix_rate <= parent_metrics.excluded_matrix_rate + 0.20)
        if patch_type == "expansion":
            if str(evaluation.get("result_set_change_status") or "changed") != "changed":
                return False
            gain_include_count = int(
                evaluation.get(
                    "gain_include_count",
                    evaluation.get("include_count", evaluation.get("gain_count", 0)),
                )
            )
            if gain_include_count <= 0:
                return False
            if int(evaluation.get("known_loss_include_count", 0)) > 0:
                return False
            if int(evaluation.get("loss_audit_include_count", 0)) > 0:
                return False
            novel_precision = float(evaluation.get("novel_precision_at_20", 0.0))
            if novel_precision < parent_metrics.novel_precision_at_20 - 0.20:
                return False
            defer_rate = float(evaluation.get("defer_rate", 1.0))
            if defer_rate > parent_metrics.defer_rate + 0.15:
                return False
            excluded_matrix_rate = float(evaluation.get("excluded_matrix_rate", 1.0))
            excluded_matrix_tolerance = 0.20
            return bool(
                excluded_matrix_rate
                <= parent_metrics.excluded_matrix_rate + excluded_matrix_tolerance
            )
        if float(evaluation.get("conservative_utility", 0.0)) <= 0.0:
            return False
        if float(evaluation.get("score_delta", 0.0)) < 0.02:
            return False
        novel_precision = float(evaluation.get("novel_precision_at_20", 0.0))
        if novel_precision < parent_metrics.novel_precision_at_20 - 0.20:
            return False
        defer_rate = float(evaluation.get("defer_rate", 1.0))
        if defer_rate > parent_metrics.defer_rate + 0.15:
            return False
        excluded_matrix_rate = float(evaluation.get("excluded_matrix_rate", 1.0))
        excluded_matrix_tolerance = 0.20
        return bool(
            excluded_matrix_rate
            <= parent_metrics.excluded_matrix_rate + excluded_matrix_tolerance
        )

    @staticmethod
    def _wilson_lower_bound(successes: int, total: int, z: float = 1.64) -> float:
        if total <= 0:
            return 0.0
        phat = successes / total
        denom = 1.0 + (z * z / total)
        centre = phat + (z * z / (2 * total))
        margin = z * math.sqrt((phat * (1.0 - phat) + (z * z / (4 * total))) / total)
        return max(0.0, (centre - margin) / denom)

    @staticmethod
    def _wilson_upper_bound(successes: int, total: int, z: float = 1.64) -> float:
        if total <= 0:
            return 1.0
        phat = successes / total
        denom = 1.0 + (z * z / total)
        centre = phat + (z * z / (2 * total))
        margin = z * math.sqrt((phat * (1.0 - phat) + (z * z / (4 * total))) / total)
        return min(1.0, (centre + margin) / denom)

    def _candidate_pairwise_evaluation(
        self,
        run_id: str,
        parent_query_id: str | None,
        candidate_query_id: str,
        *,
        patch_type: str = "expansion",
        include_count: int,
        exclude_count: int,
        defer_count: int,
        excluded_matrix_rate: float,
        loss_audit_target_records: int | None = None,
    ) -> dict[str, Any]:
        if parent_query_id is None:
            evaluated_gain = include_count + exclude_count + defer_count
            return {
                "pairwise_evaluation_status": "not_applicable_initial_query",
                "patch_type": patch_type,
                "gain_count": 0,
                "loss_count": 0,
                "known_loss_include_count": 0,
                "gain_screened_count": evaluated_gain,
                "gain_include_count": include_count,
                "gain_exclude_count": exclude_count,
                "gain_defer_count": defer_count,
                "gain_relevant_lower_bound": 0.0,
                "lost_relevant_upper_bound": 0.0,
                "conservative_utility": 0.0,
                "noise_reduction_utility": 0.0,
                "pairwise_safety_gate": "pass",
            }
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            candidate_ids = {
                str(row["global_record_id"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT global_record_id
                    FROM document_query_membership
                    WHERE run_id = ? AND query_id = ?
                    """,
                    (run_id, candidate_query_id),
                )
            }
            parent_ids = {
                str(row["global_record_id"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT global_record_id
                    FROM document_query_membership
                    WHERE run_id = ? AND query_id = ?
                    """,
                    (run_id, parent_query_id),
                )
            }
            loss_ids = parent_ids - candidate_ids
            gain_ids = candidate_ids - parent_ids
            if loss_ids:
                placeholders = ",".join("?" for _ in loss_ids)
                loss_rows = connection.execute(
                    f"""
                    SELECT current_screening_status, COUNT(*) AS count
                    FROM documents
                    WHERE global_record_id IN ({placeholders})
                    GROUP BY current_screening_status
                    """,
                    tuple(loss_ids),
                ).fetchall()
                audit_limit = max(
                    1,
                    min(
                        len(loss_ids),
                        int(
                            loss_audit_target_records
                            or DEFAULT_CANDIDATE_EVALUATION_LOSS_AUDIT_RECORDS
                        ),
                    ),
                )
                loss_audit_rows = connection.execute(
                    f"""
                    SELECT d.global_record_id, d.current_screening_status,
                           m.source_rank, m.source_name
                    FROM documents d
                    JOIN document_query_membership m
                      ON m.global_record_id = d.global_record_id
                     AND m.run_id = ?
                     AND m.query_id = ?
                    WHERE d.global_record_id IN ({placeholders})
                    ORDER BY
                      CASE COALESCE(d.current_screening_status, 'unknown')
                        WHEN 'include' THEN 0
                        WHEN 'defer_metadata' THEN 1
                        WHEN 'defer_not_downloaded' THEN 1
                        WHEN 'unknown' THEN 2
                        WHEN 'exclude' THEN 3
                        ELSE 4
                      END,
                      m.source_rank,
                      d.global_record_id
                    LIMIT ?
                    """,
                    (run_id, parent_query_id, *tuple(loss_ids), audit_limit),
                ).fetchall()
            else:
                loss_rows = []
                loss_audit_rows = []
        loss_by_status = {
            str(row["current_screening_status"] or "unknown"): int(row["count"] or 0)
            for row in loss_rows
        }
        evaluated_gain = include_count + exclude_count + defer_count
        gain_precision_lcb = self._wilson_lower_bound(include_count, evaluated_gain)
        gain_relevant_lcb = gain_precision_lcb * len(gain_ids)
        known_loss_include = int(loss_by_status.get("include", 0))
        known_loss_exclude = int(loss_by_status.get("exclude", 0))
        known_loss_defer = int(loss_by_status.get("defer_metadata", 0)) + int(
            loss_by_status.get("defer_not_downloaded", 0)
        )
        unknown_loss = max(0, len(loss_ids) - known_loss_include - known_loss_exclude)
        loss_relevance_ucb = self._wilson_upper_bound(known_loss_include, max(1, len(loss_ids)))
        lost_relevant_ucb = known_loss_include + (unknown_loss * loss_relevance_ucb)
        loss_audit_by_status: dict[str, int] = {}
        for row in loss_audit_rows:
            status = str(row["current_screening_status"] or "unknown")
            loss_audit_by_status[status] = loss_audit_by_status.get(status, 0) + 1
        loss_audit_screened = sum(loss_audit_by_status.values())
        loss_audit_include = int(loss_audit_by_status.get("include", 0))
        loss_audit_exclude = int(loss_audit_by_status.get("exclude", 0))
        loss_audit_defer = int(loss_audit_by_status.get("defer_metadata", 0)) + int(
            loss_audit_by_status.get("defer_not_downloaded", 0)
        )
        loss_audit_unknown = int(loss_audit_by_status.get("unknown", 0))
        loss_audit_relevance_ucb = (
            self._wilson_upper_bound(loss_audit_include, loss_audit_screened)
            if loss_audit_screened > 0
            else 0.0
        )
        noise_reduction_utility = (
            known_loss_exclude
            + (0.25 * loss_audit_exclude)
            - (10.0 * loss_audit_include)
            - (1.5 * loss_audit_defer)
            - (0.25 * loss_audit_unknown)
            - (5.0 * loss_audit_relevance_ucb)
            - (2.0 * excluded_matrix_rate)
        )
        lost_relevant_penalty_weight = 3.0 if patch_type == "expansion" else 10.0
        conservative_utility = (
            noise_reduction_utility
            if patch_type == "noise_reduction"
            else (
                gain_relevant_lcb
                - (lost_relevant_penalty_weight * lost_relevant_ucb)
                - (0.5 * defer_count)
                - (2.0 * excluded_matrix_rate)
            )
        )
        # Expansion patches can displace parent records under a fixed top-k scan cap even
        # when the executable Boolean query is broader. Treat that as bounded loss risk
        # in the utility calculation; reserve hard fail for patches that can truly remove
        # records from the executable query.
        safety_gate = (
            "fail"
            if patch_type in {"replacement", "noise_reduction"} and known_loss_include > 0
            else "pass"
        )
        if patch_type == "noise_reduction" and loss_audit_include > 0:
            safety_gate = "fail"
        result_set_change_status = (
            "unchanged" if len(gain_ids) == 0 and len(loss_ids) == 0 else "changed"
        )
        return {
            "pairwise_evaluation_status": "completed",
            "result_set_change_status": result_set_change_status,
            "patch_type": patch_type,
            "parent_query_id": parent_query_id,
            "candidate_query_id": candidate_query_id,
            "gain_count": len(gain_ids),
            "loss_count": len(loss_ids),
            "loss_by_screening_status": loss_by_status,
            "known_loss_include_count": known_loss_include,
            "known_loss_exclude_count": known_loss_exclude,
            "known_loss_defer_count": known_loss_defer,
            "unknown_loss_count": unknown_loss,
            "loss_audit_target_records": int(
                loss_audit_target_records or DEFAULT_CANDIDATE_EVALUATION_LOSS_AUDIT_RECORDS
            ),
            "loss_audit_screened_count": loss_audit_screened,
            "loss_audit_by_screening_status": loss_audit_by_status,
            "loss_audit_include_count": loss_audit_include,
            "loss_audit_exclude_count": loss_audit_exclude,
            "loss_audit_defer_count": loss_audit_defer,
            "loss_audit_unknown_count": loss_audit_unknown,
            "loss_audit_relevance_wilson_upper_bound": loss_audit_relevance_ucb,
            "gain_screened_count": evaluated_gain,
            "gain_include_count": include_count,
            "gain_exclude_count": exclude_count,
            "gain_defer_count": defer_count,
            "gain_precision_wilson_lower_bound": gain_precision_lcb,
            "loss_relevance_wilson_upper_bound": loss_relevance_ucb,
            "gain_relevant_lower_bound": gain_relevant_lcb,
            "lost_relevant_upper_bound": lost_relevant_ucb,
            "noise_reduction_utility": noise_reduction_utility,
            "conservative_utility": conservative_utility,
            "pairwise_safety_gate": safety_gate,
        }

    def _attach_limited_candidate_evaluation(
        self,
        run_dir: Path,
        candidate: CandidateQueryPlan,
        *,
        run_id: str | None = None,
        loaded: LoadedConfig | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> CandidateQueryPlan:
        evaluation = self._candidate_limited_evaluation(
            run_dir,
            candidate,
            run_id=run_id,
            loaded=loaded,
            runtime=runtime,
        )
        patch_dir = ensure_dir(
            self._candidate_artifact_dir(
                run_dir,
                query_id=candidate.query.query_id,
                patch_id=candidate.patch_result.patch.patch_id,
            )
        )
        write_json_atomic(patch_dir / "limited_novelty_evaluation.json", evaluation)
        return replace(candidate, limited_evaluation=evaluation)

    def _candidate_limited_evaluation(
        self,
        run_dir: Path,
        candidate: CandidateQueryPlan,
        *,
        run_id: str | None = None,
        loaded: LoadedConfig | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ref, request_ref = self._candidate_evaluation_refs(run_dir, candidate)
        if ref.exists():
            payload = read_json(ref)
            if isinstance(payload, dict):
                if payload.get("evaluation_status") == "completed":
                    return payload | {"evaluation_ref": self._display_path(ref)}
                if (
                    payload.get("evaluation_status") == "pending_screening_worker_required"
                    and run_id
                    and loaded
                    and runtime
                ):
                    return self._resume_candidate_limited_evaluation(
                        run_id=run_id,
                        run_dir=run_dir,
                        candidate=candidate,
                        loaded=loaded,
                        runtime=runtime,
                        evaluation_ref=ref,
                        request_ref=request_ref,
                        pending_payload=payload,
                    )
                if not (run_id and loaded and runtime):
                    return payload | {"evaluation_ref": self._display_path(ref)}
        request_payload = self._candidate_limited_evaluation_request(
            candidate=candidate,
            run_dir=run_dir,
            result_ref=ref,
        )
        ensure_dir(request_ref.parent)
        write_json_atomic(request_ref, request_payload)
        if run_id and loaded and runtime:
            return self._execute_candidate_limited_evaluation(
                run_id=run_id,
                run_dir=run_dir,
                candidate=candidate,
                loaded=loaded,
                runtime=runtime,
                evaluation_ref=ref,
                request_ref=request_ref,
            )
        return {
            "evaluation_status": "pending_limited_novelty_sample",
            "evaluation_ref": self._display_path(ref),
            "evaluation_request_ref": self._display_path(request_ref),
            "query_id": candidate.query.query_id,
            "patch_id": candidate.patch_result.patch.patch_id,
            "required_inputs": [
                "limited provider page artifacts",
                "novelty sample",
                "title/abstract screening decisions",
                "source status",
            ],
            "selection_eligible": False,
        }

    def _candidate_evaluation_refs(
        self, run_dir: Path, candidate: CandidateQueryPlan
    ) -> tuple[Path, Path]:
        evaluation_dir = run_dir / "query_refinement" / "candidate_evaluations"
        request_dir = evaluation_dir / "requests"
        base_ref = evaluation_dir / f"{candidate.query.query_id}.json"
        base_request_ref = request_dir / f"{candidate.query.query_id}.json"
        if self._candidate_ref_matches_patch(base_ref, candidate.patch_result.patch.patch_id):
            return base_ref, base_request_ref
        stem = self._candidate_artifact_stem(
            query_id=candidate.query.query_id,
            patch_id=candidate.patch_result.patch.patch_id,
        )
        return evaluation_dir / f"{stem}.json", request_dir / f"{stem}.json"

    def _candidate_artifact_dir(self, run_dir: Path, *, query_id: str, patch_id: str) -> Path:
        base_dir = run_dir / "query_refinement" / "applied" / query_id
        patch_ref = base_dir / "query_patch.json"
        if self._candidate_ref_matches_patch(patch_ref, patch_id):
            return base_dir
        return (
            run_dir
            / "query_refinement"
            / "applied"
            / self._candidate_artifact_stem(query_id=query_id, patch_id=patch_id)
        )

    def _candidate_ref_matches_patch(self, path: Path, patch_id: str) -> bool:
        if not path.exists():
            return True
        try:
            payload = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        existing_patch_id = payload.get("patch_id")
        if existing_patch_id is None and "query_patch" in payload:
            query_patch = payload.get("query_patch")
            if isinstance(query_patch, dict):
                existing_patch_id = query_patch.get("patch_id")
        if existing_patch_id is None:
            return True
        return str(existing_patch_id) == patch_id

    def _candidate_artifact_stem(self, *, query_id: str, patch_id: str) -> str:
        safe_patch_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", patch_id).strip("_")
        if not safe_patch_id:
            safe_patch_id = hashlib.sha256(patch_id.encode()).hexdigest()[:12]
        return f"{query_id}__{safe_patch_id}"

    def _candidate_limited_evaluation_request(
        self, *, candidate: CandidateQueryPlan, run_dir: Path, result_ref: Path
    ) -> dict[str, Any]:
        evaluation_plan = self._candidate_evaluation_plan(candidate.patch_result.patch)
        return {
            "evaluation_type": "limited_novelty_sample",
            "query_id": candidate.query.query_id,
            "patch_id": candidate.patch_result.patch.patch_id,
            "patch_type": evaluation_plan.patch_type,
            "candidate_query": candidate.query.to_dict(),
            "query_patch": candidate.patch_result.patch.to_dict(),
            "result_ref": self._display_path(result_ref),
            "executor": "Retrieval Specialist",
            "max_records_per_provider": self._configured_max_records_per_provider(run_dir),
            "target_novel_records": evaluation_plan.gain_target_records,
            "loss_audit_target_records": evaluation_plan.loss_audit_target_records,
            "sampling_design": (
                "paired_gain_loss_audit; expansion patches prioritize gain novelty, "
                "noise_reduction patches prioritize parent-loss audit"
            ),
            "required_outputs": [
                "source_completeness",
                "raw_result_count",
                "scanned_result_count",
                "novel_record_count",
                "include_count",
                "exclude_count",
                "defer_count",
                "novel_precision_at_20",
                "defer_rate",
                "excluded_matrix_rate",
                "score_delta",
                "patch_type",
                "result_set_change_status",
                "gain_count",
                "loss_count",
                "loss_audit_screened_count",
                "loss_audit_by_screening_status",
                "conservative_utility",
                "noise_reduction_utility",
            ],
        }

    def _candidate_evaluation_target_novel_records(
        self, loaded: LoadedConfig | None = None
    ) -> int:
        env_value = os.environ.get("ECMONITOR_CANDIDATE_EVALUATION_TARGET_NOVEL_RECORDS")
        if env_value:
            try:
                return max(1, int(env_value))
            except ValueError:
                return DEFAULT_CANDIDATE_EVALUATION_TARGET_NOVEL_RECORDS
        if loaded is not None:
            configured = (
                loaded.stopping.get("evaluation", {}).get(
                    "candidate_evaluation_target_novel_records"
                )
                if isinstance(loaded.stopping, dict)
                else None
            )
            if configured is not None:
                try:
                    return max(1, int(configured))
                except (TypeError, ValueError):
                    return DEFAULT_CANDIDATE_EVALUATION_TARGET_NOVEL_RECORDS
        return DEFAULT_CANDIDATE_EVALUATION_TARGET_NOVEL_RECORDS

    def _candidate_evaluation_loss_audit_records(
        self, loaded: LoadedConfig | None = None
    ) -> int:
        env_value = os.environ.get("ECMONITOR_CANDIDATE_EVALUATION_LOSS_AUDIT_RECORDS")
        if env_value:
            try:
                return max(1, int(env_value))
            except ValueError:
                return DEFAULT_CANDIDATE_EVALUATION_LOSS_AUDIT_RECORDS
        if loaded is not None:
            configured = (
                loaded.stopping.get("evaluation", {}).get(
                    "candidate_evaluation_loss_audit_records"
                )
                if isinstance(loaded.stopping, dict)
                else None
            )
            if configured is not None:
                try:
                    return max(1, int(configured))
                except (TypeError, ValueError):
                    return DEFAULT_CANDIDATE_EVALUATION_LOSS_AUDIT_RECORDS
        return DEFAULT_CANDIDATE_EVALUATION_LOSS_AUDIT_RECORDS

    def _candidate_evaluation_plan(
        self,
        patch: Any,
        loaded: LoadedConfig | None = None,
    ) -> CandidateEvaluationPlan:
        patch_type = self._candidate_patch_type(patch)
        gain_target = self._candidate_evaluation_target_novel_records(loaded)
        loss_target = self._candidate_evaluation_loss_audit_records(loaded)
        if patch_type == "noise_reduction":
            gain_target = max(1, min(gain_target, 5))
        return CandidateEvaluationPlan(
            patch_type=patch_type,
            gain_target_records=gain_target,
            loss_audit_target_records=loss_target,
        )

    def _candidate_patch_type(self, patch: Any) -> str:
        if getattr(patch, "target_concept_block", "") == "prohibited_or_rejected_terms":
            return "noise_reduction"
        if getattr(patch, "operation", "") in {"remove", "replace"}:
            return "replacement"
        return "expansion"

    def _execute_candidate_limited_evaluation(
        self,
        *,
        run_id: str,
        run_dir: Path,
        candidate: CandidateQueryPlan,
        loaded: LoadedConfig,
        runtime: dict[str, Any],
        evaluation_ref: Path,
        request_ref: Path,
    ) -> dict[str, Any]:
        query = candidate.query
        evaluation_plan = self._candidate_evaluation_plan(
            candidate.patch_result.patch,
            loaded,
        )
        candidate_dir = ensure_dir(
            run_dir / "query_refinement" / "candidate_evaluations" / "work" / query.query_id
        )
        self._write_query_artifacts(run_id, candidate_dir, query, candidate.plan, None)
        self._compile_query(candidate_dir, query)
        providers = self._live_providers_for_run(run_dir)
        self._start_query_iteration(run_id, query, candidate.plan, loaded)
        try:
            source_counts = self._search_sources_external(
                run_id=run_id,
                run_dir=run_dir,
                query=query,
                runtime=runtime,
                max_scan_depth=self._configured_max_scan_depth(run_dir),
                providers=providers,
            )
            normalized_count = self._normalize_and_register(
                run_id=run_id,
                run_dir=run_dir,
                query=query,
                batch_size=int(runtime["normalization_batch_size"]),
                target_novel_records=evaluation_plan.gain_target_records,
                fail_after_batch=None,
            )
        finally:
            self._remove_candidate_query_iteration(run_id, query.query_id)
        try:
            handoff_counts = self._screen_and_handoff_live(
                run_id=run_id,
                run_dir=run_dir,
                query=query,
                loaded=loaded,
                runtime=runtime,
                max_novelty_decisions=evaluation_plan.gain_target_records,
                novelty_against_query_id=query.parent_query_id,
            )
            loss_audit_counts = self._screen_candidate_loss_audit_live(
                run_id=run_id,
                run_dir=run_dir,
                parent_query_id=query.parent_query_id,
                candidate_query_id=query.query_id,
                loaded=loaded,
                loss_audit_target_records=evaluation_plan.loss_audit_target_records,
            )
        except ScreeningWorkerBlocked as exc:
            payload = {
                "evaluation_status": "pending_screening_worker_required",
                "evaluation_ref": self._display_path(evaluation_ref),
                "evaluation_request_ref": self._display_path(request_ref),
                "query_id": query.query_id,
                "patch_id": candidate.patch_result.patch.patch_id,
                "source_counts": source_counts,
                "normalized_record_count": normalized_count,
                "screening_request": exc.payload,
                "selection_eligible": False,
            }
            write_json_atomic(evaluation_ref, payload)
            return payload
        for key, value in loss_audit_counts.items():
            if isinstance(value, int):
                handoff_counts[key] = int(handoff_counts.get(key, 0)) + value
        duplicate_count = self._duplicate_count(run_id, query.query_id)
        metrics = self._evaluate_query(
            run_id=run_id,
            query=query,
            plan=candidate.plan,
            loaded=loaded,
            source_counts=source_counts,
            duplicate_count=duplicate_count,
            handoff_counts=handoff_counts,
            target_novel_records=evaluation_plan.gain_target_records,
        )
        reference = self._live_acceptance_reference(run_id, query.parent_query_id)
        parent_score = reference.score if reference is not None else 0.0
        score_delta = metrics.total_score - parent_score
        pairwise = self._candidate_pairwise_evaluation(
            run_id,
            query.parent_query_id,
            query.query_id,
            patch_type=evaluation_plan.patch_type,
            include_count=metrics.include_count,
            exclude_count=metrics.exclude_count,
            defer_count=metrics.defer_count,
            excluded_matrix_rate=metrics.excluded_matrix_rate,
            loss_audit_target_records=evaluation_plan.loss_audit_target_records,
        )
        payload = {
            "evaluation_status": "completed",
            "evaluation_ref": self._display_path(evaluation_ref),
            "evaluation_request_ref": self._display_path(request_ref),
            "query_id": query.query_id,
            "patch_id": candidate.patch_result.patch.patch_id,
            "patch_type": evaluation_plan.patch_type,
            "gain_target_records": evaluation_plan.gain_target_records,
            "loss_audit_target_records": evaluation_plan.loss_audit_target_records,
            "source_completeness": metrics.source_completeness,
            "raw_result_count": metrics.raw_result_count,
            "scanned_result_count": metrics.scanned_result_count,
            "normalized_record_count": normalized_count,
            "novel_record_count": metrics.novel_record_count,
            "include_count": metrics.include_count,
            "exclude_count": metrics.exclude_count,
            "defer_count": metrics.defer_count,
            "novel_precision_at_20": metrics.novel_precision_at_20,
            "defer_rate": metrics.defer_rate,
            "excluded_matrix_rate": metrics.excluded_matrix_rate,
            "score": metrics.total_score,
            "score_delta": score_delta,
            "download_requests_emitted": metrics.download_requests_emitted,
            "source_statuses": source_counts.get("source_statuses", {}),
            "selection_eligible": True,
            "completed_at": utc_now_iso(),
        } | pairwise
        write_json_atomic(evaluation_ref, payload)
        return payload

    def _resume_candidate_limited_evaluation(
        self,
        *,
        run_id: str,
        run_dir: Path,
        candidate: CandidateQueryPlan,
        loaded: LoadedConfig,
        runtime: dict[str, Any],
        evaluation_ref: Path,
        request_ref: Path,
        pending_payload: dict[str, Any],
    ) -> dict[str, Any]:
        query = candidate.query
        evaluation_plan = self._candidate_evaluation_plan(
            candidate.patch_result.patch,
            loaded,
        )
        try:
            handoff_counts = self._screen_and_handoff_live(
                run_id=run_id,
                run_dir=run_dir,
                query=query,
                loaded=loaded,
                runtime=runtime,
                max_novelty_decisions=evaluation_plan.gain_target_records,
                novelty_against_query_id=query.parent_query_id,
            )
            loss_audit_counts = self._screen_candidate_loss_audit_live(
                run_id=run_id,
                run_dir=run_dir,
                parent_query_id=query.parent_query_id,
                candidate_query_id=query.query_id,
                loaded=loaded,
                loss_audit_target_records=evaluation_plan.loss_audit_target_records,
            )
        except ScreeningWorkerBlocked as exc:
            payload = pending_payload | {
                "evaluation_status": "pending_screening_worker_required",
                "evaluation_ref": self._display_path(evaluation_ref),
                "evaluation_request_ref": self._display_path(request_ref),
                "screening_request": exc.payload,
                "selection_eligible": False,
            }
            write_json_atomic(evaluation_ref, payload)
            return payload
        for key, value in loss_audit_counts.items():
            if isinstance(value, int):
                handoff_counts[key] = int(handoff_counts.get(key, 0)) + value
        source_counts = dict(pending_payload.get("source_counts") or {})
        normalized_count = int(pending_payload.get("normalized_record_count") or 0)
        duplicate_count = self._duplicate_count(run_id, query.query_id)
        metrics = self._evaluate_query(
            run_id=run_id,
            query=query,
            plan=candidate.plan,
            loaded=loaded,
            source_counts=source_counts,
            duplicate_count=duplicate_count,
            handoff_counts=handoff_counts,
            target_novel_records=evaluation_plan.gain_target_records,
        )
        reference = self._live_acceptance_reference(run_id, query.parent_query_id)
        parent_score = reference.score if reference is not None else 0.0
        pairwise = self._candidate_pairwise_evaluation(
            run_id,
            query.parent_query_id,
            query.query_id,
            patch_type=evaluation_plan.patch_type,
            include_count=metrics.include_count,
            exclude_count=metrics.exclude_count,
            defer_count=metrics.defer_count,
            excluded_matrix_rate=metrics.excluded_matrix_rate,
            loss_audit_target_records=evaluation_plan.loss_audit_target_records,
        )
        payload = {
            "evaluation_status": "completed",
            "evaluation_ref": self._display_path(evaluation_ref),
            "evaluation_request_ref": self._display_path(request_ref),
            "query_id": query.query_id,
            "patch_id": candidate.patch_result.patch.patch_id,
            "patch_type": evaluation_plan.patch_type,
            "gain_target_records": evaluation_plan.gain_target_records,
            "loss_audit_target_records": evaluation_plan.loss_audit_target_records,
            "source_completeness": metrics.source_completeness,
            "raw_result_count": metrics.raw_result_count,
            "scanned_result_count": metrics.scanned_result_count,
            "normalized_record_count": normalized_count,
            "novel_record_count": metrics.novel_record_count,
            "include_count": metrics.include_count,
            "exclude_count": metrics.exclude_count,
            "defer_count": metrics.defer_count,
            "novel_precision_at_20": metrics.novel_precision_at_20,
            "defer_rate": metrics.defer_rate,
            "excluded_matrix_rate": metrics.excluded_matrix_rate,
            "score": metrics.total_score,
            "score_delta": metrics.total_score - parent_score,
            "download_requests_emitted": metrics.download_requests_emitted,
            "source_statuses": source_counts.get("source_statuses", {}),
            "selection_eligible": True,
            "completed_at": utc_now_iso(),
            "resumed_from": "pending_screening_worker_required",
        } | pairwise
        write_json_atomic(evaluation_ref, payload)
        return payload

    def _live_providers_for_run(self, run_dir: Path) -> list[str]:
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            providers = dict(read_json(manifest_path)).get("live_providers")
            if isinstance(providers, list) and providers:
                return [str(provider) for provider in providers]
        return SOURCE_NAMES

    def _configured_max_scan_depth(self, run_dir: Path) -> int:
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            value = dict(read_json(manifest_path)).get("configured_max_scan_depth")
            if value is not None:
                return max(1, int(value))
        loaded = ProtocolLoader(self.config_dir).load()
        return int(loaded.stopping["evaluation"]["max_scan_depth_per_source"])

    def _configured_max_records_per_provider(self, run_dir: Path) -> int:
        loaded = ProtocolLoader(self.config_dir).load()
        page_size = int(self._runtime_settings(loaded)["retrieval_page_size"])
        scan_depth = self._configured_max_scan_depth(run_dir)
        return page_size * max(1, scan_depth)

    def _remove_candidate_query_iteration(self, run_id: str, query_id: str) -> None:
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            connection.execute(
                "DELETE FROM query_iterations WHERE run_id = ? AND query_id = ?",
                (run_id, query_id),
            )
            row = connection.execute(
                """
                SELECT query_id, iteration
                FROM query_iterations
                WHERE run_id = ? AND acceptance_status = 'accepted'
                  AND finalized_at IS NOT NULL
                ORDER BY iteration DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    UPDATE runs
                    SET current_query_id = ?, current_iteration = ?
                    WHERE run_id = ?
                    """,
                    (row["query_id"], row["iteration"], run_id),
                )

    def _incomplete_query_for_iteration(
        self, run_id: str, run_dir: Path, iteration: int
    ) -> tuple[CanonicalQuery, IterationPlan] | None:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            row = connection.execute(
                """
                SELECT query_id, parent_query_id, branch_id, acceptance_status
                FROM query_iterations
                WHERE run_id = ? AND iteration = ? AND finalized_at IS NULL
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (run_id, iteration),
            ).fetchone()
        if row is None:
            return None
        query_id = str(row["query_id"])
        query_path = run_dir / "queries" / query_id / "canonical_query.yaml"
        if not query_path.exists():
            return None
        query = CanonicalQuery(**read_yaml(query_path))
        change_path = run_dir / "queries" / query_id / "query_change.json"
        change = dict(read_json(change_path)) if change_path.exists() else {}
        plan = IterationPlan(
            query_id=query.query_id,
            parent_query_id=(
                str(row["parent_query_id"]) if row["parent_query_id"] is not None else None
            ),
            branch_id=str(row["branch_id"] or f"live-resume-{query.query_id}"),
            acceptance_status=str(row["acceptance_status"] or "candidate"),
            decision=str(change.get("decision") or "accept"),
            decision_reason=str(change.get("decision_reason") or "Resume unfinished query."),
            added_terms=[str(term) for term in change.get("added_terms", [])],
            removed_terms=[str(term) for term in change.get("removed_terms", [])],
            modified_blocks=[
                str(block) for block in change.get("modified_concept_blocks", [])
            ],
            expected_effect=str(change.get("expected_effect") or query.expected_effect),
        )
        return query, plan

    def _candidate_metric_key(
        self, evaluation: dict[str, Any], *, parent_metrics: Any
    ) -> tuple[float, float, float, float, float, float]:
        score_delta = float(evaluation.get("score_delta", 0.0))
        conservative_utility = float(evaluation.get("conservative_utility", 0.0))
        novel_precision = float(evaluation.get("novel_precision_at_20", 0.0))
        defer_rate = float(evaluation.get("defer_rate", 1.0))
        excluded_matrix_rate = float(evaluation.get("excluded_matrix_rate", 1.0))
        source_complete = 1.0 if evaluation.get("source_completeness") == "complete" else 0.0
        precision_drop = parent_metrics.novel_precision_at_20 - novel_precision
        defer_increase = defer_rate - parent_metrics.defer_rate
        excluded_matrix_increase = excluded_matrix_rate - parent_metrics.excluded_matrix_rate
        return (
            source_complete,
            conservative_utility,
            score_delta,
            -max(0.0, precision_drop),
            -max(0.0, defer_increase),
            -max(0.0, excluded_matrix_increase),
        )

    def _write_query_refinement_decision(
        self,
        *,
        run_dir: Path,
        parent: CanonicalQuery,
        selected: CandidateQueryPlan | None,
        candidates: list[CandidateQueryPlan],
        rejected_refs: list[str],
        metrics: QueryMetrics,
    ) -> None:
        decision_dir = ensure_dir(run_dir / "query_refinement")
        selected_query_id = selected.query.query_id if selected else None
        selected_patch_id = selected.patch_result.patch.patch_id if selected else None
        decision_file_stem = selected_query_id or f"{parent.query_id}_no_candidate_accepted"
        decision_payload = {
                "parent_query_id": parent.query_id,
                "selected_query_id": selected_query_id,
                "selected_patch_id": selected_patch_id,
                "selection_actor": "Retrieval Specialist",
                "selection_method": "deterministic_candidate_selector",
                "selection_reason": (
                    "Selected from valid non-no-op QueryPatch candidates after all "
                    "candidate limited novelty evaluations completed."
                    if selected
                    else "No valid non-no-op QueryPatch candidate passed configured "
                    "live acceptance thresholds; parent query remains accepted."
                ),
                "parent_metrics": metrics.to_dict(),
                "candidate_evaluations": [
                    candidate.limited_evaluation or {} for candidate in candidates
                ],
                "candidate_patch_ids": [
                    candidate.patch_result.patch.patch_id for candidate in candidates
                ],
                "candidate_query_ids": [candidate.query.query_id for candidate in candidates],
                "rejected_patch_refs": rejected_refs,
                "acceptance_thresholds": {
                    "objective": "high_recall_screenable_retrieval",
                    "score_improvement_min": 0.02,
                    "score_improvement_can_be_overridden_by_pairwise_utility": True,
                    "expansion_requires_included_gain": True,
                    "expansion_rejects_known_include_loss": True,
                    "noise_reduction_requires_parent_loss_audit": True,
                    "noise_reduction_rejects_any_include_loss": True,
                    "novel_precision_decrease_max": 0.20,
                    "defer_rate_increase_max": 0.15,
                    "excluded_matrix_rate_must_not_increase": False,
                    "excluded_matrix_rate_increase_max": 0.20,
                    "source_execution_must_be_complete": True,
                },
            }
        write_json_atomic(
            decision_dir / f"{decision_file_stem}_candidate_selection.json",
            decision_payload,
        )
        if selected is None:
            fingerprinted_stem = (
                f"{parent.query_id}_no_candidate_accepted_"
                f"{self._candidate_selection_fingerprint(candidates)}"
            )
            write_json_atomic(
                decision_dir / f"{fingerprinted_stem}_candidate_selection.json",
                decision_payload,
            )

    def _rename_candidate_query(
        self, candidate: CandidateQueryPlan, *, child_query_id: str, iteration: int
    ) -> CandidateQueryPlan:
        result = QueryPatchApplier().apply(
            candidate.patch_result.parent_query,
            candidate.patch_result.patch,
            child_query_id=child_query_id,
            iteration=iteration,
        )
        plan = IterationPlan(
            query_id=result.child_query.query_id,
            parent_query_id=result.parent_query.query_id,
            branch_id=f"live-{result.patch.patch_id}",
            acceptance_status="candidate",
            decision="accept",
            decision_reason=candidate.plan.decision_reason,
            added_terms=result.patch.terms_added,
            removed_terms=result.patch.terms_removed,
            modified_blocks=[result.patch.target_concept_block],
            expected_effect=result.patch.expected_effect,
        )
        return CandidateQueryPlan(
            query=result.child_query,
            plan=plan,
            patch_result=result,
            limited_evaluation=candidate.limited_evaluation,
        )

    def _execute_iteration(
        self,
        *,
        run_id: str,
        run_dir: Path,
        query: CanonicalQuery,
        plan: IterationPlan,
        loaded: LoadedConfig,
        runtime: dict[str, Any],
        target_novel_records: int,
        max_scan_depth: int,
        machine: StateMachine,
        checkpoints: CheckpointManager,
        fail_after_operator: str | None,
        fail_after_batch: int | None,
        fail_after_source_page: int | None,
        stress_records_per_source: int | None,
        stress_mode: bool,
        live_mode: bool,
        providers: list[str] | None,
    ) -> IterationResult:
        query_dir = ensure_dir(run_dir / "queries" / query.query_id)
        self._start_query_iteration(run_id, query, plan, loaded)
        if machine.state == RetrievalState.LOAD_PROTOCOL:
            machine.transition(RetrievalState.BUILD_QUERY)
        self._write_query_artifacts(run_id, query_dir, query, plan, None)
        checkpoints.save("BUILD_QUERY", {"query_id": query.query_id, "iteration": query.iteration})
        self._maybe_stop_after_operator(
            run_id, run_dir, query, "BUILD_QUERY", fail_after_operator
        )

        machine.transition(RetrievalState.COMPILE_QUERY)
        self._compile_query(query_dir, query)
        checkpoints.save("COMPILE_QUERY", {"query_id": query.query_id, "sources": SOURCE_NAMES})
        self._maybe_stop_after_operator(
            run_id, run_dir, query, "COMPILE_QUERY", fail_after_operator
        )

        machine.transition(RetrievalState.SEARCH_SOURCES)
        search_sources_checkpoint_exists = self._search_sources_checkpoint_matches(
            run_dir, query.query_id
        )
        if live_mode:
            source_counts = self._search_sources_external(
                run_id=run_id,
                run_dir=run_dir,
                query=query,
                runtime=runtime,
                max_scan_depth=max_scan_depth,
                providers=providers,
            )
        else:
            source_counts = self._search_sources(
                run_id=run_id,
                run_dir=run_dir,
                query=query,
                runtime=runtime,
                max_scan_depth=max_scan_depth,
                fail_after_source_page=fail_after_source_page,
                stress_records_per_source=stress_records_per_source,
            )
        if source_counts.get("_interrupted"):
            return self._interrupted_result(query)
        if not (live_mode and search_sources_checkpoint_exists):
            checkpoints.save("SEARCH_SOURCES", source_counts)
        if live_mode:
            self._update_live_source_status(run_id, run_dir, source_counts)
        self._maybe_stop_after_operator(
            run_id, run_dir, query, "SEARCH_SOURCES", fail_after_operator
        )

        machine.transition(RetrievalState.NORMALIZE)
        normalized_count = self._normalize_and_register(
            run_id=run_id,
            run_dir=run_dir,
            query=query,
            batch_size=int(runtime["normalization_batch_size"]),
            target_novel_records=target_novel_records,
            fail_after_batch=fail_after_batch,
        )
        if normalized_count < 0:
            return self._interrupted_result(query)
        checkpoints.save("NORMALIZE", {"normalized_record_count": normalized_count})
        self._maybe_stop_after_operator(run_id, run_dir, query, "NORMALIZE", fail_after_operator)

        machine.transition(RetrievalState.DEDUPLICATE)
        duplicate_count = self._duplicate_count(run_id, query.query_id)
        checkpoints.save("DEDUPLICATE", {"duplicate_count": duplicate_count})
        self._maybe_stop_after_operator(run_id, run_dir, query, "DEDUPLICATE", fail_after_operator)

        machine.transition(RetrievalState.RULE_PREFILTER)
        checkpoints.save(
            "RULE_PREFILTER",
            {"mode": "live_gpt" if live_mode else "deterministic_mock"},
        )
        machine.transition(RetrievalState.SCREEN_PASS_1)
        checkpoints.save(
            "SCREEN_PASS_1",
            {"screening_mode": "title_abstract_worker" if live_mode else "rule_backed_phase1_1"},
        )
        machine.transition(RetrievalState.ENRICH_DEFERRED)
        checkpoints.save("ENRICH_DEFERRED", {"real_api_calls": 0})
        machine.transition(RetrievalState.SCREEN_PASS_2)
        checkpoints.save(
            "SCREEN_PASS_2",
            {"screening_mode": "codex_gpt_structured" if live_mode else "no_llm_phase1_1"},
        )

        machine.transition(RetrievalState.PERSIST_FINAL_SCREENING)
        if live_mode:
            try:
                handoff_counts = self._screen_and_handoff_live(
                    run_id=run_id,
                    run_dir=run_dir,
                    query=query,
                    loaded=loaded,
                    runtime=runtime,
                )
            except ScreeningWorkerBlocked as exc:
                checkpoints.save("PERSIST_FINAL_SCREENING", exc.payload)
                return replace(
                    self._interrupted_result(query),
                    saturation_status=str(exc.payload["status"]),
                )
        else:
            handoff_counts = self._screen_and_handoff(
                run_id=run_id,
                run_dir=run_dir,
                query=query,
                loaded=loaded,
                runtime=runtime,
                batch_size=int(runtime["screening_batch_size"]),
                fail_after_batch=fail_after_batch,
                validate_schema=not stress_mode,
            )
        if handoff_counts.get("_interrupted"):
            return self._interrupted_result(query)
        if handoff_counts.get("handoff_backpressure_status") == "hard_limit":
            machine.transition(RetrievalState.EMIT_DOWNLOAD_HANDOFF)
            machine.transition(RetrievalState.PAUSED_DOWNSTREAM_BACKPRESSURE)
            self._update_run_status(run_id, "paused_downstream_backpressure")
            return replace(
                self._interrupted_result(query),
                saturation_status="paused_downstream_backpressure",
            )
        checkpoints.save("PERSIST_FINAL_SCREENING", handoff_counts)
        self._maybe_stop_after_operator(
            run_id, run_dir, query, "PERSIST_FINAL_SCREENING", fail_after_operator
        )

        machine.transition(RetrievalState.EMIT_DOWNLOAD_HANDOFF)
        checkpoints.save("EMIT_DOWNLOAD_HANDOFF", handoff_counts)
        self._maybe_stop_after_operator(
            run_id, run_dir, query, "EMIT_DOWNLOAD_HANDOFF", fail_after_operator
        )

        machine.transition(RetrievalState.VERIFY_HANDOFF)
        self._verify_transactional_handoff(run_id, query.query_id)
        checkpoints.save("VERIFY_HANDOFF", {"verified": True})

        machine.transition(RetrievalState.EVALUATE_QUERY)
        metrics = self._evaluate_query(
            run_id=run_id,
            query=query,
            plan=plan,
            loaded=loaded,
            source_counts=source_counts,
            duplicate_count=duplicate_count,
            handoff_counts=handoff_counts,
            target_novel_records=target_novel_records,
        )
        if live_mode:
            metrics = self._select_live_query(metrics, query)
            plan = replace(
                plan,
                decision=metrics.decision,
                acceptance_status=(
                    "accepted" if metrics.decision == "accept" else "rejected"
                ),
                decision_reason=metrics.decision_reason,
            )
        metrics = self._apply_saturation(run_id, query, plan, metrics, loaded)
        self._persist_metrics(metrics)
        write_json_atomic(run_dir / "metrics" / "metrics.json", metrics.to_dict())
        self._write_query_artifacts(run_id, query_dir, query, plan, metrics)
        checkpoints.save("EVALUATE_QUERY", {"total_score": metrics.total_score})

        machine.transition(RetrievalState.MINE_TERMS)
        mined_term_count = self._persist_term_events(run_id, query, plan)
        checkpoints.save("MINE_TERMS", {"candidate_terms": mined_term_count})

        machine.transition(RetrievalState.PROPOSE_VARIANTS)
        checkpoints.save("PROPOSE_VARIANTS", {"variants": 3 if query.iteration < 5 else 0})
        machine.transition(RetrievalState.TEST_VARIANTS)
        checkpoints.save("TEST_VARIANTS", {"tested_variants": 1})
        machine.transition(RetrievalState.SELECT_QUERY)
        checkpoints.save("SELECT_QUERY", {"decision": plan.decision})

        if plan.decision == "accept":
            machine.transition(RetrievalState.ACCEPT_QUERY)
            checkpoints.save("ACCEPT_QUERY", {"accepted_query_id": query.query_id})
        elif plan.decision == "reject":
            machine.transition(RetrievalState.REJECT_QUERY)
            checkpoints.save("REJECT_QUERY", {"rejected_query_id": query.query_id})
            machine.transition(RetrievalState.ROLLBACK)
            checkpoints.save("ROLLBACK", {"accepted_query_id": plan.parent_query_id})
        else:
            machine.transition(RetrievalState.ROLLBACK)
            checkpoints.save("ROLLBACK", {"accepted_query_id": plan.parent_query_id})

        machine.transition(RetrievalState.CHECK_SATURATION)
        checkpoints.save("CHECK_SATURATION", {"saturation_status": metrics.saturation_status})
        machine.transition(RetrievalState.EXPORT_RESULTS)
        self._finalize_query_iteration(metrics)
        if stress_mode:
            checkpoints.save("EXPORT_RESULTS", {"paper_exports": "skipped_stress_mode"})
        else:
            self._rebuild_audit_files(run_id, run_dir)
            self.export_paper_data(run_id)
            checkpoints.save("EXPORT_RESULTS", {"paper_exports": str(self.paper_exports_dir)})

        machine.transition(RetrievalState.FINALIZE_ITERATION)
        finalization = self._finalize_iteration(
            run_id, run_dir, query.query_id, rebuild_files=not stress_mode
        )
        checkpoints.save("FINALIZE_ITERATION", finalization)
        machine.transition(RetrievalState.RELEASE_ITERATION_MEMORY)
        checkpoints.save("RELEASE_ITERATION_MEMORY", {"cleanup_performed": True})
        return IterationResult(
            query_id=query.query_id,
            iteration=query.iteration,
            total_score=metrics.total_score,
            score_delta=metrics.score_delta,
            decision=metrics.decision,
            saturation_status=metrics.saturation_status,
            row_counts=finalization["counts"],
        )

    def _search_sources(
        self,
        *,
        run_id: str,
        run_dir: Path,
        query: CanonicalQuery,
        runtime: dict[str, Any],
        max_scan_depth: int,
        fail_after_source_page: int | None,
        stress_records_per_source: int | None,
    ) -> dict[str, Any]:
        page_size = int(runtime["retrieval_page_size"])
        summary: dict[str, Any] = {
            "raw_result_count": 0,
            "scanned_result_count": 0,
            "source_statuses": {},
        }
        for source in SOURCE_NAMES:
            pages = self._mock_pages(
                source,
                query.iteration,
                page_size,
                max_scan_depth,
                stress_records_per_source=stress_records_per_source,
            )
            for page_number, page_records in enumerate(pages, start=1):
                if not page_records:
                    continue
                batch_id = f"{source}_page_{page_number:04d}"
                if self._checkpoint_completed(
                    run_id, query.query_id, query.iteration, "SEARCH_SOURCES", source, batch_id
                ):
                    summary["scanned_result_count"] += len(page_records)
                    summary["raw_result_count"] += len(page_records)
                    continue
                with MemoryTelemetry(
                    run_dir=run_dir,
                    run_id=run_id,
                    query_id=query.query_id,
                    iteration=query.iteration,
                    operator="SEARCH_SOURCES",
                    batch_id=batch_id,
                ) as telemetry:
                    raw_path = (
                        run_dir
                        / "raw_metadata"
                        / "batches"
                        / f"{query.query_id}_{batch_id}.jsonl"
                    )
                    lines = []
                    with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
                        for rank_offset, payload in enumerate(page_records, start=1):
                            rank = (page_number - 1) * page_size + rank_offset
                            raw_record = RawRecord(
                                source_name=source,
                                source_record_id=str(payload["source_record_id"]),
                                rank=rank,
                                raw=dict(payload),
                                retrieval_timestamp=utc_now_iso(),
                            )
                            raw_json = json.dumps(
                                raw_record.to_dict(), ensure_ascii=True, sort_keys=True
                            )
                            checksum = hashlib.sha256(raw_json.encode()).hexdigest()
                            lines.append(raw_json)
                            connection.execute(
                                """
                                INSERT OR IGNORE INTO source_records (
                                    source_name, source_record_id, query_id, run_id,
                                    source_rank, retrieval_page, retrieval_cursor,
                                    raw_metadata_path, raw_payload_checksum,
                                    raw_payload_json, retrieved_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    source,
                                    raw_record.source_record_id,
                                    query.query_id,
                                    run_id,
                                    rank,
                                    page_number,
                                    str((page_number - 1) * page_size),
                                    self._display_path(raw_path),
                                    checksum,
                                    raw_json,
                                    raw_record.retrieval_timestamp,
                                ),
                            )
                        self._record_checkpoint_sql(
                            connection=connection,
                            run_id=run_id,
                            query_id=query.query_id,
                            iteration=query.iteration,
                            operator="SEARCH_SOURCES",
                            batch_id=batch_id,
                            source_name=source,
                            page_cursor=str((page_number - 1) * page_size),
                            next_page_cursor=str(page_number * page_size),
                            processed_count=len(page_records),
                            persisted_count=len(page_records),
                        )
                    write_text_atomic(raw_path, "\n".join(lines) + "\n")
                    telemetry.add_records(len(page_records))
                    telemetry.add_bytes(raw_path.stat().st_size)
                summary["scanned_result_count"] += len(page_records)
                summary["raw_result_count"] += len(page_records)
                summary["source_statuses"][source] = SourceExecutionStatus.SOURCE_SUCCESS.value
                self._failure_source_page_count += 1
                if (
                    fail_after_source_page is not None
                    and self._failure_source_page_count >= fail_after_source_page
                ):
                    self._update_run_status(run_id, "interrupted_injected")
                    return summary | {"_interrupted": True}
        return summary

    def _search_sources_external(
        self,
        *,
        run_id: str,
        run_dir: Path,
        query: CanonicalQuery,
        runtime: dict[str, Any],
        max_scan_depth: int,
        providers: list[str] | None,
        max_records_per_provider: int | None = None,
    ) -> dict[str, Any]:
        checkpoint_path = run_dir / "checkpoints" / "SEARCH_SOURCES.json"
        if checkpoint_path.exists():
            checkpoint = dict(read_json(checkpoint_path))
            payload = dict(checkpoint.get("payload") or {})
            if (
                payload
                and not payload.get("_interrupted")
                and payload.get("query_id") == query.query_id
            ):
                return payload
        page_size = int(runtime["retrieval_page_size"])
        with MemoryTelemetry(
            run_dir=run_dir,
            run_id=run_id,
            query_id=query.query_id,
            iteration=query.iteration,
            operator="SEARCH_SOURCES",
            batch_id="external_metadata_discovery_v1",
        ) as telemetry:
            gateway = ExternalMetadataDiscoveryGateway(
                repo_root=self.repo_root,
                db_path=self.db_path,
                code_commit_sha=self._git_sha(),
            )
            output = gateway.invoke(
                run_id=run_id,
                query=query,
                run_dir=run_dir,
                providers=providers or SOURCE_NAMES,
                page_size=page_size,
                max_candidates=max_records_per_provider
                or page_size * max(1, max_scan_depth),
                max_scan_depth_per_provider=max_scan_depth,
                config_ref=self.config_dir / "sources.yaml",
            )
            summary = gateway.import_into_control_plane(
                run_id=run_id,
                query=query,
                discovery_output=output,
            )
            summary = summary | {"query_id": query.query_id}
            telemetry.add_records(int(summary["raw_result_count"]))
            telemetry.add_bytes(Path(str(output["output_ref"])).stat().st_size)
        return summary

    def _search_sources_checkpoint_matches(self, run_dir: Path, query_id: str) -> bool:
        checkpoint_path = run_dir / "checkpoints" / "SEARCH_SOURCES.json"
        if not checkpoint_path.exists():
            return False
        checkpoint = dict(read_json(checkpoint_path))
        payload = dict(checkpoint.get("payload") or {})
        return bool(payload and payload.get("query_id") == query_id)

    def _normalize_and_register(
        self,
        *,
        run_id: str,
        run_dir: Path,
        query: CanonicalQuery,
        batch_size: int,
        target_novel_records: int,
        fail_after_batch: int | None,
    ) -> int:
        total = 0
        while True:
            with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM source_records
                    WHERE run_id = ? AND query_id = ? AND normalized_at IS NULL
                    ORDER BY source_record_pk
                    LIMIT ?
                    """,
                    (run_id, query.query_id, batch_size),
                ).fetchall()
            if not rows:
                return total
            first_pk = int(rows[0]["source_record_pk"])
            last_pk = int(rows[-1]["source_record_pk"])
            batch_id = f"normalization_{first_pk}_{last_pk}"
            if self._checkpoint_completed(
                run_id, query.query_id, query.iteration, "NORMALIZE", None, batch_id
            ):
                total += len(rows)
                continue
            with MemoryTelemetry(
                run_dir=run_dir,
                run_id=run_id,
                query_id=query.query_id,
                iteration=query.iteration,
                operator="NORMALIZE",
                batch_id=batch_id,
            ) as telemetry:
                normalized_lines: list[str] = []
                with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
                    for row in rows:
                        raw_payload = json.loads(str(row["raw_payload_json"]))
                        raw_record = RawRecord(**raw_payload)
                        normalized = self.normalizer.normalize(
                            raw_record, str(row["raw_metadata_path"])
                        )
                        global_record_id = self._resolve_or_insert_document(
                            connection, run_id, query, normalized
                        )
                        normalized = replace(normalized, global_record_id=global_record_id)
                        first_membership = self._membership_count(
                            connection, run_id, query.query_id, global_record_id
                        ) == 0
                        already_known = self._already_known_before_query(
                            connection, run_id, query.query_id, global_record_id
                        )
                        include_in_sample = (
                            first_membership
                            and not already_known
                            and self._novelty_sample_count(
                                connection, run_id, query.query_id
                            )
                            < target_novel_records
                        )
                        novelty_position = (
                            self._novelty_sample_count(connection, run_id, query.query_id) + 1
                            if include_in_sample
                            else None
                        )
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO document_sources (
                                global_record_id, source_name, source_record_id, source_rank,
                                run_id, query_id, raw_metadata_path, metadata_version,
                                retrieved_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                global_record_id,
                                normalized.source_records[0]["source_name"],
                                normalized.source_records[0]["source_record_id"],
                                int(normalized.source_records[0]["rank"]),
                                run_id,
                                query.query_id,
                                normalized.raw_metadata_path or "",
                                normalized.normalization_version,
                                normalized.retrieval_timestamp,
                            ),
                        )
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO document_query_membership (
                                global_record_id, run_id, query_id, iteration, source_name,
                                source_rank, first_seen_in_query,
                                already_known_before_query, included_in_novelty_sample,
                                novelty_sample_position, screening_status_at_iteration,
                                created_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                            """,
                            (
                                global_record_id,
                                run_id,
                                query.query_id,
                                query.iteration,
                                normalized.source_records[0]["source_name"],
                                int(normalized.source_records[0]["rank"]),
                                1 if first_membership and not already_known else 0,
                                1 if already_known else 0,
                                1 if include_in_sample else 0,
                                novelty_position,
                                utc_now_iso(),
                            ),
                        )
                        self._maybe_candidate_duplicate(connection, normalized, global_record_id)
                        connection.execute(
                            """
                            UPDATE source_records
                            SET normalized_at = ?
                            WHERE source_record_pk = ?
                            """,
                            (utc_now_iso(), row["source_record_pk"]),
                        )
                        normalized_lines.append(
                            json.dumps(normalized.to_dict(), ensure_ascii=True, sort_keys=True)
                        )
                    self._record_checkpoint_sql(
                        connection=connection,
                        run_id=run_id,
                        query_id=query.query_id,
                        iteration=query.iteration,
                        operator="NORMALIZE",
                        batch_id=batch_id,
                        source_name=None,
                        page_cursor=None,
                        next_page_cursor=None,
                        processed_count=len(rows),
                        persisted_count=len(rows),
                    )
                batch_path = (
                    run_dir
                    / "normalized"
                    / "batches"
                    / f"{query.query_id}_{batch_id}.jsonl"
                )
                write_text_atomic(batch_path, "\n".join(normalized_lines) + "\n")
                telemetry.add_records(len(rows))
                telemetry.add_bytes(batch_path.stat().st_size)
            total += len(rows)
            self._failure_batch_count += 1
            if fail_after_batch is not None and self._failure_batch_count >= fail_after_batch:
                self._update_run_status(run_id, "interrupted_injected")
                return -1

    def _screen_and_handoff(
        self,
        *,
        run_id: str,
        run_dir: Path,
        query: CanonicalQuery,
        loaded: LoadedConfig,
        runtime: dict[str, Any],
        batch_size: int,
        fail_after_batch: int | None,
        validate_schema: bool = True,
    ) -> dict[str, Any]:
        screening_decisions = 0
        download_requests_emitted = 0
        duplicate_handoffs_suppressed = 0
        handoff_failures = 0
        pending_download_jobs = 0
        handoff_backpressure_status = "ok"
        handoff_enabled = bool(self._handoff_settings(loaded).get("enabled", True))
        while True:
            batch = self._next_screening_batch(run_id, query.query_id, batch_size)
            if not batch:
                pending_download_jobs = int(
                    self.download_queue_status()["pending_download_jobs"]
                )
                handoff_backpressure_status = self._backpressure_status(
                    pending_download_jobs, loaded
                )
                return {
                    "screening_decisions": screening_decisions,
                    "download_requests_emitted": download_requests_emitted,
                    "duplicate_handoffs_suppressed": duplicate_handoffs_suppressed,
                    "handoff_failures": handoff_failures,
                    "pending_download_jobs": pending_download_jobs,
                    "handoff_backpressure_status": handoff_backpressure_status,
                }
            first_id = batch[0].global_record_id
            last_id = batch[-1].global_record_id
            digest = hashlib.sha256((first_id + last_id).encode()).hexdigest()[:12]
            batch_id = f"screening_{digest}"
            if self._checkpoint_completed(
                run_id,
                query.query_id,
                query.iteration,
                "PERSIST_FINAL_SCREENING",
                None,
                batch_id,
            ):
                continue
            with MemoryTelemetry(
                run_dir=run_dir,
                run_id=run_id,
                query_id=query.query_id,
                iteration=query.iteration,
                operator="PERSIST_FINAL_SCREENING",
                batch_id=batch_id,
            ) as telemetry:
                decisions = self.screener.screen(
                    batch,
                    run_id=run_id,
                    query_id=query.query_id,
                    iteration=query.iteration,
                    audit_batch_id=batch_id,
                    prompt_hash=self._prompt_hash(),
                    scie_status=self._scie_status(loaded.protocol),
                )
                inserted_events: list[dict[str, Any]] = []
                duplicate_suppressed = 0
                with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
                    for record, decision in zip(batch, decisions, strict=True):
                        if validate_schema:
                            self._validate_screening_decision(decision)
                        self._insert_screening_decision(connection, decision)
                        connection.execute(
                            """
                            UPDATE document_query_membership
                            SET screening_status_at_iteration = ?
                            WHERE run_id = ? AND query_id = ? AND global_record_id = ?
                            """,
                            (decision.decision, run_id, query.query_id, record.global_record_id),
                        )
                        connection.execute(
                            """
                            UPDATE documents
                            SET current_screening_status = ?
                            WHERE global_record_id = ?
                            """,
                            (decision.decision, record.global_record_id),
                        )
                        if decision.decision != "include":
                            connection.execute(
                                """
                                UPDATE documents
                                SET current_download_status = 'not_eligible_for_handoff'
                                WHERE global_record_id = ?
                                """,
                                (record.global_record_id,),
                            )
                            continue
                        if not handoff_enabled:
                            connection.execute(
                                """
                                UPDATE documents
                                SET current_download_status = 'handoff_disabled'
                                WHERE global_record_id = ?
                                """,
                                (record.global_record_id,),
                            )
                            continue
                        event = self._download_event(record, decision, loaded)
                        inserted = self._insert_download_event(connection, event)
                        if inserted:
                            inserted_events.append(event)
                        else:
                            duplicate_suppressed += 1
                            self._insert_audit_event(
                                connection,
                                run_id=run_id,
                                query_id=query.query_id,
                                global_record_id=record.global_record_id,
                                event_type="duplicate_handoff_suppressed",
                                payload={"idempotency_key": event["idempotency_key"]},
                                actor="Retrieval Specialist",
                            )
                    self._record_checkpoint_sql(
                        connection=connection,
                        run_id=run_id,
                        query_id=query.query_id,
                        iteration=query.iteration,
                        operator="PERSIST_FINAL_SCREENING",
                        batch_id=batch_id,
                        source_name=None,
                        page_cursor=None,
                        next_page_cursor=None,
                        processed_count=len(decisions),
                        persisted_count=len(decisions),
                    )
                batch_path = (
                    run_dir
                    / "screening"
                    / "batches"
                    / f"{query.query_id}_{batch_id}.jsonl"
                )
                write_text_atomic(
                    batch_path,
                    "\n".join(
                        json.dumps(decision.to_dict(), ensure_ascii=True, sort_keys=True)
                        for decision in decisions
                    )
                    + "\n",
                )
                telemetry.add_records(len(batch))
                telemetry.add_bytes(batch_path.stat().st_size)
                screening_decisions += len(decisions)
                download_requests_emitted += len(inserted_events)
                duplicate_handoffs_suppressed += duplicate_suppressed
                self._mirror_download_events(run_dir, inserted_events)
            self._failure_batch_count += 1
            if fail_after_batch is not None and self._failure_batch_count >= fail_after_batch:
                self._update_run_status(run_id, "interrupted_injected")
                return {
                    "screening_decisions": screening_decisions,
                    "download_requests_emitted": download_requests_emitted,
                    "duplicate_handoffs_suppressed": duplicate_handoffs_suppressed,
                    "handoff_failures": handoff_failures,
                    "pending_download_jobs": pending_download_jobs,
                    "handoff_backpressure_status": handoff_backpressure_status,
                    "_interrupted": True,
                }
            pending_download_jobs = int(self.download_queue_status()["pending_download_jobs"])
            handoff_backpressure_status = self._backpressure_status(
                pending_download_jobs, loaded
            )
            if handoff_backpressure_status == "hard_limit":
                return {
                    "screening_decisions": screening_decisions,
                    "download_requests_emitted": download_requests_emitted,
                    "duplicate_handoffs_suppressed": duplicate_handoffs_suppressed,
                    "handoff_failures": handoff_failures,
                    "pending_download_jobs": pending_download_jobs,
                    "handoff_backpressure_status": handoff_backpressure_status,
                }

    def _screen_and_handoff_live(
        self,
        *,
        run_id: str,
        run_dir: Path,
        query: CanonicalQuery,
        loaded: LoadedConfig,
        runtime: dict[str, Any],
        max_novelty_decisions: int | None = None,
        novelty_against_query_id: str | None = None,
    ) -> dict[str, Any]:
        del runtime
        screening_decisions = 0
        download_requests_emitted = 0
        duplicate_handoffs_suppressed = 0
        handoff_failures = 0
        pending_download_jobs = 0
        handoff_backpressure_status = "ok"
        handoff_enabled = bool(self._handoff_settings(loaded).get("enabled", True))
        already_screened_novelty = self._screened_novelty_decision_count(
            run_id,
            query.query_id,
            novelty_against_query_id=novelty_against_query_id,
        )
        executor = TitleAbstractScreeningWorkerExecutor(
            repo_root=self.repo_root,
            run_dir=run_dir,
            schema_path=self._schema_path("retrieval", "screening_decision.schema.json"),
        )
        while True:
            if (
                max_novelty_decisions is not None
                and already_screened_novelty + screening_decisions >= max_novelty_decisions
            ):
                pending_download_jobs = int(
                    self.download_queue_status()["pending_download_jobs"]
                )
                handoff_backpressure_status = self._backpressure_status(
                    pending_download_jobs, loaded
                )
                return {
                    "screening_decisions": screening_decisions,
                    "download_requests_emitted": download_requests_emitted,
                    "duplicate_handoffs_suppressed": duplicate_handoffs_suppressed,
                    "handoff_failures": handoff_failures,
                    "pending_download_jobs": pending_download_jobs,
                    "handoff_backpressure_status": handoff_backpressure_status,
                    "screening_stop_reason": "candidate_evaluation_target_reached",
                    "screened_novelty_decisions": already_screened_novelty
                    + screening_decisions,
                    "screening_target_novelty_decisions": max_novelty_decisions,
                }
            batch = self._next_screening_batch(
                run_id,
                query.query_id,
                1,
                novelty_sample_only=True,
                novelty_against_query_id=novelty_against_query_id,
            )
            if not batch:
                pending_download_jobs = int(
                    self.download_queue_status()["pending_download_jobs"]
                )
                handoff_backpressure_status = self._backpressure_status(
                    pending_download_jobs, loaded
                )
                return {
                    "screening_decisions": screening_decisions,
                    "download_requests_emitted": download_requests_emitted,
                    "duplicate_handoffs_suppressed": duplicate_handoffs_suppressed,
                    "handoff_failures": handoff_failures,
                    "pending_download_jobs": pending_download_jobs,
                    "handoff_backpressure_status": handoff_backpressure_status,
                }
            record = batch[0]
            record_hash = hashlib.sha256(record.global_record_id.encode()).hexdigest()
            batch_id = f"screening_{record_hash[:12]}"
            if self._checkpoint_completed(
                run_id,
                query.query_id,
                query.iteration,
                "PERSIST_FINAL_SCREENING",
                None,
                batch_id,
            ):
                continue
            with MemoryTelemetry(
                run_dir=run_dir,
                run_id=run_id,
                query_id=query.query_id,
                iteration=query.iteration,
                operator="PERSIST_FINAL_SCREENING",
                batch_id=batch_id,
            ) as telemetry:
                decision = self.screener.prefilter_one(
                    record,
                    run_id=run_id,
                    query_id=query.query_id,
                    iteration=query.iteration,
                    audit_batch_id=batch_id,
                    prompt_hash=self._prompt_hash(),
                    scie_status=self._scie_status(loaded.protocol),
                )
                if decision is None:
                    decision = executor.screen_one(
                        record,
                        run_id=run_id,
                        query_id=query.query_id,
                        iteration=query.iteration,
                        audit_batch_id=batch_id,
                        protocol=loaded.protocol,
                        allowed_reason_codes=self._allowed_reason_codes(),
                        scie_status=self._scie_status(loaded.protocol),
                    )
                self._validate_screening_decision(decision)
                inserted_events: list[dict[str, Any]] = []
                duplicate_suppressed = 0
                with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
                    self._insert_screening_decision(connection, decision)
                    connection.execute(
                        """
                        UPDATE document_query_membership
                        SET screening_status_at_iteration = ?
                        WHERE run_id = ? AND query_id = ? AND global_record_id = ?
                        """,
                        (decision.decision, run_id, query.query_id, record.global_record_id),
                    )
                    connection.execute(
                        """
                        UPDATE documents
                        SET current_screening_status = ?
                        WHERE global_record_id = ?
                        """,
                        (decision.decision, record.global_record_id),
                    )
                    if decision.decision != "include":
                        connection.execute(
                            """
                            UPDATE documents
                            SET current_download_status = 'not_eligible_for_handoff'
                            WHERE global_record_id = ?
                            """,
                            (record.global_record_id,),
                        )
                    elif not handoff_enabled:
                        connection.execute(
                            """
                            UPDATE documents
                            SET current_download_status = 'handoff_disabled'
                            WHERE global_record_id = ?
                            """,
                            (record.global_record_id,),
                        )
                    else:
                        event = self._download_event(record, decision, loaded)
                        inserted = self._insert_download_event(connection, event)
                        if inserted:
                            inserted_events.append(event)
                        else:
                            duplicate_suppressed += 1
                            self._insert_audit_event(
                                connection,
                                run_id=run_id,
                                query_id=query.query_id,
                                global_record_id=record.global_record_id,
                                event_type="duplicate_handoff_suppressed",
                                payload={"idempotency_key": event["idempotency_key"]},
                                actor="Retrieval Specialist",
                            )
                    self._record_checkpoint_sql(
                        connection=connection,
                        run_id=run_id,
                        query_id=query.query_id,
                        iteration=query.iteration,
                        operator="PERSIST_FINAL_SCREENING",
                        batch_id=batch_id,
                        source_name=None,
                        page_cursor=None,
                        next_page_cursor=None,
                        processed_count=1,
                        persisted_count=1,
                    )
                batch_path = (
                    run_dir
                    / "screening"
                    / "batches"
                    / f"{query.query_id}_{batch_id}.jsonl"
                )
                write_text_atomic(
                    batch_path,
                    json.dumps(decision.to_dict(), ensure_ascii=True, sort_keys=True)
                    + "\n",
                )
                telemetry.add_records(1)
                telemetry.add_bytes(batch_path.stat().st_size)
                screening_decisions += 1
                download_requests_emitted += len(inserted_events)
                duplicate_handoffs_suppressed += duplicate_suppressed
                self._mirror_download_events(run_dir, inserted_events)
            del record, batch, decision
            release_iteration_memory()
            pending_download_jobs = int(self.download_queue_status()["pending_download_jobs"])
            handoff_backpressure_status = self._backpressure_status(
                pending_download_jobs, loaded
            )
            if handoff_backpressure_status == "hard_limit":
                return {
                    "screening_decisions": screening_decisions,
                    "download_requests_emitted": download_requests_emitted,
                    "duplicate_handoffs_suppressed": duplicate_handoffs_suppressed,
                    "handoff_failures": handoff_failures,
                    "pending_download_jobs": pending_download_jobs,
                    "handoff_backpressure_status": handoff_backpressure_status,
                }

    def _screen_candidate_loss_audit_live(
        self,
        *,
        run_id: str,
        run_dir: Path,
        parent_query_id: str | None,
        candidate_query_id: str,
        loaded: LoadedConfig,
        loss_audit_target_records: int,
    ) -> dict[str, int]:
        if parent_query_id is None or loss_audit_target_records <= 0:
            return {
                "screening_decisions": 0,
                "download_requests_emitted": 0,
                "duplicate_handoffs_suppressed": 0,
                "handoff_failures": 0,
            }
        executor = TitleAbstractScreeningWorkerExecutor(
            repo_root=self.repo_root,
            run_dir=run_dir,
            schema_path=self._schema_path("retrieval", "screening_decision.schema.json"),
        )
        screening_decisions = 0
        download_requests_emitted = 0
        duplicate_handoffs_suppressed = 0
        handoff_failures = 0
        while screening_decisions < loss_audit_target_records:
            batch = self._next_candidate_loss_audit_batch(
                run_id=run_id,
                parent_query_id=parent_query_id,
                candidate_query_id=candidate_query_id,
                batch_size=1,
            )
            if not batch:
                break
            record = batch[0]
            record_hash = hashlib.sha256(
                f"{candidate_query_id}|loss|{record.global_record_id}".encode()
            ).hexdigest()
            batch_id = f"loss_audit_{record_hash[:12]}"
            if self._checkpoint_completed(
                run_id,
                parent_query_id,
                0,
                "PERSIST_CANDIDATE_LOSS_AUDIT",
                None,
                batch_id,
            ):
                continue
            with MemoryTelemetry(
                run_dir=run_dir,
                run_id=run_id,
                query_id=parent_query_id,
                iteration=0,
                operator="PERSIST_CANDIDATE_LOSS_AUDIT",
                batch_id=batch_id,
            ) as telemetry:
                decision = self.screener.prefilter_one(
                    record,
                    run_id=run_id,
                    query_id=parent_query_id,
                    iteration=0,
                    audit_batch_id=batch_id,
                    prompt_hash=self._prompt_hash(),
                    scie_status=self._scie_status(loaded.protocol),
                )
                if decision is None:
                    decision = executor.screen_one(
                        record,
                        run_id=run_id,
                        query_id=parent_query_id,
                        iteration=0,
                        audit_batch_id=batch_id,
                        protocol=loaded.protocol,
                        allowed_reason_codes=self._allowed_reason_codes(),
                        scie_status=self._scie_status(loaded.protocol),
                    )
                self._validate_screening_decision(decision)
                inserted_events: list[dict[str, Any]] = []
                duplicate_suppressed = 0
                with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
                    self._insert_screening_decision(connection, decision)
                    connection.execute(
                        """
                        UPDATE document_query_membership
                        SET screening_status_at_iteration = ?
                        WHERE run_id = ? AND query_id = ? AND global_record_id = ?
                        """,
                        (decision.decision, run_id, parent_query_id, record.global_record_id),
                    )
                    connection.execute(
                        """
                        UPDATE documents
                        SET current_screening_status = ?
                        WHERE global_record_id = ?
                        """,
                        (decision.decision, record.global_record_id),
                    )
                    if decision.decision != "include":
                        connection.execute(
                            """
                            UPDATE documents
                            SET current_download_status = 'not_eligible_for_handoff'
                            WHERE global_record_id = ?
                            """,
                            (record.global_record_id,),
                        )
                    elif not bool(self._handoff_settings(loaded).get("enabled", True)):
                        connection.execute(
                            """
                            UPDATE documents
                            SET current_download_status = 'handoff_disabled'
                            WHERE global_record_id = ?
                            """,
                            (record.global_record_id,),
                        )
                    else:
                        event = self._download_event(record, decision, loaded)
                        inserted = self._insert_download_event(connection, event)
                        if inserted:
                            inserted_events.append(event)
                        else:
                            duplicate_suppressed += 1
                    self._record_checkpoint_sql(
                        connection=connection,
                        run_id=run_id,
                        query_id=parent_query_id,
                        iteration=0,
                        operator="PERSIST_CANDIDATE_LOSS_AUDIT",
                        batch_id=batch_id,
                        source_name=None,
                        page_cursor=None,
                        next_page_cursor=None,
                        processed_count=1,
                        persisted_count=1,
                    )
                batch_path = (
                    run_dir
                    / "screening"
                    / "batches"
                    / f"{parent_query_id}_{batch_id}.jsonl"
                )
                write_text_atomic(
                    batch_path,
                    json.dumps(decision.to_dict(), ensure_ascii=True, sort_keys=True)
                    + "\n",
                )
                telemetry.add_records(1)
                telemetry.add_bytes(batch_path.stat().st_size)
                screening_decisions += 1
                download_requests_emitted += len(inserted_events)
                duplicate_handoffs_suppressed += duplicate_suppressed
                self._mirror_download_events(run_dir, inserted_events)
            del record, batch, decision
            release_iteration_memory()
        return {
            "screening_decisions": screening_decisions,
            "download_requests_emitted": download_requests_emitted,
            "duplicate_handoffs_suppressed": duplicate_handoffs_suppressed,
            "handoff_failures": handoff_failures,
        }

    def _evaluate_query(
        self,
        *,
        run_id: str,
        query: CanonicalQuery,
        plan: IterationPlan,
        loaded: LoadedConfig,
        source_counts: dict[str, Any],
        duplicate_count: int,
        handoff_counts: dict[str, Any],
        target_novel_records: int | None = None,
    ) -> QueryMetrics:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            counts = self._decision_counts(connection, run_id, query.query_id)
            novelty = self._novelty_counts(connection, run_id, query.query_id)
            sample = self._novelty_sample_counts(connection, run_id, query.query_id)
            rates = self._rate_components(connection, run_id, query.query_id)
            parent_score = self._parent_score(connection, run_id, plan.parent_query_id)
            screening_model = self._screening_model_summary(
                connection, run_id, query.query_id
            )
        include_count = counts["include"]
        exclude_count = counts["exclude"]
        defer_count = counts["defer"]
        evaluated_count = include_count + exclude_count
        marginal_eligible_count = int(rates["marginal_eligible_count"])
        fully_evaluated_novel = int(rates["fully_evaluated_novel"])
        cumulative_eligible_count = int(rates["cumulative_eligible_count"])
        retrieval_source_breadth = float(rates["retrieval_source_breadth"])
        eligible_source_breadth = float(rates["eligible_source_breadth"])
        metadata_completeness = float(rates["metadata_completeness"])
        scope_diversity = float(rates["scope_diversity"])
        eligible_waterbody_diversity = float(rates["eligible_waterbody_diversity"])
        excluded_matrix_rate = float(rates["excluded_matrix_rate"])
        laboratory_study_rate = float(rates["laboratory_study_rate"])
        no_concentration_rate = float(rates["no_concentration_rate"])
        known_eligible_overlap_rate = float(rates["known_eligible_overlap_rate"])
        known_ineligible_overlap_rate = float(rates["known_ineligible_overlap_rate"])
        query_complexity = float(rates["query_complexity"])
        retrospective_query_coverage = float(rates["retrospective_query_coverage"])
        target_novel = int(
            target_novel_records
            if target_novel_records is not None
            else loaded.stopping["evaluation"]["target_novel_records_total"]
        )
        eligible_precision = include_count / max(1, evaluated_count)
        novel_precision = sample["include"] / max(1, sample["evaluated"])
        marginal_relevant_yield = marginal_eligible_count / max(1, fully_evaluated_novel)
        novelty_rate = novelty["novel_record_count"] / max(
            1, int(source_counts["scanned_result_count"])
        )
        normalized_yield = clamp(marginal_eligible_count / max(1, target_novel))
        scoring = loaded.scoring
        positive = scoring["positive_weights"]
        penalty = scoring["penalty_weights"]
        cumulative_yield = clamp(cumulative_eligible_count / max(1, target_novel * 3))
        positive_score = (
            float(positive["novel_precision_at_20"]) * novel_precision
            + float(positive["normalized_novel_eligible_yield"]) * normalized_yield
            + float(positive["novelty_rate"]) * novelty_rate
            + float(positive["cross_source_breadth"]) * retrieval_source_breadth
            + float(positive["scope_diversity"]) * scope_diversity
            + float(positive["metadata_completeness"]) * metadata_completeness
            + float(positive.get("cumulative_eligible_yield", 0.0)) * cumulative_yield
        )
        defer_rate = defer_count / max(1, include_count + exclude_count + defer_count)
        penalty_score = (
            float(penalty["defer_rate"]) * defer_rate
            + float(penalty["excluded_matrix_rate"]) * excluded_matrix_rate
            + float(penalty["laboratory_study_rate"]) * laboratory_study_rate
            + float(penalty["no_concentration_rate"]) * no_concentration_rate
            + float(penalty["known_ineligible_overlap_rate"])
            * known_ineligible_overlap_rate
            + float(penalty["query_complexity"]) * query_complexity
        )
        total_score = clamp(positive_score - penalty_score)
        score_delta = total_score - parent_score
        return QueryMetrics(
            run_id=run_id,
            iteration=query.iteration,
            query_id=query.query_id,
            parent_query_id=query.parent_query_id,
            raw_result_count=int(source_counts["raw_result_count"]),
            scanned_result_count=int(source_counts["scanned_result_count"]),
            known_record_count=novelty["known_record_count"],
            novel_record_count=novelty["novel_record_count"],
            target_novel_n=target_novel,
            actual_novel_n=sample["actual_novel_n"],
            target_reached=sample["actual_novel_n"] >= target_novel,
            include_count=include_count,
            exclude_count=exclude_count,
            defer_count=defer_count,
            eligible_precision=eligible_precision,
            novel_precision_at_20=clamp(novel_precision),
            novel_eligible_yield=marginal_eligible_count,
            normalized_novel_eligible_yield=normalized_yield,
            marginal_relevant_yield=marginal_relevant_yield,
            novelty_rate=clamp(novelty_rate),
            cumulative_eligible_count=cumulative_eligible_count,
            retrospective_query_coverage=retrospective_query_coverage,
            cross_source_breadth=retrieval_source_breadth,
            metadata_completeness=metadata_completeness,
            scope_diversity=scope_diversity,
            excluded_matrix_rate=excluded_matrix_rate,
            laboratory_study_rate=laboratory_study_rate,
            no_concentration_rate=no_concentration_rate,
            duplicate_rate=duplicate_count / max(1, int(source_counts["raw_result_count"])),
            known_eligible_overlap_rate=known_eligible_overlap_rate,
            known_ineligible_overlap_rate=known_ineligible_overlap_rate,
            query_complexity=query_complexity,
            positive_score=positive_score,
            penalty_score=penalty_score,
            total_score=total_score,
            score_delta=score_delta,
            decision=plan.decision,
            decision_reason=plan.decision_reason,
            saturation_status="not_saturated",
            source_completeness=self._source_completeness_from_statuses(source_counts),
            timestamp=utc_now_iso(),
            code_commit_sha=self._git_sha(),
            config_hash=loaded.config_hash,
            prompt_hash=self._prompt_hash(),
            model_name=screening_model["model_name"],
            model_parameters=screening_model["model_parameters"],
            download_requests_emitted=int(handoff_counts["download_requests_emitted"]),
            duplicate_handoffs_suppressed=min(
                int(handoff_counts["duplicate_handoffs_suppressed"]), include_count
            ),
            pending_download_jobs_at_iteration_end=int(handoff_counts["pending_download_jobs"]),
            handoff_backpressure_status=str(handoff_counts["handoff_backpressure_status"]),
            evaluated_record_count=evaluated_count,
            deferred_record_count=defer_count,
            defer_rate=defer_rate,
            marginal_eligible_count=marginal_eligible_count,
            retrieval_source_breadth=retrieval_source_breadth,
            eligible_source_breadth=eligible_source_breadth,
            eligible_waterbody_diversity=eligible_waterbody_diversity,
            coverage_scope="novelty_frontier",
        )

    def _apply_saturation(
        self,
        run_id: str,
        query: CanonicalQuery,
        plan: IterationPlan,
        metrics: QueryMetrics,
        loaded: LoadedConfig,
    ) -> QueryMetrics:
        rounds = int(loaded.stopping["evaluation"]["saturation_rounds"])
        novelty_threshold = float(
            loaded.stopping["evaluation"]["novelty_rate_saturation_threshold"]
        )
        low_novelty = metrics.novelty_rate < novelty_threshold
        low_yield = metrics.marginal_eligible_count <= 2
        low_score = query.iteration > 1 and abs(metrics.score_delta) < 0.01
        no_terms = not plan.added_terms and query.iteration > 1
        known_noise = metrics.known_ineligible_overlap_rate > 0.5
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            row = connection.execute(
                "SELECT * FROM saturation_counters WHERE run_id = ?", (run_id,)
            ).fetchone()
            counters = {
                "consecutive_low_novelty_rounds": 0,
                "consecutive_low_yield_rounds": 0,
                "consecutive_low_score_improvement_rounds": 0,
                "consecutive_no_effective_term_rounds": 0,
                "consecutive_known_noise_dominance_rounds": 0,
            }
            if row is not None:
                counters.update({key: int(row[key]) for key in counters})
            counters["consecutive_low_novelty_rounds"] = (
                counters["consecutive_low_novelty_rounds"] + 1 if low_novelty else 0
            )
            counters["consecutive_low_yield_rounds"] = (
                counters["consecutive_low_yield_rounds"] + 1 if low_yield else 0
            )
            counters["consecutive_low_score_improvement_rounds"] = (
                counters["consecutive_low_score_improvement_rounds"] + 1 if low_score else 0
            )
            counters["consecutive_no_effective_term_rounds"] = (
                counters["consecutive_no_effective_term_rounds"] + 1 if no_terms else 0
            )
            counters["consecutive_known_noise_dominance_rounds"] = (
                counters["consecutive_known_noise_dominance_rounds"] + 1 if known_noise else 0
            )
            connection.execute(
                """
                INSERT INTO saturation_counters (
                    run_id, consecutive_low_novelty_rounds,
                    consecutive_low_yield_rounds,
                    consecutive_low_score_improvement_rounds,
                    consecutive_no_effective_term_rounds,
                    consecutive_known_noise_dominance_rounds, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    consecutive_low_novelty_rounds = excluded.consecutive_low_novelty_rounds,
                    consecutive_low_yield_rounds = excluded.consecutive_low_yield_rounds,
                    consecutive_low_score_improvement_rounds =
                        excluded.consecutive_low_score_improvement_rounds,
                    consecutive_no_effective_term_rounds =
                        excluded.consecutive_no_effective_term_rounds,
                    consecutive_known_noise_dominance_rounds =
                        excluded.consecutive_known_noise_dominance_rounds,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    counters["consecutive_low_novelty_rounds"],
                    counters["consecutive_low_yield_rounds"],
                    counters["consecutive_low_score_improvement_rounds"],
                    counters["consecutive_no_effective_term_rounds"],
                    counters["consecutive_known_noise_dominance_rounds"],
                    utc_now_iso(),
                ),
            )
        status = "not_saturated"
        if plan.saturation_override is not None:
            status = plan.saturation_override
        elif query.iteration > 1 and (
            counters["consecutive_low_novelty_rounds"] >= rounds
            or counters["consecutive_low_yield_rounds"] >= rounds
            or counters["consecutive_low_score_improvement_rounds"] >= rounds
            or counters["consecutive_no_effective_term_rounds"] >= rounds
        ):
            status = "saturated_noise" if known_noise else "saturated_narrow"
        return replace(metrics, saturation_status=status)

    def _select_live_query(self, metrics: QueryMetrics, query: CanonicalQuery) -> QueryMetrics:
        reference = self._live_acceptance_reference(metrics.run_id, query.parent_query_id)
        if query.iteration == 1 or reference is None:
            decision = "accept"
            reason = "Initial high-recall live query accepted as deterministic baseline."
        elif metrics.source_completeness != "complete":
            decision = "reject"
            reason = (
                "Rejected live query because source execution was incomplete; "
                "apparent improvements from failed sources are not accepted."
            )
        elif reference.source_completeness != "complete":
            decision = "reject"
            reason = (
                "Rejected live query because parent source execution was incomplete; "
                "configured acceptance thresholds require complete comparable runs."
            )
        elif self._selected_candidate_still_passes_pairwise_acceptance(
            metrics.run_id,
            query.query_id,
            reference=reference,
        ) and self._selected_candidate_preserves_live_yield(metrics, reference):
            decision = "accept"
            reason = (
                "Accepted live query because the selected QueryPatch candidate passed "
                "high-recall pairwise gain/loss acceptance criteria with complete "
                "source execution."
            )
        elif self._has_candidate_selection_for_query(metrics.run_id, query.query_id):
            decision = "reject"
            reason = (
                "Rejected live query because the selected QueryPatch candidate no "
                "longer passed high-recall pairwise gain/loss acceptance criteria."
            )
        elif metrics.score_delta < 0.02 and metrics.marginal_eligible_count <= 0:
            decision = "reject"
            reason = (
                "Rejected live query because score improvement was below 0.02 and "
                "no additional eligible records were found."
            )
        elif metrics.novel_precision_at_20 < reference.novel_precision_at_20 - 0.20:
            decision = "reject"
            reason = (
                "Rejected live query because high-recall screening burden guard failed: "
                "novel precision decreased by more than 0.20."
            )
        elif metrics.defer_rate > reference.defer_rate + 0.15:
            decision = "reject"
            reason = (
                "Rejected live query because high-recall screening burden guard failed: "
                "defer rate increased by more than 0.15."
            )
        elif metrics.excluded_matrix_rate > reference.excluded_matrix_rate + 0.20:
            decision = "reject"
            reason = (
                "Rejected live query because high-recall screening burden guard failed: "
                "excluded-matrix rate increased by more than 0.20."
            )
        else:
            decision = "accept"
            reason = "Accepted live query because high-recall acceptance guards passed."
        return replace(metrics, decision=decision, decision_reason=reason)

    def _selected_candidate_preserves_live_yield(
        self, metrics: QueryMetrics, reference: LiveAcceptanceReference
    ) -> bool:
        if metrics.marginal_eligible_count > 0:
            return True
        # A noise-reduction patch may pass pairwise loss audit without creating gain
        # records, but it must not collapse the active retrieval frontier.
        return bool(metrics.total_score >= (reference.score * 0.5))

    def _has_candidate_selection_for_query(self, run_id: str, query_id: str) -> bool:
        decision_dir = self.runs_dir / run_id / "query_refinement"
        if not decision_dir.exists():
            return False
        for path in sorted(decision_dir.glob("*_candidate_selection.json")):
            payload = dict(read_json(path))
            if payload.get("selected_query_id") == query_id:
                return True
        return False

    def _selected_candidate_still_passes_pairwise_acceptance(
        self,
        run_id: str,
        query_id: str,
        *,
        reference: LiveAcceptanceReference,
    ) -> bool:
        run_dir = self.runs_dir / run_id
        decision_dir = run_dir / "query_refinement"
        if not decision_dir.exists():
            return False
        for path in sorted(decision_dir.glob("*_candidate_selection.json")):
            payload = dict(read_json(path))
            if payload.get("selected_query_id") != query_id:
                continue
            for evaluation in payload.get("candidate_evaluations", []):
                if not isinstance(evaluation, dict):
                    continue
                if evaluation.get("query_id") != query_id:
                    continue
                return self._candidate_passes_live_acceptance(
                    evaluation,
                    parent_metrics=reference,
                )
        return False

    def _live_acceptance_reference(
        self, run_id: str, parent_query_id: str | None
    ) -> LiveAcceptanceReference | None:
        if parent_query_id is None:
            return None
        latest = self._latest_metrics(self.runs_dir / run_id, parent_query_id)
        if latest is not None:
            return LiveAcceptanceReference(
                score=latest.total_score,
                novel_precision_at_20=latest.novel_precision_at_20,
                defer_rate=latest.defer_rate,
                excluded_matrix_rate=latest.excluded_matrix_rate,
                source_completeness=latest.source_completeness,
            )
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            audit_row = connection.execute(
                """
                SELECT payload_json
                FROM audit_events
                WHERE run_id = ? AND query_id = ? AND event_type = 'query_metrics'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id, parent_query_id),
            ).fetchone()
            if audit_row is not None:
                payload = json.loads(str(audit_row["payload_json"]))
                return LiveAcceptanceReference(
                    score=float(payload["total_score"]),
                    novel_precision_at_20=float(payload["novel_precision_at_20"]),
                    defer_rate=float(payload["defer_rate"]),
                    excluded_matrix_rate=float(payload["excluded_matrix_rate"]),
                    source_completeness=str(payload.get("source_completeness", "unknown")),
                )
            rows = connection.execute(
                """
                SELECT metric_name, metric_value
                FROM metric_values
                WHERE run_id = ? AND query_id = ?
                  AND metric_name IN (
                    'total_score',
                    'novel_precision_at_20',
                    'defer_rate',
                    'excluded_matrix_rate'
                  )
                """,
                (run_id, parent_query_id),
            ).fetchall()
        values = {str(row["metric_name"]): float(row["metric_value"]) for row in rows}
        required = {
            "total_score",
            "novel_precision_at_20",
            "defer_rate",
            "excluded_matrix_rate",
        }
        if not required <= set(values):
            return None
        return LiveAcceptanceReference(
            score=values["total_score"],
            novel_precision_at_20=values["novel_precision_at_20"],
            defer_rate=values["defer_rate"],
            excluded_matrix_rate=values["excluded_matrix_rate"],
            source_completeness="unknown",
        )

    def _source_completeness_from_statuses(self, source_counts: dict[str, Any]) -> str:
        statuses = set(dict(source_counts.get("source_statuses", {})).values())
        complete_statuses = {
            SourceExecutionStatus.SOURCE_SUCCESS.value,
            SourceExecutionStatus.SOURCE_NO_RESULTS.value,
            "success",
            "no-results",
        }
        partial_statuses = {
            SourceExecutionStatus.SOURCE_PARTIAL.value,
            SourceExecutionStatus.SOURCE_RATE_LIMITED.value,
            "partial",
            "rate-limited",
            "configuration_required",
        }
        failed_statuses = {
            SourceExecutionStatus.SOURCE_FAILED.value,
            SourceExecutionStatus.SOURCE_NOT_RUN.value,
            "failed",
            "not-run",
        }
        if not statuses:
            return "failed"
        if statuses <= complete_statuses:
            return "complete"
        if statuses & partial_statuses:
            return "partial"
        if statuses & failed_statuses:
            return "failed" if not (statuses & complete_statuses) else "partial"
        return "failed"

    def _update_live_source_status(
        self, run_id: str, run_dir: Path, source_counts: dict[str, Any]
    ) -> None:
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            return
        manifest = dict(read_json(manifest_path))
        source_statuses = dict(source_counts.get("source_statuses", {}))
        if source_statuses:
            manifest["source_health_status"] = source_statuses
            source_completeness = self._source_completeness_from_statuses(source_counts)
            manifest["source_completeness"] = source_completeness
            if manifest.get("run_status") == "completed":
                manifest["run_completeness"] = source_completeness
            write_json_atomic(manifest_path, manifest)
            with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
                connection.execute(
                    """
                    UPDATE runs
                    SET completeness = ?,
                        source_status_json = ?
                    WHERE run_id = ?
                    """,
                    (
                        source_completeness,
                        json.dumps(source_statuses, sort_keys=True),
                        run_id,
                    ),
                )

    def _persist_metrics(self, metrics: QueryMetrics) -> None:
        numeric = {
            key: value
            for key, value in metrics.to_dict().items()
            if isinstance(value, int | float | bool)
        }
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            for key, value in numeric.items():
                connection.execute(
                    """
                    INSERT INTO metric_values (
                        run_id, query_id, iteration, metric_name, metric_value,
                        metric_unit, metric_version, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'unitless', ?, ?)
                    ON CONFLICT(run_id, query_id, iteration, metric_name)
                    DO UPDATE SET metric_value = excluded.metric_value,
                                  created_at = excluded.created_at
                    """,
                    (
                        metrics.run_id,
                        metrics.query_id,
                        metrics.iteration,
                        key,
                        float(value),
                        METRIC_VERSION,
                        metrics.timestamp,
                    ),
                )
            connection.execute(
                """
                UPDATE query_iterations
                SET score = ?, score_delta = ?, saturation_status = ?
                WHERE run_id = ? AND query_id = ?
                """,
                (
                    metrics.total_score,
                    metrics.score_delta,
                    metrics.saturation_status,
                    metrics.run_id,
                    metrics.query_id,
                ),
            )
            self._insert_audit_event(
                connection,
                run_id=metrics.run_id,
                query_id=metrics.query_id,
                global_record_id=None,
                event_type="query_metrics",
                payload=metrics.to_dict(),
                actor="QueryEvaluator",
            )

    def _finalize_query_iteration(self, metrics: QueryMetrics) -> None:
        checksum = hashlib.sha256(
            json.dumps(metrics.to_dict(), sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            row = connection.execute(
                """
                SELECT acceptance_status
                FROM query_iterations
                WHERE run_id = ? AND query_id = ?
                """,
                (metrics.run_id, metrics.query_id),
            ).fetchone()
            existing_status = str(row["acceptance_status"]) if row else ""
            acceptance_status = (
                existing_status
                if existing_status == "rollback"
                else "accepted"
                if metrics.decision == "accept"
                else "rejected"
            )
            connection.execute(
                """
                UPDATE query_iterations
                SET query_status = 'completed',
                    acceptance_status = ?,
                    finalized_at = ?,
                    completion_checksum = ?
                WHERE run_id = ? AND query_id = ?
                """,
                (
                    acceptance_status,
                    utc_now_iso(),
                    checksum,
                    metrics.run_id,
                    metrics.query_id,
                ),
            )
            if metrics.decision == "accept":
                connection.execute(
                    """
                    UPDATE runs
                    SET current_iteration = ?,
                        current_query_id = ?,
                        accepted_query_id = ?,
                        current_state = 'RELEASE_ITERATION_MEMORY'
                    WHERE run_id = ?
                    """,
                    (metrics.iteration, metrics.query_id, metrics.query_id, metrics.run_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE runs
                    SET current_iteration = ?,
                        current_query_id = ?,
                        current_state = 'RELEASE_ITERATION_MEMORY'
                    WHERE run_id = ?
                    """,
                    (metrics.iteration, metrics.query_id, metrics.run_id),
                )

    def _finalize_iteration(
        self, run_id: str, run_dir: Path, query_id: str, *, rebuild_files: bool = True
    ) -> dict[str, Any]:
        if rebuild_files:
            self._rebuild_audit_files(run_id, run_dir)
            artifacts = {
                "raw_records": run_dir / "raw_metadata" / "raw_records.jsonl",
                "normalized_records": run_dir / "normalized" / "records.jsonl",
                "deduplicated_records": run_dir / "deduplication" / "deduplicated_records.jsonl",
                "screening_decisions": run_dir / "screening" / "screening_decisions.jsonl",
                "download_events": run_dir / "handoff" / "download" / "download_events.jsonl",
            }
            counts = {name: self._line_count(path) for name, path in artifacts.items()}
            checksums = {name: sha256_file(path) for name, path in artifacts.items()}
        else:
            counts = self._sql_artifact_counts(run_id)
            checksums = {"mode": "skipped_stress_mode"}
        finalization = {
            "query_id": query_id,
            "iteration_status": "COMPLETED",
            "counts": counts,
            "checksums": checksums,
            "timestamp": utc_now_iso(),
        }
        write_json_atomic(
            run_dir / "checksums" / f"{query_id}_iteration_completion.json", finalization
        )
        write_json_atomic(run_dir / "iteration_completion.json", finalization)
        return finalization

    def _sql_artifact_counts(self, run_id: str) -> dict[str, int]:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            return {
                "raw_records": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM source_records WHERE run_id = ?", (run_id,)
                    ).fetchone()[0]
                ),
                "normalized_records": int(
                    connection.execute(
                        """
                        SELECT COUNT(DISTINCT global_record_id)
                        FROM document_query_membership
                        WHERE run_id = ?
                        """,
                        (run_id,),
                    ).fetchone()[0]
                ),
                "deduplicated_records": int(
                    connection.execute(
                        """
                        SELECT COUNT(DISTINCT global_record_id)
                        FROM document_query_membership
                        WHERE run_id = ?
                        """,
                        (run_id,),
                    ).fetchone()[0]
                ),
                "screening_decisions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM screening_decisions WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                ),
                "download_events": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM download_outbox WHERE run_id = ?", (run_id,)
                    ).fetchone()[0]
                ),
            }

    def _export_rows(self, run_id: str) -> dict[str, list[dict[str, Any]]]:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            query_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM query_iterations
                    WHERE run_id = ? AND finalized_at IS NOT NULL
                    ORDER BY iteration
                    """,
                    (run_id,),
                )
            ]
            metric_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT run_id, iteration, query_id, metric_name, metric_value,
                           metric_unit, metric_version, created_at AS timestamp
                    FROM metric_values
                    WHERE run_id = ?
                    ORDER BY iteration, metric_name
                    """,
                    (run_id,),
                )
            ]
            audit_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT query_id, event_type, payload_json
                    FROM audit_events
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )
            ]
            decisions = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT payload_json
                    FROM screening_decisions
                    WHERE run_id = ? AND is_current = 1
                    ORDER BY iteration, query_id, global_record_id
                    """,
                    (run_id,),
                )
            ]
            terms = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT run_id, iteration, query_id, term, concept_block, action,
                           previous_status, new_status, reason,
                           supporting_positive_documents, supporting_negative_documents,
                           discriminative_score, created_at AS timestamp
                    FROM term_ledger
                    WHERE run_id = ?
                    ORDER BY iteration, term
                    """,
                    (run_id,),
                )
            ]
            source_rows = self._source_contribution_rows(connection, run_id)
            handoff_rows = self._handoff_rows(connection, run_id)
            audit_pool_rows = self._audit_pool_rows_if_configured(run_id)
        metrics_by_query: dict[str, dict[str, Any]] = {}
        for row in metric_rows:
            metrics_by_query.setdefault(str(row["query_id"]), {})[str(row["metric_name"])] = row[
                "metric_value"
            ]
        changes: dict[str, dict[str, Any]] = {}
        for row in audit_rows:
            if row["event_type"] == "query_change":
                changes[str(row["query_id"])] = json.loads(str(row["payload_json"]))
        wide_rows = []
        query_evolution = []
        saturation_rows = []
        run_index = []
        for query_row in query_rows:
            query_id = str(query_row["query_id"])
            change = changes.get(query_id, {})
            metric_values = metrics_by_query.get(query_id, {})
            wide = {
                "run_id": run_id,
                "iteration": query_row["iteration"],
                "query_id": query_id,
                "parent_query_id": query_row["parent_query_id"] or "",
                "branch_id": query_row["branch_id"],
                "added_terms": change.get("added_terms", []),
                "removed_terms": change.get("removed_terms", []),
                "replaced_terms": change.get("replaced_terms", []),
                "modified_blocks": change.get("modified_concept_blocks", []),
                "change_rationale": change.get("change_rationale", ""),
                "decision": change.get("decision", query_row["acceptance_status"]),
                "decision_reason": change.get("decision_reason", ""),
                "saturation_status": query_row["saturation_status"],
                "timestamp": query_row["finalized_at"],
            } | metric_values
            wide_rows.append(wide)
            query_evolution.append(
                {
                    "run_id": run_id,
                    "iteration": query_row["iteration"],
                    "query_id": query_id,
                    "parent_query_id": query_row["parent_query_id"] or "",
                    "added_terms": change.get("added_terms", []),
                    "removed_terms": change.get("removed_terms", []),
                    "modified_blocks": change.get("modified_concept_blocks", []),
                    "change_rationale": change.get("change_rationale", ""),
                    "decision": change.get("decision", query_row["acceptance_status"]),
                    "score": query_row["score"],
                    "timestamp": query_row["finalized_at"],
                }
            )
            saturation_rows.append(
                {
                    "run_id": run_id,
                    "iteration": query_row["iteration"],
                    "query_id": query_id,
                    "saturation_status": query_row["saturation_status"],
                    "novelty_rate": metric_values.get("novelty_rate", ""),
                    "score_delta": query_row["score_delta"],
                    "timestamp": query_row["finalized_at"],
                }
            )
        if query_rows:
            latest = query_rows[-1]
            run_index.append(
                {
                    "run_id": run_id,
                    "latest_iteration": latest["iteration"],
                    "latest_query_id": latest["query_id"],
                    "run_status": "completed",
                    "source_completeness": metrics_by_query.get(
                        str(latest["query_id"]), {}
                    ).get("source_completeness", "unknown"),
                    "timestamp": latest["finalized_at"],
                }
            )
        decision_rows = [
            json.loads(str(row["payload_json"])) for row in decisions
        ]
        exclusion_rows = self._exclusion_rows(decision_rows)
        return {
            "query_metrics_wide": wide_rows,
            "query_metrics_long": metric_rows,
            "query_evolution": query_evolution,
            "term_evolution": terms,
            "source_contribution": source_rows,
            "exclusion_reason_evolution": exclusion_rows,
            "saturation_trajectory": saturation_rows,
            "screening_decision_evolution": decision_rows,
            "download_handoff_trajectory": handoff_rows,
            "audit_pool_recall": audit_pool_rows["audit_pool_recall"],
            "audit_pool_recall_summary": audit_pool_rows["audit_pool_recall_summary"],
            "run_index": run_index,
        }

    def _write_export_tables(
        self, export_dir: Path, rows: dict[str, list[dict[str, Any]]], run_id: str
    ) -> None:
        defaults: dict[str, list[str]] = {
            "query_metrics_wide": [
                "run_id",
                "iteration",
                "query_id",
                "parent_query_id",
                "branch_id",
                "added_terms",
                "removed_terms",
                "replaced_terms",
                "modified_blocks",
                "change_rationale",
                "raw_result_count",
                "scanned_result_count",
                "known_record_count",
                "novel_record_count",
                "target_novel_n",
                "actual_novel_n",
                "target_reached",
                "include_count",
                "exclude_count",
                "defer_count",
                "eligible_precision",
                "novel_precision_at_20",
                "novel_eligible_yield",
                "normalized_novel_eligible_yield",
                "marginal_relevant_yield",
                "marginal_eligible_count",
                "novelty_rate",
                "cumulative_eligible_count",
                "retrospective_query_coverage",
                "cross_source_breadth",
                "retrieval_source_breadth",
                "eligible_source_breadth",
                "metadata_completeness",
                "scope_diversity",
                "eligible_waterbody_diversity",
                "excluded_matrix_rate",
                "laboratory_study_rate",
                "no_concentration_rate",
                "duplicate_rate",
                "known_eligible_overlap_rate",
                "known_ineligible_overlap_rate",
                "query_complexity",
                "positive_score",
                "penalty_score",
                "total_score",
                "score_delta",
                "decision",
                "decision_reason",
                "saturation_status",
                "source_completeness",
                "download_requests_emitted",
                "duplicate_handoffs_suppressed",
                "pending_download_jobs_at_iteration_end",
                "handoff_backpressure_status",
                "timestamp",
            ],
            "query_metrics_long": [
                "run_id",
                "iteration",
                "query_id",
                "metric_name",
                "metric_value",
                "metric_unit",
                "metric_version",
                "timestamp",
            ],
            "query_evolution": [
                "run_id",
                "iteration",
                "query_id",
                "parent_query_id",
                "added_terms",
                "removed_terms",
                "modified_blocks",
                "change_rationale",
                "decision",
                "score",
                "timestamp",
            ],
            "term_evolution": [
                "run_id",
                "iteration",
                "query_id",
                "term",
                "concept_block",
                "action",
                "previous_status",
                "new_status",
                "reason",
                "supporting_positive_documents",
                "supporting_negative_documents",
                "discriminative_score",
                "timestamp",
            ],
            "source_contribution": [
                "run_id",
                "iteration",
                "query_id",
                "source",
                "returned_records",
                "scanned_records",
                "novel_records",
                "novel_eligible_records",
                "duplicate_records",
                "missing_abstracts",
                "deferred_records",
                "API_failures",
                "unique_contributions",
                "overlap_counts_with_other_sources",
            ],
            "exclusion_reason_evolution": [
                "run_id",
                "iteration",
                "query_id",
                "reason_code",
                "count",
                "timestamp",
            ],
            "saturation_trajectory": [
                "run_id",
                "iteration",
                "query_id",
                "saturation_status",
                "novelty_rate",
                "score_delta",
                "timestamp",
            ],
            "screening_decision_evolution": [
                "screening_schema_version",
                "screening_decision_id",
                "global_record_id",
                "run_id",
                "query_id",
                "iteration",
                "screening_pass",
                "decision",
                "confidence",
                "reason_codes",
                "evidence_spans",
                "screening_timestamp",
                "decision_actor",
            ],
            "download_handoff_trajectory": [
                "run_id",
                "iteration",
                "query_id",
                "newly_included_records",
                "download_requests_emitted",
                "duplicate_handoffs_suppressed",
                "handoff_failures",
                "pending_download_jobs",
                "claimed_download_jobs",
                "successful_downloads_observed",
                "skipped_existing_observed",
                "retryable_download_failures_observed",
                "terminal_download_failures_observed",
                "dead_letter_jobs",
                "handoff_backpressure_status",
                "handoff_latency_seconds",
                "timestamp",
            ],
            "run_index": [
                "run_id",
                "latest_iteration",
                "latest_query_id",
                "run_status",
                "source_completeness",
                "timestamp",
            ],
            "audit_pool_recall": [
                "audit_id",
                "role",
                "expected_relevance",
                "retrieved",
                "match_status",
                "match_method",
                "global_record_id",
                "matched_title",
                "matched_query_ids",
                "matched_sources",
                "title",
                "doi",
                "pmid",
                "openalex_id",
                "semantic_scholar_id",
                "rationale",
                "source_reference",
            ],
            "audit_pool_recall_summary": [
                "role",
                "total",
                "retrieved",
                "missing",
                "recall",
                "missing_audit_ids",
            ],
        }
        for name, table_rows in rows.items():
            fieldnames = defaults[name]
            extras = sorted({key for row in table_rows for key in row} - set(fieldnames))
            write_csv_atomic(export_dir / f"{name}.csv", table_rows, fieldnames + extras)
        self._write_resource_trajectory(export_dir, run_id)
        write_text_atomic(
            export_dir / "FIGURE_DATA_README.md",
            "# Figure Data\n\nPhase 1.1 exports are rebuilt from SQLite control-plane state.\n",
        )
        write_text_atomic(
            export_dir / "METRIC_DEFINITIONS.md",
            "# Metric Definitions\n\nSee `docs/methods/metric_definitions.md`.\n",
        )
        write_text_atomic(
            export_dir / "QUERY_EVOLUTION_SUMMARY.md",
            "# Query Evolution Summary\n\n"
            "Deterministic mock variants exercise accept, reject, rollback, "
            "and saturation branches.\n",
        )

    def _audit_pool_rows_if_configured(self, run_id: str) -> dict[str, list[dict[str, Any]]]:
        path = self.config_dir / "audit_pool.yaml"
        if not path.exists():
            return {"audit_pool_recall": [], "audit_pool_recall_summary": []}
        return AuditPoolEvaluator.from_yaml(path).evaluate(self._audit_pool_document_rows(run_id))

    def _audit_pool_document_rows(self, run_id: str) -> list[dict[str, Any]]:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                      d.global_record_id,
                      d.normalized_doi,
                      d.pmid,
                      d.openalex_id,
                      d.semantic_scholar_id,
                      d.canonical_title,
                      d.normalized_title,
                      GROUP_CONCAT(DISTINCT m.query_id) AS query_ids,
                      GROUP_CONCAT(DISTINCT m.source_name) AS sources
                    FROM documents d
                    JOIN document_query_membership m
                      ON m.global_record_id = d.global_record_id
                    WHERE m.run_id = ?
                    GROUP BY d.global_record_id
                    ORDER BY d.global_record_id
                    """,
                    (run_id,),
                )
            ]
        for row in rows:
            row["query_ids"] = _split_group_concat(row.get("query_ids"))
            row["sources"] = _split_group_concat(row.get("sources"))
        return rows

    def _candidate_pool_audit_document_rows(self, pool_id: str) -> list[dict[str, Any]]:
        records = self._read_jsonl_records(
            self.runs_dir / pool_id / "candidate_pool" / "records.jsonl"
        )
        rows: list[dict[str, Any]] = []
        for record in records:
            title = str(record.get("title") or "")
            normalized_title = str(
                record.get("normalized_title") or self._normalize_title_for_pool(title)
            )
            source_providers = record.get("source_providers")
            query_families = record.get("query_families")
            rows.append(
                {
                    "global_record_id": str(
                        record.get("candidate_pool_key")
                        or record.get("global_record_id")
                        or self._candidate_pool_key(record, normalized_title)
                    ),
                    "normalized_doi": self._candidate_pool_normalized_doi(
                        record.get("doi")
                    ),
                    "pmid": record.get("pmid"),
                    "openalex_id": record.get("openalex_id"),
                    "semantic_scholar_id": record.get("semantic_scholar_id"),
                    "canonical_title": title,
                    "normalized_title": normalized_title,
                    "query_ids": (
                        query_families
                        if isinstance(query_families, list)
                        else [record.get("query_family") or record.get("source_query_id")]
                    ),
                    "sources": (
                        source_providers
                        if isinstance(source_providers, list)
                        else [record.get("source_provider")]
                    ),
                }
            )
        return rows

    @staticmethod
    def _candidate_pool_normalized_doi(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        if not text:
            return None
        text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
        text = re.sub(r"^doi:\s*", "", text)
        return text or None

    def _write_audit_pool_exports(
        self, export_dir: Path, rows: dict[str, list[dict[str, Any]]]
    ) -> None:
        recall_fields = [
            "audit_id",
            "role",
            "expected_relevance",
            "retrieved",
            "match_status",
            "match_method",
            "global_record_id",
            "matched_title",
            "matched_query_ids",
            "matched_sources",
            "title",
            "doi",
            "pmid",
            "openalex_id",
            "semantic_scholar_id",
            "rationale",
            "source_reference",
        ]
        summary_fields = [
            "role",
            "total",
            "retrieved",
            "missing",
            "recall",
            "missing_audit_ids",
        ]
        write_csv_atomic(
            export_dir / "audit_pool_recall.csv",
            rows["audit_pool_recall"],
            recall_fields,
        )
        write_csv_atomic(
            export_dir / "audit_pool_recall_summary.csv",
            rows["audit_pool_recall_summary"],
            summary_fields,
        )

    def _write_candidate_pool_audit_summary_markdown(
        self,
        *,
        export_dir: Path,
        pool_id: str,
        audit_pool_path: Path,
        rows: dict[str, list[dict[str, Any]]],
    ) -> None:
        summary = rows["audit_pool_recall_summary"]
        recall_rows = rows["audit_pool_recall"]
        lines = [
            "# Candidate Pool Audit Recall",
            "",
            f"- pool_id: `{pool_id}`",
            f"- audit_pool: `{self._display_path(audit_pool_path)}`",
            f"- generated_at: `{utc_now_iso()}`",
            "",
            "## Recall Summary",
            "",
            "| role | total | retrieved | missing | recall | missing_audit_ids |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for row in summary:
            lines.append(
                "| "
                f"{row['role']} | {row['total']} | {row['retrieved']} | "
                f"{row['missing']} | {row['recall']} | "
                f"{', '.join(row['missing_audit_ids'])} |"
            )
        missing = [row for row in recall_rows if not row["retrieved"]]
        lines.extend(["", "## Missing Entries", ""])
        if missing:
            for row in missing:
                lines.append(
                    f"- `{row['audit_id']}` ({row['role']}): {row['title']}"
                )
        else:
            lines.append("- None")
        lines.extend(
            [
                "",
                "## Interpretation",
                "",
                (
                    "- This checks whether the high-recall union candidate pool retrieves "
                    "known development seeds, holdout sentinels, and external audit records."
                ),
                (
                    "- Missing holdout sentinels indicate retrieval coverage risk and should "
                    "drive the next query-family or source-recovery work."
                ),
            ]
        )
        write_text_atomic(
            export_dir / "CANDIDATE_POOL_AUDIT_RECALL.md",
            "\n".join(lines) + "\n",
        )

    def _write_export_manifest(self, run_id: str, export_dir: Path) -> Path:
        checksums = {
            path.name: sha256_file(path)
            for path in sorted(export_dir.glob("*"))
            if path.is_file() and path.name != "export_manifest.json"
        }
        row_counts = {}
        for path in sorted(export_dir.glob("*.csv")):
            row_counts[path.name] = max(0, self._line_count(path) - 1)
        manifest = {
            "run_ids": [run_id],
            "source_database": self._display_path(self.db_path),
            "source_database_checksum": ControlPlane(
                self.db_path, self._git_sha()
            ).database_checksum(),
            "export_timestamp": utc_now_iso(),
            "code_commit": self._git_sha(),
            "schema_versions": {"control_plane": SCHEMA_VERSION, "screening": "1.1.0"},
            "metric_versions": {"default": METRIC_VERSION},
            "row_counts": row_counts,
            "file_checksums": checksums,
        }
        path = export_dir / "export_manifest.json"
        write_json_atomic(path, manifest)
        return path

    def _mock_pages(
        self,
        source: str,
        iteration: int,
        page_size: int,
        max_scan_depth: int,
        *,
        stress_records_per_source: int | None = None,
    ) -> list[list[dict[str, Any]]]:
        fixture = cast(dict[str, list[dict[str, Any]]], read_json(self._fixture_path()))
        base = [dict(row) for row in fixture.get(source, [])]
        records: list[dict[str, Any]] = []
        if stress_records_per_source is not None:
            records.extend(base[:2])
            records.extend(
                self._synthetic_records(
                    source,
                    iteration,
                    max(0, stress_records_per_source - len(records)),
                )
            )
        elif iteration == 1:
            records.extend(base)
        elif iteration == 5:
            records.extend(base[:2])
            records.extend(self._synthetic_records(source, iteration, 1, narrow=True))
        else:
            records.extend(base[:2])
            records.extend(self._synthetic_records(source, iteration, 3))
            records.extend(base[2:])
        records = records[:max_scan_depth]
        return [records[index : index + page_size] for index in range(0, len(records), page_size)]

    def _synthetic_records(
        self, source: str, iteration: int, count: int, *, narrow: bool = False
    ) -> list[dict[str, Any]]:
        source_prefix = {
            "crossref": "cr",
            "openalex": "oa",
            "semantic_scholar": "s2",
            "pubmed": "pm",
        }[source]
        waterbodies = ["estuary", "lake", "reservoir", "river"]
        records = []
        for offset in range(count):
            waterbody = (
                "reservoir"
                if narrow
                else waterbodies[(iteration + offset) % len(waterbodies)]
            )
            doi = f"10.2000/{source_prefix}.{iteration}.{offset}"
            if iteration == 3 and offset == 1:
                # Rejected query branch still discovers an eligible document for handoff.
                waterbody = "estuary"
            if iteration == 4 and offset == 0:
                # Duplicate across query iterations, exercising handoff suppression.
                doi = f"10.2000/{source_prefix}.3.1"
            matrix = waterbody
            if iteration == 3 and offset == 2:
                matrix = "wastewater"
            records.append(
                {
                    "source_record_id": f"{source_prefix}-{iteration}-{offset}",
                    "doi": doi,
                    "title": f"Emerging contaminants in {waterbody} water iteration {iteration}",
                    "abstract": (
                        f"Measured concentrations in real {waterbody} water samples "
                        "for deterministic mock retrieval."
                    ),
                    "abstract_source": "mock",
                    "keywords": ["emerging contaminants", waterbody, "concentration"],
                    "authors": [f"MockAuthor{iteration}{offset}"],
                    "publication_date": f"202{iteration % 5}-01-0{offset + 1}",
                    "publication_year": 2020 + iteration,
                    "journal_title": "Mock Phase 1.1 Journal",
                    "issn": [],
                    "eissn": [],
                    "document_type": "journal article",
                    "language": "en",
                    "source_relevance_score": 0.8 - offset * 0.05,
                    "sampled_matrices": [matrix],
                    "study_type": "field monitoring",
                    "has_real_field_sample": True,
                    "has_concentration_evidence": True,
                    "article_ec_scope": "true",
                }
            )
        return records

    def _start_query_iteration(
        self, run_id: str, query: CanonicalQuery, plan: IterationPlan, loaded: LoadedConfig
    ) -> None:
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            cutoff = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            connection.execute(
                """
                INSERT INTO query_iterations (
                    run_id, iteration, query_id, parent_query_id, branch_id,
                    query_status, acceptance_status, score, score_delta,
                    saturation_status, query_known_cutoff, started_at
                )
                VALUES (?, ?, ?, ?, ?, 'running', ?, NULL, NULL,
                        'not_saturated', ?, ?)
                ON CONFLICT(run_id, query_id) DO UPDATE SET
                    query_status = CASE
                        WHEN query_iterations.finalized_at IS NULL THEN 'running'
                        ELSE query_iterations.query_status
                    END
                """,
                (
                    run_id,
                    query.iteration,
                    query.query_id,
                    query.parent_query_id,
                    plan.branch_id,
                    plan.acceptance_status,
                    cutoff,
                    utc_now_iso(),
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET current_iteration = ?, current_query_id = ?, current_state = 'BUILD_QUERY'
                WHERE run_id = ?
                """,
                (query.iteration, query.query_id, run_id),
            )
            self._insert_audit_event(
                connection,
                run_id=run_id,
                query_id=query.query_id,
                global_record_id=None,
                event_type="query_change",
                payload=self._query_change_payload(run_id, query, plan, loaded),
                actor="Retrieval Specialist",
            )

    def _write_query_artifacts(
        self,
        run_id: str,
        query_dir: Path,
        query: CanonicalQuery,
        plan: IterationPlan,
        metrics: QueryMetrics | None,
    ) -> None:
        write_yaml_atomic(query_dir / "canonical_query.yaml", query.to_dict())
        change = self._query_change_payload(run_id, query, plan, None, metrics)
        write_json_atomic(query_dir / "query_change.json", change)
        write_json_atomic(
            query_dir / "decision.json",
            {
                "query_id": query.query_id,
                "decision": plan.decision,
                "decision_reason": plan.decision_reason,
                "timestamp": utc_now_iso(),
                "decided_by": "QuerySelector",
            },
        )
        if metrics is not None:
            write_json_atomic(query_dir / "metrics.json", metrics.to_dict())
            write_json_atomic(
                query_dir / "screening_summary.json",
                {
                    "include": metrics.include_count,
                    "exclude": metrics.exclude_count,
                    "defer": metrics.defer_count,
                },
            )
            write_json_atomic(
                query_dir / "term_changes.json",
                {"added_terms": plan.added_terms, "removed_terms": plan.removed_terms},
            )

    def _compile_query(self, query_dir: Path, query: CanonicalQuery) -> None:
        compiler = CanonicalQueryCompiler()
        for source in SOURCE_NAMES:
            compiled = compiler.compile_for_source(query, source)
            write_json_atomic(query_dir / f"compiled_{source}_query.json", compiled.to_dict())

    def _resolve_or_insert_document(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        query: CanonicalQuery,
        record: NormalizedRecord,
    ) -> str:
        existing = self._find_document_by_identity(connection, record)
        global_record_id = existing or record.global_record_id
        connection.execute(
            """
            INSERT OR IGNORE INTO documents (
                global_record_id, normalized_doi, pmid, openalex_id, semantic_scholar_id,
                crossref_id, canonical_title, normalized_title, publication_year,
                first_author, journal_title, abstract_original, abstract_source,
                keywords_json, authors_json, issn_json, eissn_json, document_type,
                language, sampled_matrices_json, study_type, has_real_field_sample,
                has_concentration_evidence, article_ec_scope, first_seen_run_id,
                first_seen_query_id, first_seen_at, latest_metadata_version,
                current_screening_status, current_download_status
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, NULL, NULL
            )
            """,
            (
                global_record_id,
                record.normalized_doi,
                record.pmid,
                record.openalex_id,
                record.semantic_scholar_id,
                record.crossref_id,
                record.title_original,
                record.title_normalized,
                record.publication_year,
                record.first_author,
                record.journal_title,
                record.abstract_original,
                record.abstract_source,
                json.dumps(record.keywords, sort_keys=True, ensure_ascii=True),
                json.dumps(record.authors, sort_keys=True, ensure_ascii=True),
                json.dumps(record.issn, sort_keys=True, ensure_ascii=True),
                json.dumps(record.eissn, sort_keys=True, ensure_ascii=True),
                record.document_type,
                record.language,
                json.dumps(record.sampled_matrices, sort_keys=True, ensure_ascii=True),
                record.study_type,
                self._bool_to_int(record.has_real_field_sample),
                self._bool_to_int(record.has_concentration_evidence),
                record.article_ec_scope,
                run_id,
                query.query_id,
                utc_now_iso(),
                record.normalization_version,
            ),
        )
        return global_record_id

    def _find_document_by_identity(
        self, connection: sqlite3.Connection, record: NormalizedRecord
    ) -> str | None:
        checks = [
            ("normalized_doi", record.normalized_doi),
            ("pmid", record.pmid),
            ("openalex_id", record.openalex_id),
            ("semantic_scholar_id", record.semantic_scholar_id),
            ("crossref_id", record.crossref_id),
        ]
        for column, value in checks:
            if value:
                row = connection.execute(
                    f"SELECT global_record_id FROM documents WHERE {column} = ?",
                    (value,),
                ).fetchone()
                if row is not None:
                    return str(row["global_record_id"])
        return None

    def _next_screening_batch(
        self,
        run_id: str,
        query_id: str,
        batch_size: int,
        *,
        novelty_sample_only: bool = False,
        novelty_against_query_id: str | None = None,
    ) -> list[NormalizedRecord]:
        novelty_filter = (
            "AND m.included_in_novelty_sample = 1"
            if novelty_sample_only and not novelty_against_query_id
            else ""
        )
        pairwise_gain_filter = ""
        parameters: tuple[Any, ...]
        if novelty_against_query_id:
            pairwise_gain_filter = """
                  AND NOT EXISTS (
                    SELECT 1
                    FROM document_query_membership parent
                    WHERE parent.run_id = m.run_id
                      AND parent.query_id = ?
                      AND parent.global_record_id = m.global_record_id
                  )
            """
            parameters = (run_id, query_id, novelty_against_query_id, batch_size)
        else:
            parameters = (run_id, query_id, batch_size)
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT d.*
                FROM documents d
                JOIN document_query_membership m
                  ON m.global_record_id = d.global_record_id
                WHERE m.run_id = ? AND m.query_id = ?
                  {novelty_filter}
                  {pairwise_gain_filter}
                  AND NOT EXISTS (
                    SELECT 1 FROM screening_decisions s
                    WHERE s.run_id = m.run_id
                      AND s.query_id = m.query_id
                      AND s.global_record_id = m.global_record_id
                      AND s.is_current = 1
                  )
                ORDER BY d.global_record_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return [
                self._record_from_document_row(connection, row, run_id, query_id)
                for row in rows
            ]

    def _next_candidate_loss_audit_batch(
        self,
        *,
        run_id: str,
        parent_query_id: str,
        candidate_query_id: str,
        batch_size: int,
    ) -> list[NormalizedRecord]:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT d.*
                FROM documents d
                JOIN document_query_membership parent
                  ON parent.global_record_id = d.global_record_id
                 AND parent.run_id = ?
                 AND parent.query_id = ?
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM document_query_membership candidate
                    WHERE candidate.run_id = parent.run_id
                      AND candidate.query_id = ?
                      AND candidate.global_record_id = parent.global_record_id
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM screening_decisions s
                    WHERE s.run_id = parent.run_id
                      AND s.query_id = parent.query_id
                      AND s.global_record_id = parent.global_record_id
                      AND s.is_current = 1
                  )
                ORDER BY
                  CASE COALESCE(d.current_screening_status, 'unknown')
                    WHEN 'include' THEN 0
                    WHEN 'defer_metadata' THEN 1
                    WHEN 'defer_not_downloaded' THEN 1
                    WHEN 'unknown' THEN 2
                    WHEN 'exclude' THEN 3
                    ELSE 4
                  END,
                  parent.source_rank,
                  d.global_record_id
                LIMIT ?
                """,
                (run_id, parent_query_id, candidate_query_id, batch_size),
            ).fetchall()
            return [
                self._record_from_document_row(connection, row, run_id, parent_query_id)
                for row in rows
            ]

    def _record_from_document_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row, run_id: str, query_id: str
    ) -> NormalizedRecord:
        sources = [
            dict(source)
            for source in connection.execute(
                """
                SELECT source_name, source_record_id, source_rank AS rank
                FROM document_sources
                WHERE global_record_id = ? AND run_id = ? AND query_id = ?
                ORDER BY source_rank, source_name
                """,
                (row["global_record_id"], run_id, query_id),
            )
        ]
        retrieved_from = sorted({str(source["source_name"]) for source in sources})
        source_rank = min((int(source["rank"]) for source in sources), default=0)
        return NormalizedRecord(
            global_record_id=str(row["global_record_id"]),
            source_records=sources,
            doi=str(row["normalized_doi"]) if row["normalized_doi"] else None,
            normalized_doi=str(row["normalized_doi"]) if row["normalized_doi"] else None,
            pmid=str(row["pmid"]) if row["pmid"] else None,
            openalex_id=str(row["openalex_id"]) if row["openalex_id"] else None,
            semantic_scholar_id=str(row["semantic_scholar_id"])
            if row["semantic_scholar_id"]
            else None,
            crossref_id=str(row["crossref_id"]) if row["crossref_id"] else None,
            title_original=str(row["canonical_title"]),
            title_normalized=str(row["normalized_title"]),
            abstract_original=str(row["abstract_original"]) if row["abstract_original"] else None,
            abstract_source=str(row["abstract_source"]) if row["abstract_source"] else None,
            keywords=json.loads(str(row["keywords_json"])),
            authors=json.loads(str(row["authors_json"])),
            first_author=str(row["first_author"]) if row["first_author"] else None,
            publication_date=None,
            publication_year=int(row["publication_year"]) if row["publication_year"] else None,
            journal_title=str(row["journal_title"]) if row["journal_title"] else None,
            issn=json.loads(str(row["issn_json"])),
            eissn=json.loads(str(row["eissn_json"])),
            document_type=str(row["document_type"]) if row["document_type"] else None,
            language=str(row["language"]) if row["language"] else None,
            source_rank=source_rank,
            source_relevance_score=None,
            retrieved_from=retrieved_from,
            retrieval_timestamp=str(row["first_seen_at"]),
            raw_metadata_path=None,
            normalization_version=str(row["latest_metadata_version"]),
            sampled_matrices=json.loads(str(row["sampled_matrices_json"])),
            study_type=str(row["study_type"]) if row["study_type"] else None,
            has_real_field_sample=self._int_to_bool(row["has_real_field_sample"]),
            has_concentration_evidence=self._int_to_bool(row["has_concentration_evidence"]),
            article_ec_scope=str(row["article_ec_scope"]),
        )

    def _insert_screening_decision(
        self, connection: sqlite3.Connection, decision: ScreeningDecision
    ) -> None:
        payload = decision.to_dict()
        connection.execute(
            """
            UPDATE screening_decisions
            SET is_current = 0
            WHERE global_record_id = ? AND run_id = ? AND query_id = ? AND is_current = 1
            """,
            (decision.global_record_id, decision.run_id, decision.query_id),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO screening_decisions (
                screening_decision_id, global_record_id, run_id, query_id, iteration,
                screening_pass, decision, confidence, reason_codes_json,
                evidence_spans_json, prompt_version, prompt_hash, model_name,
                model_version, raw_response_path, payload_json, created_at,
                supersedes_decision_id, is_current
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
            """,
            (
                decision.screening_decision_id,
                decision.global_record_id,
                decision.run_id,
                decision.query_id,
                decision.iteration,
                decision.screening_pass,
                decision.decision,
                decision.confidence,
                json.dumps(decision.reason_codes, sort_keys=True, ensure_ascii=True),
                json.dumps(decision.evidence_spans, sort_keys=True, ensure_ascii=True),
                decision.prompt_version,
                decision.prompt_hash,
                decision.model_name,
                decision.model_version,
                decision.raw_model_response_path,
                json.dumps(payload, sort_keys=True, ensure_ascii=True),
                decision.screening_timestamp,
            ),
        )

    def _download_event(
        self, record: NormalizedRecord, decision: ScreeningDecision, loaded: LoadedConfig
    ) -> dict[str, Any]:
        key = f"download:{record.global_record_id}:{DOCUMENT_VERSION}"
        event_id = f"download_event_{hashlib.sha256(key.encode()).hexdigest()[:24]}"
        payload: dict[str, Any] = {
            "handoff_schema_version": "1.1.0",
            "event_id": event_id,
            "event_type": "DOWNLOAD_REQUESTED",
            "idempotency_key": key,
            "run_id": decision.run_id,
            "iteration": decision.iteration,
            "query_id": decision.query_id,
            "global_record_id": record.global_record_id,
            "document_version": DOCUMENT_VERSION,
            "normalized_DOI": record.normalized_doi,
            "PMID": record.pmid,
            "OpenAlex_ID": record.openalex_id,
            "Semantic_Scholar_ID": record.semantic_scholar_id,
            "Crossref_ID": record.crossref_id,
            "title": record.title_original,
            "authors": record.authors,
            "first_author": record.first_author,
            "publication_year": record.publication_year,
            "journal_title": record.journal_title,
            "ISSN": record.issn,
            "eISSN": record.eissn,
            "language": record.language,
            "document_type": record.document_type,
            "scie_status": self._scie_status(loaded.protocol),
            "source_metadata_paths": [
                str(source.get("raw_metadata_path"))
                for source in record.source_records
                if source.get("raw_metadata_path")
            ],
            "source_provenance": record.source_records,
            "candidate_fulltext_urls": [],
            "open_access_status": None,
            "screening_decision": decision.decision,
            "screening_confidence": decision.confidence,
            "screening_reason_codes": decision.reason_codes,
            "screening_evidence_path": (
                f"runs/{decision.run_id}/screening/batches/"
                f"{decision.query_id}_{decision.audit_batch_id}.jsonl"
            ),
            "retrieval_lane": self._retrieval_lane_for_decision(decision),
            "priority": 50,
            "requested_at": utc_now_iso(),
            "payload_checksum": "",
        }
        payload["payload_checksum"] = self._payload_checksum(payload)
        if self._download_schema is None:
            self._download_schema = read_json(
                self._schema_path("handoff", "download_request.schema.json")
            )
        validate(instance=payload, schema=self._download_schema)
        return payload

    def _retrieval_lane_for_decision(self, decision: ScreeningDecision) -> str:
        if decision.decision_actor == "TitleAbstractScreeningWorker":
            return "live_retrieval"
        return "mock_phase1_1"

    def _insert_download_event(
        self, connection: sqlite3.Connection, event: dict[str, Any]
    ) -> bool:
        before = connection.total_changes
        connection.execute(
            """
            INSERT OR IGNORE INTO download_outbox (
                event_id, idempotency_key, event_type, global_record_id,
                document_version, run_id, query_id, iteration, payload_json,
                payload_checksum, status, created_at, published_at,
                retry_count, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, ?, 0, NULL)
            """,
            (
                event["event_id"],
                event["idempotency_key"],
                event["event_type"],
                event["global_record_id"],
                event["document_version"],
                event["run_id"],
                event["query_id"],
                event["iteration"],
                json.dumps(event, sort_keys=True, ensure_ascii=True),
                event["payload_checksum"],
                event["requested_at"],
                event["requested_at"],
            ),
        )
        inserted = connection.total_changes > before
        if inserted:
            connection.execute(
                """
                INSERT OR IGNORE INTO download_jobs (
                    download_job_id, idempotency_key, global_record_id, job_state,
                    claimed_by, claimed_at, lease_expires_at, attempt_count,
                    last_attempt_at, completed_at, result_reference, failure_reason
                )
                VALUES (?, ?, ?, 'pending', NULL, NULL, NULL, 0, NULL, NULL, NULL, NULL)
                """,
                (
                    f"download_job_{hashlib.sha256(str(event['idempotency_key']).encode()).hexdigest()[:24]}",
                    event["idempotency_key"],
                    event["global_record_id"],
                ),
            )
            connection.execute(
                """
                UPDATE documents
                SET current_download_status = 'handoff_emitted'
                WHERE global_record_id = ?
                """,
                (event["global_record_id"],),
            )
        else:
            connection.execute(
                """
                UPDATE documents
                SET current_download_status = 'handoff_duplicate_suppressed'
                WHERE global_record_id = ?
                """,
                (event["global_record_id"],),
            )
        return inserted

    def _verify_transactional_handoff(self, run_id: str, query_id: str) -> None:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            missing_event = connection.execute(
                """
                SELECT s.screening_decision_id
                FROM screening_decisions s
                JOIN documents d ON d.global_record_id = s.global_record_id
                WHERE s.run_id = ? AND s.query_id = ? AND s.decision = 'include'
                  AND s.is_current = 1
                  AND d.current_download_status NOT IN (
                      'handoff_emitted', 'handoff_duplicate_suppressed', 'handoff_disabled'
                  )
                LIMIT 1
                """,
                (run_id, query_id),
            ).fetchone()
            if missing_event is not None:
                raise RuntimeError("Include decision exists without durable handoff state")
            orphan_event = connection.execute(
                """
                SELECT o.event_id
                FROM download_outbox o
                WHERE o.run_id = ? AND o.query_id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM screening_decisions s
                    WHERE s.global_record_id = o.global_record_id
                      AND s.run_id = o.run_id
                      AND s.query_id = o.query_id
                      AND s.decision = 'include'
                      AND s.is_current = 1
                  )
                LIMIT 1
                """,
                (run_id, query_id),
            ).fetchone()
            if orphan_event is not None:
                raise RuntimeError("Download event exists without final include decision")

    def _rebuild_audit_files(self, run_id: str, run_dir: Path) -> None:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            raw_rows = [
                str(row["raw_payload_json"])
                for row in connection.execute(
                    """
                    SELECT raw_payload_json
                    FROM source_records
                    WHERE run_id = ?
                    ORDER BY query_id, source_name, retrieval_page, source_rank
                    """,
                    (run_id,),
                )
            ]
            documents = [
                self._record_from_document_row(
                    connection, row, run_id, str(row["first_seen_query_id"])
                ).to_dict()
                for row in connection.execute(
                    """
                    SELECT DISTINCT d.*
                    FROM documents d
                    JOIN document_query_membership m ON m.global_record_id = d.global_record_id
                    WHERE m.run_id = ?
                    ORDER BY d.global_record_id
                    """,
                    (run_id,),
                )
            ]
            decisions = [
                str(row["payload_json"])
                for row in connection.execute(
                    """
                    SELECT payload_json
                    FROM screening_decisions
                    WHERE run_id = ? AND is_current = 1
                    ORDER BY iteration, query_id, global_record_id
                    """,
                    (run_id,),
                )
            ]
            events = [
                str(row["payload_json"])
                for row in connection.execute(
                    """
                    SELECT payload_json
                    FROM download_outbox
                    WHERE run_id = ?
                    ORDER BY created_at, event_id
                    """,
                    (run_id,),
                )
            ]
        write_text_atomic(
            run_dir / "raw_metadata" / "raw_records.jsonl",
            "\n".join(raw_rows) + ("\n" if raw_rows else ""),
        )
        normalized_lines = [
            json.dumps(document, ensure_ascii=True, sort_keys=True) for document in documents
        ]
        write_text_atomic(
            run_dir / "normalized" / "records.jsonl",
            "\n".join(normalized_lines) + ("\n" if normalized_lines else ""),
        )
        write_json_atomic(
            run_dir / "normalized" / "records_summary.json",
            {
                "record_count": len(documents),
                "canonical_stream": "normalized/records.jsonl",
            },
        )
        write_text_atomic(
            run_dir / "deduplication" / "deduplicated_records.jsonl",
            "\n".join(normalized_lines) + ("\n" if normalized_lines else ""),
        )
        write_json_atomic(
            run_dir / "deduplication" / "deduplication.json",
            {
                "record_count": len(documents),
                "duplicate_count": self._duplicate_count_for_run(run_id),
            },
        )
        write_text_atomic(
            run_dir / "screening" / "screening_decisions.jsonl",
            "\n".join(decisions) + ("\n" if decisions else ""),
        )
        write_json_atomic(
            run_dir / "exports" / "topical_fit_profile.json",
            self._topical_fit_profile(decisions),
        )
        self._write_term_candidate_exports(run_dir, run_id)
        write_json_atomic(
            run_dir / "screening" / "screening_decisions_summary.json",
            {
                "decision_count": len(decisions),
                "canonical_stream": "screening/screening_decisions.jsonl",
            },
        )
        write_text_atomic(
            run_dir / "handoff" / "download" / "download_events.jsonl",
            "\n".join(events) + ("\n" if events else ""),
        )
        self._rebuild_global_handoff_logs()

    def _candidate_pool_screening_protocol(self) -> dict[str, Any]:
        return {
            "screening_context": "high_recall_candidate_pool",
            "objective": (
                "Build the most complete screenable candidate pool for natural "
                "surface-water emerging-contaminant occurrence, monitoring, "
                "field sampling, abundance, and concentration evidence. The pool is "
                "high-recall, but include decisions require primary or newly reported "
                "ambient surface-water evidence rather than review-only synthesis."
            ),
            "eligible_scope": [
                "natural or ambient surface water",
                "river, stream, lake, reservoir, estuary, wetland, coastal water, freshwater",
                (
                    "emerging contaminants and pollutant families including PFAS, "
                    "pharmaceuticals, antibiotics, hormones/endocrine disruptors, "
                    "pesticides, micro/nanoplastics, and suspect/non-target screening"
                ),
                (
                    "field monitoring, occurrence, distribution, detection, "
                    "quantification, abundance, or concentration evidence from primary "
                    "ambient surface-water measurements"
                ),
            ],
            "ineligible_scope": [
                (
                    "review, systematic review, meta-analysis, bibliometric analysis, "
                    "critical assessment, editorial, commentary, correction, or policy "
                    "paper unless it reports new direct ambient surface-water measurements"
                ),
                (
                    "treatment/removal/adsorption/degradation without ambient "
                    "receiving-water measurements"
                ),
                "laboratory-only spiked experiments",
                (
                    "groundwater, drinking water, wastewater-only, sediment-only, "
                    "soil-only, food/product/biota-only matrices unless surface-water "
                    "data are separately extractable"
                ),
                "method development without environmental surface-water application",
                (
                    "risk-assessment-only or removal-strategy-only papers without new "
                    "field measurements"
                ),
            ],
            "decision_policy": {
                "clear_match": "include",
                "clear_mismatch": "exclude",
                "missing_or_ambiguous_metadata": "defer_metadata",
                "no_pdf_inference": True,
                "review_or_assessment_without_new_measurements": "exclude",
                "mixed_matrix_with_extractable_surface_water_data": "include",
                "mixed_matrix_without_extractable_surface_water_data": "defer_metadata",
            },
            "term_mining": {
                "positive_terms": (
                    "pollutant family, natural waterbody, field sampling, monitoring, "
                    "occurrence, concentration, abundance, distribution, and analytical "
                    "screening phrases supported by the title/abstract/keywords"
                ),
                "noise_terms": (
                    "review, meta-analysis, bibliometric analysis, critical assessment, "
                    "correction, policy-only, removal-only, adsorption-only, "
                    "treatment-only, laboratory-only, "
                    "groundwater, wastewater-only, sediment-only, biota-only, "
                    "fish-only, modeling-only, method-only"
                ),
            },
        }

    def _candidate_pool_source_run(self, run_id: str) -> dict[str, Any]:
        run_dir = self.runs_dir / run_id
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Missing source run manifest: {manifest_path}")
        manifest = dict(read_json(manifest_path))
        config_ref_path = run_dir / "query_family_config_ref.json"
        config_ref = dict(read_json(config_ref_path)) if config_ref_path.exists() else {}
        protocol_path = run_dir / "protocol_snapshot.yaml"
        protocol = dict(read_yaml(protocol_path)) if protocol_path.exists() else {}
        query_family = str(protocol.get("protocol_version") or run_id)
        candidates_path = run_dir / "external_metadata" / "Q0001" / "candidates.jsonl"
        if not candidates_path.exists():
            raise FileNotFoundError(f"Missing source run candidates: {candidates_path}")
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "query_family": query_family,
            "protocol_version": query_family,
            "config_dir": config_ref.get("config_dir"),
            "config_name": config_ref.get("config_name"),
            "candidate_ref": str(candidates_path),
            "status": manifest.get("run_status", "unknown"),
            "normalized_record_count": manifest.get("normalized_record_count", 0),
        }

    def _candidate_pool_records(self, source_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for source_run in source_runs:
            path = Path(str(source_run["candidate_ref"]))
            for position, payload in enumerate(self._read_jsonl_records(path), start=1):
                records.append(
                    self._candidate_pool_record(
                        payload=payload,
                        source_run=source_run,
                        source_position=position,
                    )
                )
        return records

    def _candidate_pool_record(
        self, *, payload: dict[str, Any], source_run: dict[str, Any], source_position: int
    ) -> dict[str, Any]:
        title = str(payload.get("title") or "")
        normalized_title = self._normalize_title_for_pool(title)
        provider = str(payload.get("source_provider") or payload.get("source_name") or "unknown")
        return {
            "candidate_pool_key": self._candidate_pool_key(payload, normalized_title),
            "title": title,
            "normalized_title": normalized_title,
            "abstract": payload.get("abstract"),
            "doi": payload.get("doi"),
            "pmid": payload.get("pmid"),
            "openalex_id": payload.get("openalex_id"),
            "provider_record_id": payload.get("provider_record_id"),
            "source_record_id": payload.get("source_record_id"),
            "document_type": payload.get("document_type"),
            "language": payload.get("language"),
            "year": payload.get("year"),
            "journal": payload.get("journal"),
            "keywords": payload.get("keywords") or [],
            "authors": payload.get("authors") or [],
            "source_provider": provider,
            "source_rank": payload.get("rank"),
            "source_query_id": payload.get("query_id"),
            "query_family": source_run["query_family"],
            "source_run_id": source_run["run_id"],
            "source_position": source_position,
            "retrieved_at": payload.get("retrieved_at"),
            "url": payload.get("url"),
            "provenance": [
                {
                    "run_id": source_run["run_id"],
                    "query_family": source_run["query_family"],
                    "provider": provider,
                    "source_record_id": payload.get("source_record_id"),
                    "provider_record_id": payload.get("provider_record_id"),
                    "rank": payload.get("rank"),
                    "position": source_position,
                }
            ],
        }

    def _candidate_pool_records_with_metadata_enrichment(
        self,
        *,
        pool_root: Path,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Overlay recovered metadata without mutating the raw candidate pool."""
        enrichment_ref = pool_root / "metadata_enrichment" / "enriched_records.jsonl"
        if not enrichment_ref.exists():
            return records
        enriched_by_key: dict[str, dict[str, Any]] = {}
        for row in self._read_jsonl_records(enrichment_ref):
            key = self._normalize_candidate_pool_key(str(row.get("candidate_pool_key") or ""))
            if key:
                enriched_by_key[key] = row
        if not enriched_by_key:
            return records

        metadata_fields = (
            "abstract",
            "keywords",
            "authors",
            "document_type",
            "language",
            "publication_date",
            "year",
            "journal",
            "doi",
            "pmid",
            "openalex_id",
            "semantic_scholar_id",
        )
        overlaid: list[dict[str, Any]] = []
        for record in records:
            key = self._normalize_candidate_pool_key(
                str(record.get("candidate_pool_key") or "")
            )
            enrichment = enriched_by_key.get(key)
            if not enrichment:
                overlaid.append(record)
                continue
            merged = dict(record)
            applied_fields: list[str] = []
            for field in metadata_fields:
                current = merged.get(field)
                candidate = enrichment.get(field)
                if (
                    not self._candidate_pool_metadata_value_present(current)
                    and self._candidate_pool_metadata_value_present(candidate)
                ):
                    merged[field] = candidate
                    applied_fields.append(field)
            if applied_fields:
                merged["_metadata_enrichment_applied"] = True
                merged["_metadata_enrichment_provider"] = enrichment.get(
                    "enrichment_provider"
                )
                merged["_metadata_enrichment_fields"] = applied_fields
            overlaid.append(merged)
        return overlaid

    @staticmethod
    def _candidate_pool_metadata_value_present(value: Any) -> bool:
        if value is None or value == []:
            return False
        return not (isinstance(value, str) and not value.strip())

    def _candidate_pool_normalized_record(self, payload: dict[str, Any]) -> NormalizedRecord:
        title = str(payload.get("title") or "")
        normalized_title = str(
            payload.get("normalized_title") or self._normalize_title_for_pool(title)
        )
        key = str(
            payload.get("candidate_pool_key")
            or self._candidate_pool_key(payload, normalized_title)
        )
        source_records = [
            {
                "source_name": provenance.get("provider", "candidate_pool"),
                "source_record_id": provenance.get("source_record_id")
                or provenance.get("provider_record_id")
                or key,
                "rank": int(provenance.get("rank") or 0),
            }
            for provenance in payload.get("provenance", [])
            if isinstance(provenance, dict)
        ]
        if not source_records:
            source_records = [
                {
                    "source_name": str(payload.get("source_provider") or "candidate_pool"),
                    "source_record_id": str(payload.get("source_record_id") or key),
                    "rank": int(payload.get("source_rank") or 0),
                }
            ]
        retrieved_from = sorted({str(row["source_name"]) for row in source_records})
        source_rank = min((int(row["rank"]) for row in source_records), default=0)
        authors = [str(item) for item in payload.get("authors", [])]
        return NormalizedRecord(
            global_record_id=key,
            source_records=source_records,
            doi=str(payload["doi"]) if payload.get("doi") else None,
            normalized_doi=str(payload["doi"]).lower() if payload.get("doi") else None,
            pmid=str(payload["pmid"]) if payload.get("pmid") else None,
            openalex_id=str(payload["openalex_id"]) if payload.get("openalex_id") else None,
            semantic_scholar_id=None,
            crossref_id=None,
            title_original=title,
            title_normalized=normalized_title,
            abstract_original=str(payload["abstract"]) if payload.get("abstract") else None,
            abstract_source=None,
            keywords=[str(item) for item in payload.get("keywords", [])],
            authors=authors,
            first_author=authors[0] if authors else None,
            publication_date=None,
            publication_year=int(payload["year"]) if payload.get("year") else None,
            journal_title=str(payload["journal"]) if payload.get("journal") else None,
            issn=[],
            eissn=[],
            document_type=str(payload["document_type"]) if payload.get("document_type") else None,
            language=str(payload["language"]) if payload.get("language") else None,
            source_rank=source_rank,
            source_relevance_score=None,
            retrieved_from=retrieved_from,
            retrieval_timestamp=str(payload.get("retrieved_at") or utc_now_iso()),
            raw_metadata_path=None,
        )

    @staticmethod
    def _candidate_pool_record_matches_filters(
        payload: dict[str, Any],
        *,
        provider_filter: set[str],
        family_filter: set[str],
    ) -> bool:
        if provider_filter:
            providers = payload.get("source_providers", [])
            if not isinstance(providers, list):
                providers = [payload.get("source_provider") or providers]
            if not {str(provider) for provider in providers} & provider_filter:
                return False
        if family_filter:
            families = payload.get("query_families", [])
            if not isinstance(families, list):
                families = [payload.get("query_family") or families]
            if not {str(family) for family in families} & family_filter:
                return False
        return True

    def _prioritized_candidate_pool_screening_records(
        self, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Prioritize useful screening signal while preserving broad family coverage."""

        buckets: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
        for index, record in enumerate(records):
            abstract = str(record.get("abstract") or "").strip()
            has_abstract_rank = 0 if abstract else 1
            family_key = self._candidate_pool_primary_value(
                record,
                list_field="query_families",
                fallback_field="query_family",
                fallback="unknown_family",
            )
            provider_key = self._candidate_pool_primary_value(
                record,
                list_field="source_providers",
                fallback_field="source_provider",
                fallback="unknown_provider",
            )
            record["_screening_original_position"] = index
            buckets.setdefault((has_abstract_rank, family_key, provider_key), []).append(record)

        for bucket_records in buckets.values():
            bucket_records.sort(
                key=lambda item: (
                    self._candidate_pool_source_rank(item),
                    int(item.get("_screening_original_position", 0)),
                )
            )

        prioritized: list[dict[str, Any]] = []
        for abstract_rank in (0, 1):
            keys = sorted(key for key in buckets if key[0] == abstract_rank)
            while keys:
                next_keys: list[tuple[int, str, str]] = []
                for key in keys:
                    bucket = buckets[key]
                    if bucket:
                        prioritized.append(bucket.pop(0))
                    if bucket:
                        next_keys.append(key)
                keys = next_keys
        for record in prioritized:
            record.pop("_screening_original_position", None)
        return prioritized

    @staticmethod
    def _candidate_pool_primary_value(
        record: dict[str, Any],
        *,
        list_field: str,
        fallback_field: str,
        fallback: str,
    ) -> str:
        value = record.get(list_field)
        if isinstance(value, list) and value:
            return str(value[0])
        if value:
            return str(value)
        return str(record.get(fallback_field) or fallback)

    @staticmethod
    def _candidate_pool_source_rank(record: dict[str, Any]) -> int:
        value = record.get("source_rank")
        if value is None:
            return 1_000_000
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1_000_000

    def _deduplicate_candidate_pool_records(
        self, records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        by_key: dict[str, dict[str, Any]] = {}
        duplicate_links: list[dict[str, Any]] = []
        for record in records:
            key = str(record["candidate_pool_key"])
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = record
                continue
            existing["provenance"].extend(record["provenance"])
            existing["query_families"] = sorted(
                {
                    *existing.get("query_families", [existing["query_family"]]),
                    str(record["query_family"]),
                }
            )
            existing["source_providers"] = sorted(
                {
                    *existing.get("source_providers", [existing["source_provider"]]),
                    str(record["source_provider"]),
                }
            )
            existing["source_run_ids"] = sorted(
                {
                    *existing.get("source_run_ids", [existing["source_run_id"]]),
                    str(record["source_run_id"]),
                }
            )
            duplicate_links.append(
                {
                    "candidate_pool_key": key,
                    "kept_title": existing["title"],
                    "duplicate_title": record["title"],
                    "duplicate_source_run_id": record["source_run_id"],
                    "duplicate_query_family": record["query_family"],
                    "duplicate_provider": record["source_provider"],
                }
            )
        for record in by_key.values():
            record.setdefault("query_families", [record["query_family"]])
            record.setdefault("source_providers", [record["source_provider"]])
            record.setdefault("source_run_ids", [record["source_run_id"]])
            record["query_family_count"] = len(record["query_families"])
            record["source_provider_count"] = len(record["source_providers"])
        return (
            sorted(by_key.values(), key=lambda item: str(item["candidate_pool_key"])),
            duplicate_links,
        )

    def _candidate_pool_key(self, payload: dict[str, Any], normalized_title: str) -> str:
        for field in ("doi", "pmid", "openalex_id", "provider_record_id", "source_record_id"):
            value = payload.get(field)
            if value:
                return f"{field}:{str(value).strip().lower()}"
        return "title:" + hashlib.sha256(normalized_title.encode()).hexdigest()

    def _candidate_pool_counter(
        self, records: list[dict[str, Any]], field: str
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            key = str(record.get(field) or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def _write_candidate_pool_summary(
        self, pool_dir: Path, manifest: dict[str, Any], records: list[dict[str, Any]]
    ) -> None:
        missing_abstracts = sum(1 for record in records if not record.get("abstract"))
        missing_identifiers = sum(
            1
            for record in records
            if not any(
                record.get(field)
                for field in ("doi", "pmid", "openalex_id", "provider_record_id")
            )
        )
        representative = [
            {
                "title": record["title"],
                "query_families": record["query_families"],
                "source_providers": record["source_providers"],
                "document_type": record.get("document_type"),
                "language": record.get("language"),
            }
            for record in records[:20]
        ]
        summary = {
            "pool_id": manifest["run_id"],
            "raw_candidate_count": manifest["raw_candidate_count"],
            "deduplicated_candidate_count": manifest["deduplicated_candidate_count"],
            "duplicate_link_count": manifest["duplicate_link_count"],
            "provider_counts": manifest["provider_counts"],
            "query_family_counts": manifest["query_family_counts"],
            "missing_abstracts": missing_abstracts,
            "missing_identifiers": missing_identifiers,
            "representative_records": representative,
            "next_step": "title_abstract_screening_before_query_family_expansion",
        }
        write_json_atomic(pool_dir / "candidate_pool" / "summary.json", summary)
        lines = [
            "# High-Recall Candidate Pool",
            "",
            f"- pool_id: {manifest['run_id']}",
            f"- raw candidates: {manifest['raw_candidate_count']}",
            f"- deduplicated candidates: {manifest['deduplicated_candidate_count']}",
            f"- duplicate links: {manifest['duplicate_link_count']}",
            f"- missing abstracts: {missing_abstracts}",
            f"- missing identifiers: {missing_identifiers}",
            "- screening: not started",
            "- query refinement: not started",
            "- PDF download: not started",
            "",
            "## Source Runs",
            "",
        ]
        lines.extend(f"- `{run_id}`" for run_id in manifest["source_run_ids"])
        lines.extend(["", "## Provider Counts", ""])
        lines.extend(
            f"- {provider}: {count}"
            for provider, count in manifest["provider_counts"].items()
        )
        write_text_atomic(pool_dir / "candidate_pool" / "SUMMARY.md", "\n".join(lines) + "\n")

    def _write_candidate_pool_screening_summary(
        self,
        *,
        pool_root: Path,
        manifest: dict[str, Any],
        new_decisions: list[dict[str, Any]],
    ) -> None:
        decision_rows = self._read_jsonl_records(
            pool_root / "screening_decisions.jsonl"
        )
        all_decisions = self._latest_candidate_pool_decisions(decision_rows)
        decision_counts: dict[str, int] = {}
        family_counts: dict[str, dict[str, int]] = {}
        provider_counts: dict[str, dict[str, int]] = {}
        term_counts: dict[str, dict[str, int]] = {}
        for decision in all_decisions:
            label = str(decision.get("decision") or "unknown")
            decision_counts[label] = decision_counts.get(label, 0) + 1
            for family in decision.get("query_families", []):
                family_key = str(family)
                family_counts.setdefault(family_key, {})
                family_counts[family_key][label] = family_counts[family_key].get(label, 0) + 1
            for provider in decision.get("source_providers", []):
                provider_key = str(provider)
                provider_counts.setdefault(provider_key, {})
                provider_counts[provider_key][label] = (
                    provider_counts[provider_key].get(label, 0) + 1
                )
            for evidence in decision.get("query_term_evidence", []):
                if not isinstance(evidence, dict):
                    continue
                block = str(evidence.get("concept_block") or "unknown")
                term = str(evidence.get("term") or "").strip().lower()
                if not term:
                    continue
                term_counts.setdefault(block, {})
                term_counts[block][term] = term_counts[block].get(term, 0) + 1
        summary = {
            "pool_id": manifest.get("run_id", pool_root.parent.name),
            "screened_total": len(all_decisions),
            "decision_rows_total": len(decision_rows),
            "superseded_decision_rows": len(decision_rows) - len(all_decisions),
            "new_decisions": len(new_decisions),
            "decision_counts": dict(sorted(decision_counts.items())),
            "family_decision_counts": {
                family: dict(sorted(counts.items()))
                for family, counts in sorted(family_counts.items())
            },
            "provider_decision_counts": {
                provider: dict(sorted(counts.items()))
                for provider, counts in sorted(provider_counts.items())
            },
            "top_query_terms": {
                block: dict(
                    sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:50]
                )
                for block, counts in sorted(term_counts.items())
            },
            "next_step": "review_family_noise_and_expand_missing_query_families",
        }
        write_json_atomic(pool_root / "screening_summary.json", summary)
        lines = [
            "# Candidate Pool Screening Summary",
            "",
            f"- pool_id: {summary['pool_id']}",
            f"- screened total: {summary['screened_total']}",
            f"- new decisions: {summary['new_decisions']}",
            "",
            "## Decisions",
            "",
        ]
        lines.extend(
            f"- {decision}: {count}"
            for decision, count in summary["decision_counts"].items()
        )
        write_text_atomic(pool_root / "SCREENING_SUMMARY.md", "\n".join(lines) + "\n")

    def _candidate_pool_analysis(
        self,
        *,
        pool_id: str,
        manifest: dict[str, Any],
        records: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        original_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        decision_rows_total = len(decisions)
        decisions = self._latest_candidate_pool_decisions(decisions)
        records_by_id = {
            str(record.get("candidate_pool_key") or ""): record
            for record in records
            if record.get("candidate_pool_key")
        }
        screened_ids = {str(decision.get("global_record_id") or "") for decision in decisions}
        decision_counts = self._candidate_pool_decision_counts(decisions)
        family_metrics = self._candidate_pool_group_metrics(
            records=records,
            decisions=decisions,
            group_field="query_families",
        )
        provider_metrics = self._candidate_pool_group_metrics(
            records=records,
            decisions=decisions,
            group_field="source_providers",
        )
        term_analysis = self._candidate_pool_term_analysis(decisions)
        examples = self._candidate_pool_examples(records_by_id, decisions)
        screened_total = len(decisions)
        include_count = decision_counts.get("include", 0)
        exclude_count = decision_counts.get("exclude", 0)
        defer_count = decision_counts.get("defer_metadata", 0)
        unscreened_total = max(0, len(records) - len(screened_ids))
        original_by_key = {
            str(record.get("candidate_pool_key") or ""): record
            for record in (original_records or records)
            if record.get("candidate_pool_key")
        }
        abstracts_recovered_by_enrichment = sum(
            1
            for key, record in original_by_key.items()
            if not self._candidate_pool_metadata_value_present(record.get("abstract"))
            and self._candidate_pool_metadata_value_present(
                records_by_id.get(key, {}).get("abstract")
            )
        )
        return {
            "analysis_version": "candidate-pool-analysis-v3",
            "pool_id": pool_id,
            "objective": (
                "Build a high-recall, screenable candidate pool for natural surface-water "
                "emerging-contaminant occurrence, monitoring, field sampling, and "
                "concentration evidence. Query families are evaluated by marginal "
                "screened contribution, unique included records, and noise burden, "
                "not by single-query precision."
            ),
            "candidate_pool": {
                "raw_candidate_count": manifest.get("raw_candidate_count"),
                "deduplicated_candidate_count": len(records),
                "duplicate_link_count": manifest.get("duplicate_link_count"),
                "missing_abstracts": sum(
                    1
                    for record in records
                    if not self._candidate_pool_metadata_value_present(
                        record.get("abstract")
                    )
                ),
                "missing_abstracts_before_enrichment": sum(
                    1
                    for record in original_by_key.values()
                    if not self._candidate_pool_metadata_value_present(
                        record.get("abstract")
                    )
                ),
                "abstracts_recovered_by_enrichment": abstracts_recovered_by_enrichment,
                "records_with_metadata_enrichment": sum(
                    1
                    for record in records
                    if record.get("_metadata_enrichment_applied")
                ),
                "missing_identifiers": sum(
                    1
                    for record in records
                    if not any(
                        record.get(field)
                        for field in ("doi", "pmid", "openalex_id", "provider_record_id")
                    )
                ),
            },
            "screening": {
                "screened_total": screened_total,
                "decision_rows_total": decision_rows_total,
                "superseded_decision_rows": decision_rows_total - screened_total,
                "unscreened_total": unscreened_total,
                "decision_counts": decision_counts,
                "include_rate": include_count / screened_total if screened_total else 0.0,
                "exclude_rate": exclude_count / screened_total if screened_total else 0.0,
                "defer_rate": defer_count / screened_total if screened_total else 0.0,
            },
            "query_family_metrics": family_metrics,
            "provider_metrics": provider_metrics,
            "term_analysis": term_analysis,
            "representative_examples": examples,
            "interpretation": self._candidate_pool_interpretation(
                screened_total=screened_total,
                family_metrics=family_metrics,
                term_analysis=term_analysis,
            ),
            "recommended_next_action": self._candidate_pool_recommended_next_action(
                screened_total=screened_total,
                unscreened_total=unscreened_total,
            ),
            "guardrails": [
                "Do not collapse this workflow into one precision-optimized Boolean query.",
                "Keep additive query-family branches that contribute unique included records.",
                "Use screening to remove noise before final corpus construction.",
                "Treat missing abstracts as defer/risk, not automatic exclude.",
                "Use noise terms to refine branches conservatively; avoid broad automatic NOT.",
            ],
        }

    def _query_family_construction_plan(
        self, *, pool_id: str, analysis: dict[str, Any]
    ) -> dict[str, Any]:
        family_metrics = dict(analysis.get("query_family_metrics") or {})
        provider_metrics = dict(analysis.get("provider_metrics") or {})
        screening = dict(analysis.get("screening") or {})
        positive_terms = dict(
            (analysis.get("term_analysis") or {}).get("positive_terms_by_block") or {}
        )
        noise_terms = dict(
            (analysis.get("term_analysis") or {}).get("noise_terms_by_block") or {}
        )
        config_names = set(self._known_query_family_config_names())
        config_root = self.repo_root / "configs" / "retrieval_experiments"
        if config_root.exists():
            config_names.update(
                child.name
                for child in config_root.iterdir()
                if child.is_dir() and child.name.startswith("qfamily_")
            )
        existing_dirs = {
            name: config_root / name
            for name in sorted(config_names)
        }
        selected: list[dict[str, Any]] = []
        for family, row in sorted(family_metrics.items()):
            role = str(row.get("recommended_role") or "")
            unique_include = int(row.get("unique_include_count") or 0)
            include_count = int((row.get("decision_counts") or {}).get("include", 0))
            exclude_count = int((row.get("decision_counts") or {}).get("exclude", 0))
            screened_count = int(row.get("screened_count") or 0)
            config_name = self._query_family_config_name_from_metric(family)
            if unique_include > 0 or include_count > 0:
                config_dir = existing_dirs.get(config_name, Path(config_name))
                include_rate = include_count / screened_count if screened_count else 0.0
                if unique_include > 0:
                    action = "preserve"
                    reason = "contributes unique screened includes"
                elif include_rate >= 0.2:
                    action = "preserve_pending_review"
                    reason = "contributes screened includes but unique yield is unconfirmed"
                else:
                    action = "hold_and_refine"
                    reason = "non-unique low-yield branch; keep only with noise controls"
                selected.append(
                    {
                        "query_family": family,
                        "config_dir": self._display_path(config_dir),
                        "config_name": config_name,
                        "action": action,
                        "reason": reason,
                        "unique_include_count": unique_include,
                        "include_count": include_count,
                        "exclude_count": exclude_count,
                        "screened_count": screened_count,
                        "recommended_role": role,
                    }
                )
        gap_configs = [
            "qfamily_artificial_sweeteners_tracers_surface_water",
            "qfamily_tire_wear_6ppd_surface_water",
            "qfamily_pesticide_gap_neonicotinoid_fipronil_glyphosate_surface_water",
            "qfamily_qac_disinfectant_residues_surface_water_refined_v2",
            "qfamily_antibiotics_pharmaceuticals_surface_water_refined_v2",
            "qfamily_industrial_additives_flame_retardants_surface_water_refined_v2",
        ]
        selected_config_names = {str(item["config_name"]) for item in selected}
        current_held_config_names = {
            str(item["config_name"])
            for item in selected
            if item["action"] == "hold_and_refine"
        }
        recently_held_config_names = (
            current_held_config_names
            | self._recently_held_query_family_config_names(pool_id)
        )
        expansion = []
        for config_name in gap_configs:
            if config_name in selected_config_names:
                continue
            resolved_config_name = config_name
            if self._query_family_was_recently_held(
                config_name, recently_held_config_names
            ):
                successor = self._query_family_refined_successor_name(
                    config_name, existing_dirs.keys()
                )
                if successor is None:
                    continue
                resolved_config_name = successor
            gap_config_dir = existing_dirs.get(resolved_config_name)
            if gap_config_dir is None or not gap_config_dir.exists():
                continue
            expansion.append(
                {
                    "config_name": resolved_config_name,
                    "config_dir": self._display_path(gap_config_dir),
                    "action": "add_or_rerun",
                    "reason": self._query_family_gap_reason(resolved_config_name),
                }
            )
        weak = []
        for family, row in sorted(family_metrics.items()):
            unique_include = int(row.get("unique_include_count") or 0)
            include_count = int((row.get("decision_counts") or {}).get("include", 0))
            exclude_count = int((row.get("decision_counts") or {}).get("exclude", 0))
            defer_count = int((row.get("decision_counts") or {}).get("defer_metadata", 0))
            screened_count = int(row.get("screened_count") or 0)
            include_rate = include_count / screened_count if screened_count else 0.0
            exclude_rate = exclude_count / screened_count if screened_count else 0.0
            should_refine_for_noise = (
                screened_count >= 8
                and unique_include == 0
                and include_rate <= 0.15
                and exclude_rate >= 0.75
            )
            should_refine_for_defer = (
                unique_include <= 1
                and screened_count >= 8
                and defer_count > include_count
            )
            if should_refine_for_noise or should_refine_for_defer:
                reason = (
                    "low unique include yield with high noise burden"
                    if should_refine_for_noise
                    else "low unique include yield with high defer burden"
                )
                weak.append(
                    {
                        "query_family": family,
                        "config_name": self._query_family_config_name_from_metric(family),
                        "action": "refine_not_delete",
                        "reason": reason,
                        "unique_include_count": unique_include,
                        "include_count": include_count,
                        "exclude_count": exclude_count,
                        "defer_count": defer_count,
                        "screened_count": screened_count,
                        "include_rate": include_rate,
                        "exclude_rate": exclude_rate,
                        "candidate_refinement_focus": (
                            self._query_family_refinement_focus(
                                family=family,
                                config_name=self._query_family_config_name_from_metric(family),
                                noise_terms=noise_terms,
                            )
                        ),
                    }
                )
        recommended_configs = []
        held_for_refinement_configs = []
        for item in selected:
            config_dir = self.repo_root / "configs" / "retrieval_experiments" / str(
                item["config_name"]
            )
            if config_dir.exists():
                if item["action"] == "hold_and_refine":
                    held_for_refinement_configs.append(
                        {
                            "config_name": item["config_name"],
                            "config_dir": self._display_path(config_dir),
                            "query_family": item["query_family"],
                            "reason": item["reason"],
                        }
                    )
                else:
                    recommended_configs.append(self._display_path(config_dir))
        recommended_configs.extend(str(item["config_dir"]) for item in expansion)
        next_pool_id = self._next_query_family_pool_id(pool_id)
        return {
            "plan_version": "query-family-construction-v1",
            "pool_id": pool_id,
            "objective": (
                "Construct a high-recall, screenable query-family set for natural or "
                "ambient surface-water emerging-contaminant occurrence evidence. The "
                "retrieval product is the union candidate pool, not one precision-optimized "
                "Boolean query."
            ),
            "source_evidence": {
                "analysis_ref": self._display_path(
                    self.runs_dir / pool_id / "candidate_pool" / "candidate_pool_analysis.json"
                ),
                "screened_total": (analysis.get("screening") or {}).get("screened_total"),
                "decision_counts": screening.get("decision_counts") or {},
                "deduplicated_candidate_count": (
                    analysis.get("candidate_pool") or {}
                ).get("deduplicated_candidate_count"),
                "provider_metrics": provider_metrics,
            },
            "preserve_productive_families": selected,
            "refine_weak_or_noisy_families": weak,
            "expand_missing_or_undercovered_families": expansion,
            "held_for_refinement_family_config_dirs": held_for_refinement_configs,
            "term_signals": {
                "positive_terms_by_block": positive_terms,
                "noise_terms_by_block": noise_terms,
            },
            "guardrails": [
                "Do not collapse productive branches into one broad Q0001 query.",
                (
                    "Keep any branch with unique included records unless a reviewed "
                    "loss audit proves it is redundant."
                ),
                (
                    "Use noise terms to prioritize screening and branch refinement "
                    "before adding broad NOT clauses."
                ),
                "Treat missing abstracts as defer or review risk, not automatic exclusion.",
                "Run title/abstract screening before PDF download or Download Specialist handoff.",
            ],
            "recommended_next_pool_id": next_pool_id,
            "recommended_next_family_config_dirs": recommended_configs,
            "recommended_command_template": (
                "ecmonitor-retrieval build-high-recall-candidate-pool "
                f"--pool-id {next_pool_id} "
                "--date-from 2006-01-01 --date-to <date_to> "
                "--max-records-per-provider 200 --provider crossref --provider pubmed "
                "--family-config-dir <repeat for each ready config; hold_and_refine "
                "families require config revision before rerun>"
            ),
            "next_steps": [
                "Review or rescreen high-priority include/defer rows before retiring any branch.",
                "Revise hold_and_refine family configs before adding them to the next pool.",
                (
                    "Run the recommended family set with Crossref and PubMed at "
                    "200 records per provider."
                ),
                (
                    "Reuse screening decisions from the current pool where stable "
                    "candidate keys match."
                ),
                "Screen only novel records one document at a time.",
                "Analyze unique include contribution and defer burden before adding more branches.",
                "Export review priority before any PDF download work.",
            ],
        }

    def _candidate_pool_post_review_plan(
        self,
        *,
        pool_id: str,
        analysis: dict[str, Any],
        review: dict[str, Any],
        priority: dict[str, Any],
    ) -> dict[str, Any]:
        family_actions = []
        for family, row in sorted((analysis.get("query_family_metrics") or {}).items()):
            counts = row.get("decision_counts") or {}
            include_count = int(counts.get("include") or 0)
            unique_include_count = int(row.get("unique_include_count") or 0)
            defer_count = int(counts.get("defer_metadata") or 0)
            candidate_count = int(row.get("candidate_count") or 0)
            defer_rate = defer_count / candidate_count if candidate_count else 0.0
            if unique_include_count > 0:
                action = "preserve"
                reason = "unique included records confirm recall contribution"
            elif include_count > 0:
                action = "hold_pending_review"
                reason = "included records are not unique; review before retaining as a branch"
            elif defer_rate >= 0.75:
                action = "pause_until_metadata_enriched"
                reason = "no includes and high defer burden indicate metadata-limited evidence"
            else:
                action = "refine_or_retire"
                reason = "low yield without enough defer evidence to justify expansion"
            family_actions.append(
                {
                    "query_family": family,
                    "action": action,
                    "reason": reason,
                    "candidate_count": candidate_count,
                    "include_count": include_count,
                    "unique_include_count": unique_include_count,
                    "exclude_count": int(counts.get("exclude") or 0),
                    "defer_count": defer_count,
                    "defer_rate": defer_rate,
                    "recommended_role": row.get("recommended_role"),
                }
            )
        action_counts: dict[str, int] = {}
        for row in family_actions:
            action = str(row["action"])
            action_counts[action] = action_counts.get(action, 0) + 1
        risk_flags = dict(priority.get("exported_top_risk_flags") or {})
        return {
            "plan_version": "candidate-pool-post-review-v1",
            "pool_id": pool_id,
            "recommended_next_action": "metadata_enrichment_and_priority_review_before_expansion",
            "evidence": {
                "candidate_pool": analysis.get("candidate_pool") or {},
                "screening": analysis.get("screening") or {},
                "audit_review": review,
                "review_priority": {
                    "priority_queue_total": priority.get("priority_queue_total"),
                    "priority_queue_exported_top_n": priority.get(
                        "priority_queue_exported_top_n"
                    ),
                    "source_counts": priority.get("source_counts") or {},
                    "top_risk_flags": risk_flags,
                },
            },
            "family_actions": family_actions,
            "family_action_counts": dict(sorted(action_counts.items())),
            "metadata_blockers": [
                {
                    "blocker": "missing_abstracts",
                    "count": (analysis.get("candidate_pool") or {}).get("missing_abstracts"),
                    "action": "enrich_metadata_or_review_titles_before_more_retrieval",
                },
                {
                    "blocker": "audit_review_defer",
                    "count": (review.get("review_decision_counts") or {}).get(
                        "defer_metadata", 0
                    ),
                    "action": "do_not_promote_audit_rows_without abstract evidence",
                },
            ],
            "guardrails": [
                (
                    "Do not broaden query families until high-priority defers are "
                    "reviewed or enriched."
                ),
                "Do not retire a family with unique included records.",
                "Do not apply global NOT terms from risk flags without loss-audit evidence.",
                "Keep PDF download disabled until title/abstract review stabilizes.",
            ],
        }

    def _candidate_pool_existing_priority_rows(self, export_dir: Path) -> list[dict[str, Any]]:
        priority_dir = export_dir / "review_priority"
        if not priority_dir.exists():
            return []
        priority_files = sorted(priority_dir.glob("review_priority_queue_top*.csv"))
        if not priority_files:
            return []
        return self._read_csv_dicts(priority_files[-1])

    def _candidate_pool_metadata_enrichment_plan(
        self,
        *,
        pool_id: str,
        records: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        priority_rows: list[dict[str, Any]],
        limit: int,
        enriched_records: list[dict[str, Any]] | None = None,
        enrichment_attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        decision_by_id = {
            str(row.get("global_record_id") or ""): row
            for row in decisions
            if row.get("global_record_id")
        }
        priority_by_id = {
            str(row.get("global_record_id") or ""): row
            for row in priority_rows
            if row.get("global_record_id")
        }
        enriched_keys = {
            self._normalize_candidate_pool_key(str(row.get("candidate_pool_key") or ""))
            for row in (enriched_records or [])
            if row.get("candidate_pool_key")
        }
        latest_attempt_by_key: dict[str, dict[str, Any]] = {}
        for row in enrichment_attempts or []:
            candidate_key = self._normalize_candidate_pool_key(
                str(row.get("candidate_pool_key") or "")
            )
            if candidate_key:
                latest_attempt_by_key[candidate_key] = row
        terminal_attempt_keys = {
            candidate_key
            for candidate_key, row in latest_attempt_by_key.items()
            if self._metadata_enrichment_attempt_is_terminal(row)
        }
        retryable_attempt_keys = {
            candidate_key
            for candidate_key, row in latest_attempt_by_key.items()
            if not self._metadata_enrichment_attempt_is_terminal(row)
        }
        unresolved_records = [
            record
            for record in records
            if not record.get("abstract")
            and self._normalize_candidate_pool_key(
                str(record.get("candidate_pool_key") or "")
            )
            not in enriched_keys
        ]
        candidate_rows = [
            self._candidate_pool_metadata_enrichment_row(
                record=record,
                decision=decision_by_id.get(str(record.get("candidate_pool_key") or "")),
                priority=priority_by_id.get(str(record.get("candidate_pool_key") or "")),
            )
            for record in unresolved_records
            if self._normalize_candidate_pool_key(
                str(record.get("candidate_pool_key") or "")
            )
            not in terminal_attempt_keys
        ]
        queue = [
            row
            for row in candidate_rows
            if row["preferred_enrichment_route"]
            != "insufficient_identifier_metadata_review_only"
        ]
        review_only = [
            row
            for row in candidate_rows
            if row["preferred_enrichment_route"]
            == "insufficient_identifier_metadata_review_only"
        ]
        queue.sort(
            key=lambda row: (
                -int(row["enrichment_priority_score"]),
                str(row["candidate_pool_key"]),
            )
        )
        exported = queue[: max(0, limit)]
        route_counts: dict[str, int] = {}
        provider_counts: dict[str, int] = {}
        family_counts: dict[str, int] = {}
        for row in queue:
            route = str(row["preferred_enrichment_route"])
            route_counts[route] = route_counts.get(route, 0) + 1
            for provider in self._jsonish_list(row.get("source_providers")):
                key = str(provider)
                provider_counts[key] = provider_counts.get(key, 0) + 1
            for family in self._jsonish_list(row.get("query_families")):
                key = str(family)
                family_counts[key] = family_counts.get(key, 0) + 1
        terminal_missing_abstract = len(terminal_attempt_keys - enriched_keys)
        retryable_missing_abstract = len(retryable_attempt_keys - enriched_keys)
        metadata_outcomes_accounted = terminal_missing_abstract + len(review_only)
        metadata_lookup_outcome_complete = (
            not queue
            and retryable_missing_abstract == 0
            and len(unresolved_records) == metadata_outcomes_accounted
        )
        return {
            "plan_version": "candidate-pool-metadata-enrichment-v2",
            "pool_id": pool_id,
            "recommended_next_action": (
                "metadata_enrichment_outcomes_complete"
                if metadata_lookup_outcome_complete
                else "enrich_missing_abstract_metadata_before_more_query_family_expansion"
            ),
            "summary": {
                "deduplicated_candidate_count": len(records),
                "screened_decision_count": len(decisions),
                "records_missing_abstract": len(unresolved_records),
                "records_with_recovered_abstract": len(enriched_keys),
                "enrichment_terminal_attempts": len(terminal_attempt_keys - enriched_keys),
                "enrichment_retryable_attempts": len(
                    retryable_attempt_keys - enriched_keys
                ),
                "terminal_missing_abstract": terminal_missing_abstract,
                "retryable_missing_abstract": retryable_missing_abstract,
                "metadata_outcomes_accounted": metadata_outcomes_accounted,
                "metadata_lookup_outcome_complete": metadata_lookup_outcome_complete,
                "enrichment_queue_total": len(queue),
                "enrichment_queue_exported": len(exported),
                "review_only_missing_abstract": len(review_only),
                "route_counts": dict(sorted(route_counts.items())),
                "provider_counts": dict(sorted(provider_counts.items())),
                "top_query_families": dict(
                    sorted(family_counts.items(), key=lambda item: (-item[1], item[0]))[:25]
                ),
            },
            "enrichment_queue": exported,
            "guardrails": [
                "Use DOI, PMID, OpenAlex ID, and provider record IDs only for metadata lookup.",
                "Do not download PDFs during metadata enrichment.",
                "Do not infer include or exclude decisions from missing abstracts.",
                "Do not overwrite raw provider provenance; write enrichment as separate metadata.",
                "Do not log API keys, emails, or provider secrets.",
            ],
        }

    def _candidate_pool_metadata_enrichment_row(
        self,
        *,
        record: dict[str, Any],
        decision: dict[str, Any] | None,
        priority: dict[str, Any] | None,
    ) -> dict[str, Any]:
        providers = record.get("source_providers") or [record.get("source_provider")]
        families = record.get("query_families") or [record.get("query_family")]
        route = self._candidate_pool_enrichment_route(record)
        score = 0
        reasons: list[str] = []
        provisional = str((decision or {}).get("decision") or "")
        if provisional == "include":
            score += 100
            reasons.append("provisional_include_missing_abstract")
        elif provisional == "defer_metadata":
            score += 60
            reasons.append("defer_metadata_missing_abstract")
        elif provisional == "exclude":
            score += 15
            reasons.append("exclude_missing_abstract_low_priority")
        else:
            score += 25
            reasons.append("unscreened_missing_abstract")
        if priority:
            with suppress(ValueError, TypeError):
                score += min(50, int(priority.get("review_rank_score") or 0) // 4)
            reasons.append("present_in_review_priority_queue")
        if record.get("doi"):
            score += 30
            reasons.append("doi_available")
        if record.get("pmid"):
            score += 35
            reasons.append("pmid_available")
        if record.get("openalex_id"):
            score += 25
            reasons.append("openalex_id_available")
        if route == "insufficient_identifier_metadata_review_only":
            score -= 40
            reasons.append("no_stable_identifier_for_api_enrichment")
        return {
            "enrichment_priority_score": score,
            "candidate_pool_key": record.get("candidate_pool_key"),
            "title": record.get("title"),
            "publication_year": record.get("year"),
            "doi": record.get("doi"),
            "pmid": record.get("pmid"),
            "openalex_id": record.get("openalex_id"),
            "provider_record_id": record.get("provider_record_id"),
            "source_providers": providers if isinstance(providers, list) else [providers],
            "query_families": families if isinstance(families, list) else [families],
            "provisional_decision": provisional or "unscreened",
            "preferred_enrichment_route": route,
            "priority_reasons": reasons,
            "metadata_to_request": [
                "abstract",
                "keywords",
                "document_type",
                "language",
                "publication_date",
                "journal",
                "identifiers",
            ],
        }

    @staticmethod
    def _candidate_pool_enrichment_route(record: dict[str, Any]) -> str:
        if record.get("pmid"):
            return "pubmed_by_pmid"
        if record.get("doi"):
            return "crossref_openalex_by_doi"
        if record.get("openalex_id"):
            return "openalex_by_work_id"
        source_text = str(record.get("source_provider") or record.get("source_providers") or "")
        if record.get("provider_record_id") and "pubmed" in source_text.lower():
            return "pubmed_by_provider_record_id"
        return "insufficient_identifier_metadata_review_only"

    def _write_candidate_pool_metadata_enrichment_markdown(
        self, path: Path, plan: dict[str, Any]
    ) -> None:
        summary = plan["summary"]
        lines = [
            "# Candidate Pool Metadata Enrichment Plan",
            "",
            f"Pool ID: `{plan['pool_id']}`",
            "",
            f"- recommended next action: `{plan['recommended_next_action']}`",
            f"- deduplicated candidates: {summary['deduplicated_candidate_count']}",
            f"- records missing abstract: {summary['records_missing_abstract']}",
            (
                "- records with recovered abstract: "
                f"{summary['records_with_recovered_abstract']}"
            ),
            f"- terminal lookup attempts: {summary['enrichment_terminal_attempts']}",
            f"- retryable lookup attempts: {summary['enrichment_retryable_attempts']}",
            f"- enrichment queue total: {summary['enrichment_queue_total']}",
            f"- enrichment queue exported: {summary['enrichment_queue_exported']}",
            f"- review-only missing abstracts: {summary['review_only_missing_abstract']}",
            "",
            "## Enrichment Routes",
            "",
        ]
        lines.extend(
            f"- {route}: {count}"
            for route, count in summary["route_counts"].items()
        )
        lines.extend(["", "## Top Query Families With Missing Abstracts", ""])
        lines.extend(
            f"- `{family}`: {count}"
            for family, count in summary["top_query_families"].items()
        )
        lines.extend(
            [
                "",
                "## Top Enrichment Queue",
                "",
                "| rank | score | route | decision | title |",
                "|---:|---:|---|---|---|",
            ]
        )
        for index, row in enumerate(plan["enrichment_queue"][:50], start=1):
            title = str(row.get("title") or "").replace("|", " ")
            if len(title) > 120:
                title = title[:117] + "..."
            lines.append(
                f"| {index} | {row['enrichment_priority_score']} | "
                f"{row['preferred_enrichment_route']} | "
                f"{row['provisional_decision']} | {title} |"
            )
        lines.extend(["", "## Guardrails", ""])
        lines.extend(f"- {item}" for item in plan["guardrails"])
        write_text_atomic(path, "\n".join(lines) + "\n")

    def _candidate_pool_enrichment_matches(
        self,
        *,
        requested_records: list[dict[str, Any]],
        candidates_ref: Path,
    ) -> list[dict[str, Any]]:
        candidate_rows = self._read_jsonl_records(candidates_ref)
        by_doi = {
            self._candidate_pool_normalized_doi(row.get("doi")): row
            for row in candidate_rows
            if self._candidate_pool_normalized_doi(row.get("doi"))
        }
        by_pmid = {
            str(row.get("pmid") or row.get("provider_record_id") or ""): row
            for row in candidate_rows
            if row.get("pmid") or row.get("provider_record_id")
        }
        enriched: list[dict[str, Any]] = []
        for record in requested_records:
            candidate = None
            normalized_doi = self._candidate_pool_normalized_doi(record.get("doi"))
            if normalized_doi:
                candidate = by_doi.get(normalized_doi)
            if candidate is None and record.get("pmid"):
                candidate = by_pmid.get(str(record["pmid"]))
            if candidate is None:
                continue
            abstract = candidate.get("abstract")
            if not abstract:
                continue
            merged = {
                "candidate_pool_key": record.get("candidate_pool_key"),
                "source_candidate_pool_title": record.get("title"),
                "enrichment_provider": candidate.get("source_provider"),
                "enrichment_source_record_id": candidate.get("source_record_id"),
                "title": candidate.get("title") or record.get("title"),
                "abstract": abstract,
                "doi": candidate.get("doi") or record.get("doi"),
                "pmid": candidate.get("pmid") or record.get("pmid"),
                "openalex_id": candidate.get("openalex_id") or record.get("openalex_id"),
                "document_type": candidate.get("document_type") or record.get("document_type"),
                "language": candidate.get("language") or record.get("language"),
                "journal": candidate.get("journal") or record.get("journal"),
                "year": candidate.get("year") or record.get("year"),
                "keywords": candidate.get("keywords") or record.get("keywords") or [],
                "authors": candidate.get("authors") or record.get("authors") or [],
                "raw_enrichment": candidate,
                "enriched_at": utc_now_iso(),
            }
            enriched.append(merged)
        return enriched

    def _write_candidate_pool_metadata_enrichment_summary_markdown(
        self,
        path: Path,
        summary: dict[str, Any],
        enriched_records: list[dict[str, Any]],
    ) -> None:
        cumulative_enriched = summary.get(
            "cumulative_enriched_records", summary["enriched_records"]
        )
        cumulative_with_abstract = summary.get(
            "cumulative_records_with_abstract", summary["records_with_abstract"]
        )
        lines = [
            "# Candidate Pool Metadata Enrichment Summary",
            "",
            f"Pool ID: `{summary['pool_id']}`",
            "",
            f"- status: `{summary['status']}`",
            f"- requested records: {summary['requested_records']}",
            f"- enriched records: {summary['enriched_records']}",
            f"- records with abstract: {summary['records_with_abstract']}",
            (
                "- cumulative enriched records: "
                f"{cumulative_enriched}"
            ),
            (
                "- cumulative records with abstract: "
                f"{cumulative_with_abstract}"
            ),
            f"- retryable records: {summary['retryable_records']}",
            f"- retry-exhausted records: {summary['retry_exhausted_records']}",
            f"- remaining missing abstract: {summary['remaining_missing_abstract']}",
            f"- remaining enrichment queue: {summary['remaining_enrichment_queue']}",
            (
                "- metadata outcome complete: "
                f"{summary.get('metadata_lookup_outcome_complete', False)}"
            ),
            f"- batches: {summary['batch_count']} ({summary['failed_batches']} failed)",
            f"- providers attempted: {', '.join(summary['providers_attempted'])}",
            "",
            "## Artifacts",
            "",
            f"- input: `{summary['input_ref']}`",
            f"- output: `{summary['output_ref']}`",
            f"- queries: `{summary['queries_ref']}`",
            f"- enriched records: `{summary['enriched_records_ref']}`",
            f"- source status: `{summary['source_status_ref']}`",
            f"- provider states: `{summary['provider_states_ref']}`",
            "",
            "## Representative Enriched Records",
            "",
            "| provider | title |",
            "|---|---|",
        ]
        for row in enriched_records[:25]:
            title = str(row.get("title") or "").replace("|", " ")
            if len(title) > 140:
                title = title[:137] + "..."
            lines.append(f"| {row.get('enrichment_provider')} | {title} |")
        lines.extend(["", "## Guardrails", ""])
        lines.extend(f"- {item}" for item in summary["guardrails"])
        write_text_atomic(path, "\n".join(lines) + "\n")

    @staticmethod
    def _known_query_family_config_names() -> list[str]:
        return [
            "qfamily_antibiotics_pharmaceuticals_surface_water_refined_v2",
            "qfamily_artificial_sweeteners_tracers_surface_water",
            "qfamily_benzothiazoles_triclosan_surface_water",
            "qfamily_chlorinated_paraffins_siloxanes_surface_water",
            "qfamily_disinfection_quat_surface_water_refined_v2",
            "qfamily_generic_cec_surface_water",
            "qfamily_hormones_endocrine_surface_water",
            "qfamily_industrial_additives_flame_retardants_surface_water_refined_v2",
            "qfamily_macrolide_river_surface_water_refined_v2",
            "qfamily_microplastics_nanoplastics_surface_water",
            "qfamily_nontarget_suspect_screening_surface_water",
            "qfamily_organophosphate_esters_surface_water",
            "qfamily_passive_sampler_freshwater_cec",
            "qfamily_pcp_lifestyle_markers_surface_water",
            "qfamily_pesticide_gap_neonicotinoid_fipronil_glyphosate_surface_water",
            "qfamily_pesticides_surface_water",
            "qfamily_pfas_occurrence_surface_water",
            "qfamily_plasticizers_phthalates_surface_water",
            "qfamily_qac_disinfectant_residues_surface_water_refined_v2",
            "qfamily_steroid_hormones_waterbody_refined_v2",
            "qfamily_surface_water_biota_integrated_cec",
            "qfamily_tire_wear_6ppd_surface_water",
            "qfamily_uv_filters_benzotriazoles_surface_water",
        ]

    def _recently_held_query_family_config_names(self, pool_id: str) -> set[str]:
        held: set[str] = set()
        previous_pool_id = self._previous_query_family_pool_id(pool_id)
        if previous_pool_id is None:
            return held
        plan_ref = (
            self.repo_root
            / "docs"
            / "retrieval_runs"
            / "exports"
            / previous_pool_id
            / "next_query_family_construction_plan.json"
        )
        if not plan_ref.exists():
            return held
        try:
            previous_plan = read_json(plan_ref)
        except (OSError, ValueError, TypeError):
            return held
        for item in previous_plan.get("held_for_refinement_family_config_dirs") or []:
            config_name = str((item or {}).get("config_name") or "")
            if config_name:
                held.add(config_name)
        return held

    @staticmethod
    def _previous_query_family_pool_id(pool_id: str) -> str | None:
        match = re.search(r"_v(\d+)(?=_)", pool_id)
        if match is None:
            return None
        version = int(match.group(1))
        if version <= 1:
            return None
        return pool_id[: match.start(1)] + str(version - 1) + pool_id[match.end(1) :]

    @staticmethod
    def _query_family_was_recently_held(
        config_name: str, held_config_names: set[str]
    ) -> bool:
        target_key = Phase11Runner._query_family_base_config_key(config_name)
        return any(
            Phase11Runner._query_family_base_config_key(held_name) == target_key
            for held_name in held_config_names
        )

    @staticmethod
    def _query_family_refined_successor_name(
        config_name: str, available_config_names: Iterable[str]
    ) -> str | None:
        target_key = Phase11Runner._query_family_base_config_key(config_name)
        candidates = [
            name
            for name in available_config_names
            if Phase11Runner._query_family_base_config_key(name) == target_key
            and name != config_name
            and re.search(r"_refined_v\d+$", name)
        ]
        if not candidates:
            return None
        return max(candidates, key=Phase11Runner._query_family_refined_version)

    @staticmethod
    def _query_family_refined_version(config_name: str) -> int:
        match = re.search(r"_refined_v(\d+)$", config_name)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _query_family_base_config_key(config_name: str) -> str:
        return re.sub(r"_refined_v\d+$", "", config_name)

    @staticmethod
    def _query_family_config_name_from_metric(family: str) -> str:
        raw = re.sub(r"^0\.1\.\d+(?:\.\d+)?-", "", family)
        raw = re.sub(r"^qfamily-", "", raw)
        raw = raw.replace("-refined", "-refined-v2")
        return "qfamily_" + raw.replace("-", "_")

    @staticmethod
    def _query_family_gap_reason(config_name: str) -> str:
        reasons = {
            "qfamily_artificial_sweeteners_tracers_surface_water": (
                "anthropogenic tracer and lifestyle-marker branch is undercovered as a "
                "standalone family"
            ),
            "qfamily_tire_wear_6ppd_surface_water": (
                "tire-wear chemicals and 6PPD-quinone are emerging stormwater and "
                "receiving-water contaminants"
            ),
            "qfamily_pesticide_gap_neonicotinoid_fipronil_glyphosate_surface_water": (
                "narrows pesticide coverage toward high-priority compounds likely diluted "
                "inside the broader pesticide branch"
            ),
            "qfamily_qac_disinfectant_residues_surface_water_refined_v2": (
                "captures quaternary ammonium disinfectant residues without relying on the "
                "noisy broad disinfection branch"
            ),
            "qfamily_antibiotics_pharmaceuticals_surface_water_refined_v2": (
                "keeps pharmaceuticals and antibiotics as a dedicated high-yield occurrence branch"
            ),
            "qfamily_industrial_additives_flame_retardants_surface_water_refined_v2": (
                "broadens industrial additive and flame-retardant coverage beyond OPE-only terms"
            ),
        }
        return reasons.get(config_name, "fills an undercovered contaminant-family branch")

    @staticmethod
    def _next_query_family_pool_id(pool_id: str) -> str:
        match = re.search(r"_v(\d+)(?=_)|_v(\d+)$", pool_id)
        if not match:
            return f"{pool_id}_refined_v2"
        version = int(next(group for group in match.groups() if group is not None))
        start, end = match.span()
        return f"{pool_id[:start]}_v{version + 1}{pool_id[end:]}"

    @staticmethod
    def _query_family_refinement_focus(
        *,
        family: str,
        config_name: str,
        noise_terms: dict[str, Any],
    ) -> list[str]:
        text = f"{family} {config_name}".lower()
        focus: list[str] = []
        if "disinfection" in text or "quat" in text or "qac" in text:
            focus.extend(
                [
                    (
                        "separate ambient receiving-water monitoring from "
                        "drinking-water/treatment-process formation studies"
                    ),
                    (
                        "require occurrence/concentration or field monitoring "
                        "terms with surface-water terms"
                    ),
                ]
            )
        if "flame" in text or "industrial" in text or "organophosphate" in text:
            focus.extend(
                [
                    (
                        "separate environmental OPE/BFR occurrence studies from "
                        "polymer flame-retardant material-performance studies"
                    ),
                    "prefer mass-spectrometry and ng/L environmental concentration context",
                ]
            )
        if "tire" in text or "6ppd" in text:
            focus.extend(
                [
                    (
                        "separate receiving-water or stormwater occurrence from "
                        "tire-product, road-dust, and laboratory leaching studies"
                    ),
                    "keep 6PPD/6PPD-quinone recall while auditing simulated-runoff noise",
                ]
            )
        if "steroid" in text or "hormone" in text or "endocrine" in text:
            focus.extend(
                [
                    (
                        "separate ambient EDC concentration studies from animal, "
                        "clinical, endocrine-gland, and in-vitro toxicology"
                    ),
                    "require waterbody terms plus occurrence/concentration terms where possible",
                ]
            )
        if "pesticide" in text or "neonicotinoid" in text:
            focus.extend(
                [
                    (
                        "separate surface-water pesticide residue monitoring from "
                        "crop spraying equipment and pollinator toxicology"
                    ),
                    (
                        "preserve high-priority compound names only when paired "
                        "with waterbody/monitoring context"
                    ),
                ]
            )
        if "microplastic" in text or "biota" in text:
            focus.extend(
                [
                    (
                        "separate direct water-column/surface-water microplastic "
                        "studies from fish-only or model-only studies"
                    ),
                    "audit biota-integrated records for separately extractable water samples",
                ]
            )
        top_noise: list[str] = []
        for rows in noise_terms.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and row.get("value"):
                    top_noise.append(str(row["value"]))
                if len(top_noise) >= 5:
                    break
            if len(top_noise) >= 5:
                break
        if top_noise:
            focus.append(
                "audit loss risk before applying noise controls for: "
                + ", ".join(top_noise)
            )
        return list(dict.fromkeys(focus))

    def _candidate_pool_decision_counts(
        self, decisions: list[dict[str, Any]]
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for decision in decisions:
            label = str(decision.get("decision") or "unknown")
            counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items()))

    def _candidate_pool_group_metrics(
        self,
        *,
        records: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        group_field: str,
    ) -> dict[str, dict[str, Any]]:
        total_by_group: dict[str, int] = {}
        for record in records:
            groups = record.get(group_field, [])
            if not isinstance(groups, list):
                groups = [groups]
            for group in groups:
                key = str(group)
                total_by_group[key] = total_by_group.get(key, 0) + 1
        metrics: dict[str, dict[str, Any]] = {
            group: {
                "candidate_count": count,
                "unique_candidate_count": 0,
                "screened_count": 0,
                "unique_screened_count": 0,
                "decision_counts": {},
                "unique_decision_counts": {},
                "include_rate_screened": 0.0,
                "defer_rate_screened": 0.0,
                "unique_include_count": 0,
                "unique_include_rate_screened": 0.0,
                "screening_burden_per_include": None,
                "recommended_role": "unscreened",
            }
            for group, count in total_by_group.items()
        }
        for record in records:
            groups = record.get(group_field, [])
            if not isinstance(groups, list):
                groups = [groups]
            normalized_groups = sorted({str(group) for group in groups if str(group)})
            if len(normalized_groups) != 1:
                continue
            key = normalized_groups[0]
            if key in metrics:
                metrics[key]["unique_candidate_count"] = (
                    int(metrics[key]["unique_candidate_count"]) + 1
                )
        for decision in decisions:
            label = str(decision.get("decision") or "unknown")
            groups = decision.get(group_field, [])
            if not isinstance(groups, list):
                groups = [groups]
            normalized_groups = sorted({str(group) for group in groups if str(group)})
            for group in groups:
                key = str(group)
                row = metrics.setdefault(
                    key,
                    {
                        "candidate_count": 0,
                        "unique_candidate_count": 0,
                        "screened_count": 0,
                        "unique_screened_count": 0,
                        "decision_counts": {},
                        "unique_decision_counts": {},
                        "include_rate_screened": 0.0,
                        "defer_rate_screened": 0.0,
                        "unique_include_count": 0,
                        "unique_include_rate_screened": 0.0,
                        "screening_burden_per_include": None,
                        "recommended_role": "screened_only",
                    },
                )
                row["screened_count"] = int(row["screened_count"]) + 1
                row_counts = row["decision_counts"]
                if isinstance(row_counts, dict):
                    row_counts[label] = int(row_counts.get(label, 0)) + 1
                if len(normalized_groups) == 1 and normalized_groups[0] == key:
                    row["unique_screened_count"] = int(row["unique_screened_count"]) + 1
                    unique_counts = row["unique_decision_counts"]
                    if isinstance(unique_counts, dict):
                        unique_counts[label] = int(unique_counts.get(label, 0)) + 1
        for row in metrics.values():
            screened_count = int(row["screened_count"])
            counts = row["decision_counts"]
            include_count = int(counts.get("include", 0)) if isinstance(counts, dict) else 0
            defer_count = int(counts.get("defer_metadata", 0)) if isinstance(counts, dict) else 0
            exclude_count = int(counts.get("exclude", 0)) if isinstance(counts, dict) else 0
            unique_screened_count = int(row["unique_screened_count"])
            unique_counts = row["unique_decision_counts"]
            unique_include_count = (
                int(unique_counts.get("include", 0)) if isinstance(unique_counts, dict) else 0
            )
            row["include_rate_screened"] = (
                include_count / screened_count if screened_count else 0.0
            )
            row["defer_rate_screened"] = defer_count / screened_count if screened_count else 0.0
            row["unique_include_count"] = unique_include_count
            row["unique_include_rate_screened"] = (
                unique_include_count / unique_screened_count
                if unique_screened_count
                else 0.0
            )
            row["screening_burden_per_include"] = (
                screened_count / include_count if include_count else None
            )
            row["recommended_role"] = self._candidate_pool_group_role(
                screened_count=screened_count,
                include_count=include_count,
                exclude_count=exclude_count,
                defer_count=defer_count,
                unique_include_count=unique_include_count,
            )
        return dict(sorted(metrics.items()))

    @staticmethod
    def _candidate_pool_group_role(
        *,
        screened_count: int,
        include_count: int,
        exclude_count: int,
        defer_count: int,
        unique_include_count: int,
    ) -> str:
        if screened_count == 0:
            return "needs_screening"
        include_rate = include_count / screened_count
        if include_count == 0 and screened_count >= 3:
            return "high_noise_or_low_yield_branch"
        if unique_include_count > 0 and include_rate >= 0.35:
            return "unique_recall_branch"
        if include_count > 0 and include_rate >= 0.5:
            return "strong_recall_branch"
        if include_count > 0:
            return "keep_as_recall_branch_with_noise_controls"
        if defer_count > exclude_count:
            return "metadata_limited_branch"
        return "needs_more_screening"

    def _candidate_pool_term_analysis(
        self, decisions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        positive_terms: dict[str, dict[str, int]] = {}
        noise_terms: dict[str, dict[str, int]] = {}
        defer_terms: dict[str, dict[str, int]] = {}
        reason_counts: dict[str, dict[str, int]] = {}
        for decision in decisions:
            label = str(decision.get("decision") or "unknown")
            for reason in decision.get("reason_codes", []):
                reason_key = str(reason)
                reason_counts.setdefault(label, {})
                reason_counts[label][reason_key] = reason_counts[label].get(reason_key, 0) + 1
            for evidence in decision.get("query_term_evidence", []):
                if not isinstance(evidence, dict):
                    continue
                term = self._normalize_candidate_term(str(evidence.get("term") or ""))
                if not term:
                    continue
                block = str(evidence.get("concept_block") or "unknown")
                if label == "include":
                    bucket = positive_terms
                elif label == "exclude":
                    bucket = noise_terms
                elif label == "defer_metadata":
                    bucket = defer_terms
                else:
                    continue
                bucket.setdefault(block, {})
                bucket[block][term] = bucket[block].get(term, 0) + 1
        return {
            "positive_terms_by_block": {
                block: self._top_counts(counts)
                for block, counts in sorted(positive_terms.items())
            },
            "noise_terms_by_block": {
                block: self._top_counts(counts)
                for block, counts in sorted(noise_terms.items())
            },
            "defer_terms_by_block": {
                block: self._top_counts(counts)
                for block, counts in sorted(defer_terms.items())
            },
            "exclude_reason_counts": self._top_counts(reason_counts.get("exclude", {})),
            "defer_reason_counts": self._top_counts(reason_counts.get("defer_metadata", {})),
        }

    def _candidate_pool_targeted_audit(
        self,
        *,
        pool_id: str,
        records: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        sample_size: int,
    ) -> dict[str, Any]:
        records_by_id = {
            str(record.get("candidate_pool_key") or ""): record
            for record in records
            if record.get("candidate_pool_key")
        }
        enriched = [
            decision
            | {
                "record": records_by_id.get(
                    str(decision.get("global_record_id") or ""), {}
                )
            }
            for decision in decisions
        ]
        crossref_include = [
            row
            for row in enriched
            if row.get("decision") == "include"
            and "crossref" in self._candidate_pool_lower_list(row, "source_providers")
        ]
        risky_exclude = [
            row
            for row in enriched
            if row.get("decision") == "exclude"
            and self._candidate_pool_signal_matches(row, "pollutant")
            and self._candidate_pool_signal_matches(row, "water")
        ]
        risky_exclude.sort(key=self._candidate_pool_audit_priority, reverse=True)
        defer_rows = [row for row in enriched if row.get("decision") == "defer_metadata"]
        defer_rows.sort(key=self._candidate_pool_audit_priority, reverse=True)
        include_with_noise = [
            row
            for row in enriched
            if row.get("decision") == "include"
            and self._candidate_pool_signal_matches(row, "noise")
        ]
        include_with_noise.sort(
            key=lambda row: len(self._candidate_pool_signal_terms(row, "noise")),
            reverse=True,
        )
        samples = {
            "crossref_include_review_sample": [
                self._candidate_pool_audit_row(row, "crossref_include_review")
                for row in crossref_include[:sample_size]
            ],
            "risky_exclude_false_negative_sample": [
                self._candidate_pool_audit_row(row, "risky_exclude_false_negative")
                for row in risky_exclude[:sample_size]
            ],
            "defer_resolution_sample": [
                self._candidate_pool_audit_row(row, "defer_resolution")
                for row in defer_rows[:sample_size]
            ],
            "include_with_noise_loss_audit_sample": [
                self._candidate_pool_audit_row(row, "include_with_noise_loss_audit")
                for row in include_with_noise[:sample_size]
            ],
        }
        noise_summary = self._candidate_pool_noise_loss_audit(enriched)
        summary = {
            "pool_id": pool_id,
            "records_total": len(records),
            "decisions_total": len(decisions),
            "sample_size": sample_size,
            "sample_counts": {name: len(rows) for name, rows in samples.items()},
            "noise_term_loss_audit": noise_summary,
            "interpretation": [
                (
                    "Crossref include records require review because Crossref has high "
                    "pool-level noise."
                ),
                (
                    "Risky excludes contain both pollutant and surface-water signals and "
                    "are the highest false-negative-risk set."
                ),
                (
                    "Noise terms with include_occurrences > 0 are unsafe as broad NOT "
                    "terms without loss audit."
                ),
            ],
        }
        return {"summary": summary, "samples": samples}

    def _candidate_pool_review_queue(
        self,
        *,
        records: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        audit_root: Path,
    ) -> list[dict[str, Any]]:
        records_by_id = {
            str(record.get("candidate_pool_key") or ""): record
            for record in records
            if record.get("candidate_pool_key")
        }
        decisions_by_id = {
            str(decision.get("global_record_id") or ""): decision
            for decision in decisions
            if decision.get("global_record_id")
        }
        sample_paths = [
            audit_root / "include_with_noise_loss_audit_sample.json",
            audit_root / "risky_exclude_false_negative_sample.json",
            audit_root / "defer_resolution_sample.json",
            audit_root / "crossref_include_review_sample.json",
        ]
        queued: dict[str, dict[str, Any]] = {}
        for path in sample_paths:
            if not path.exists():
                continue
            payload = read_json(path)
            if not isinstance(payload, list):
                continue
            for row in payload:
                if not isinstance(row, dict):
                    continue
                record_id = str(row.get("global_record_id") or "")
                record = records_by_id.get(record_id)
                decision = decisions_by_id.get(record_id, {})
                if not record:
                    continue
                existing = queued.get(record_id)
                audit_category = str(row.get("audit_category") or "targeted_audit")
                if existing:
                    existing.setdefault("audit_categories", []).append(audit_category)
                    continue
                queued[record_id] = {
                    "global_record_id": record_id,
                    "record": record,
                    "audit_category": audit_category,
                    "audit_categories": [audit_category],
                    "audit_summary": row,
                    "provisional_decision": decision.get("decision") or row.get("decision"),
                    "provisional_reason_codes": decision.get("reason_codes", []),
                    "query_families": self._candidate_pool_group_values(
                        decision | {"record": record}, "query_families"
                    ),
                    "source_providers": self._candidate_pool_group_values(
                        decision | {"record": record}, "source_providers"
                    ),
                }
        return sorted(
            queued.values(),
            key=lambda item: (
                self._candidate_pool_review_category_priority(
                    cast(list[str], item.get("audit_categories") or [])
                ),
                str(item.get("global_record_id") or ""),
            ),
        )

    @staticmethod
    def _candidate_pool_review_category_priority(categories: list[str]) -> int:
        order = {
            "include_with_noise_loss_audit": 0,
            "risky_exclude_false_negative": 1,
            "defer_resolution": 2,
            "crossref_include_review": 3,
        }
        return min((order.get(category, 99) for category in categories), default=99)

    @staticmethod
    def _candidate_pool_review_outcome(*, provisional: str, reviewed: str) -> str:
        if provisional == "defer_metadata" and reviewed == "defer_metadata":
            return "still_needs_metadata"
        if provisional == reviewed:
            return "confirmed"
        if provisional == "include" and reviewed != "include":
            return "include_downgraded"
        if provisional != "include" and reviewed == "include":
            return "false_negative_risk"
        if provisional.startswith("defer") and reviewed in {"exclude", "include"}:
            return "defer_resolved"
        return "changed"

    def _candidate_pool_review_summary(
        self,
        *,
        pool_id: str,
        review_queue: list[dict[str, Any]],
        review_decisions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        outcome_counts: dict[str, int] = {}
        decision_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        for item in review_queue:
            for category in cast(list[str], item.get("audit_categories") or []):
                category_counts[category] = category_counts.get(category, 0) + 1
        for decision in review_decisions:
            outcome = str(decision.get("review_outcome") or "unknown")
            final_decision = str(decision.get("decision") or "unknown")
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            decision_counts[final_decision] = decision_counts.get(final_decision, 0) + 1
        return {
            "pool_id": pool_id,
            "review_queue_size": len(review_queue),
            "reviewed_total": len(review_decisions),
            "remaining_review_queue": max(0, len(review_queue) - len(review_decisions)),
            "review_decision_counts": dict(sorted(decision_counts.items())),
            "review_outcome_counts": dict(sorted(outcome_counts.items())),
            "audit_category_counts": dict(sorted(category_counts.items())),
            "interpretation": [
                (
                    "Use this targeted audit to calibrate the provisional candidate-pool "
                    "screening before treating included records as final eligible records."
                ),
                (
                    "Any false_negative_risk outcome means risky excludes must be reviewed "
                    "more broadly before final corpus construction."
                ),
                (
                    "Include downgrades indicate query-family noise or provisional screener "
                    "over-inclusion; they should refine screening guidance before more "
                    "retrieval expansion."
                ),
            ],
        }

    def _write_candidate_pool_review_summary_markdown(
        self, pool_root: Path, summary: dict[str, Any]
    ) -> None:
        lines = [
            "# Candidate Pool Audit Review Summary",
            "",
            f"Pool ID: `{summary['pool_id']}`",
            "",
            f"- review queue size: {summary['review_queue_size']}",
            f"- reviewed total: {summary['reviewed_total']}",
            f"- remaining review queue: {summary['remaining_review_queue']}",
            "",
            "## Review Decisions",
            "",
        ]
        lines.extend(
            f"- {decision}: {count}"
            for decision, count in summary["review_decision_counts"].items()
        )
        lines.extend(["", "## Review Outcomes", ""])
        lines.extend(
            f"- {outcome}: {count}"
            for outcome, count in summary["review_outcome_counts"].items()
        )
        lines.extend(["", "## Audit Categories", ""])
        lines.extend(
            f"- {category}: {count}"
            for category, count in summary["audit_category_counts"].items()
        )
        lines.extend(["", "## Interpretation", ""])
        lines.extend(f"- {item}" for item in summary["interpretation"])
        write_text_atomic(pool_root / "AUDIT_REVIEW_SUMMARY.md", "\n".join(lines) + "\n")

    def _write_candidate_pool_review_exports(
        self,
        *,
        export_dir: Path,
        pool_id: str,
        records: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        review_decisions: list[dict[str, Any]],
    ) -> dict[str, str]:
        records_by_id = {
            str(record.get("candidate_pool_key") or ""): record
            for record in records
            if record.get("candidate_pool_key")
        }
        review_by_id = {
            str(decision.get("global_record_id") or ""): decision
            for decision in review_decisions
            if decision.get("global_record_id")
        }
        rows = [
            self._candidate_pool_export_row(
                decision=decision,
                record=records_by_id.get(str(decision.get("global_record_id") or ""), {}),
                review_decision=review_by_id.get(str(decision.get("global_record_id") or "")),
            )
            for decision in decisions
        ]
        needs_review = [
            row
            for row in rows
            if self._candidate_pool_export_needs_review(row)
        ]
        final_include = [
            row
            for row in rows
            if row["final_decision"] == "include"
            and not self._candidate_pool_export_needs_review(row)
        ]
        include_candidate_needs_review = [
            row
            for row in needs_review
            if row["final_decision"] == "include"
        ]
        resolved_exclude = [
            row
            for row in rows
            if row["final_decision"] in {"exclude", "defer_not_downloaded"}
            or (
                row["final_decision"] == "defer_metadata"
                and not self._candidate_pool_export_needs_review(row)
            )
        ]
        false_negative_risk = [
            row for row in rows if row["review_outcome"] == "false_negative_risk"
        ]
        fieldnames = [
            "global_record_id",
            "title",
            "publication_year",
            "document_type",
            "language",
            "doi",
            "pmid",
            "source_providers",
            "query_families",
            "provisional_decision",
            "final_decision",
            "final_review_status",
            "review_outcome",
            "include_review_priority",
            "risk_flags",
            "reason_codes",
            "review_reason_codes",
            "evidence_spans",
            "review_evidence_spans",
            "has_abstract",
        ]
        files = {
            "final_included_candidates.csv": final_include,
            "include_candidate_needs_review.csv": include_candidate_needs_review,
            "records_needing_review.csv": needs_review,
            "resolved_exclude_or_defer.csv": resolved_exclude,
            "false_negative_risk_review.csv": false_negative_risk,
        }
        source_state = {
            "screening_decision_rows": len(decisions),
            "screened_records": len(
                {
                    str(decision.get("global_record_id") or "")
                    for decision in decisions
                    if decision.get("global_record_id")
                }
            ),
            "audit_review_decision_rows": len(review_decisions),
            "missing_abstracts_after_enrichment": sum(
                1
                for record in records
                if not self._candidate_pool_metadata_value_present(
                    record.get("abstract")
                )
            ),
            "records_with_metadata_enrichment": sum(
                1
                for record in records
                if record.get("_metadata_enrichment_applied")
            ),
        }
        exported: dict[str, str] = {}
        for filename, file_rows in files.items():
            write_csv_atomic(export_dir / filename, file_rows, fieldnames)
            exported[filename] = self._display_path(export_dir / filename)
        manifest = {
            "pool_id": pool_id,
            "calibration_policy_version": CANDIDATE_POOL_CALIBRATION_POLICY_VERSION,
            "export_dir": self._display_path(export_dir),
            "counts": {
                "screened_total": len(rows),
                "final_include": len(final_include),
                "include_candidate_needs_review": len(include_candidate_needs_review),
                "needs_review": len(needs_review),
                "resolved_exclude_or_defer": len(resolved_exclude),
                "false_negative_risk_review": len(false_negative_risk),
                "audit_reviewed": len(review_decisions),
            },
            "source_state": source_state,
            "files": exported,
            "note": (
                "final_included_candidates.csv contains strict included records that "
                "do not require unresolved review. Unreviewed include candidates remain "
                "in include_candidate_needs_review.csv and records_needing_review.csv."
            ),
        }
        write_json_atomic(export_dir / "audit_review_export_manifest.json", manifest)
        self._write_candidate_pool_review_export_markdown(export_dir, manifest)
        exported["audit_review_export_manifest.json"] = self._display_path(
            export_dir / "audit_review_export_manifest.json"
        )
        exported["AUDIT_REVIEW_EXPORT_MANIFEST.md"] = self._display_path(
            export_dir / "AUDIT_REVIEW_EXPORT_MANIFEST.md"
        )
        return exported

    def _candidate_pool_review_priority_rows(
        self,
        *,
        include_candidates: list[dict[str, Any]],
        needs_review: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        priority_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for bucket, rows in [
            ("include_candidate", include_candidates),
            ("needs_review", needs_review),
        ]:
            for row in rows:
                record_id = str(row.get("global_record_id") or "")
                if not record_id or record_id in seen:
                    continue
                seen.add(record_id)
                score, reasons = self._candidate_pool_review_priority_score(row, bucket)
                priority_rows.append(
                    {
                        "review_rank_score": score,
                        "review_bucket": bucket,
                        "review_reasons": reasons,
                        "global_record_id": record_id,
                        "title": row.get("title", ""),
                        "publication_year": row.get("publication_year", ""),
                        "source_providers": self._jsonish_list(row.get("source_providers")),
                        "query_families": self._jsonish_list(row.get("query_families")),
                        "provisional_decision": row.get("provisional_decision", ""),
                        "final_decision": row.get("final_decision", ""),
                        "final_review_status": row.get("final_review_status", ""),
                        "review_outcome": row.get("review_outcome", ""),
                        "risk_flags": self._jsonish_list(row.get("risk_flags")),
                        "reason_codes": self._jsonish_list(row.get("reason_codes")),
                        "has_abstract": row.get("has_abstract", ""),
                        "doi": row.get("doi", ""),
                        "pmid": row.get("pmid", ""),
                        "document_type": row.get("document_type", ""),
                        "language": row.get("language", ""),
                        "evidence_spans": self._jsonish_list(row.get("evidence_spans")),
                    }
                )
        return priority_rows

    def _candidate_pool_review_priority_score(
        self, row: dict[str, Any], bucket: str
    ) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        risk_flags = [str(value) for value in self._jsonish_list(row.get("risk_flags"))]
        families = self._jsonish_list(row.get("query_families"))
        providers = [
            str(value).lower() for value in self._jsonish_list(row.get("source_providers"))
        ]
        has_abstract = str(row.get("has_abstract") or "").lower() == "true"
        final_decision = str(row.get("final_decision") or row.get("provisional_decision") or "")
        priority = str(row.get("include_review_priority") or "")
        if bucket == "include_candidate":
            score += 100
            reasons.append("candidate_include_needs_confirmation")
        if bucket == "needs_review":
            score += 40
            reasons.append("unresolved_review_required")
        if final_decision == "include":
            score += 40
            reasons.append("calibrated_include_candidate")
        if priority == "high":
            score += 25
            reasons.append("high_review_priority")
        if has_abstract:
            score += 15
            reasons.append("has_abstract")
        else:
            score += 8
            reasons.append("missing_abstract_metadata_resolution")
        if "pubmed" in providers:
            score += 10
            reasons.append("pubmed_metadata_available")
        if any(
            flag in {"missing_surface_water_signal", "missing_monitoring_signal"}
            for flag in risk_flags
        ):
            score -= 12
            reasons.append("missing_core_signal_risk")
        if any(flag.startswith("noise:") for flag in risk_flags):
            score -= 5
            reasons.append("noise_flag_present")
        if any(
            "review" in flag or "treatment" in flag or "method" in flag
            for flag in risk_flags
        ):
            score -= 10
            reasons.append("method_or_review_noise_risk")
        score += min(len(families), 3) * 2
        return score, reasons

    def _candidate_pool_review_priority_summary(
        self,
        *,
        pool_id: str,
        priority_rows: list[dict[str, Any]],
        exported_rows: list[dict[str, Any]],
        rows_by_file: dict[str, list[dict[str, Any]]],
        queue_ref: Path,
    ) -> dict[str, Any]:
        provider_counts: dict[str, int] = {}
        family_counts: dict[str, int] = {}
        decision_counts: dict[str, int] = {}
        risk_counts: dict[str, int] = {}
        for row in exported_rows:
            decision = str(row.get("final_decision") or "unknown")
            decision_counts[decision] = decision_counts.get(decision, 0) + 1
            for provider in self._jsonish_list(row.get("source_providers")):
                key = str(provider)
                provider_counts[key] = provider_counts.get(key, 0) + 1
            for family in self._jsonish_list(row.get("query_families")):
                key = str(family)
                family_counts[key] = family_counts.get(key, 0) + 1
            for risk in self._jsonish_list(row.get("risk_flags")):
                key = str(risk)
                risk_counts[key] = risk_counts.get(key, 0) + 1
        return {
            "pool_id": pool_id,
            "priority_queue_total": len(priority_rows),
            "priority_queue_exported_top_n": len(exported_rows),
            "priority_queue_ref": self._display_path(queue_ref),
            "source_counts": {
                "include_candidate_needs_review": len(
                    rows_by_file["include_candidate_needs_review.csv"]
                ),
                "records_needing_review": len(rows_by_file["records_needing_review.csv"]),
                "resolved_exclude_or_defer": len(
                    rows_by_file["resolved_exclude_or_defer.csv"]
                ),
                "final_included_candidates": len(
                    rows_by_file["final_included_candidates.csv"]
                ),
            },
            "exported_decision_counts": dict(sorted(decision_counts.items())),
            "exported_provider_counts": dict(sorted(provider_counts.items())),
            "exported_top_query_families": dict(
                sorted(family_counts.items(), key=lambda item: (-item[1], item[0]))[:20]
            ),
            "exported_top_risk_flags": dict(
                sorted(risk_counts.items(), key=lambda item: (-item[1], item[0]))[:20]
            ),
            "review_policy": [
                (
                    "Review include candidates with abstracts first because they can "
                    "quickly move into final included candidates."
                ),
                (
                    "Review high-priority defers with surface-water and "
                    "occurrence/concentration evidence before broadening queries further."
                ),
                (
                    "Do not apply global NOT terms from noise diagnostics until "
                    "loss-risk rows are reviewed."
                ),
                "Keep PDF download disabled until title/abstract review stabilizes.",
            ],
        }

    def _write_candidate_pool_review_priority_markdown(
        self, markdown_ref: Path, summary: dict[str, Any]
    ) -> None:
        lines = [
            "# Candidate Pool Review Priority Queue",
            "",
            f"Pool ID: `{summary['pool_id']}`",
            "",
            "## Counts",
            "",
        ]
        lines.extend(
            f"- {name}: {count}"
            for name, count in summary["source_counts"].items()
        )
        lines.extend(
            [
                "",
                "## Priority Queue",
                "",
                f"- total candidate rows ranked: {summary['priority_queue_total']}",
                f"- exported top rows: {summary['priority_queue_exported_top_n']}",
                "",
                "## Exported Provider Counts",
                "",
            ]
        )
        lines.extend(
            f"- {name}: {count}"
            for name, count in summary["exported_provider_counts"].items()
        )
        lines.extend(["", "## Exported Query Families", ""])
        lines.extend(
            f"- `{name}`: {count}"
            for name, count in summary["exported_top_query_families"].items()
        )
        lines.extend(["", "## Review Policy", ""])
        lines.extend(f"- {item}" for item in summary["review_policy"])
        lines.extend(
            [
                "",
                "## Artifacts",
                "",
                f"- `{summary['priority_queue_ref']}`",
                "- `review_priority_summary.json`",
                "",
            ]
        )
        write_text_atomic(markdown_ref, "\n".join(lines))

    def _read_csv_dicts(self, path: Path) -> list[dict[str, Any]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    @staticmethod
    def _jsonish_list(value: Any) -> list[Any]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            with suppress(json.JSONDecodeError):
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
                return [parsed]
        return [value]

    @staticmethod
    def _candidate_pool_export_needs_review(row: dict[str, Any]) -> bool:
        final_review_status = str(row.get("final_review_status") or "")
        final_decision = str(row.get("final_decision") or "")
        review_outcome = str(row.get("review_outcome") or "")
        reason_codes = {str(value) for value in row.get("reason_codes") or []}
        has_abstract = bool(row.get("has_abstract"))
        if final_review_status == "reviewed":
            return False
        if final_review_status == "audit_calibrated":
            if final_decision in {"exclude", "defer_not_downloaded"}:
                return False
            if (
                final_decision == "defer_metadata"
                and has_abstract
                and "D_METADATA_MISSING" not in reason_codes
                and review_outcome
                in {
                    "calibrated_method_treatment_or_model_ambiguous",
                    "calibrated_review_include_method_or_toxicity_guard",
                    "calibrated_provisional_include_method_or_toxicity_guard",
                    "calibrated_review_or_publication_artifact",
                    "calibrated_ineligible_matrix_without_surface_water",
                }
            ):
                return False
        if final_decision == "defer_metadata":
            return True
        if final_decision == "include" and row.get("risk_flags"):
            return True
        if (
            row["provisional_decision"] == "include"
            and row["include_review_priority"] == "high"
            and final_decision != "exclude"
        ):
            return True
        if row["provisional_decision"] != "exclude":
            return False
        risk_flags = set(row.get("risk_flags") or [])
        # Excludes only need audit when the text still contains enough topical
        # signal that a false negative is plausible. Missing-signal flags alone
        # describe resolved noise and should not flood the review queue.
        return not {
            "missing_surface_water_signal",
            "missing_pollutant_signal",
        }.intersection(risk_flags)

    def _candidate_pool_export_row(
        self,
        *,
        decision: dict[str, Any],
        record: dict[str, Any],
        review_decision: dict[str, Any] | None,
    ) -> dict[str, Any]:
        calibrated = self._candidate_pool_calibrated_decision(
            decision=decision,
            record=record,
            review_decision=review_decision,
        )
        final_decision = calibrated["decision"]
        risk_flags = self._candidate_pool_export_risk_flags(decision=decision, record=record)
        return {
            "global_record_id": decision.get("global_record_id"),
            "title": record.get("title") or record.get("title_original"),
            "publication_year": record.get("publication_year") or record.get("year"),
            "document_type": record.get("document_type"),
            "language": record.get("language"),
            "doi": record.get("doi"),
            "pmid": record.get("pmid"),
            "source_providers": self._candidate_pool_group_values(
                decision | {"record": record}, "source_providers"
            ),
            "query_families": self._candidate_pool_group_values(
                decision | {"record": record}, "query_families"
            ),
            "provisional_decision": decision.get("decision"),
            "final_decision": final_decision,
            "final_review_status": calibrated["status"],
            "review_outcome": calibrated["reason"],
            "include_review_priority": "high" if risk_flags else "standard",
            "risk_flags": risk_flags,
            "reason_codes": decision.get("reason_codes", []),
            "review_reason_codes": (review_decision or {}).get("reason_codes", []),
            "evidence_spans": decision.get("evidence_spans", [])[:3],
            "review_evidence_spans": (review_decision or {}).get("evidence_spans", [])[:3],
            "has_abstract": bool(record.get("abstract") or record.get("abstract_original")),
        }

    def _candidate_pool_calibrated_decision(
        self,
        *,
        decision: dict[str, Any],
        record: dict[str, Any],
        review_decision: dict[str, Any] | None,
    ) -> dict[str, str]:
        provisional = str(decision.get("decision") or "unknown")
        decision_with_record = decision | {"record": record}
        text = self._candidate_pool_text(decision_with_record)
        has_abstract = bool(record.get("abstract") or record.get("abstract_original"))

        artifact = re.search(
            r"\b(systematic review|meta[- ]analysis|critical review|bibliometric|"
            r"scientometric|corrigendum|correction|editorial|commentary|policy)\b",
            text,
            re.IGNORECASE,
        )
        if artifact:
            return {
                "decision": "exclude",
                "status": "audit_calibrated",
                "reason": "calibrated_review_or_publication_artifact",
            }

        water = self._candidate_pool_signal_matches(decision_with_record, "water")
        pollutant = self._candidate_pool_signal_matches(decision_with_record, "pollutant")
        monitor = self._candidate_pool_signal_matches(decision_with_record, "monitor")
        field_positive = water and pollutant and monitor
        direct_surface_measurement = self._candidate_pool_direct_surface_measurement(text)

        excluded_matrix_without_surface = re.search(
            r"\b(groundwater|aquifer|wastewater|drinking water|sediment|soil|"
            r"fish|biota|serum|clinical)\b",
            text,
            re.IGNORECASE,
        ) and not water
        if excluded_matrix_without_surface:
            return {
                "decision": "exclude",
                "status": "audit_calibrated",
                "reason": "calibrated_ineligible_matrix_without_surface_water",
            }

        treatment_or_method = re.search(
            r"\b(removal|adsorption|degradation|artificial uv|uv irradiation|"
            r"photodissociation|method development|probe|sensor|model|modeling|"
            r"simulation|simulated|laboratory|spiked|water treatment|biosensor|"
            r"electrode|assay|microextraction|extraction|analytical method|"
            r"method validation|toxicity|ecotoxicity|exposure|bioaccumulation)\b",
            text,
            re.IGNORECASE,
        )
        sensor_or_assay_focus = re.search(
            r"\b(probe|sensor|biosensor|electrode|assay|method validation|"
            r"developed for|concentration method|detection method|sampling method)\b",
            text,
            re.IGNORECASE,
        )
        analytical_method_focus = re.search(
            r"\b(microextraction|analytical method|determination of)\b",
            text,
            re.IGNORECASE,
        )
        modeling_only = re.search(r"\b(model|modeling|simulation|simulated)\b", text, re.IGNORECASE)
        ineligible_clinical_matrix = re.search(
            r"\b(clinical|serum|human serum|patient|thyroid-stimulating hormone)\b",
            text,
            re.IGNORECASE,
        )
        wastewater_without_surface_context = re.search(
            r"\bwastewater\b", text, re.IGNORECASE
        ) and not re.search(
            r"\b(surface water|river|stream|lake|reservoir|estuary|wetland|"
            r"freshwater|pond|watershed|catchment|basin|creek|coastal water)\b",
            text,
            re.IGNORECASE,
        )
        hard_method_or_ineligible = bool(
            sensor_or_assay_focus
            or ineligible_clinical_matrix
            or wastewater_without_surface_context
            or excluded_matrix_without_surface
        )
        method_guard = bool(
            sensor_or_assay_focus
            or (
                analytical_method_focus
                and not (direct_surface_measurement or field_positive)
            )
            or (treatment_or_method and not direct_surface_measurement)
        )
        if review_decision:
            reviewed = str(review_decision.get("decision") or "unknown")
            if reviewed == "include" and method_guard:
                return {
                    "decision": (
                        "exclude"
                        if hard_method_or_ineligible
                        else "defer_metadata"
                        if water and pollutant
                        else "exclude"
                    ),
                    "status": "audit_calibrated",
                    "reason": "calibrated_review_include_method_or_toxicity_guard",
                }
            return {
                "decision": reviewed,
                "status": "reviewed",
                "reason": str(review_decision.get("review_outcome") or "review_override"),
            }

        if provisional == "include":
            if method_guard:
                return {
                    "decision": (
                        "exclude"
                        if hard_method_or_ineligible
                        else "defer_metadata"
                        if water and pollutant
                        else "exclude"
                    ),
                    "status": "audit_calibrated",
                    "reason": "calibrated_provisional_include_method_or_toxicity_guard",
                }
            return {
                "decision": provisional,
                "status": "provisional",
                "reason": "provisional_kept",
            }

        if modeling_only:
            return {
                "decision": "defer_metadata",
                "status": "audit_calibrated",
                "reason": "calibrated_method_treatment_or_model_ambiguous",
            }
        if treatment_or_method and not direct_surface_measurement:
            if hard_method_or_ineligible:
                return {
                    "decision": "exclude",
                    "status": "audit_calibrated",
                    "reason": "calibrated_method_treatment_or_model_ambiguous",
                }
            return {
                "decision": "defer_metadata" if water and pollutant else "exclude",
                "status": "audit_calibrated",
                "reason": "calibrated_method_treatment_or_model_ambiguous",
            }

        if provisional in {"exclude", "defer_metadata"} and field_positive:
            if treatment_or_method and not direct_surface_measurement:
                decision_label = "defer_metadata"
            else:
                decision_label = (
                    "include"
                    if direct_surface_measurement or not has_abstract
                    else "defer_metadata"
                )
            return {
                "decision": decision_label,
                "status": "audit_calibrated",
                "reason": "calibrated_high_recall_surface_water_signal",
            }

        return {
            "decision": provisional,
            "status": "provisional",
            "reason": "provisional_kept",
        }

    @staticmethod
    def _candidate_pool_direct_surface_measurement(text: str) -> bool:
        monitor = (
            r"occurrence|abundance|concentration|distribution|quantification|"
            r"quantifying|detected|detecting|measured|target quantification|trace level|"
            r"screening|pollution|contamination"
        )
        water = (
            r"surface water|river|lake|estuary|freshwater|wetland|reservoir|"
            r"coastal water"
        )
        return bool(
            re.search(rf"\b({monitor})\b.{{0,80}}\b({water})\b", text, re.IGNORECASE)
            or re.search(
                rf"\b({water})\b.{{0,80}}\b({monitor})\b", text, re.IGNORECASE
            )
            or re.search(
                rf"\b(applied to|validated on|field samples?|environmental samples?)\b"
                rf".{{0,120}}\b({water})\b.{{0,160}}\b({monitor})\b",
                text,
                re.IGNORECASE,
            )
            or re.search(
                rf"\b({water})\b.{{0,80}}\bsamples?\b.{{0,160}}\b({monitor})\b",
                text,
                re.IGNORECASE,
            )
            or re.search(
                r"\b(microplastic|nanoplastic|pfas|perfluoro|polyfluoro|"
                r"pharmaceutical|antibiotic|pesticide|herbicide|insecticide|"
                r"hormone|endocrine)\b.{0,80}\b(surface water|river|lake|"
                r"estuary|freshwater|wetland|reservoir|coastal water)\b",
                text,
                re.IGNORECASE,
            )
            or re.search(
                r"\b(surface water|river|lake|estuary|freshwater|wetland|"
                r"reservoir|coastal water)\b.{0,80}\b(microplastic|"
                r"nanoplastic|pfas|perfluoro|polyfluoro|pharmaceutical|"
                r"antibiotic|pesticide|herbicide|insecticide|hormone|"
                r"endocrine)\b",
                text,
                re.IGNORECASE,
            )
        )

    def _candidate_pool_export_risk_flags(
        self, *, decision: dict[str, Any], record: dict[str, Any]
    ) -> list[str]:
        decision_with_record = decision | {"record": record}
        flags: list[str] = []
        if self._candidate_pool_signal_matches(decision_with_record, "noise"):
            flags.extend(
                f"noise:{term}"
                for term in self._candidate_pool_signal_terms(decision_with_record, "noise")[:5]
            )
        if not self._candidate_pool_signal_matches(decision_with_record, "water"):
            flags.append("missing_surface_water_signal")
        if not self._candidate_pool_signal_matches(decision_with_record, "pollutant"):
            flags.append("missing_pollutant_signal")
        if not self._candidate_pool_signal_matches(decision_with_record, "monitor"):
            flags.append("missing_monitoring_signal")
        if not (record.get("abstract") or record.get("abstract_original")):
            flags.append("missing_abstract")
        return flags

    def _write_candidate_pool_review_export_markdown(
        self, export_dir: Path, manifest: dict[str, Any]
    ) -> None:
        counts = cast(dict[str, Any], manifest["counts"])
        lines = [
            "# Candidate Pool Review Export Manifest",
            "",
            f"Pool ID: `{manifest['pool_id']}`",
            "",
            "## Counts",
            "",
        ]
        lines.extend(f"- {key}: {value}" for key, value in counts.items())
        lines.extend(["", "## Files", ""])
        files = cast(dict[str, str], manifest["files"])
        lines.extend(f"- {name}: `{path}`" for name, path in files.items())
        lines.extend(["", "## Note", "", str(manifest["note"])])
        write_text_atomic(
            export_dir / "AUDIT_REVIEW_EXPORT_MANIFEST.md",
            "\n".join(lines) + "\n",
        )

    def _candidate_pool_audit_row(
        self, decision: dict[str, Any], audit_category: str
    ) -> dict[str, Any]:
        record = cast(dict[str, Any], decision.get("record") or {})
        return {
            "audit_category": audit_category,
            "global_record_id": decision.get("global_record_id"),
            "decision": decision.get("decision"),
            "confidence": decision.get("confidence"),
            "reason_codes": decision.get("reason_codes", []),
            "title": record.get("title") or record.get("title_original"),
            "publication_year": record.get("publication_year") or record.get("year"),
            "document_type": record.get("document_type"),
            "language": record.get("language"),
            "providers": self._candidate_pool_group_values(decision, "source_providers"),
            "query_families": self._candidate_pool_group_values(decision, "query_families"),
            "has_abstract": bool(record.get("abstract") or record.get("abstract_original")),
            "signals": {
                "pollutant": self._candidate_pool_signal_terms(decision, "pollutant")[:10],
                "water": self._candidate_pool_signal_terms(decision, "water")[:10],
                "monitor": self._candidate_pool_signal_terms(decision, "monitor")[:10],
                "noise": self._candidate_pool_signal_terms(decision, "noise")[:10],
            },
            "evidence_spans": decision.get("evidence_spans", [])[:3],
        }

    def _candidate_pool_noise_loss_audit(
        self, decisions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        counts: dict[str, dict[str, int]] = {}
        for decision in decisions:
            label = str(decision.get("decision") or "unknown")
            for term in self._candidate_pool_signal_terms(decision, "noise"):
                row = counts.setdefault(
                    term,
                    {
                        "total_occurrences": 0,
                        "include_occurrences": 0,
                        "exclude_occurrences": 0,
                        "defer_occurrences": 0,
                    },
                )
                row["total_occurrences"] += 1
                if label == "include":
                    row["include_occurrences"] += 1
                elif label == "exclude":
                    row["exclude_occurrences"] += 1
                elif label == "defer_metadata":
                    row["defer_occurrences"] += 1
        rows: list[dict[str, Any]] = []
        for term, count_row in counts.items():
            include_count = count_row["include_occurrences"]
            exclude_count = count_row["exclude_occurrences"]
            defer_count = count_row["defer_occurrences"]
            rows.append(
                {
                    "term": term,
                    **count_row,
                    "loss_risk": (
                        "high"
                        if include_count
                        else "defer_risk"
                        if defer_count
                        else "lower"
                    ),
                    "exclusion_enrichment": round(exclude_count / max(1, include_count), 3),
                }
            )
        return sorted(
            rows,
            key=lambda row: (-int(row["total_occurrences"]), str(row["term"])),
        )[:30]

    def _candidate_pool_audit_priority(self, decision: dict[str, Any]) -> tuple[int, int, int]:
        return (
            len(self._candidate_pool_signal_terms(decision, "pollutant"))
            + len(self._candidate_pool_signal_terms(decision, "water"))
            + len(self._candidate_pool_signal_terms(decision, "monitor")),
            1 if self._candidate_pool_signal_matches(decision, "monitor") else 0,
            1 if cast(dict[str, Any], decision.get("record") or {}).get("abstract") else 0,
        )

    def _candidate_pool_text(self, decision: dict[str, Any]) -> str:
        record = cast(dict[str, Any], decision.get("record") or {})
        return " ".join(
            str(record.get(field) or "")
            for field in (
                "title",
                "title_original",
                "abstract",
                "abstract_original",
                "journal",
                "journal_title",
            )
        ).lower()

    def _candidate_pool_signal_matches(self, decision: dict[str, Any], signal: str) -> bool:
        return bool(self._candidate_pool_signal_terms(decision, signal))

    def _candidate_pool_signal_terms(self, decision: dict[str, Any], signal: str) -> list[str]:
        patterns = self._candidate_pool_audit_patterns()
        text = self._candidate_pool_text(decision)
        return sorted({match.group(0).lower() for match in patterns[signal].finditer(text)})

    @staticmethod
    def _candidate_pool_audit_patterns() -> dict[str, re.Pattern[str]]:
        return {
            "pollutant": re.compile(
                r"\b("
                r"emerging contaminant|contaminants of emerging concern|microplastic|"
                r"nanoplastic|pfas|perfluoro|polyfluoro|pfoa|pfos|pharmaceutical|"
                r"antibiotic|pesticide|herbicide|insecticide|fungicide|hormone|"
                r"endocrine|estrogen|bisphenol|paraben|micropollutant|"
                r"suspect screening|non[- ]target"
                r")\b",
                re.IGNORECASE,
            ),
            "water": re.compile(
                r"\b("
                r"surface water|river|stream|lake|reservoir|estuary|wetland|"
                r"freshwater|pond|watershed|catchment|basin|creek|coastal water"
                r")\b",
                re.IGNORECASE,
            ),
            "monitor": re.compile(
                r"\b("
                r"concentration|occurrence|monitoring|detected|quantified|measured|"
                r"sampling|abundance|distribution|pollution|contamination|load|level|ng/l|ug/l|mg/l|"
                r"particles/l|items/l"
                r")\b",
                re.IGNORECASE,
            ),
            "noise": re.compile(
                r"\b("
                r"model|fish|toxicity|treatment|cell|review|degradation|removal|"
                r"sensor|adsorption|policy|sediment|wastewater|groundwater|biota|"
                r"correction|simulated|drinking water|laboratory|spiked|clinical|soil|"
                r"wildlife|bobcat|otter|resistome|antibiotic resistance|virulence|"
                r"salmonella|aeromonas|microbial|metagenomic|risk assessment|"
                r"ecological risk|macroinvertebrate|programme|program|reduce"
                r")\b",
                re.IGNORECASE,
            ),
        }

    def _candidate_pool_group_values(
        self, decision: dict[str, Any], group_field: str
    ) -> list[Any]:
        record = cast(dict[str, Any], decision.get("record") or {})
        values = record.get(group_field) or decision.get(group_field) or []
        return values if isinstance(values, list) else [values]

    def _candidate_pool_lower_list(self, decision: dict[str, Any], group_field: str) -> list[str]:
        return [
            str(value).lower()
            for value in self._candidate_pool_group_values(decision, group_field)
        ]

    def _write_candidate_pool_targeted_audit_markdown(
        self, audit_root: Path, summary: dict[str, Any]
    ) -> None:
        lines = [
            "# Candidate Pool Targeted Audit Sample Summary",
            "",
            f"Pool ID: `{summary['pool_id']}`",
            "",
            "## Sample Counts",
            "",
        ]
        lines.extend(
            f"- {name}: {count}"
            for name, count in summary["sample_counts"].items()
        )
        lines.extend(
            [
                "",
                "## Noise-Term Loss Audit",
                "",
                (
                    "| term | total | include occurrences | exclude occurrences | "
                    "defer occurrences | loss risk | exclude/include enrichment |"
                ),
                "|---|---:|---:|---:|---:|---|---:|",
            ]
        )
        for row in summary["noise_term_loss_audit"][:20]:
            lines.append(
                f"| `{row['term']}` | {row['total_occurrences']} | "
                f"{row['include_occurrences']} | {row['exclude_occurrences']} | "
                f"{row['defer_occurrences']} | {row['loss_risk']} | "
                f"{row['exclusion_enrichment']} |"
            )
        lines.extend(["", "## Interpretation", ""])
        lines.extend(f"- {item}" for item in summary["interpretation"])
        write_text_atomic(audit_root / "TARGETED_AUDIT_SUMMARY.md", "\n".join(lines) + "\n")

    def _candidate_pool_examples(
        self,
        records_by_id: dict[str, dict[str, Any]],
        decisions: list[dict[str, Any]],
        limit_per_decision: int = 8,
    ) -> dict[str, list[dict[str, Any]]]:
        examples: dict[str, list[dict[str, Any]]] = {}
        for decision in decisions:
            label = str(decision.get("decision") or "unknown")
            bucket = examples.setdefault(label, [])
            if len(bucket) >= limit_per_decision:
                continue
            record_id = str(decision.get("global_record_id") or "")
            record = records_by_id.get(record_id, {})
            bucket.append(
                {
                    "global_record_id": record_id,
                    "title": record.get("title"),
                    "query_families": decision.get("query_families", []),
                    "source_providers": decision.get("source_providers", []),
                    "reason_codes": decision.get("reason_codes", []),
                    "evidence_spans": decision.get("evidence_spans", [])[:2],
                    "document_type": record.get("document_type"),
                    "language": record.get("language"),
                }
            )
        return dict(sorted(examples.items()))

    def _candidate_pool_interpretation(
        self,
        *,
        screened_total: int,
        family_metrics: dict[str, dict[str, Any]],
        term_analysis: dict[str, Any],
    ) -> list[str]:
        notes = [
            (
                "The candidate pool should be treated as the retrieval product; "
                "screening removes expected noise before final evidence export."
            )
        ]
        if screened_total < 50:
            notes.append(
                "Screening evidence is still thin; use branch metrics as directional signals."
            )
        strong = [
            family
            for family, row in family_metrics.items()
            if row.get("recommended_role") in {"strong_recall_branch", "unique_recall_branch"}
        ]
        unique = [
            family
            for family, row in family_metrics.items()
            if int(row.get("unique_include_count") or 0) > 0
        ]
        noisy = [
            family
            for family, row in family_metrics.items()
            if row.get("recommended_role") == "high_noise_or_low_yield_branch"
        ]
        if strong:
            notes.append("Strong recall branches so far: " + ", ".join(strong[:6]) + ".")
        if unique:
            notes.append(
                "Branches with screened unique included records should be preserved in the "
                "high-recall family set: "
                + ", ".join(unique[:6])
                + "."
            )
        if noisy:
            notes.append(
                "High-noise or low-yield branches need refinement before expansion: "
                + ", ".join(noisy[:6])
                + "."
            )
        noise_terms = term_analysis.get("noise_terms_by_block", {})
        if isinstance(noise_terms, dict) and noise_terms:
            notes.append(
                "Use noise-term evidence to diagnose branches, but avoid broad automatic NOT "
                "unless loss audits prove no included records are removed."
            )
        return notes

    @staticmethod
    def _candidate_pool_recommended_next_action(
        *, screened_total: int, unscreened_total: int
    ) -> str:
        if screened_total < 50 and unscreened_total > 0:
            return "screen_more_candidate_pool_records_to_reach_at_least_50_decisions"
        if screened_total < 100 and unscreened_total > 0:
            return "screen_more_candidate_pool_records_to_reach_at_least_100_decisions"
        return "refine_noisy_query_families_and_expand_missing_high_recall_branches"

    def _write_candidate_pool_analysis_markdown(
        self, pool_root: Path, analysis: dict[str, Any]
    ) -> None:
        screening = analysis["screening"]
        candidate_pool = analysis["candidate_pool"]
        lines = [
            "# Candidate Pool Analysis",
            "",
            f"- pool_id: `{analysis['pool_id']}`",
            f"- deduplicated candidates: {candidate_pool['deduplicated_candidate_count']}",
            f"- missing abstracts after enrichment: {candidate_pool['missing_abstracts']}",
            f"- missing abstracts before enrichment: "
            f"{candidate_pool['missing_abstracts_before_enrichment']}",
            f"- abstracts recovered by enrichment: "
            f"{candidate_pool['abstracts_recovered_by_enrichment']}",
            f"- screened total: {screening['screened_total']}",
            f"- unscreened total: {screening['unscreened_total']}",
            f"- include rate among screened: {screening['include_rate']:.3f}",
            f"- exclude rate among screened: {screening['exclude_rate']:.3f}",
            f"- defer rate among screened: {screening['defer_rate']:.3f}",
            f"- recommended next action: `{analysis['recommended_next_action']}`",
            f"- acceptance status: `{analysis['completion_assessment']['status']}`",
            "",
            "## Acceptance Gates",
            "",
        ]
        lines.extend(
            f"- {name}: {'pass' if passed else 'blocked'}"
            for name, passed in analysis["completion_assessment"]["gates"].items()
        )
        lines.extend([
            "",
            "## Decision Counts",
            "",
        ])
        lines.extend(
            f"- {decision}: {count}"
            for decision, count in screening["decision_counts"].items()
        )
        lines.extend(["", "## Query Family Metrics", ""])
        lines.append(
            "| query family | candidates | unique candidates | screened | include | "
            "unique include | exclude | defer | include rate | role |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for family, row in analysis["query_family_metrics"].items():
            counts = row["decision_counts"]
            lines.append(
                f"| `{family}` | {row['candidate_count']} | "
                f"{row['unique_candidate_count']} | {row['screened_count']} | "
                f"{counts.get('include', 0)} | {row['unique_include_count']} | "
                f"{counts.get('exclude', 0)} | "
                f"{counts.get('defer_metadata', 0)} | "
                f"{row['include_rate_screened']:.3f} | {row['recommended_role']} |"
            )
        lines.extend(["", "## Provider Metrics", ""])
        lines.append(
            "| provider | candidates | unique candidates | screened | include | unique include | "
            "exclude | defer | include rate |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for provider, row in analysis["provider_metrics"].items():
            counts = row["decision_counts"]
            lines.append(
                f"| `{provider}` | {row['candidate_count']} | "
                f"{row['unique_candidate_count']} | {row['screened_count']} | "
                f"{counts.get('include', 0)} | {row['unique_include_count']} | "
                f"{counts.get('exclude', 0)} | "
                f"{counts.get('defer_metadata', 0)} | "
                f"{row['include_rate_screened']:.3f} |"
            )
        lines.extend(["", "## Interpretation", ""])
        lines.extend(f"- {note}" for note in analysis["interpretation"])
        lines.extend(["", "## Guardrails", ""])
        lines.extend(f"- {note}" for note in analysis["guardrails"])
        write_text_atomic(
            pool_root / "CANDIDATE_POOL_ANALYSIS.md",
            "\n".join(lines) + "\n",
        )

    def _write_query_family_construction_plan_markdown(
        self, path: Path, plan: dict[str, Any]
    ) -> None:
        source = plan["source_evidence"]
        lines = [
            "# Next Query-Family Construction Plan",
            "",
            f"Pool ID: `{plan['pool_id']}`",
            "",
            "## Objective",
            "",
            str(plan["objective"]),
            "",
            "## Evidence Base",
            "",
            f"- screened total: {source.get('screened_total')}",
            f"- decision counts: `{source.get('decision_counts')}`",
            f"- deduplicated candidate count: {source.get('deduplicated_candidate_count')}",
            f"- analysis: `{source.get('analysis_ref')}`",
            "",
            "## Preserve Productive Families",
            "",
            "| family | screened | unique include | include | exclude | action | reason |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
        for item in plan["preserve_productive_families"]:
            lines.append(
                f"| `{item['query_family']}` | {item.get('screened_count', 0)} | "
                f"{item['unique_include_count']} | {item['include_count']} | "
                f"{item.get('exclude_count', 0)} | {item['action']} | {item['reason']} |"
            )
        lines.extend(
            [
                "",
                "## Refine Weak Or Noisy Families",
                "",
                (
                    "| family | screened | unique include | include | exclude | "
                    "defer | action | reason | focus |"
                ),
                "|---|---:|---:|---:|---:|---:|---|---|---|",
            ]
        )
        for item in plan["refine_weak_or_noisy_families"]:
            focus = "; ".join(item.get("candidate_refinement_focus") or [])
            lines.append(
                f"| `{item['query_family']}` | {item.get('screened_count', 0)} | "
                f"{item['unique_include_count']} | {item['include_count']} | "
                f"{item.get('exclude_count', 0)} | {item['defer_count']} | "
                f"{item['action']} | {item['reason']} | {focus} |"
            )
        lines.extend(
            [
                "",
                "## Expand Missing Or Undercovered Families",
                "",
                "| config | action | reason |",
                "|---|---|---|",
            ]
        )
        for item in plan["expand_missing_or_undercovered_families"]:
            lines.append(
                f"| `{item['config_name']}` | {item['action']} | {item['reason']} |"
            )
        lines.extend(["", "## Guardrails", ""])
        lines.extend(f"- {note}" for note in plan["guardrails"])
        lines.extend(
            [
                "",
                "## Held For Refinement Before Rerun",
                "",
                "| config | family | reason |",
                "|---|---|---|",
            ]
        )
        for item in plan.get("held_for_refinement_family_config_dirs", []):
            lines.append(
                f"| `{item['config_name']}` | `{item['query_family']}` | "
                f"{item['reason']} |"
            )
        lines.extend(
            [
                "",
                "## Recommended Next Run",
                "",
                f"- next pool ID: `{plan['recommended_next_pool_id']}`",
                f"- family configs: {len(plan['recommended_next_family_config_dirs'])}",
                "",
                "```bash",
                str(plan["recommended_command_template"]),
                "```",
                "",
                "## Next Steps",
                "",
            ]
        )
        lines.extend(f"- {step}" for step in plan["next_steps"])
        write_text_atomic(path, "\n".join(lines) + "\n")

    def _write_candidate_pool_post_review_plan_markdown(
        self, path: Path, plan: dict[str, Any]
    ) -> None:
        evidence = plan["evidence"]
        screening = evidence.get("screening") or {}
        candidate_pool = evidence.get("candidate_pool") or {}
        review = evidence.get("audit_review") or {}
        priority = evidence.get("review_priority") or {}
        lines = [
            "# Post-Review Candidate Pool Action Plan",
            "",
            f"Pool ID: `{plan['pool_id']}`",
            "",
            f"- recommended next action: `{plan['recommended_next_action']}`",
            f"- deduplicated candidates: {candidate_pool.get('deduplicated_candidate_count')}",
            f"- missing abstracts: {candidate_pool.get('missing_abstracts')}",
            f"- screened total: {screening.get('screened_total')}",
            f"- include: {(screening.get('decision_counts') or {}).get('include', 0)}",
            f"- exclude: {(screening.get('decision_counts') or {}).get('exclude', 0)}",
            "- defer_metadata: "
            f"{(screening.get('decision_counts') or {}).get('defer_metadata', 0)}",
            f"- audit reviewed: {review.get('reviewed_total', 0)}",
            f"- remaining audit review queue: {review.get('remaining_review_queue', 0)}",
            f"- priority queue total: {priority.get('priority_queue_total')}",
            "",
            "## Family Actions",
            "",
            "| family | action | include | unique include | defer | reason |",
            "|---|---|---:|---:|---:|---|",
        ]
        for row in plan["family_actions"]:
            lines.append(
                f"| `{row['query_family']}` | {row['action']} | "
                f"{row['include_count']} | {row['unique_include_count']} | "
                f"{row['defer_count']} | {row['reason']} |"
            )
        lines.extend(["", "## Metadata Blockers", ""])
        for blocker in plan["metadata_blockers"]:
            lines.append(
                f"- {blocker['blocker']}: {blocker['count']} "
                f"({blocker['action']})"
            )
        lines.extend(["", "## Top Review Risk Flags", ""])
        for flag, count in (priority.get("top_risk_flags") or {}).items():
            lines.append(f"- {flag}: {count}")
        lines.extend(["", "## Guardrails", ""])
        lines.extend(f"- {note}" for note in plan["guardrails"])
        write_text_atomic(path, "\n".join(lines) + "\n")



    def _read_jsonl_records(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if line.strip():
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        records.append(payload)
        return records

    @staticmethod
    def _latest_candidate_pool_decisions(
        decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        latest_by_record: dict[str, dict[str, Any]] = {}
        without_record_id: list[dict[str, Any]] = []
        for decision in decisions:
            record_id = str(decision.get("global_record_id") or "").strip()
            if not record_id:
                without_record_id.append(decision)
                continue
            latest_by_record[record_id] = decision
        return [*latest_by_record.values(), *without_record_id]

    def _normalize_candidate_pool_key(self, value: str) -> str:
        key = value.strip().lower()
        if key.startswith("doi:"):
            return "doi:" + key[4:].strip()
        if key.startswith("http://dx.doi.org/"):
            return "doi:" + key.removeprefix("http://dx.doi.org/").strip()
        if key.startswith("https://doi.org/"):
            return "doi:" + key.removeprefix("https://doi.org/").strip()
        if "/" in key and not key.startswith(("pmid:", "openalex:", "title:")):
            return "doi:" + key
        return key

    def _normalize_title_for_pool(self, title: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())

    def _topical_fit_profile(self, decision_payloads: list[str]) -> dict[str, Any]:
        decisions = []
        for payload_text in decision_payloads:
            with suppress(json.JSONDecodeError):
                payload = json.loads(payload_text)
                if isinstance(payload, dict):
                    decisions.append(payload)
        decision_counts: dict[str, int] = {}
        reason_counts: dict[str, dict[str, int]] = {}
        term_counts: dict[str, dict[str, dict[str, int]]] = {}
        examples: dict[str, list[dict[str, str]]] = {}
        for payload in decisions:
            decision = str(payload.get("decision") or "unknown")
            decision_counts[decision] = decision_counts.get(decision, 0) + 1
            examples.setdefault(decision, [])
            if len(examples[decision]) < 8:
                examples[decision].append(
                    {
                        "global_record_id": str(payload.get("global_record_id") or ""),
                        "decision": decision,
                        "reason_codes": ", ".join(
                            str(reason) for reason in payload.get("reason_codes", [])
                        ),
                    }
                )
            reason_bucket = reason_counts.setdefault(decision, {})
            for reason in payload.get("reason_codes", []):
                reason_text = str(reason)
                reason_bucket[reason_text] = reason_bucket.get(reason_text, 0) + 1
            for evidence in payload.get("query_term_evidence", []):
                if not isinstance(evidence, dict):
                    continue
                term = self._normalize_candidate_term(str(evidence.get("term") or ""))
                if not term:
                    continue
                block = str(evidence.get("concept_block") or "unclassified")
                block_counts = term_counts.setdefault(decision, {}).setdefault(block, {})
                block_counts[term] = block_counts.get(term, 0) + 1

        return {
            "profile_version": "topical-fit-v1",
            "objective": (
                "Improve retrieval topical fit for natural-water emerging-contaminant "
                "occurrence, monitoring, and concentration studies."
            ),
            "decision_counts": decision_counts,
            "dominant_false_positive_reasons": self._top_counts(
                reason_counts.get("exclude", {})
            ),
            "true_positive_signals": {
                block: self._top_counts(counts)
                for block, counts in term_counts.get("include", {}).items()
            },
            "false_positive_signals": {
                block: self._top_counts(counts)
                for block, counts in term_counts.get("exclude", {}).items()
            },
            "defer_signals": {
                block: self._top_counts(counts)
                for block, counts in term_counts.get("defer_metadata", {}).items()
            },
            "decision_examples": examples,
            "query_refinement_guidance": [
                "Prefer candidate patches that reduce dominant false-positive reasons.",
                (
                    "Do not add broad pollutant-class terms unless supported by "
                    "multiple independent includes."
                ),
                (
                    "Prefer context phrases shared by true positives, such as "
                    "occurrence, measured concentration, surface water, river, "
                    "receiving waters, or target quantification."
                ),
                (
                    "Reject or avoid candidates likely to lose known includes during "
                    "pairwise loss audit."
                ),
                "Skip candidates that do not change executable provider queries or result sets.",
            ],
        }

    @staticmethod
    def _top_counts(counts: dict[str, int], limit: int = 20) -> list[dict[str, Any]]:
        return [
            {"value": value, "count": count}
            for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[
                :limit
            ]
        ]

    def _write_term_candidate_exports(self, run_dir: Path, run_id: str) -> None:
        export_dir = ensure_dir(run_dir / "exports")
        fieldnames = [
            "run_id",
            "query_id",
            "iteration",
            "term",
            "concept_block",
            "action",
            "previous_status",
            "new_status",
            "reason",
            "supporting_positive_documents",
            "supporting_negative_documents",
            "discriminative_score",
            "created_at",
        ]
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT run_id, query_id, iteration, term, concept_block, action,
                           previous_status, new_status, reason,
                           supporting_positive_documents,
                           supporting_negative_documents, discriminative_score,
                           created_at
                    FROM term_ledger
                    WHERE run_id = ?
                    ORDER BY iteration, action, term
                    """,
                    (run_id,),
                )
            ]
        positive_rows = [
            row for row in rows if str(row.get("action") or "") == "positive_candidate"
        ]
        negative_rows = [
            row for row in rows if str(row.get("action") or "") == "negative_noise_candidate"
        ]
        write_csv_atomic(export_dir / "positive_term_candidates.csv", positive_rows, fieldnames)
        write_csv_atomic(export_dir / "negative_noise_candidates.csv", negative_rows, fieldnames)

    def _rebuild_global_handoff_logs(self) -> None:
        if not self._path_is_relative_to(self.runs_dir, self.repo_root):
            return
        root = ensure_dir(self.repo_root / "handoff" / "download")
        outbox_dir = ensure_dir(root / "outbox")
        ack_dir = ensure_dir(root / "acknowledgements")
        result_dir = ensure_dir(root / "results")
        dead_dir = ensure_dir(root / "dead_letter")
        snapshot_dir = ensure_dir(root / "snapshots")
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            events = [
                str(row["payload_json"])
                for row in connection.execute(
                    "SELECT payload_json FROM download_outbox ORDER BY created_at, event_id"
                )
            ]
            jobs = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT idempotency_key, global_record_id, job_state, claimed_by,
                           claimed_at, lease_expires_at, attempt_count, completed_at,
                           result_reference, failure_reason
                    FROM download_jobs
                    ORDER BY idempotency_key
                    """
                )
            ]
        write_text_atomic(
            outbox_dir / "download_events.jsonl",
            "\n".join(events) + ("\n" if events else ""),
        )
        for path in [
            ack_dir / "download_acknowledgements.jsonl",
            result_dir / "download_results.jsonl",
            dead_dir / "dead_letter_events.jsonl",
        ]:
            if not path.exists():
                write_text_atomic(path, "")
        write_csv_atomic(
            snapshot_dir / "download_queue_snapshot.csv",
            jobs,
            [
                "idempotency_key",
                "global_record_id",
                "job_state",
                "claimed_by",
                "claimed_at",
                "lease_expires_at",
                "attempt_count",
                "completed_at",
                "result_reference",
                "failure_reason",
            ],
        )

    def _mirror_download_events(
        self, run_dir: Path, events: list[dict[str, Any]]
    ) -> None:
        for event in events:
            append_jsonl(run_dir / "handoff" / "download" / "download_events.jsonl", event)
        self._rebuild_global_handoff_logs()

    def _write_resource_trajectory(self, export_dir: Path, run_id: str) -> None:
        run_dir = self.runs_dir / run_id
        rows: list[dict[str, Any]] = []
        memory_log = run_dir / "logs" / "memory_usage.jsonl"
        if memory_log.exists():
            with memory_log.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        rows.append(json.loads(line))
        write_csv_atomic(
            export_dir / "runtime_resource_trajectory.csv",
            rows,
            [
                "run_id",
                "query_id",
                "iteration",
                "operator",
                "batch_id",
                "records_processed",
                "process_rss_before_mb",
                "process_rss_peak_mb",
                "process_rss_after_cleanup_mb",
                "python_traced_memory_mb",
                "resident_memory_before_mb",
                "resident_memory_peak_mb",
                "resident_memory_after_cleanup_mb",
                "bytes_written",
                "duration_seconds",
                "cleanup_performed",
                "timestamp",
            ],
        )

    def _prepare_run_dir(self, run_id: str) -> Path:
        run_dir = ensure_dir(self.runs_dir / run_id)
        for child in [
            "queries",
            "logs",
            "raw_metadata/batches",
            "normalized/batches",
            "deduplication",
            "screening/batches",
            "metrics",
            "exports",
            "errors",
            "checksums",
            "handoff/download",
            "registry",
            "state",
        ]:
            ensure_dir(run_dir / child)
        for jsonl in [
            run_dir / "logs" / "memory_usage.jsonl",
            run_dir / "logs" / "decision_log.jsonl",
            run_dir / "logs" / "state_transition_log.jsonl",
            run_dir / "logs" / "error_log.jsonl",
            run_dir / "handoff" / "download" / "download_events.jsonl",
            run_dir / "handoff" / "download" / "handoff_status.jsonl",
        ]:
            if not jsonl.exists():
                write_text_atomic(jsonl, "")
        return run_dir

    def _write_snapshots(self, run_dir: Path, loaded: LoadedConfig) -> None:
        write_yaml_atomic(run_dir / "protocol_snapshot.yaml", loaded.protocol)
        write_yaml_atomic(run_dir / "scoring_snapshot.yaml", loaded.scoring)
        write_yaml_atomic(run_dir / "model_snapshot.yaml", loaded.model)
        write_yaml_atomic(run_dir / "source_snapshot.yaml", loaded.sources)
        write_yaml_atomic(run_dir / "runtime_snapshot.yaml", loaded.runtime)

    def _manifest(
        self,
        run_id: str,
        date_to: str,
        loaded: LoadedConfig,
        runtime: dict[str, Any],
        date_from: str | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "start_time": utc_now_iso(),
            "end_time": None,
            "date_from": date_from or str(loaded.protocol["date_range"]["date_from"]),
            "date_to": date_to,
            "code_commit_sha": self._git_sha(),
            "git_branch": self._git_branch(),
            "dirty_worktree_status": self._git_dirty_status(),
            "python_version": sys.version,
            "platform": platform.platform(),
            "dependency_lock_hash": self._lock_hash(),
            "protocol_version": str(loaded.protocol.get("protocol_version", "0.1.0")),
            "protocol_hash": self._file_hash(self.config_dir / "protocol.yaml"),
            "scoring_version": str(loaded.scoring.get("scoring_version", "0.1.0")),
            "scoring_hash": self._file_hash(self.config_dir / "scoring.yaml"),
            "config_hash": loaded.config_hash,
            "prompt_hashes": self._prompt_hash(),
            "model_names": [],
            "model_parameters": {"llm_enabled": False},
            "random_seed": 1226,
            "adapter_versions": {
                "mock": "1.1.0",
                "external_metadata_discovery_v1": "file_based_skill_boundary",
            },
            "schema_versions": {
                "control_plane": SCHEMA_VERSION,
                "screening": "1.1.0",
                "handoff": "1.1.0",
            },
            "SCIE_registry_version": self._scie_registry_hash(),
            "document_registry_version": "1.1.0",
            "source_health_status": dict.fromkeys(
                SOURCE_NAMES, SourceExecutionStatus.SOURCE_SUCCESS.value
            ),
            "runtime_source_health_mode": "mock_no_external_api_calls",
            "configured_max_iterations": int(runtime.get("default_mock_iterations", 5)),
            "run_completeness": "unknown",
            "run_status": "running",
        }

    def _write_manifest_update(
        self, run_dir: Path, manifest: dict[str, Any], updates: dict[str, Any]
    ) -> None:
        manifest_path = run_dir / "manifest.json"
        current = dict(read_json(manifest_path)) if manifest_path.exists() else dict(manifest)
        current.update(updates)
        manifest.clear()
        manifest.update(current)
        write_json_atomic(manifest_path, current)

    def _paused_manifest_update(self, run_id: str, status: str) -> dict[str, Any]:
        self._update_run_status(run_id, status)
        source_completeness = self._latest_source_completeness(run_id)
        return {
            "run_status": status,
            "run_completeness": "paused",
            "source_completeness": source_completeness,
        }

    def _upsert_run(
        self, run_id: str, date_to: str, loaded: LoadedConfig, manifest: dict[str, Any]
    ) -> None:
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, status, completeness, date_from, date_to,
                    current_iteration, current_query_id, accepted_query_id,
                    current_state, started_at, completed_at, code_commit_sha,
                    git_branch, config_hash, prompt_hash, protocol_version,
                    scoring_version, model_version, source_status_json,
                    failure_reason
                )
                VALUES (?, 'running', 'unknown', ?, ?, 0, NULL, NULL,
                        'LOAD_PROTOCOL', ?, NULL, ?, ?, ?, ?, ?, ?, ?,
                        ?, NULL)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = 'running',
                    current_state = 'LOAD_PROTOCOL',
                    failure_reason = NULL
                """,
                (
                    run_id,
                    str(manifest["date_from"]),
                    date_to,
                    str(manifest["start_time"]),
                    str(manifest["code_commit_sha"]),
                    str(manifest["git_branch"]),
                    loaded.config_hash,
                    self._prompt_hash(),
                    str(manifest["protocol_version"]),
                    str(manifest["scoring_version"]),
                    "none",
                    json.dumps(manifest["source_health_status"], sort_keys=True),
                ),
            )

    def _complete_run(
        self, run_id: str, result: IterationResult, *, completeness: str = "complete"
    ) -> None:
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = 'completed',
                    completeness = ?,
                    current_iteration = ?,
                    current_query_id = ?,
                    current_state = 'STOP',
                    completed_at = ?
                WHERE run_id = ?
                """,
                (completeness, result.iteration, result.query_id, utc_now_iso(), run_id),
            )

    def _next_iteration(self, run_id: str) -> int:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            incomplete = connection.execute(
                """
                SELECT iteration
                FROM query_iterations
                WHERE run_id = ? AND finalized_at IS NULL
                ORDER BY iteration
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if incomplete is not None:
                return int(incomplete["iteration"])
            row = connection.execute(
                """
                SELECT COALESCE(MAX(iteration), 0) + 1 AS next_iteration
                FROM query_iterations
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            return int(row["next_iteration"])

    def _record_checkpoint_sql(
        self,
        *,
        connection: sqlite3.Connection,
        run_id: str,
        query_id: str,
        iteration: int,
        operator: str,
        batch_id: str,
        source_name: str | None,
        page_cursor: str | None,
        next_page_cursor: str | None,
        processed_count: int,
        persisted_count: int,
    ) -> None:
        connection.execute(
            """
            DELETE FROM operator_checkpoints
            WHERE run_id = ? AND query_id = ? AND iteration = ?
              AND operator = ? AND COALESCE(source_name, '') = COALESCE(?, '')
              AND batch_id = ?
            """,
            (run_id, query_id, iteration, operator, source_name, batch_id),
        )
        connection.execute(
            """
            INSERT INTO operator_checkpoints (
                run_id, query_id, iteration, operator, source_name, batch_id,
                page_cursor, next_page_cursor, input_checksum, output_checksum,
                processed_count, persisted_count, status, started_at, completed_at,
                retry_count, error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, 'completed', ?, ?, 0, NULL)
            """,
            (
                run_id,
                query_id,
                iteration,
                operator,
                source_name,
                batch_id,
                page_cursor,
                next_page_cursor,
                processed_count,
                persisted_count,
                utc_now_iso(),
                utc_now_iso(),
            ),
        )

    def _checkpoint_completed(
        self,
        run_id: str,
        query_id: str,
        iteration: int,
        operator: str,
        source_name: str | None,
        batch_id: str,
    ) -> bool:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            row = connection.execute(
                """
                SELECT status
                FROM operator_checkpoints
                WHERE run_id = ? AND query_id = ? AND iteration = ?
                  AND operator = ? AND COALESCE(source_name, '') = COALESCE(?, '')
                  AND batch_id = ?
                """,
                (run_id, query_id, iteration, operator, source_name, batch_id),
            ).fetchone()
            return row is not None and row["status"] == "completed"

    def _line_count(self, path: Path) -> int:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def _memory_pause_if_needed(
        self, run_id: str, runtime: dict[str, Any]
    ) -> dict[str, Any] | None:
        hard = runtime.get("hard_memory_limit_mb")
        if hard is not None and process_rss_mb() >= float(hard):
            self._update_run_status(run_id, "paused_memory_limit")
            return {
                "status": "paused_memory_limit",
                "process_rss_mb": round(process_rss_mb(), 3),
                "hard_memory_limit_mb": float(hard),
            }
        return None

    def _update_run_status(self, run_id: str, status: str) -> None:
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, current_state = ? WHERE run_id = ?",
                (status, status.upper(), run_id),
            )

    def _maybe_stop_after_operator(
        self,
        run_id: str,
        run_dir: Path,
        query: CanonicalQuery,
        operator: str,
        fail_after_operator: str | None,
    ) -> None:
        if fail_after_operator is None or operator != fail_after_operator:
            return
        self._failure_operator_count += 1
        self._update_run_status(run_id, "interrupted_injected")
        append_jsonl(
            run_dir / "logs" / "error_log.jsonl",
            {
                "timestamp": utc_now_iso(),
                "run_id": run_id,
                "query_id": query.query_id,
                "operator": operator,
                "error": "Injected operator interruption",
            },
        )
        raise RuntimeError(f"Injected failure after operator {operator}")

    def _interrupted_result(self, query: CanonicalQuery) -> IterationResult:
        return IterationResult(
            query_id=query.query_id,
            iteration=query.iteration,
            total_score=None,
            score_delta=None,
            decision="interrupted",
            saturation_status="interrupted_injected",
            row_counts={},
        )

    def _runtime_settings(self, loaded: LoadedConfig) -> dict[str, Any]:
        return dict(loaded.runtime.get("runtime", {}))

    def _handoff_settings(self, loaded: LoadedConfig) -> dict[str, Any]:
        return dict(loaded.runtime.get("handoff", {}))

    def _backpressure_status(self, pending_jobs: int, loaded: LoadedConfig) -> str:
        handoff = self._handoff_settings(loaded)
        hard = int(handoff.get("max_pending_jobs_hard", 2000))
        soft = int(handoff.get("max_pending_jobs_soft", 500))
        if pending_jobs >= hard:
            return "hard_limit"
        if pending_jobs >= soft:
            return "soft_limit"
        return "ok"

    def _allowed_reason_codes(self) -> dict[str, Any]:
        path = self.repo_root / "registry" / "reason_codes.yaml"
        if not path.exists():
            return {}
        loaded = read_yaml(path)
        return dict(loaded) if isinstance(loaded, dict) else {}

    def _latest_accepted_query(self, run_id: str, run_dir: Path) -> CanonicalQuery | None:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            row = connection.execute(
                """
                SELECT query_id
                FROM query_iterations
                WHERE run_id = ?
                  AND query_status = 'completed'
                  AND acceptance_status = 'accepted'
                  AND score IS NOT NULL
                ORDER BY iteration DESC
                LIMIT 1
                """
                ,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        path = run_dir / "queries" / str(row["query_id"]) / "canonical_query.yaml"
        if not path.exists():
            return None
        return CanonicalQuery(**read_yaml(path))

    def _latest_metrics(self, run_dir: Path, query_id: str) -> QueryMetrics | None:
        path = run_dir / "queries" / query_id / "metrics.json"
        if not path.exists():
            return None
        return QueryMetrics(**read_json(path))

    def _query_refinement_evidence_refs(self, run_dir: Path, run_id: str) -> dict[str, str]:
        latest_query = self._latest_accepted_query(run_id, run_dir)
        if latest_query is not None:
            self._persist_term_events(run_id, latest_query, self._term_mining_plan(latest_query))
            self._rebuild_audit_files(run_id, run_dir)
        return {
            "term_ledger": self._display_path(run_dir / "exports" / "term_evolution.csv"),
            "topical_fit_profile": self._display_path(
                run_dir / "exports" / "topical_fit_profile.json"
            ),
            "positive_term_candidates": self._display_path(
                run_dir / "exports" / "positive_term_candidates.csv"
            ),
            "negative_noise_candidates": self._display_path(
                run_dir / "exports" / "negative_noise_candidates.csv"
            ),
            "screening_decisions": self._display_path(
                run_dir / "exports" / "screening_decision_evolution.csv"
            ),
            "exclusion_reasons": self._display_path(
                run_dir / "exports" / "exclusion_reason_evolution.csv"
            ),
            "source_contribution": self._display_path(
                run_dir / "exports" / "source_contribution.csv"
            ),
        }

    def _term_mining_plan(self, query: CanonicalQuery) -> IterationPlan:
        return IterationPlan(
            query_id=query.query_id,
            parent_query_id=query.parent_query_id,
            branch_id="term-mining",
            acceptance_status="accepted",
            decision="accept",
            decision_reason="Refresh term candidates from current screening evidence.",
            added_terms=[],
            removed_terms=[],
            modified_blocks=[],
            expected_effect=query.expected_effect,
        )

    def _previous_query_changes(self, run_dir: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_rejected_patch_keys: set[tuple[str, str]] = set()
        for path in sorted((run_dir / "queries").glob("Q*/query_change.json")):
            rows.append(read_json(path))
        for path in sorted((run_dir / "query_refinement").glob("Q*_candidate_selection.json")):
            payload = dict(read_json(path))
            for evaluation in payload.get("candidate_evaluations", []):
                if not isinstance(evaluation, dict):
                    continue
                patch_id = evaluation.get("patch_id")
                query_id = evaluation.get("query_id")
                if not patch_id or not query_id:
                    continue
                parent_query_id = str(payload.get("parent_query_id") or "")
                patch_key = (parent_query_id, str(patch_id))
                if patch_key in seen_rejected_patch_keys:
                    continue
                seen_rejected_patch_keys.add(patch_key)
                rows.append(
                    {
                        "run_id": payload.get("parent_metrics", {}).get("run_id"),
                        "query_id": query_id,
                        "parent_query_id": parent_query_id,
                        "patch_id": patch_id,
                        "decision": "reject",
                        "decision_reason": (
                            "Candidate limited novelty evaluation did not meet "
                            "deterministic acceptance criteria."
                        ),
                        "score_after": evaluation.get("score"),
                        "metric_deltas": {"score_delta": evaluation.get("score_delta")},
                        "source_completeness": evaluation.get("source_completeness"),
                        "include_count": evaluation.get("include_count"),
                        "exclude_count": evaluation.get("exclude_count"),
                        "defer_count": evaluation.get("defer_count"),
                    }
                )
        return rows

    def _query_change_payload(
        self,
        run_id: str,
        query: CanonicalQuery,
        plan: IterationPlan,
        loaded: LoadedConfig | None,
        metrics: QueryMetrics | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "iteration": query.iteration,
            "query_id": query.query_id,
            "parent_query_id": query.parent_query_id,
            "added_terms": query.added_terms,
            "removed_terms": query.removed_terms,
            "replaced_terms": [],
            "modified_concept_blocks": query.modified_concept_blocks,
            "change_rationale": plan.decision_reason,
            "evidence_for_change": query.evidence_for_change,
            "triggering_metrics": {},
            "expected_effect": query.expected_effect,
            "observed_effect": {"total_score": metrics.total_score if metrics else None},
            "score_before": self._parent_score_for_payload(run_id, plan.parent_query_id),
            "score_after": metrics.total_score if metrics else None,
            "metric_deltas": {"score_delta": metrics.score_delta if metrics else None},
            "decision": plan.decision,
            "decision_reason": plan.decision_reason,
            "proposed_by": "Retrieval Specialist",
            "evaluated_by": "QueryEvaluator",
            "protocol_hash": loaded.config_hash if loaded else None,
            "timestamp": utc_now_iso(),
        }

    def _parent_score_for_payload(self, run_id: str, parent_query_id: str | None) -> float:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            return self._parent_score(connection, run_id, parent_query_id)

    def _parent_score(
        self, connection: sqlite3.Connection, run_id: str, parent_query_id: str | None
    ) -> float:
        if parent_query_id is None:
            return 0.0
        row = connection.execute(
            "SELECT score FROM query_iterations WHERE run_id = ? AND query_id = ?",
            (run_id, parent_query_id),
        ).fetchone()
        if row is None or row["score"] is None:
            return 0.0
        return float(row["score"])

    def _membership_count(
        self, connection: sqlite3.Connection, run_id: str, query_id: str, global_record_id: str
    ) -> int:
        return int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM document_query_membership
                WHERE run_id = ? AND query_id = ? AND global_record_id = ?
                """,
                (run_id, query_id, global_record_id),
            ).fetchone()[0]
        )

    def _already_known_before_query(
        self, connection: sqlite3.Connection, run_id: str, query_id: str, global_record_id: str
    ) -> bool:
        row = connection.execute(
            """
            SELECT first_seen_run_id, first_seen_query_id
            FROM documents
            WHERE global_record_id = ?
            """,
            (global_record_id,),
        ).fetchone()
        if row is None:
            return False
        return not (
            str(row["first_seen_run_id"]) == run_id
            and str(row["first_seen_query_id"]) == query_id
        )

    def _novelty_sample_count(
        self, connection: sqlite3.Connection, run_id: str, query_id: str
    ) -> int:
        return int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT global_record_id)
                FROM document_query_membership
                WHERE run_id = ? AND query_id = ? AND included_in_novelty_sample = 1
                """,
                (run_id, query_id),
            ).fetchone()[0]
        )

    def _maybe_candidate_duplicate(
        self, connection: sqlite3.Connection, record: NormalizedRecord, global_record_id: str
    ) -> None:
        if any(
            [
                record.normalized_doi,
                record.pmid,
                record.openalex_id,
                record.semantic_scholar_id,
                record.crossref_id,
            ]
        ):
            return
        rows = connection.execute(
            """
            SELECT global_record_id
            FROM documents
            WHERE normalized_title = ? AND publication_year IS ? AND first_author IS ?
              AND global_record_id != ?
            """,
            (
                record.title_normalized,
                record.publication_year,
                record.first_author,
                global_record_id,
            ),
        ).fetchall()
        for row in rows:
            record_a = str(row["global_record_id"])
            cluster_id = hashlib.sha256(f"{record_a}|{global_record_id}".encode()).hexdigest()[:16]
            connection.execute(
                """
                INSERT OR IGNORE INTO candidate_duplicate_clusters (
                    cluster_id, record_a, record_b, confidence,
                    matched_fields_json, conflicting_fields_json, merge_status,
                    reviewed_by, reviewed_at
                )
                VALUES (?, ?, ?, 0.6, ?, '[]', 'pending_review', NULL, NULL)
                """,
                (
                    f"candidate:{cluster_id}",
                    record_a,
                    global_record_id,
                    json.dumps(["normalized_title", "publication_year", "first_author"]),
                ),
            )

    def _duplicate_count(self, run_id: str, query_id: str) -> int:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            rows = connection.execute(
                """
                SELECT global_record_id, COUNT(*) AS memberships
                FROM document_query_membership
                WHERE run_id = ? AND query_id = ?
                GROUP BY global_record_id
                HAVING COUNT(*) > 1
                """,
                (run_id, query_id),
            ).fetchall()
            return sum(int(row["memberships"]) - 1 for row in rows)

    def _duplicate_count_for_run(self, run_id: str) -> int:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            rows = connection.execute(
                """
                SELECT global_record_id, COUNT(*) AS memberships
                FROM document_query_membership
                WHERE run_id = ?
                GROUP BY global_record_id
                HAVING COUNT(*) > 1
                """,
                (run_id,),
            ).fetchall()
            return sum(int(row["memberships"]) - 1 for row in rows)

    def _decision_counts(
        self, connection: sqlite3.Connection, run_id: str, query_id: str
    ) -> dict[str, int]:
        rows = connection.execute(
            """
            SELECT decision, COUNT(*) AS count
            FROM screening_decisions
            WHERE run_id = ? AND query_id = ? AND is_current = 1
            GROUP BY decision
            """,
            (run_id, query_id),
        ).fetchall()
        counts = {str(row["decision"]): int(row["count"]) for row in rows}
        return {
            "include": counts.get("include", 0),
            "exclude": counts.get("exclude", 0),
            "defer": counts.get("defer_metadata", 0) + counts.get("defer_not_downloaded", 0),
        }

    def _screening_model_summary(
        self, connection: sqlite3.Connection, run_id: str, query_id: str
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT DISTINCT model_name
            FROM screening_decisions
            WHERE run_id = ? AND query_id = ? AND is_current = 1
              AND model_name IS NOT NULL AND model_name != '' AND model_name != 'none'
            ORDER BY model_name
            """,
            (run_id, query_id),
        ).fetchall()
        model_names = [str(row["model_name"]) for row in rows]
        if not model_names:
            return {"model_name": "none", "model_parameters": {"llm_enabled": False}}
        return {
            "model_name": ",".join(model_names),
            "model_parameters": {
                "llm_enabled": True,
                "screening_execution": "durable_file_based_worker_request",
                "screening_isolation": "one_document_per_conversation",
            },
        }

    def _screened_novelty_decision_count(
        self,
        run_id: str,
        query_id: str,
        *,
        novelty_against_query_id: str | None = None,
    ) -> int:
        pairwise_gain_filter = ""
        parameters: tuple[Any, ...]
        if novelty_against_query_id:
            pairwise_gain_filter = """
                  AND NOT EXISTS (
                    SELECT 1
                    FROM document_query_membership parent
                    WHERE parent.run_id = m.run_id
                      AND parent.query_id = ?
                      AND parent.global_record_id = m.global_record_id
                  )
            """
            parameters = (run_id, query_id, novelty_against_query_id)
        else:
            parameters = (run_id, query_id)
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(DISTINCT m.global_record_id) AS count
                FROM document_query_membership m
                JOIN screening_decisions s
                  ON s.global_record_id = m.global_record_id
                 AND s.run_id = m.run_id
                 AND s.query_id = m.query_id
                 AND s.is_current = 1
                WHERE m.run_id = ?
                  AND m.query_id = ?
                  AND m.included_in_novelty_sample = 1
                  {pairwise_gain_filter}
                """,
                parameters,
            ).fetchone()
        return int(row["count"] or 0)

    def _novelty_counts(
        self, connection: sqlite3.Connection, run_id: str, query_id: str
    ) -> dict[str, int]:
        row = connection.execute(
            """
            SELECT
                COUNT(DISTINCT CASE WHEN already_known_before_query = 1 THEN global_record_id END)
                    AS known_record_count,
                COUNT(DISTINCT CASE WHEN already_known_before_query = 0 THEN global_record_id END)
                    AS novel_record_count
            FROM document_query_membership
            WHERE run_id = ? AND query_id = ?
            """,
            (run_id, query_id),
        ).fetchone()
        return {
            "known_record_count": int(row["known_record_count"] or 0),
            "novel_record_count": int(row["novel_record_count"] or 0),
        }

    def _novelty_sample_counts(
        self, connection: sqlite3.Connection, run_id: str, query_id: str
    ) -> dict[str, int]:
        row = connection.execute(
            """
            SELECT
                COUNT(DISTINCT m.global_record_id) AS actual_novel_n,
                COUNT(DISTINCT CASE WHEN s.decision IN ('include', 'exclude')
                    THEN m.global_record_id END) AS evaluated,
                COUNT(DISTINCT CASE WHEN s.decision = 'include'
                    THEN m.global_record_id END) AS include
            FROM document_query_membership m
            LEFT JOIN screening_decisions s
              ON s.global_record_id = m.global_record_id
             AND s.run_id = m.run_id
             AND s.query_id = m.query_id
             AND s.is_current = 1
            WHERE m.run_id = ? AND m.query_id = ?
              AND m.included_in_novelty_sample = 1
            """,
            (run_id, query_id),
        ).fetchone()
        return {
            "actual_novel_n": int(row["actual_novel_n"] or 0),
            "evaluated": int(row["evaluated"] or 0),
            "include": int(row["include"] or 0),
        }

    def _rate_components(
        self, connection: sqlite3.Connection, run_id: str, query_id: str
    ) -> dict[str, float | int]:
        reason_rows = connection.execute(
            """
            SELECT decision, reason_codes_json, global_record_id
            FROM screening_decisions
            WHERE run_id = ? AND query_id = ? AND is_current = 1
            """,
            (run_id, query_id),
        ).fetchall()
        total_decisions = max(1, len(reason_rows))
        excluded_matrix = 0
        lab = 0
        no_concentration = 0
        for row in reason_rows:
            reasons = set(json.loads(str(row["reason_codes_json"])))
            if reasons & {"E_MIXED_MATRIX", "E_GROUNDWATER", "E_WATER_PLANT", "E_WASTEWATER"}:
                excluded_matrix += 1
            if "E_LAB_STUDY" in reasons:
                lab += 1
            if "E_NO_CONCENTRATION" in reasons:
                no_concentration += 1
        marginal_row = connection.execute(
            """
            SELECT
                COUNT(DISTINCT CASE WHEN s.decision = 'include' THEN m.global_record_id END)
                    AS marginal_eligible_count,
                COUNT(DISTINCT CASE WHEN s.decision IN ('include', 'exclude')
                    THEN m.global_record_id END) AS fully_evaluated_novel
            FROM document_query_membership m
            JOIN screening_decisions s
              ON s.global_record_id = m.global_record_id
             AND s.run_id = m.run_id
             AND s.query_id = m.query_id
             AND s.is_current = 1
            WHERE m.run_id = ? AND m.query_id = ?
              AND m.first_seen_in_query = 1
            """,
            (run_id, query_id),
        ).fetchone()
        known_row = connection.execute(
            """
            SELECT
                COUNT(DISTINCT m.global_record_id) AS known_count,
                COUNT(DISTINCT CASE WHEN d.current_screening_status = 'include'
                    THEN m.global_record_id END) AS known_eligible,
                COUNT(DISTINCT CASE WHEN d.current_screening_status = 'exclude'
                    THEN m.global_record_id END) AS known_ineligible
            FROM document_query_membership m
            JOIN documents d ON d.global_record_id = m.global_record_id
            WHERE m.run_id = ? AND m.query_id = ?
              AND m.already_known_before_query = 1
            """,
            (run_id, query_id),
        ).fetchone()
        source_row = connection.execute(
            """
            SELECT
                COUNT(DISTINCT source_name) AS retrieval_sources,
                COUNT(DISTINCT CASE WHEN s.decision = 'include'
                    THEN m.source_name END) AS eligible_sources
            FROM document_query_membership m
            LEFT JOIN screening_decisions s
              ON s.global_record_id = m.global_record_id
             AND s.run_id = m.run_id
             AND s.query_id = m.query_id
             AND s.is_current = 1
            WHERE m.run_id = ? AND m.query_id = ?
            """,
            (run_id, query_id),
        ).fetchone()
        doc_rows = connection.execute(
            """
            SELECT d.sampled_matrices_json, d.abstract_original, s.decision
            FROM documents d
            JOIN screening_decisions s ON s.global_record_id = d.global_record_id
            WHERE s.run_id = ? AND s.query_id = ? AND s.is_current = 1
            """,
            (run_id, query_id),
        ).fetchall()
        complete = sum(1 for row in doc_rows if row["abstract_original"])
        included_matrices = {
            matrix
            for row in doc_rows
            if row["decision"] == "include"
            for matrix in json.loads(str(row["sampled_matrices_json"]))
        }
        cumulative_eligible = int(
            connection.execute(
                "SELECT COUNT(*) FROM documents WHERE current_screening_status = 'include'"
            ).fetchone()[0]
        )
        run_queries = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM query_iterations
                WHERE run_id = ? AND finalized_at IS NOT NULL
                """,
                (run_id,),
            ).fetchone()[0]
        )
        known_count = int(known_row["known_count"] or 0)
        return {
            "excluded_matrix_rate": excluded_matrix / total_decisions,
            "laboratory_study_rate": lab / total_decisions,
            "no_concentration_rate": no_concentration / total_decisions,
            "marginal_eligible_count": int(marginal_row["marginal_eligible_count"] or 0),
            "fully_evaluated_novel": int(marginal_row["fully_evaluated_novel"] or 0),
            "known_eligible_overlap_rate": int(known_row["known_eligible"] or 0)
            / max(1, known_count),
            "known_ineligible_overlap_rate": int(known_row["known_ineligible"] or 0)
            / max(1, known_count),
            "retrieval_source_breadth": clamp(
                int(source_row["retrieval_sources"] or 0) / len(SOURCE_NAMES)
            ),
            "eligible_source_breadth": clamp(
                int(source_row["eligible_sources"] or 0) / len(SOURCE_NAMES)
            ),
            "metadata_completeness": complete / max(1, len(doc_rows)),
            "scope_diversity": clamp(len(included_matrices) / 6),
            "eligible_waterbody_diversity": clamp(len(included_matrices) / 6),
            "query_complexity": clamp((run_queries + 20) / 100),
            "cumulative_eligible_count": cumulative_eligible,
            "retrospective_query_coverage": clamp(run_queries / 5),
        }

    def _source_contribution_rows(
        self, connection: sqlite3.Connection, run_id: str
    ) -> list[dict[str, Any]]:
        rows = []
        for row in connection.execute(
            """
            SELECT run_id, iteration, query_id, source_name,
                   COUNT(*) AS scanned_records,
                   COUNT(DISTINCT CASE WHEN already_known_before_query = 0
                       THEN global_record_id END) AS novel_records,
                   COUNT(DISTINCT global_record_id) AS unique_contributions
            FROM document_query_membership
            WHERE run_id = ?
            GROUP BY run_id, iteration, query_id, source_name
            ORDER BY iteration, source_name
            """,
            (run_id,),
        ):
            decision_row = connection.execute(
                """
                SELECT
                    COUNT(DISTINCT CASE WHEN s.decision = 'include'
                        THEN m.global_record_id END) AS novel_eligible_records,
                    COUNT(DISTINCT CASE WHEN s.decision LIKE 'defer%'
                        THEN m.global_record_id END) AS deferred_records
                FROM document_query_membership m
                LEFT JOIN screening_decisions s
                  ON s.global_record_id = m.global_record_id
                 AND s.run_id = m.run_id
                 AND s.query_id = m.query_id
                 AND s.is_current = 1
                WHERE m.run_id = ? AND m.query_id = ? AND m.source_name = ?
                """,
                (run_id, row["query_id"], row["source_name"]),
            ).fetchone()
            rows.append(
                {
                    "run_id": run_id,
                    "iteration": row["iteration"],
                    "query_id": row["query_id"],
                    "source": row["source_name"],
                    "returned_records": row["scanned_records"],
                    "scanned_records": row["scanned_records"],
                    "novel_records": row["novel_records"],
                    "novel_eligible_records": int(decision_row["novel_eligible_records"] or 0),
                    "duplicate_records": int(row["scanned_records"])
                    - int(row["unique_contributions"]),
                    "missing_abstracts": 0,
                    "deferred_records": int(decision_row["deferred_records"] or 0),
                    "API_failures": 0,
                    "unique_contributions": row["unique_contributions"],
                    "overlap_counts_with_other_sources": int(row["scanned_records"])
                    - int(row["unique_contributions"]),
                }
            )
        return rows

    def _handoff_rows(
        self, connection: sqlite3.Connection, run_id: str
    ) -> list[dict[str, Any]]:
        rows = []
        for query_row in connection.execute(
            """
            SELECT iteration, query_id, finalized_at
            FROM query_iterations
            WHERE run_id = ? AND finalized_at IS NOT NULL
            ORDER BY iteration
            """,
            (run_id,),
        ):
            metric_values = {
                str(row["metric_name"]): float(row["metric_value"])
                for row in connection.execute(
                    """
                    SELECT metric_name, metric_value
                    FROM metric_values
                    WHERE run_id = ? AND query_id = ?
                    """,
                    (run_id, query_row["query_id"]),
                )
            }
            counts = connection.execute(
                """
                SELECT
                    COUNT(DISTINCT CASE WHEN s.decision = 'include'
                        THEN s.global_record_id END) AS included,
                    COUNT(DISTINCT o.event_id) AS emitted,
                    COUNT(DISTINCT CASE WHEN d.current_download_status =
                        'handoff_duplicate_suppressed' THEN s.global_record_id END)
                        AS duplicate_suppressed
                FROM screening_decisions s
                LEFT JOIN download_outbox o
                  ON o.global_record_id = s.global_record_id
                 AND o.run_id = s.run_id
                 AND o.query_id = s.query_id
                LEFT JOIN documents d ON d.global_record_id = s.global_record_id
                WHERE s.run_id = ? AND s.query_id = ? AND s.is_current = 1
                """,
                (run_id, query_row["query_id"]),
            ).fetchone()
            rows.append(
                {
                    "run_id": run_id,
                    "iteration": query_row["iteration"],
                    "query_id": query_row["query_id"],
                    "newly_included_records": int(
                        metric_values.get("include_count", int(counts["included"] or 0))
                    ),
                    "download_requests_emitted": int(
                        metric_values.get(
                            "download_requests_emitted", int(counts["emitted"] or 0)
                        )
                    ),
                    "duplicate_handoffs_suppressed": int(
                        metric_values.get(
                            "duplicate_handoffs_suppressed",
                            int(counts["duplicate_suppressed"] or 0),
                        )
                    ),
                    "handoff_failures": 0,
                    "pending_download_jobs": int(
                        metric_values.get("pending_download_jobs_at_iteration_end", 0)
                    ),
                    "claimed_download_jobs": 0,
                    "successful_downloads_observed": 0,
                    "skipped_existing_observed": 0,
                    "retryable_download_failures_observed": 0,
                    "terminal_download_failures_observed": 0,
                    "dead_letter_jobs": 0,
                    "handoff_backpressure_status": "ok",
                    "handoff_latency_seconds": "",
                    "timestamp": query_row["finalized_at"],
                }
            )
        return rows

    def _exclusion_rows(self, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[tuple[str, int, str, str], int] = {}
        timestamps: dict[tuple[str, int, str, str], str] = {}
        for decision in decisions:
            for reason in decision.get("reason_codes", []):
                key = (
                    str(decision["run_id"]),
                    int(decision["iteration"]),
                    str(decision["query_id"]),
                    str(reason),
                )
                counts[key] = counts.get(key, 0) + 1
                timestamps[key] = str(decision["screening_timestamp"])
        return [
            {
                "run_id": run_id,
                "iteration": iteration,
                "query_id": query_id,
                "reason_code": reason,
                "count": count,
                "timestamp": timestamps[(run_id, iteration, query_id, reason)],
            }
            for (run_id, iteration, query_id, reason), count in sorted(counts.items())
        ]

    def _persist_term_events(self, run_id: str, query: CanonicalQuery, plan: IterationPlan) -> int:
        rows = []
        for term in plan.added_terms:
            rows.append(
                {
                    "term": term,
                    "concept_block": "query_patch",
                    "action": "added",
                    "previous_status": "inactive",
                    "new_status": "active",
                    "reason": plan.decision_reason,
                    "supporting_positive_documents": [],
                    "supporting_negative_documents": [],
                    "discriminative_score": 0.0,
                }
            )
        for term in plan.removed_terms:
            rows.append(
                {
                    "term": term,
                    "concept_block": "query_patch",
                    "action": "removed",
                    "previous_status": "active",
                    "new_status": "rejected",
                    "reason": plan.decision_reason,
                    "supporting_positive_documents": [],
                    "supporting_negative_documents": [],
                    "discriminative_score": 0.0,
                }
            )
        if not rows and query.iteration == 1:
            rows = [
                {
                    "term": term,
                    "concept_block": "emerging_contaminant_terms",
                    "action": "initial",
                    "previous_status": "",
                    "new_status": "active",
                    "reason": plan.decision_reason,
                    "supporting_positive_documents": [],
                    "supporting_negative_documents": [],
                    "discriminative_score": 0.0,
                }
                for term in query.emerging_contaminant_terms[:3]
            ]
        rows.extend(self._mine_positive_term_candidates(run_id, query))
        rows.extend(self._mine_negative_term_candidates(run_id, query))
        with ControlPlane(self.db_path, self._git_sha()).transaction() as connection:
            for row in rows:
                term = str(row["term"])
                action = str(row["action"])
                event_id = hashlib.sha256(
                    f"{run_id}|{query.query_id}|{term}|{action}".encode()
                ).hexdigest()[:24]
                connection.execute(
                    """
                    INSERT OR IGNORE INTO term_ledger (
                        term_event_id, run_id, query_id, iteration, term,
                        concept_block, action, previous_status, new_status,
                        reason, supporting_positive_documents,
                        supporting_negative_documents, discriminative_score, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"term:{event_id}",
                        run_id,
                        query.query_id,
                        query.iteration,
                        term,
                        row["concept_block"],
                        action,
                        row["previous_status"],
                        row["new_status"],
                        row["reason"],
                        json.dumps(
                            row["supporting_positive_documents"],
                            sort_keys=True,
                            ensure_ascii=True,
                        ),
                        json.dumps(
                            row["supporting_negative_documents"],
                            sort_keys=True,
                            ensure_ascii=True,
                        ),
                        row["discriminative_score"],
                        utc_now_iso(),
                    ),
                )
        return len(rows)

    def _mine_positive_term_candidates(
        self, run_id: str, query: CanonicalQuery
    ) -> list[dict[str, Any]]:
        active_terms = {
            variant
            for block in (
                query.emerging_contaminant_terms,
                query.surface_water_terms,
                query.monitoring_and_concentration_terms,
                query.optional_context_terms,
            )
            for term in block
            for variant in self._term_variants(term)
        }
        positive_docs = self._term_mining_documents(run_id, query.query_id, decision="include")
        negative_docs = self._term_mining_documents(run_id, query.query_id, decision="exclude")
        positive_counts: dict[str, set[str]] = {}
        negative_counts: dict[str, set[str]] = {}
        for document in positive_docs:
            for term in self._candidate_terms_from_document(document):
                normalized = self._normalize_candidate_term(term)
                if normalized and normalized not in active_terms:
                    positive_counts.setdefault(normalized, set()).add(
                        str(document["global_record_id"])
                    )
        for document in negative_docs:
            for term in self._candidate_terms_from_document(document):
                normalized = self._normalize_candidate_term(term)
                if normalized:
                    negative_counts.setdefault(normalized, set()).add(
                        str(document["global_record_id"])
                    )
        candidates: list[dict[str, Any]] = []
        for term, positive_ids in positive_counts.items():
            concept_block = self._classify_candidate_term(term)
            if concept_block is None:
                continue
            if len(positive_ids) < self._minimum_positive_support_for_candidate(
                term, concept_block
            ):
                continue
            negative_ids = negative_counts.get(term, set())
            discriminative_score = len(positive_ids) / max(1, len(positive_ids) + len(negative_ids))
            if discriminative_score < 0.5:
                continue
            candidates.append(
                {
                    "term": term,
                    "concept_block": concept_block,
                    "action": "positive_candidate",
                    "previous_status": "absent",
                    "new_status": "candidate",
                    "reason": (
                        "High-frequency included-document title/abstract/keyword term "
                        "absent from active canonical query."
                    ),
                    "supporting_positive_documents": sorted(positive_ids),
                    "supporting_negative_documents": sorted(negative_ids),
                    "discriminative_score": round(discriminative_score, 6),
                }
            )
        return sorted(
            candidates,
            key=lambda row: (
                -self._positive_candidate_context_score(str(row["term"])),
                self._broad_positive_candidate_penalty(str(row["term"])),
                -len(cast(list[str], row["supporting_positive_documents"])),
                len(cast(list[str], row["supporting_negative_documents"])),
                -float(cast(float, row["discriminative_score"])),
                str(row["term"]),
            ),
        )[:25]

    def _mine_negative_term_candidates(
        self, run_id: str, query: CanonicalQuery
    ) -> list[dict[str, Any]]:
        active_terms = {
            variant
            for block in (
                query.emerging_contaminant_terms,
                query.surface_water_terms,
                query.monitoring_and_concentration_terms,
                query.optional_context_terms,
                query.prohibited_or_rejected_terms,
            )
            for term in block
            for variant in self._term_variants(term)
        }
        positive_docs = self._term_mining_documents(run_id, query.query_id, decision="include")
        negative_docs = self._term_mining_documents(run_id, query.query_id, decision="exclude")
        positive_counts: dict[str, set[str]] = {}
        negative_counts: dict[str, set[str]] = {}
        for document in positive_docs:
            for term in self._candidate_terms_from_document(document):
                normalized = self._normalize_candidate_term(term)
                if normalized:
                    positive_counts.setdefault(normalized, set()).add(
                        str(document["global_record_id"])
                    )
        for document in negative_docs:
            for term in self._candidate_terms_from_document(document):
                normalized = self._normalize_candidate_term(term)
                if normalized and normalized not in active_terms:
                    negative_counts.setdefault(normalized, set()).add(
                        str(document["global_record_id"])
                    )
        candidates: list[dict[str, Any]] = []
        for term, negative_ids in negative_counts.items():
            if len(negative_ids) < self._minimum_negative_support_for_candidate(term):
                continue
            positive_ids = positive_counts.get(term, set())
            discriminative_score = len(negative_ids) / max(1, len(positive_ids) + len(negative_ids))
            if discriminative_score < 0.75:
                continue
            if not self._is_noise_candidate_phrase(term):
                continue
            candidates.append(
                {
                    "term": term,
                    "concept_block": "prohibited_or_rejected_terms",
                    "action": "negative_noise_candidate",
                    "previous_status": "absent",
                    "new_status": "candidate",
                    "reason": (
                        "Repeated excluded-document title/abstract/keyword term that may "
                        "reduce off-scope retrieval noise if tested as an exclusion."
                    ),
                    "supporting_positive_documents": sorted(positive_ids),
                    "supporting_negative_documents": sorted(negative_ids),
                    "discriminative_score": round(discriminative_score, 6),
                }
            )
        return sorted(
            candidates,
            key=lambda row: (
                self._broad_negative_candidate_penalty(str(row["term"])),
                -self._specific_negative_candidate_score(str(row["term"])),
                len(cast(list[str], row["supporting_positive_documents"])),
                -len(cast(list[str], row["supporting_negative_documents"])),
                -float(cast(float, row["discriminative_score"])),
                str(row["term"]),
            ),
        )[:25]

    def _term_mining_documents(
        self, run_id: str, query_id: str, *, decision: str
    ) -> list[dict[str, Any]]:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT DISTINCT d.global_record_id, d.canonical_title,
                           d.abstract_original, d.keywords_json, d.study_type,
                           s.payload_json
                    FROM screening_decisions s
                    JOIN documents d ON d.global_record_id = s.global_record_id
                    WHERE s.run_id = ?
                      AND s.query_id = ?
                      AND s.decision = ?
                      AND s.is_current = 1
                    ORDER BY d.global_record_id
                    """,
                    (run_id, query_id, decision),
                )
            ]

    def _candidate_terms_from_document(self, document: dict[str, Any]) -> set[str]:
        text_parts = [
            str(document.get("canonical_title") or ""),
            str(document.get("abstract_original") or ""),
            str(document.get("study_type") or ""),
        ]
        model_terms: set[str] = set()
        with suppress(json.JSONDecodeError):
            text_parts.extend(json.loads(str(document.get("keywords_json") or "[]")))
        with suppress(json.JSONDecodeError):
            payload = json.loads(str(document.get("payload_json") or "{}"))
            text_parts.extend(payload.get("included_waterbody_types", []))
            text_parts.append(str(payload.get("study_type") or ""))
            for evidence in payload.get("query_term_evidence", []):
                if not isinstance(evidence, dict):
                    continue
                concept_block = str(evidence.get("concept_block") or "")
                confidence = evidence.get("confidence", 0.0)
                if isinstance(confidence, int | float) and confidence < 0.5:
                    continue
                term = self._normalize_candidate_term(str(evidence.get("term") or ""))
                candidate_terms = {term}
                if concept_block in {
                    "exclusion_candidate_terms",
                    "prohibited_or_rejected_terms",
                }:
                    candidate_terms.update(self._split_model_noise_term(term))
                for candidate_term in candidate_terms:
                    if (
                        self._is_candidate_phrase(candidate_term)
                        or concept_block
                        in {
                            "emerging_contaminant_terms",
                            "surface_water_terms",
                            "monitoring_and_concentration_terms",
                            "optional_context_terms",
                        }
                        and self._is_model_supported_positive_candidate(candidate_term)
                        or concept_block
                        in {
                            "exclusion_candidate_terms",
                            "prohibited_or_rejected_terms",
                        }
                        and self._is_noise_candidate_phrase(candidate_term)
                    ):
                        model_terms.add(candidate_term)
                text_parts.append(str(evidence.get("evidence_span") or ""))
        # Deterministic prefilter decisions may serialize an empty
        # query_term_evidence array. Treat that as "no model terms available"
        # and fall back to title/abstract mining instead of suppressing all
        # candidate generation for otherwise useful include/exclude records.
        if model_terms:
            return model_terms
        tokens = [
            token
            for token in re.findall(r"[a-zA-Z][a-zA-Z0-9-]+", " ".join(text_parts).lower())
            if token not in TERM_MINING_STOPWORDS and len(token) > 2
        ]
        terms: set[str] = set()
        for size in (1, 2, 3):
            for index in range(0, max(0, len(tokens) - size + 1)):
                phrase = " ".join(tokens[index : index + size])
                if self._is_candidate_phrase(phrase):
                    terms.add(phrase)
        return terms

    def _is_candidate_phrase(self, phrase: str) -> bool:
        normalized_phrase = self._normalize_candidate_term(phrase)
        if normalized_phrase in self._generic_overlap_candidate_terms():
            return False
        if self._has_nonterminal_generic_overlap_token(normalized_phrase):
            return False
        if normalized_phrase in TERM_MINING_STOPWORDS:
            return False
        if any(token in TERM_MINING_STOPWORDS for token in normalized_phrase.split()):
            return False
        if len(normalized_phrase) < 4:
            return False
        if self._is_noise_candidate_phrase(normalized_phrase):
            return True
        if not self._is_positive_context_candidate_phrase(normalized_phrase):
            return False
        signal_terms = {
            "antibiotic",
            "antibiotics",
            "abundance",
            "characterization",
            "cec",
            "cecs",
            "concentration",
            "concentrations",
            "compound",
            "compounds",
            "contamination",
            "contaminant",
            "contaminants",
            "emerging",
            "field",
            "freshwater",
            "index",
            "lake",
            "load",
            "measured",
            "measurement",
            "microplastic",
            "microplastics",
            "micropollutant",
            "micropollutants",
            "monitoring",
            "nanoplastic",
            "nanoplastics",
            "occurrence",
            "pfas",
            "pharmaceutical",
            "pharmaceuticals",
            "pollution",
            "quantification",
            "river",
            "sample",
            "samples",
            "sampling",
            "stream",
            "surface",
            "water",
        }
        tokens = normalized_phrase.split()
        return any(token in signal_terms for token in tokens)

    def _is_positive_context_candidate_phrase(self, term: str) -> bool:
        tokens = term.split()
        if len(tokens) == 1:
            return tokens[0] in {
                "antibiotic",
                "antibiotics",
                "cec",
                "cecs",
                "microplastic",
                "microplastics",
                "micropollutant",
                "micropollutants",
                "nanoplastic",
                "nanoplastics",
                "pfas",
                "pharmaceutical",
                "pharmaceuticals",
            }
        meaningful_pairs = {
            "field monitoring",
            "field sampling",
            "freshwater systems",
            "lake samples",
            "lake water",
            "measured concentration",
            "measured concentrations",
            "monitoring network",
            "occurrence distribution",
            "passive sampler",
            "passive samplers",
            "river water",
            "surface water",
            "water body",
            "water samples",
        }
        if term in meaningful_pairs:
            return True
        if "surface" in tokens and "water" in tokens:
            return True
        if {"river", "water"} <= set(tokens) or {"lake", "water"} <= set(tokens):
            return True
        if "occurrence" in tokens and (
            "distribution" in tokens or "spatial" in tokens or "spatiotemporal" in tokens
        ):
            return True
        if "concentration" in tokens or "concentrations" in tokens:
            return bool(
                set(tokens)
                & {
                    "antibiotic",
                    "antibiotics",
                    "cec",
                    "cecs",
                    "microplastic",
                    "microplastics",
                    "micropollutant",
                    "micropollutants",
                    "pfas",
                    "pharmaceutical",
                    "pharmaceuticals",
                    "surface",
                    "river",
                    "lake",
                    "water",
                }
            )
        if "samples" in tokens or "sampling" in tokens:
            return bool(set(tokens) & {"field", "water", "river", "lake", "surface"})
        if "monitoring" in tokens:
            return bool(
                set(tokens)
                & {
                    "field",
                    "freshwater",
                    "passive",
                    "river",
                    "surface",
                    "water",
                }
            )
        if "pollution" in tokens:
            return bool(set(tokens) & {"microplastic", "microplastics", "load", "index"})
        return False

    def _classify_candidate_term(self, term: str) -> str | None:
        tokens = set(term.split())
        if tokens & {
            "pfas",
            "pharmaceutical",
            "pharmaceuticals",
            "antibiotic",
            "antibiotics",
            "microplastic",
            "microplastics",
            "nanoplastic",
            "nanoplastics",
            "micropollutant",
            "micropollutants",
            "contaminant",
            "contaminants",
            "cec",
            "cecs",
        }:
            return "emerging_contaminant_terms"
        if tokens & {"river", "lake", "stream", "freshwater", "surface"}:
            return "surface_water_terms"
        if tokens & {
            "concentration",
            "concentrations",
            "monitoring",
            "occurrence",
            "measured",
            "measurement",
            "field",
            "sample",
            "samples",
            "sampling",
            "detection",
            "abundance",
            "characterization",
            "quantification",
            "load",
            "index",
        }:
            return "monitoring_and_concentration_terms"
        return None

    def _is_model_supported_positive_candidate(self, term: str) -> bool:
        if not term or term in TERM_MINING_STOPWORDS:
            return False
        if any(token in TERM_MINING_STOPWORDS for token in term.split()):
            return False
        tokens = set(term.split())
        positive_tokens = {
            "abundance",
            "antibiotic",
            "antibiotics",
            "characterization",
            "contamination",
            "detection",
            "emergence",
            "emerging",
            "estuary",
            "index",
            "lake",
            "load",
            "measured",
            "microplastic",
            "microplastics",
            "micropollutant",
            "micropollutants",
            "monitoring",
            "nanoplastic",
            "nanoplastics",
            "occurrence",
            "pfas",
            "pharmaceutical",
            "pharmaceuticals",
            "pollution",
            "quantification",
            "reservoir",
            "river",
            "stream",
            "surface",
            "water",
        }
        return bool(tokens & positive_tokens)

    def _is_noise_candidate_phrase(self, term: str) -> bool:
        normalized_term = self._normalize_candidate_term(term)
        if normalized_term in self._generic_overlap_candidate_terms():
            return False
        if self._has_nonterminal_generic_overlap_token(normalized_term):
            return False
        if normalized_term in TERM_MINING_STOPWORDS:
            return False
        if any(token in TERM_MINING_STOPWORDS for token in normalized_term.split()):
            return False
        if len(normalized_term) < 4:
            return False
        noise_terms = {
            "algorithm",
            "aquaculture",
            "benthic",
            "biota",
            "bird",
            "birds",
            "bottle",
            "bottled",
            "correction",
            "deposition",
            "fish",
            "gastropod",
            "gastropods",
            "health",
            "hydrodynamic",
            "hydrology",
            "laboratory",
            "litter",
            "membrane",
            "method",
            "mine",
            "mineral",
            "model",
            "modeling",
            "modelling",
            "mollusc",
            "molluscs",
            "mollusk",
            "mollusks",
            "optimization",
            "pool",
            "porous",
            "prediction",
            "remediation",
            "remote",
            "removal",
            "remove",
            "reuse",
            "review",
            "risk",
            "sand",
            "sediment",
            "sediments",
            "sensing",
            "simulation",
            "treatment",
            "treating",
            "wastewater",
        }
        return any(token in noise_terms for token in normalized_term.split())

    @staticmethod
    def _generic_overlap_candidate_terms() -> set[str]:
        return {
            "concern surface",
            "concern surface water",
            "emerging concern surface",
            "monitoring contaminants",
            "monitoring contaminants emerging",
            "removal contaminants",
            "removal contaminants emerging",
        }

    @staticmethod
    def _has_nonterminal_generic_overlap_token(term: str) -> bool:
        tokens = term.split()
        generic = {"concern", "contaminant", "contaminants", "emerging"}
        return any(token in generic for token in tokens[:-1])

    def _minimum_positive_support_for_candidate(
        self, term: str, concept_block: str
    ) -> int:
        tokens = set(term.split())
        high_value_context = {
            "field monitoring",
            "field sampling",
            "freshwater systems",
            "monitoring network",
            "monitoring networks",
            "occurrence distribution",
            "passive sampler",
            "passive samplers",
            "pollution load index",
            "river water",
            "water samples",
        }
        if concept_block != "emerging_contaminant_terms":
            if term in high_value_context:
                return 1
            if tokens & {"river", "lake", "surface", "freshwater", "water"} and tokens & {
                "monitoring",
                "occurrence",
                "sample",
                "samples",
                "sampling",
            }:
                return 1
            return 2
        broad_tokens = {
            "antibiotic",
            "antibiotics",
            "pharmaceutical",
            "pharmaceuticals",
            "pfas",
            "microplastic",
            "microplastics",
            "pesticide",
            "pesticides",
            "metal",
            "metals",
        }
        if tokens & broad_tokens:
            return 2
        return 1

    def _minimum_negative_support_for_candidate(self, term: str) -> int:
        high_precision_noise = {
            "review",
            "correction",
            "groundwater",
            "treatment",
            "removal",
            "sediment",
            "sediments",
            "fish",
            "gastropod",
            "gastropods",
            "hydrodynamic",
            "laboratory",
            "model",
            "modeling",
            "modelling",
            "mollusc",
            "molluscs",
            "mollusk",
            "mollusks",
            "pool",
            "porous",
            "prediction",
            "risk",
        }
        if any(token in high_precision_noise for token in term.split()):
            return 1
        return 2

    def _broad_negative_candidate_penalty(self, term: str) -> int:
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
        normalized = self._normalize_candidate_term(term)
        return 1 if normalized in broad_singletons else 0

    def _positive_candidate_context_score(self, term: str) -> int:
        tokens = self._normalize_candidate_term(term).split()
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
            "range",
            "sample",
            "samples",
            "sampling",
            "spatiotemporal",
            "surface",
            "water",
        }
        return phrase_bonus + sum(1 for token in tokens if token in context_tokens)

    def _broad_positive_candidate_penalty(self, term: str) -> int:
        normalized = self._normalize_candidate_term(term)
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

    def _specific_negative_candidate_score(self, term: str) -> int:
        tokens = self._normalize_candidate_term(term).split()
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

    def _split_model_noise_term(self, term: str) -> set[str]:
        pieces = {
            self._normalize_candidate_term(piece)
            for piece in re.split(r"\s+(?:or|and)\s+|/|,", term)
        }
        return {piece for piece in pieces if piece and piece != term}

    def _normalize_candidate_term(self, term: str) -> str:
        normalized = re.sub(r"[*?]+", "", term.lower().replace("-", " "))
        return " ".join(normalized.split())

    def _term_variants(self, term: str) -> set[str]:
        normalized = self._normalize_candidate_term(term)
        tokens = normalized.split()
        singular_tokens = [
            token[:-1] if len(token) > 4 and token.endswith("s") else token
            for token in tokens
        ]
        return {normalized, " ".join(singular_tokens)}

    def _validate_screening_decision(self, decision: ScreeningDecision) -> None:
        if self._screening_schema is None:
            self._screening_schema = read_json(
                self._schema_path("retrieval", "screening_decision.schema.json")
            )
        validate(instance=decision.to_dict(), schema=self._screening_schema)

    def _schema_path(self, namespace: str, name: str) -> Path:
        repo_path = self.repo_root / "schemas" / namespace / name
        if repo_path.exists():
            return repo_path
        source_repo = Path(__file__).resolve().parents[4]
        return source_repo / "schemas" / namespace / name

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.repo_root))
        except ValueError:
            return str(path)

    def _path_is_relative_to(self, path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
        except ValueError:
            return False
        return True

    def _insert_audit_event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        query_id: str | None,
        global_record_id: str | None,
        event_type: str,
        payload: dict[str, Any],
        actor: str,
    ) -> None:
        event_id = hashlib.sha256(
            f"{run_id}|{query_id}|{global_record_id}|{event_type}|"
            f"{json.dumps(payload, sort_keys=True, ensure_ascii=True)}".encode()
        ).hexdigest()[:32]
        connection.execute(
            """
            INSERT OR REPLACE INTO audit_events (
                audit_event_id, run_id, query_id, global_record_id, event_type,
                payload_json, actor, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit:{event_id}",
                run_id,
                query_id,
                global_record_id,
                event_type,
                json.dumps(payload, sort_keys=True, ensure_ascii=True),
                actor,
                utc_now_iso(),
            ),
        )

    def _snapshot_run_db(self, run_dir: Path) -> None:
        if not self.db_path.exists():
            return
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        state_dir = ensure_dir(run_dir / "state")
        max_snapshot_bytes = int(
            os.environ.get("ECMONITOR_RUN_DB_SNAPSHOT_MAX_BYTES", str(256 * 1024 * 1024))
        )
        database_size = self.db_path.stat().st_size
        if database_size > max_snapshot_bytes:
            write_json_atomic(
                state_dir / "control_plane_reference.json",
                {
                    "snapshot_status": "shared_control_plane_referenced",
                    "database_ref": str(self.db_path.resolve()),
                    "database_size_bytes": database_size,
                    "snapshot_limit_bytes": max_snapshot_bytes,
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_dir.name,
                    "reason": (
                        "The shared control plane exceeds the per-run snapshot limit. "
                        "Run-specific JSONL, provider pages, manifests, and audit exports "
                        "remain in this run directory."
                    ),
                },
            )
            return
        shutil.copy2(self.db_path, state_dir / "control.sqlite3")

    def _write_run_summary(self, run_dir: Path, result: IterationResult) -> None:
        write_text_atomic(
            run_dir / "RUN_SUMMARY.md",
            "\n".join(
                [
                    "# Retrieval Specialist Run Summary",
                    "",
                    f"- run_id: {run_dir.name}",
                    "- run status: completed",
                    f"- final accepted query: {result.query_id}",
                    f"- number of iterations: {result.iteration}",
                    (
                        f"- final score: {result.total_score:.4f}"
                        if result.total_score is not None
                        else "- final score: unavailable"
                    ),
                    f"- saturation status: {result.saturation_status}",
                    f"- source mode: {self._run_source_mode(run_dir)}",
                    f"- LLM mode: {self._run_llm_mode(run_dir)}",
                    "- persistence: SQLite control plane with JSONL/CSV audit exports",
                    "",
                ]
            ),
        )

    def _update_status_files(
        self, run_id: str, result: IterationResult, export_result: dict[str, Any]
    ) -> None:
        write_text_atomic(
            self.repo_root / "LATEST_RUN.md",
            "\n".join(
                [
                    "# Latest Run",
                    "",
                    "| Field | Value |",
                    "| --- | --- |",
                    f"| run_id | {run_id} |",
                    "| run status | completed |",
                    f"| code commit SHA | {self._git_sha()} |",
                    f"| final accepted query | {result.query_id} |",
                    f"| number of iterations | {result.iteration} |",
                    "| initial score | see paper_exports/query_metrics_wide.csv |",
                    (
                        f"| final score | {result.total_score:.4f} |"
                        if result.total_score is not None
                        else "| final score | unavailable |"
                    ),
                    "| novel eligible documents | see metric `marginal_eligible_count` |",
                    f"| source completeness | {self._latest_source_completeness(run_id)} |",
                    f"| saturation status | {result.saturation_status} |",
                    "| main exclusion reasons | see paper_exports/exclusion_reason_evolution.csv |",
                    "| major anomalies | none in deterministic mock run |",
                    f"| output directory | runs/{run_id} |",
                    "| export manifest | "
                    f"{self._display_path(Path(export_result['export_manifest']))} |",
                    "",
                ]
            ),
        )
        write_text_atomic(
            self.repo_root / "PROJECT_STATUS.md",
            "\n".join(
                [
                    "# Project Status",
                    "",
                    "## Current Development Phase",
                    "",
                    "Live Retrieval Specialist integration: ECfinder skill-boundary "
                    "discovery with durable screening/query-refinement workflow.",
                    "",
                    "## Implemented",
                    "",
                    "- SQLite control-plane migration and integrity commands.",
                    "- Persistent multi-iteration mock loop with accept, reject, rollback, "
                    "and saturation branches.",
                    "- Bounded source-page, normalization, screening, and export artifacts.",
                    "- ECfinder external_metadata_discovery_v1 runner boundary for "
                    "live metadata discovery.",
                    "- Durable one-document-per-conversation GPT screening "
                    "request/result workflow.",
                    "- Deterministic live query-acceptance thresholds and QueryPatch application.",
                    "- Global document registry and novelty membership tables.",
                    "- Structured screening schema validation.",
                    "- Transactional download outbox and mock download-job accounting.",
                    "- Paper exports rebuilt from persisted SQLite state.",
                    "",
                    "## Remaining External Blockers",
                    "",
                    "- Strict four-source formal retrieval requires "
                    "`SEMANTIC_SCHOLAR_API_KEY`; without it, live preflight "
                    "excludes Semantic Scholar and permits only explicit degraded "
                    "Crossref/OpenAlex/PubMed execution.",
                    "- PDF acquisition remains owned by Download Specialist.",
                    "",
                    "## Latest Test Status",
                    "",
                    "See final Phase 1.1 report for the exact command results.",
                    "",
                    "## Latest Runtime Status",
                    "",
                    f"Latest Retrieval Specialist run completed: `runs/{run_id}`.",
                    "",
                ]
            ),
        )

    def _run_source_mode(self, run_dir: Path) -> str:
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            manifest = dict(read_json(manifest_path))
            if manifest.get("runtime_source_health_mode") == "external_metadata_discovery_v1":
                return "live external_metadata_discovery_v1 skill boundary"
        return "deterministic mock; no real metadata APIs called"

    def _run_llm_mode(self, run_dir: Path) -> str:
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            model_parameters = dict(dict(read_json(manifest_path)).get("model_parameters") or {})
            if model_parameters.get("llm_enabled"):
                return "durable file-based GPT screening/query workflow"
        return "disabled; deterministic screening only"

    def _latest_source_completeness(self, run_id: str) -> str:
        with ControlPlane(self.db_path, self._git_sha()).connect() as connection:
            row = connection.execute(
                "SELECT completeness FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return str(row["completeness"]) if row is not None else "unknown"

    def _fixture_path(self) -> Path:
        return self.repo_root / "tests" / "fixtures" / "mock_records.json"

    def _payload_checksum(self, payload: dict[str, Any]) -> str:
        unsigned = dict(payload)
        unsigned["payload_checksum"] = ""
        return hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()

    def _file_hash(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"

    def _lock_hash(self) -> str:
        return self._file_hash(self.repo_root / "uv.lock")

    def _prompt_hash(self) -> str:
        if self._prompt_hash_value is not None:
            return self._prompt_hash_value
        prompt_dir = self.repo_root / "prompts" / "retrieval"
        digest = hashlib.sha256()
        for path in sorted(prompt_dir.glob("*.md")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        self._prompt_hash_value = digest.hexdigest()
        return self._prompt_hash_value

    def _scie_registry_hash(self) -> str:
        return self._file_hash(self.repo_root / "registry" / "scie_journals.csv")

    def _scie_status(self, protocol: dict[str, Any]) -> str:
        registry_path = self.repo_root / str(protocol["scie"]["registry_path"])
        if not registry_path.exists():
            return "unknown"
        content = registry_path.read_text(encoding="utf-8").strip().splitlines()
        return "matched" if len(content) > 1 else "unknown"

    def _git_sha(self) -> str:
        return self._git_sha_value

    def _git_branch(self) -> str:
        return self._git_branch_value

    def _git_dirty_status(self) -> str:
        return self._git_dirty_value

    def _git(self, args: list[str], default: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                check=True,
                text=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return default
        return result.stdout.strip() or default

    def _bool_to_int(self, value: bool | None) -> int | None:
        if value is None:
            return None
        return 1 if value else 0

    def _int_to_bool(self, value: Any) -> bool | None:
        if value is None:
            return None
        return bool(value)

    def _utc_plus_seconds(self, seconds: int) -> str:
        from datetime import UTC, datetime, timedelta

        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    def _new_run_id(self, prefix: str = "retrieval_phase1_1") -> str:
        seed = f"{utc_now_iso()}|{self._git_sha()}".encode()
        short_hash = hashlib.sha256(seed).hexdigest()[:8]
        timestamp = utc_now_iso().replace("-", "").replace(":", "")
        return f"{prefix}_{timestamp}_{short_hash}"


def _split_group_concat(value: object) -> list[str]:
    if value is None:
        return []
    return [item for item in str(value).split(",") if item]
