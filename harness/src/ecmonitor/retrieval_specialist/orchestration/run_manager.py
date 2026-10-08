"""Public command facade for the Retrieval Specialist harness."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ecmonitor.retrieval_specialist.adapters import MockSourceAdapter
from ecmonitor.retrieval_specialist.models import (
    CanonicalQuery,
    NormalizedRecord,
    QueryMetrics,
    RawRecord,
    ScreeningDecision,
    SourceExecutionStatus,
)
from ecmonitor.retrieval_specialist.operators.deduplicator import DeduplicationResult, Deduplicator
from ecmonitor.retrieval_specialist.operators.exporter import Exporter
from ecmonitor.retrieval_specialist.operators.metadata_normalizer import MetadataNormalizer
from ecmonitor.retrieval_specialist.operators.protocol_loader import ProtocolLoader
from ecmonitor.retrieval_specialist.operators.screener import TitleAbstractScreener
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.orchestration.checkpoints import CheckpointManager
from ecmonitor.retrieval_specialist.orchestration.handoff import (
    DownloadHandoffOutbox,
    HandoffResult,
)
from ecmonitor.retrieval_specialist.orchestration.memory import (
    MemoryTelemetry,
)
from ecmonitor.retrieval_specialist.orchestration.phase11_runner import Phase11Runner
from ecmonitor.retrieval_specialist.storage.atomic_io import (
    append_jsonl,
    count_jsonl,
    ensure_dir,
    read_json,
    read_jsonl,
    read_yaml,
    sha256_file,
    write_csv_atomic,
    write_json_atomic,
    write_text_atomic,
    write_yaml_atomic,
)
from ecmonitor.retrieval_specialist.storage.control_plane import ControlPlane
from ecmonitor.security import redact

SOURCE_NAMES = ["crossref", "openalex", "semantic_scholar", "pubmed"]


class RunManager:
    """Create, resume, inspect, and roll back Retrieval Specialist runs."""

    def __init__(
        self,
        repo_root: Path,
        config_dir: Path | None = None,
        runs_dir: Path | None = None,
        paper_exports_dir: Path | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.config_dir = config_dir or repo_root / "configs" / "retrieval"
        self.runs_dir = runs_dir or repo_root / "runs"
        self.paper_exports_dir = paper_exports_dir or repo_root / "paper_exports"

    def validate_config(self) -> dict[str, Any]:
        loaded = ProtocolLoader(self.config_dir).load()
        return {
            "status": "ok",
            "config_hash": loaded.config_hash,
            "protocol_version": loaded.protocol.get("protocol_version"),
            "runtime_version": loaded.runtime.get("runtime_version"),
            "scie_status": self._scie_status(loaded.protocol),
            "external_metadata_discovery": loaded.sources.get("external_metadata_discovery", {}),
        }

    def db_migrate(self) -> dict[str, Any]:
        return ControlPlane(self._control_db_path(), self._git_sha()).migrate()

    def db_status(self) -> dict[str, Any]:
        return ControlPlane(self._control_db_path(), self._git_sha()).status()

    def db_integrity_check(self) -> dict[str, Any]:
        return ControlPlane(self._control_db_path(), self._git_sha()).integrity_check()

    def health_check(self) -> dict[str, Any]:
        skill_path = os.environ.get("ECFINDER_METADATA_SKILL_PATH", "").strip()
        if not skill_path:
            fixture_path = self.repo_root / "tests" / "fixtures" / "mock_records.json"
            return {
                source: MockSourceAdapter(source, fixture_path).health_check().value
                for source in SOURCE_NAMES
            }
        output_ref = self.repo_root / "runs" / "_health" / "external_metadata_health.json"
        output_ref.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = (
            str(Path(skill_path) / "src")
            + os.pathsep
            + str(self.repo_root / "src")
            + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ecfinder.skills.external_metadata_discovery.runner",
                "--health-check",
                "--output",
                str(output_ref),
            ],
            cwd=Path(skill_path),
            env=env,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            return {
                "status": "failed",
                "mode": "external_metadata_discovery_v1",
                "error": (result.stderr.strip() or result.stdout.strip())[:500],
            }
        payload = read_json(output_ref)
        if not isinstance(payload, dict):
            return {"status": "failed", "mode": "external_metadata_discovery_v1"}
        return payload

    def mock_run(
        self,
        *,
        date_to: str,
        date_from: str | None = None,
        run_id: str | None = None,
        target_novel_records: int | None = None,
        max_scan_depth: int | None = None,
        max_iterations: int | None = None,
        resume: bool = False,
        fail_after_operator: str | None = None,
        fail_after_batch: int | None = None,
        fail_after_source_page: int | None = None,
        stress_records_per_source: int | None = None,
        force_max_iterations: bool = False,
    ) -> dict[str, Any]:
        return self._phase11_runner().run(
            date_from=date_from,
            date_to=date_to,
            run_id=run_id,
            target_novel_records=target_novel_records,
            max_scan_depth=max_scan_depth,
            max_iterations=max_iterations,
            resume=resume,
            fail_after_operator=fail_after_operator,
            fail_after_batch=fail_after_batch,
            fail_after_source_page=fail_after_source_page,
            stress_records_per_source=stress_records_per_source,
            force_max_iterations=force_max_iterations,
        )

    def discovery_dry_run(
        self,
        *,
        date_to: str,
        date_from: str | None = None,
        max_records_per_provider: int = 5,
        run_id: str | None = None,
        providers: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._phase11_runner().discovery_dry_run(
            date_to=date_to,
            date_from=date_from,
            max_records_per_provider=max_records_per_provider,
            run_id=run_id,
            providers=providers,
        )

    def live_preflight(
        self, *, providers: list[str] | None = None, strict: bool = False
    ) -> dict[str, Any]:
        return self._live_provider_preflight(
            providers or SOURCE_NAMES,
            strict=strict or providers is not None,
        )

    def live_run(
        self,
        *,
        date_to: str,
        date_from: str | None = None,
        run_id: str | None = None,
        target_novel_records: int | None = None,
        max_scan_depth: int | None = None,
        max_iterations: int | None = None,
        providers: list[str] | None = None,
        allow_degraded_sources: bool = False,
        force_max_iterations: bool = False,
        require_stable_provider_auth: bool = True,
    ) -> dict[str, Any]:
        preflight = self._live_provider_preflight(
            providers or SOURCE_NAMES,
            strict=providers is not None and not allow_degraded_sources,
        )
        if preflight["status"] != "ok":
            return preflight | {
                "run_id": run_id,
                "date_from": date_from,
                "date_to": date_to,
                "status": "preflight_blocked",
            }
        unavailable = dict(preflight.get("unavailable_providers") or {})
        if unavailable and not allow_degraded_sources:
            return preflight | {
                "run_id": run_id,
                "date_from": date_from,
                "date_to": date_to,
                "status": "preflight_blocked",
                "reason": "degraded_provider_set_requires_explicit_acknowledgement",
                "required_acknowledgement": "allow_degraded_sources",
            }
        unstable = self._unstable_live_provider_auth(preflight, providers or SOURCE_NAMES)
        if unstable and require_stable_provider_auth and not allow_degraded_sources:
            return preflight | {
                "run_id": run_id,
                "date_from": date_from,
                "date_to": date_to,
                "status": "preflight_blocked",
                "reason": "unstable_provider_auth_requires_explicit_acknowledgement",
                "required_acknowledgement": "allow_degraded_sources",
                "unstable_providers": unstable,
            }
        selected_providers = list(preflight.get("selected_providers") or providers or SOURCE_NAMES)
        return self._phase11_runner().run(
            date_from=date_from,
            date_to=date_to,
            run_id=run_id,
            target_novel_records=target_novel_records,
            max_scan_depth=max_scan_depth,
            max_iterations=max_iterations,
            live_mode=True,
            providers=selected_providers,
            force_max_iterations=force_max_iterations,
        )

    def _live_provider_preflight(
        self, providers: list[str], *, strict: bool = True
    ) -> dict[str, Any]:
        skill_path = os.environ.get("ECFINDER_METADATA_SKILL_PATH", "").strip()
        if not skill_path:
            return {
                "status": "blocked",
                "reason": "ECFINDER_METADATA_SKILL_PATH missing",
                "providers_requested": providers,
                "blocked_providers": providers,
                "provider_health": {},
            }
        health = self.health_check()
        provider_health = dict(health.get("providers", {}))
        environment = dict(health.get("environment_validation") or {})
        blocked: dict[str, dict[str, Any]] = {}
        selected: list[str] = []
        for provider in providers:
            status = dict(provider_health.get(provider, {}))
            if self._provider_requires_stable_auth(provider, environment):
                blocked[provider] = self._blocked_provider_payload(
                    status,
                    override={
                        "authentication": "anonymous",
                        "status": "configuration_required",
                        "error_classification": "missing_stable_api_key",
                    },
                )
                continue
            if self._provider_usable_for_live_run(provider, status):
                selected.append(provider)
            else:
                blocked[provider] = self._blocked_provider_payload(status)
        if strict and blocked:
            return {
                "status": "blocked",
                "reason": "provider_health_preflight_failed",
                "providers_requested": providers,
                "blocked_providers": blocked,
                "environment_validation": health.get("environment_validation", {}),
            }
        if not selected:
            return {
                "status": "blocked",
                "reason": "no_usable_live_providers",
                "providers_requested": providers,
                "blocked_providers": blocked,
                "environment_validation": health.get("environment_validation", {}),
            }
        return {
            "status": "ok",
            "reason": (
                "provider_health_preflight_passed"
                if not blocked
                else "provider_health_preflight_degraded"
            ),
            "providers_requested": providers,
            "selected_providers": selected,
            "unavailable_providers": blocked,
            "environment_validation": health.get("environment_validation", {}),
        }

    def _provider_requires_stable_auth(
        self, provider: str, environment: dict[str, Any]
    ) -> bool:
        return (
            provider == "semantic_scholar"
            and environment.get("SEMANTIC_SCHOLAR_API_KEY") != "configured"
        )

    def _blocked_provider_payload(
        self, status: dict[str, Any], *, override: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        merged = status | dict(override or {})
        return {
            "connectivity": merged.get("connectivity", "missing"),
            "authentication": merged.get("authentication", "missing"),
            "api_response_status": merged.get("api_response_status", "missing"),
            "status": merged.get("status", "missing"),
            "record_count": merged.get("record_count", 0),
            "error_classification": merged.get("error_classification", ""),
            "diagnostics": self._safe_provider_diagnostics(
                dict(merged.get("diagnostics") or {})
            ),
        }

    def _provider_usable_for_live_run(self, provider: str, status: dict[str, Any]) -> bool:
        provider_status = str(status.get("status") or "")
        if provider_status in {"success", "no-results"}:
            return True
        if provider != "pubmed" or provider_status != "partial":
            return False
        diagnostics = dict(status.get("diagnostics") or {})
        fallback = diagnostics.get("fallback") == "pubmed_web_html"
        response_status = status.get("api_response_status") == "eutils_blocked_pubmed_html_fallback"
        records_available = int(status.get("record_count") or 0) > 0
        return fallback and response_status and records_available

    def _unstable_live_provider_auth(
        self, preflight: dict[str, Any], providers: list[str]
    ) -> dict[str, dict[str, Any]]:
        if "semantic_scholar" not in providers:
            return {}
        environment = dict(preflight.get("environment_validation") or {})
        if environment.get("SEMANTIC_SCHOLAR_API_KEY") == "configured":
            return {}
        if "semantic_scholar" not in set(preflight.get("selected_providers") or []):
            return {}
        return {
            "semantic_scholar": {
                "authentication": "anonymous",
                "risk": "intermittent_429_rate_limit",
                "required_env": "SEMANTIC_SCHOLAR_API_KEY",
            }
        }

    def _safe_provider_diagnostics(self, diagnostics: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "classification",
            "content_type",
            "fallback",
            "stage",
            "http_status",
            "attempts",
            "retry_after",
            "blocked_by_ncbi",
            "final_url_host",
            "final_url_path",
            "suggested_action",
        }
        return {key: value for key, value in diagnostics.items() if key in allowed}

    def resume(self, run_id: str) -> dict[str, Any]:
        return self._phase11_runner().resume(run_id)
        run_dir = self.runs_dir / run_id
        latest = CheckpointManager(run_dir).latest()
        manifest = read_json(run_dir / "manifest.json")
        if latest.get("state") == "STOP":
            append_jsonl(
                run_dir / "logs" / "decision_log.jsonl",
                {"timestamp": utc_now_iso(), "decision": "resume_noop", "run_id": run_id},
            )
            return {"run_id": run_id, "status": "already_completed", "latest_checkpoint": latest}
        return self.mock_run(date_to=str(manifest["date_to"]), run_id=run_id, resume=True)

    def inspect(self, run_id: str) -> dict[str, Any]:
        return self._phase11_runner().inspect(run_id)
        run_dir = self.runs_dir / run_id
        return {
            "manifest": read_json(run_dir / "manifest.json"),
            "latest_checkpoint": CheckpointManager(run_dir).latest(),
            "metrics": read_json(run_dir / "metrics" / "metrics.json"),
        }

    def rollback(self, run_id: str, query_id: str) -> dict[str, Any]:
        return self._phase11_runner().rollback(run_id, query_id)
        return CheckpointManager(self.runs_dir / run_id).rollback_to_query(query_id)

    def export_paper_data(self, run_id: str) -> dict[str, Any]:
        return self._phase11_runner().export_paper_data(run_id)
        run_dir = self.runs_dir / run_id
        query_path = next((run_dir / "queries").glob("*/canonical_query.yaml"))
        query_payload = read_yaml(query_path)
        query = CanonicalQuery(**query_payload)
        metrics = QueryMetrics(**read_json(run_dir / "metrics" / "metrics.json"))
        decisions = [
            ScreeningDecision(**row)
            for row in read_jsonl(run_dir / "screening" / "screening_decisions.jsonl")
        ]
        source_summary = read_json(
            run_dir / "queries" / query.query_id / "source_results_summary.json"
        )
        self._write_exports(query, metrics, decisions, source_summary, run_dir)
        return {"run_id": run_id, "rebuilt_exports": str(run_dir / "exports")}

    def export_all_runs(self) -> dict[str, Any]:
        return self._phase11_runner().export_all_runs()

    def evaluate_audit_pool(
        self, run_id: str, audit_pool_path: Path | None = None
    ) -> dict[str, Any]:
        return self._phase11_runner().evaluate_audit_pool(run_id, audit_pool_path)

    def evaluate_candidate_pool_audit(
        self, *, pool_id: str, audit_pool_path: Path | None = None
    ) -> dict[str, Any]:
        return self._phase11_runner().evaluate_candidate_pool_audit(
            pool_id=pool_id,
            audit_pool_path=audit_pool_path,
        )

    def build_candidate_pool(
        self, *, source_run_ids: list[str], pool_id: str | None = None
    ) -> dict[str, Any]:
        return self._phase11_runner().build_candidate_pool(
            source_run_ids=source_run_ids,
            pool_id=pool_id,
        )

    def screen_candidate_pool(
        self,
        *,
        pool_id: str,
        max_records: int | None = None,
        providers: list[str] | None = None,
        query_families: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._phase11_runner().screen_candidate_pool(
            pool_id=pool_id,
            max_records=max_records,
            providers=providers,
            query_families=query_families,
        )

    def reuse_candidate_pool_screening(
        self, *, pool_id: str, source_pool_ids: list[str]
    ) -> dict[str, Any]:
        return self._phase11_runner().reuse_candidate_pool_screening(
            pool_id=pool_id,
            source_pool_ids=source_pool_ids,
        )

    def safe_defer_candidate_pool_screening(
        self, *, pool_id: str, max_records: int | None = None
    ) -> dict[str, Any]:
        return self._phase11_runner().safe_defer_candidate_pool_screening(
            pool_id=pool_id,
            max_records=max_records,
        )

    def analyze_candidate_pool(self, *, pool_id: str) -> dict[str, Any]:
        return self._phase11_runner().analyze_candidate_pool(pool_id=pool_id)

    def plan_query_family_construction(self, *, pool_id: str) -> dict[str, Any]:
        return self._phase11_runner().plan_query_family_construction(pool_id=pool_id)

    def plan_candidate_pool_post_review(self, *, pool_id: str) -> dict[str, Any]:
        return self._phase11_runner().plan_candidate_pool_post_review(pool_id=pool_id)

    def plan_candidate_pool_metadata_enrichment(
        self, *, pool_id: str, limit: int = 500
    ) -> dict[str, Any]:
        return self._phase11_runner().plan_candidate_pool_metadata_enrichment(
            pool_id=pool_id,
            limit=limit,
        )

    def enrich_candidate_pool_metadata(
        self, *, pool_id: str, limit: int = 25, batch_size: int = 10
    ) -> dict[str, Any]:
        return self._phase11_runner().enrich_candidate_pool_metadata(
            pool_id=pool_id,
            limit=limit,
            batch_size=batch_size,
        )

    def rescreen_enriched_candidate_pool_metadata(
        self, *, pool_id: str, max_records: int | None = None
    ) -> dict[str, Any]:
        return self._phase11_runner().rescreen_enriched_candidate_pool_metadata(
            pool_id=pool_id,
            max_records=max_records,
        )

    def audit_candidate_pool(self, *, pool_id: str, sample_size: int = 40) -> dict[str, Any]:
        return self._phase11_runner().audit_candidate_pool(
            pool_id=pool_id,
            sample_size=sample_size,
        )

    def review_candidate_pool_audit(
        self, *, pool_id: str, max_records: int | None = None
    ) -> dict[str, Any]:
        return self._phase11_runner().review_candidate_pool_audit(
            pool_id=pool_id,
            max_records=max_records,
        )

    def safe_defer_candidate_pool_audit_review(
        self, *, pool_id: str, max_records: int | None = None
    ) -> dict[str, Any]:
        return self._phase11_runner().safe_defer_candidate_pool_audit_review(
            pool_id=pool_id,
            max_records=max_records,
        )

    def export_candidate_pool_review(self, *, pool_id: str) -> dict[str, Any]:
        return self._phase11_runner().export_candidate_pool_review(pool_id=pool_id)

    def build_candidate_pool_review_priority(
        self, *, pool_id: str, limit: int = 300
    ) -> dict[str, Any]:
        return self._phase11_runner().build_candidate_pool_review_priority(
            pool_id=pool_id,
            limit=limit,
        )

    def build_high_recall_candidate_pool(
        self,
        *,
        family_config_dirs: list[Path],
        date_to: str,
        date_from: str | None = None,
        pool_id: str | None = None,
        max_records_per_provider: int = 100,
        providers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run bounded discovery for query-family branches and union the records."""

        if not family_config_dirs:
            raise ValueError("At least one query-family config directory is required")
        pool_id = pool_id or self._new_candidate_pool_id()
        summary_dir = ensure_dir(self.runs_dir / pool_id / "candidate_pool")
        source_run_ids: list[str] = []
        source_runs: list[dict[str, Any]] = []
        failed_source_runs: list[dict[str, Any]] = []
        skipped_source_runs: list[dict[str, Any]] = []

        def write_progress_summary(status: str) -> None:
            write_json_atomic(
                summary_dir / "high_recall_build_summary.json",
                {
                    "pool_id": pool_id,
                    "status": status,
                    "mode": "high_recall_query_family_candidate_pool",
                    "date_from": date_from,
                    "date_to": date_to,
                    "max_records_per_provider": max_records_per_provider,
                    "providers": providers or SOURCE_NAMES,
                    "source_run_ids": source_run_ids,
                    "source_runs": source_runs,
                    "failed_source_runs": failed_source_runs,
                    "skipped_source_runs": skipped_source_runs,
                    "candidate_pool": None,
                    "guardrails": [
                        (
                            "This command builds a high-recall union pool, not one "
                            "precision-optimized query."
                        ),
                        (
                            "No GPT screening, query refinement, PDF download, or "
                            "Download Specialist is run."
                        ),
                        (
                            "Completed family runs are reused on resume to avoid "
                            "repeating source calls."
                        ),
                    ],
                },
            )

        for family_config_dir in family_config_dirs:
            config_dir = family_config_dir.resolve()
            if not config_dir.exists():
                raise FileNotFoundError(f"Missing query-family config directory: {config_dir}")
            protocol = ProtocolLoader(config_dir).load().protocol
            family_key = self._query_family_key(config_dir, protocol)
            run_id = self._high_recall_family_run_id(pool_id, family_key)
            existing = self._completed_high_recall_family_run(run_id)
            if existing is not None:
                self._write_high_recall_family_config_ref(run_id, config_dir)
                if existing["candidate_count"] > 0:
                    source_run_ids.append(run_id)
                source_runs.append(
                    {
                        "run_id": run_id,
                        "query_family": protocol.get("protocol_version", family_key),
                        "config_dir": str(config_dir),
                        "status": existing["status"],
                        "normalized_record_count": existing["normalized_record_count"],
                        "candidate_count": existing["candidate_count"],
                        "source_statuses": existing["source_statuses"],
                        "reused_existing_run": True,
                    }
                )
                if existing["candidate_count"] <= 0:
                    failed_source_runs.append(
                        {
                            "run_id": run_id,
                            "query_family": protocol.get("protocol_version", family_key),
                            "config_dir": str(config_dir),
                            "status": existing["status"],
                            "error_class": "NoReusableCandidates",
                            "error_message": "completed family run produced no reusable candidates",
                            "source_statuses": existing["source_statuses"],
                        }
                    )
                skipped_source_runs.append(
                    {
                        "run_id": run_id,
                        "query_family": protocol.get("protocol_version", family_key),
                        "status": existing["reuse_status"],
                        "candidate_count": existing["candidate_count"],
                    }
                )
                write_progress_summary("running")
                continue
            runner = Phase11Runner(
                repo_root=self.repo_root,
                config_dir=config_dir,
                runs_dir=self.runs_dir,
                paper_exports_dir=self.paper_exports_dir,
                db_path=self._control_db_path(),
            )
            try:
                result = runner.discovery_dry_run(
                    date_to=date_to,
                    date_from=date_from,
                    max_records_per_provider=max_records_per_provider,
                    run_id=run_id,
                    providers=providers,
                )
                self._write_high_recall_family_config_ref(run_id, config_dir)
            except Exception as exc:
                failure = {
                    "run_id": run_id,
                    "query_family": protocol.get("protocol_version", family_key),
                    "config_dir": str(config_dir),
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "error_message": redact(str(exc)),
                }
                failed_source_runs.append(failure)
                failure_dir = ensure_dir(self.runs_dir / run_id / "errors")
                write_json_atomic(
                    failure_dir / "high_recall_family_failure.json",
                    failure,
                )
                continue
            source_run_ids.append(run_id)
            source_runs.append(
                {
                    "run_id": run_id,
                    "query_family": protocol.get("protocol_version", family_key),
                    "config_dir": str(config_dir),
                    "status": result.get("status"),
                    "normalized_record_count": result.get("normalized_record_count"),
                    "candidate_count": result.get("raw_result_count"),
                    "source_statuses": result.get("source_statuses", {}),
                    "reused_existing_run": False,
                }
            )
            write_progress_summary("running")
        if not source_run_ids:
            summary: dict[str, Any] = {
                "pool_id": pool_id,
                "status": "failed",
                "mode": "high_recall_query_family_candidate_pool",
                "date_from": date_from,
                "date_to": date_to,
                "max_records_per_provider": max_records_per_provider,
                "providers": providers or SOURCE_NAMES,
                "source_run_ids": [],
                "source_runs": [],
                "failed_source_runs": failed_source_runs,
                "skipped_source_runs": skipped_source_runs,
                "candidate_pool": None,
                "failure_reason": "all_query_family_discovery_runs_failed",
            }
            write_json_atomic(
                summary_dir / "high_recall_build_summary.json",
                summary,
            )
            return summary
        pool_result = self.build_candidate_pool(
            source_run_ids=source_run_ids,
            pool_id=pool_id,
        )
        status = "completed" if not failed_source_runs else "partial"
        completed_summary: dict[str, Any] = {
            "pool_id": pool_id,
            "status": status,
            "mode": "high_recall_query_family_candidate_pool",
            "date_from": date_from,
            "date_to": date_to,
            "max_records_per_provider": max_records_per_provider,
            "providers": providers or SOURCE_NAMES,
            "source_run_ids": source_run_ids,
            "source_runs": source_runs,
            "failed_source_runs": failed_source_runs,
            "skipped_source_runs": skipped_source_runs,
            "candidate_pool": pool_result,
            "guardrails": [
                "This command builds a high-recall union pool, not one precision-optimized query.",
                "No GPT screening, query refinement, PDF download, or Download Specialist is run.",
                "Branch quality is evaluated later by candidate-pool screening and analysis.",
                "Completed family runs are reused on resume to avoid repeating source calls.",
            ],
        }
        write_json_atomic(
            summary_dir / "high_recall_build_summary.json",
            completed_summary,
        )
        return completed_summary

    def _write_high_recall_family_config_ref(self, run_id: str, config_dir: Path) -> None:
        write_json_atomic(
            self.runs_dir / run_id / "query_family_config_ref.json",
            {
                "run_id": run_id,
                "config_dir": str(config_dir),
                "config_name": config_dir.name,
            },
        )

    def _new_candidate_pool_id(self) -> str:
        return "retrieval_high_recall_candidate_pool_" + utc_now_iso().replace(
            ":", ""
        ).replace("-", "").replace(".", "")

    def _completed_high_recall_family_run(self, run_id: str) -> dict[str, Any] | None:
        run_dir = self.runs_dir / run_id
        manifest_path = run_dir / "manifest.json"
        candidates_path = run_dir / "external_metadata" / "Q0001" / "candidates.jsonl"
        if not manifest_path.exists() or not candidates_path.exists():
            return None
        manifest = dict(read_json(manifest_path))
        if manifest.get("run_status") != "completed":
            return None
        candidate_count = count_jsonl(candidates_path)
        run_completeness = str(manifest.get("run_completeness") or "")
        source_statuses = dict(manifest.get("source_health_status") or {})
        failed_sources = [
            provider
            for provider, status in source_statuses.items()
            if str(status).lower() == "failed"
        ]
        return {
            "candidate_count": candidate_count,
            "normalized_record_count": manifest.get("normalized_record_count", 0),
            "source_statuses": source_statuses,
            "status": "completed" if candidate_count > 0 else "completed_no_reusable_candidates",
            "reuse_status": (
                "reused_completed"
                if candidate_count > 0
                else "skipped_completed_no_reusable_candidates"
            ),
            "run_completeness": run_completeness,
            "failed_sources": failed_sources,
        }

    def _query_family_key(self, config_dir: Path, protocol: dict[str, Any]) -> str:
        raw = str(protocol.get("protocol_version") or config_dir.name)
        raw = raw.replace("0.1.0-", "")
        return re.sub(r"[^a-zA-Z0-9]+", "_", raw).strip("_").lower() or config_dir.name

    @staticmethod
    def _high_recall_family_run_id(pool_id: str, family_key: str) -> str:
        suffix = hashlib.sha1(family_key.encode("utf-8")).hexdigest()[:10]
        short_key = family_key[:56].strip("_")
        return f"{pool_id}__{short_key}_{suffix}"

    def mock_download_claim(
        self, *, worker_id: str, lease_seconds: int = 300
    ) -> dict[str, Any]:
        return self._phase11_runner().mock_download_claim(
            worker_id=worker_id, lease_seconds=lease_seconds
        )

    def mock_download_complete(
        self, *, idempotency_key: str, final_status: str = "succeeded"
    ) -> dict[str, Any]:
        return self._phase11_runner().mock_download_complete(
            idempotency_key=idempotency_key, final_status=final_status
        )

    def mock_download_fail(
        self,
        *,
        idempotency_key: str,
        retryable: bool = True,
        failure_reason: str = "mock_failure",
    ) -> dict[str, Any]:
        return self._phase11_runner().mock_download_fail(
            idempotency_key=idempotency_key,
            retryable=retryable,
            failure_reason=failure_reason,
        )

    def download_queue_status(self) -> dict[str, Any]:
        return self._phase11_runner().download_queue_status()

    def _phase11_runner(self) -> Phase11Runner:
        return Phase11Runner(
            repo_root=self.repo_root,
            config_dir=self.config_dir,
            runs_dir=self.runs_dir,
            paper_exports_dir=self.paper_exports_dir,
            db_path=self._control_db_path(),
        )

    def _search_sources_bounded(
        self,
        *,
        run_id: str,
        run_dir: Path,
        query: CanonicalQuery,
        per_source_limit: int,
        page_size: int,
    ) -> list[dict[str, Any]]:
        fixture_path = self.repo_root / "tests" / "fixtures" / "mock_records.json"
        summary: list[dict[str, Any]] = []
        raw_jsonl = run_dir / "raw_metadata" / "raw_records.jsonl"
        for source in SOURCE_NAMES:
            adapter = MockSourceAdapter(source, fixture_path)
            scanned = 0
            raw_total = 0
            status = SourceExecutionStatus.SOURCE_NO_RESULTS
            for page in adapter.iter_pages(
                query, page_size=page_size, max_records=per_source_limit
            ):
                status = page.status
                raw_total = page.raw_result_count
                batch_id = f"{source}_page_{page.page_id:04d}"
                batch_path = run_dir / "raw_metadata" / "batches" / f"{batch_id}.jsonl"
                relative_batch_path = batch_path.relative_to(self.repo_root).as_posix()
                with MemoryTelemetry(
                    run_dir=run_dir,
                    run_id=run_id,
                    query_id=query.query_id,
                    iteration=query.iteration,
                    operator="SEARCH_SOURCES",
                    batch_id=batch_id,
                ) as telemetry:
                    batch_rows = [record.to_dict() for record in page.records]
                    for row in batch_rows:
                        if isinstance(row.get("raw"), dict):
                            row["raw"]["raw_metadata_path"] = relative_batch_path
                    for row in batch_rows:
                        append_jsonl(raw_jsonl, row)
                    write_text_atomic(
                        batch_path,
                        "".join(
                            json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n"
                            for row in batch_rows
                        ),
                    )
                    telemetry.add_records(len(batch_rows))
                    telemetry.add_bytes(batch_path.stat().st_size)
                scanned += len(page.records)
            summary.append(
                {
                    "source_name": source,
                    "status": status.value,
                    "raw_result_count": raw_total,
                    "scanned_result_count": scanned,
                    "warnings": [],
                }
            )
        return summary

    def _normalize_bounded(self, *, run_id: str, run_dir: Path, batch_size: int) -> int:
        normalizer = MetadataNormalizer()
        normalized_jsonl = run_dir / "normalized" / "records.jsonl"
        total = 0
        batch: list[dict[str, Any]] = []
        batch_id = 1
        for row in read_jsonl(run_dir / "raw_metadata" / "raw_records.jsonl"):
            batch.append(row)
            if len(batch) >= batch_size:
                total += self._persist_normalized_batch(
                    run_id, run_dir, normalizer, batch, batch_id, normalized_jsonl
                )
                batch = []
                batch_id += 1
        if batch:
            total += self._persist_normalized_batch(
                run_id, run_dir, normalizer, batch, batch_id, normalized_jsonl
            )
        write_json_atomic(
            run_dir / "normalized" / "records_summary.json",
            {
                "record_count": total,
                "canonical_stream": str(normalized_jsonl.relative_to(run_dir)),
            },
        )
        return total

    def _persist_normalized_batch(
        self,
        run_id: str,
        run_dir: Path,
        normalizer: MetadataNormalizer,
        rows: list[dict[str, Any]],
        batch_id: int,
        normalized_jsonl: Path,
    ) -> int:
        batch_name = f"normalization_batch_{batch_id:04d}"
        with MemoryTelemetry(
            run_dir=run_dir,
            run_id=run_id,
            query_id="Q0001",
            iteration=1,
            operator="NORMALIZE",
            batch_id=batch_name,
        ) as telemetry:
            normalized = []
            for row in rows:
                raw_record = RawRecord(**row)
                raw_metadata_path = raw_record.raw.get(
                    "raw_metadata_path",
                    f"runs/{run_id}/raw_metadata/raw_records.jsonl",
                )
                normalized_record = normalizer.normalize(
                    raw_record,
                    str(raw_metadata_path),
                )
                normalized.append(normalized_record.to_dict())
                append_jsonl(normalized_jsonl, normalized_record.to_dict())
            batch_path = run_dir / "normalized" / "batches" / f"{batch_name}.jsonl"
            write_text_atomic(
                batch_path,
                "".join(
                    json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n" for row in normalized
                ),
            )
            telemetry.add_records(len(rows))
            telemetry.add_bytes(batch_path.stat().st_size)
        return len(rows)

    def _deduplicate_persisted_records(self, run_dir: Path) -> DeduplicationResult:
        records = [
            NormalizedRecord(**row) for row in read_jsonl(run_dir / "normalized" / "records.jsonl")
        ]
        deduplication = Deduplicator().deduplicate(records)
        write_json_atomic(run_dir / "deduplication" / "deduplication.json", deduplication.to_dict())
        for record in deduplication.records:
            append_jsonl(run_dir / "deduplication" / "deduplicated_records.jsonl", record.to_dict())
        for candidate in deduplication.candidate_duplicates:
            append_jsonl(run_dir / "deduplication" / "candidate_duplicates.jsonl", candidate)
        return deduplication

    def _screen_persisted_records(
        self,
        *,
        run_id: str,
        run_dir: Path,
        records: list[NormalizedRecord],
        batch_size: int,
    ) -> list[ScreeningDecision]:
        screener = TitleAbstractScreener()
        decisions: list[ScreeningDecision] = []
        for index in range(0, len(records), batch_size):
            batch = records[index : index + batch_size]
            batch_name = f"screening_batch_{index // batch_size + 1:04d}"
            with MemoryTelemetry(
                run_dir=run_dir,
                run_id=run_id,
                query_id="Q0001",
                iteration=1,
                operator="SCREEN_PASS_2",
                batch_id=batch_name,
            ) as telemetry:
                batch_decisions = screener.screen(batch)
                for decision in batch_decisions:
                    append_jsonl(
                        run_dir / "screening" / "screening_decisions.jsonl", decision.to_dict()
                    )
                    append_jsonl(run_dir / "logs" / "screening_decisions.jsonl", decision.to_dict())
                batch_path = run_dir / "screening" / "batches" / f"{batch_name}.jsonl"
                write_text_atomic(
                    batch_path,
                    "".join(
                        json.dumps(decision.to_dict(), ensure_ascii=True, sort_keys=True) + "\n"
                        for decision in batch_decisions
                    ),
                )
                telemetry.add_records(len(batch))
                telemetry.add_bytes(batch_path.stat().st_size)
                decisions.extend(batch_decisions)
        write_json_atomic(
            run_dir / "screening" / "screening_decisions.json",
            [decision.to_dict() for decision in decisions],
        )
        return decisions

    def _emit_download_handoffs(
        self,
        *,
        run_id: str,
        run_dir: Path,
        query_id: str,
        iteration: int,
        records: list[NormalizedRecord],
        decisions: list[ScreeningDecision],
        handoff_config: dict[str, Any],
        scie_status: str,
    ) -> HandoffResult:
        outbox = DownloadHandoffOutbox(self.repo_root, run_dir, handoff_config)
        with MemoryTelemetry(
            run_dir=run_dir,
            run_id=run_id,
            query_id=query_id,
            iteration=iteration,
            operator="EMIT_DOWNLOAD_HANDOFF",
            batch_id="handoff_batch_0001",
        ) as telemetry:
            result = outbox.emit_for_batch(
                run_id=run_id,
                iteration=iteration,
                query_id=query_id,
                records=records,
                decisions=decisions,
                scie_status=scie_status,
                screening_evidence_path=f"runs/{run_id}/screening/screening_decisions.jsonl",
            )
            telemetry.add_records(result.emitted + result.duplicate_suppressed)
            telemetry.add_bytes(
                (run_dir / "handoff" / "download" / "download_events.jsonl").stat().st_size
            )
        return result

    def _verify_handoff(
        self, run_dir: Path, decisions: list[ScreeningDecision], handoff_result: HandoffResult
    ) -> None:
        include_count = sum(1 for decision in decisions if decision.decision == "include")
        if (
            handoff_result.emitted + handoff_result.duplicate_suppressed + handoff_result.failures
            != include_count
        ):
            raise RuntimeError(
                "Handoff verification failed: include decisions do not match handoff outcomes"
            )
        for event in read_jsonl(run_dir / "handoff" / "download" / "download_events.jsonl"):
            if event.get("event_type") == "DOWNLOAD_REQUESTED" and not event.get(
                "payload_checksum"
            ):
                raise RuntimeError("Handoff verification failed: missing payload checksum")

    def _write_query_decision(
        self, query_dir: Path, run_id: str, query: CanonicalQuery, metrics: QueryMetrics
    ) -> None:
        write_json_atomic(
            query_dir / "decision.json",
            {
                "query_id": query.query_id,
                "decision": "accept",
                "decision_reason": "Initial Phase 1 baseline query.",
                "timestamp": utc_now_iso(),
                "decided_by": "Codex",
            },
        )
        write_json_atomic(
            query_dir / "query_change.json",
            {
                "run_id": run_id,
                "iteration": query.iteration,
                "query_id": query.query_id,
                "parent_query_id": query.parent_query_id,
                "added_terms": query.added_terms,
                "removed_terms": query.removed_terms,
                "replaced_terms": [],
                "modified_concept_blocks": query.modified_concept_blocks,
                "change_rationale": query.change_rationale,
                "evidence_for_change": query.evidence_for_change,
                "triggering_metrics": {},
                "expected_effect": query.expected_effect,
                "observed_effect": {"total_score": metrics.total_score},
                "score_before": 0.0,
                "score_after": metrics.total_score,
                "metric_deltas": {},
                "decision": "accept",
                "decision_reason": "Initial query accepted.",
                "proposed_by": "Retrieval Specialist",
                "evaluated_by": "Codex",
                "timestamp": utc_now_iso(),
            },
        )

    def _write_exports(
        self,
        query: CanonicalQuery,
        metrics: QueryMetrics,
        decisions: list[ScreeningDecision],
        source_summary: list[dict[str, Any]],
        run_dir: Path,
    ) -> None:
        for export_dir in [self.paper_exports_dir, run_dir / "exports"]:
            Exporter(export_dir).export_iteration(query, metrics, decisions, source_summary)
            self._write_resource_trajectory(export_dir, run_dir)
            self._write_handoff_trajectory(export_dir, metrics)

    def _write_resource_trajectory(self, export_dir: Path, run_dir: Path) -> None:
        rows: list[dict[str, Any]] = []
        for row in read_jsonl(run_dir / "logs" / "memory_usage.jsonl"):
            rows.append(row)
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
                "resident_memory_before_mb",
                "resident_memory_peak_mb",
                "resident_memory_after_cleanup_mb",
                "bytes_written",
                "duration_seconds",
                "cleanup_performed",
                "timestamp",
            ],
        )

    def _write_handoff_trajectory(self, export_dir: Path, metrics: QueryMetrics) -> None:
        write_csv_atomic(
            export_dir / "download_handoff_trajectory.csv",
            [
                {
                    "run_id": metrics.run_id,
                    "iteration": metrics.iteration,
                    "query_id": metrics.query_id,
                    "newly_included_records": metrics.include_count,
                    "download_requests_emitted": metrics.download_requests_emitted,
                    "duplicate_handoffs_suppressed": metrics.duplicate_handoffs_suppressed,
                    "handoff_failures": 0,
                    "pending_download_jobs": metrics.pending_download_jobs_at_iteration_end,
                    "claimed_download_jobs": 0,
                    "successful_downloads_observed": 0,
                    "skipped_existing_observed": 0,
                    "retryable_download_failures_observed": 0,
                    "terminal_download_failures_observed": 0,
                    "dead_letter_jobs": 0,
                    "handoff_backpressure_status": metrics.handoff_backpressure_status,
                    "handoff_latency_seconds": "",
                    "timestamp": metrics.timestamp,
                }
            ],
            [
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
        )

    def _finalize_iteration(
        self, *, run_dir: Path, query_id: str, expected_counts: dict[str, int]
    ) -> dict[str, Any]:
        artifacts = {
            "raw_records": run_dir / "raw_metadata" / "raw_records.jsonl",
            "normalized_records": run_dir / "normalized" / "records.jsonl",
            "deduplicated_records": run_dir / "deduplication" / "deduplicated_records.jsonl",
            "screening_decisions": run_dir / "screening" / "screening_decisions.jsonl",
            "download_events": run_dir / "handoff" / "download" / "download_events.jsonl",
        }
        counts: dict[str, int] = {}
        checksums: dict[str, str] = {}
        for name, path in artifacts.items():
            if not path.exists():
                raise RuntimeError(f"Cannot finalize iteration; missing artifact: {path}")
            counts[name] = count_jsonl(path)
            checksums[name] = sha256_file(path)
        for name, expected in expected_counts.items():
            if name == "download_events":
                if counts[name] < expected:
                    raise RuntimeError(
                        f"Cannot finalize iteration; {name} has fewer rows than expected"
                    )
            elif counts[name] != expected:
                raise RuntimeError(f"Cannot finalize iteration; {name} count mismatch")
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

    def _new_run_id(self) -> str:
        seed = f"{utc_now_iso()}|{self._git_sha()}".encode()
        short_hash = hashlib.sha256(seed).hexdigest()[:8]
        timestamp = utc_now_iso().replace("-", "").replace(":", "")
        return f"retrieval_{timestamp}_{short_hash}"

    def _prepare_run_dir(self, run_id: str) -> Path:
        run_dir = ensure_dir(self.runs_dir / run_id)
        for child in [
            "queries",
            "logs",
            "raw_metadata",
            "raw_metadata/batches",
            "normalized",
            "normalized/batches",
            "deduplication",
            "screening",
            "screening/batches",
            "metrics",
            "exports",
            "errors",
            "checksums",
            "handoff/download",
            "registry",
        ]:
            ensure_dir(run_dir / child)
        for jsonl in [
            run_dir / "raw_metadata" / "raw_records.jsonl",
            run_dir / "normalized" / "records.jsonl",
            run_dir / "deduplication" / "deduplicated_records.jsonl",
            run_dir / "screening" / "screening_decisions.jsonl",
            run_dir / "handoff" / "download" / "download_events.jsonl",
            run_dir / "handoff" / "download" / "handoff_status.jsonl",
            run_dir / "logs" / "memory_usage.jsonl",
        ]:
            if not jsonl.exists():
                write_text_atomic(jsonl, "")
        return run_dir

    def _write_snapshots(self, run_dir: Path, loaded: Any) -> None:
        write_yaml_atomic(run_dir / "protocol_snapshot.yaml", loaded.protocol)
        write_yaml_atomic(run_dir / "scoring_snapshot.yaml", loaded.scoring)
        write_yaml_atomic(run_dir / "model_snapshot.yaml", loaded.model)
        write_yaml_atomic(run_dir / "source_snapshot.yaml", loaded.sources)
        write_yaml_atomic(run_dir / "runtime_snapshot.yaml", loaded.runtime)

    def _manifest(
        self, run_id: str, date_to: str, config_hash: str, git_snapshot: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "start_time": utc_now_iso(),
            "end_time": None,
            "date_from": "2006-01-01",
            "date_to": date_to,
            "code_commit_sha": git_snapshot["code_commit_sha"],
            "git_branch": git_snapshot["git_branch"],
            "dirty_worktree_status": git_snapshot["dirty_worktree_status"],
            "python_version": sys.version,
            "platform": platform.platform(),
            "dependency_lock_hash": self._lock_hash(),
            "protocol_version": "0.1.0",
            "protocol_hash": config_hash,
            "scoring_version": "0.1.0",
            "scoring_hash": config_hash,
            "prompt_hashes": self._prompt_hash(),
            "model_names": [],
            "model_parameters": {"llm_enabled": False},
            "random_seed": 1226,
            "adapter_versions": {
                "mock": "0.1.0",
                "external_metadata_discovery_v1": "file_based_skill_boundary",
            },
            "schema_versions": {"retrieval": "0.1.0", "handoff": "0.1.0"},
            "SCIE_registry_version": self._scie_registry_hash(),
            "document_registry_version": "0.1.0",
            "source_health_status": self.health_check(),
            "run_completeness": "unknown",
            "run_status": "running",
        }

    def _write_run_summary(self, run_dir: Path, query_id: str, metrics: QueryMetrics) -> None:
        write_text_atomic(
            run_dir / "RUN_SUMMARY.md",
            "\n".join(
                [
                    "# Retrieval Specialist Run Summary",
                    "",
                    f"- run_id: {metrics.run_id}",
                    "- run status: completed",
                    f"- final accepted query: {query_id}",
                    f"- final score: {metrics.total_score:.4f}",
                    f"- novel eligible documents: {metrics.novel_eligible_yield}",
                    f"- download requests emitted: {metrics.download_requests_emitted}",
                    f"- source completeness: {metrics.source_completeness}",
                    f"- saturation status: {metrics.saturation_status}",
                    f"- handoff backpressure: {metrics.handoff_backpressure_status}",
                    "",
                ]
            ),
        )

    def _update_status_files(self, run_id: str, query_id: str, metrics: QueryMetrics) -> None:
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
                    f"| code commit SHA | {metrics.code_commit_sha} |",
                    f"| final accepted query | {query_id} |",
                    f"| number of iterations | {metrics.iteration} |",
                    "| initial score | not_applicable |",
                    f"| final score | {metrics.total_score:.4f} |",
                    f"| novel eligible documents | {metrics.novel_eligible_yield} |",
                    f"| download requests emitted | {metrics.download_requests_emitted} |",
                    f"| source completeness | {metrics.source_completeness} |",
                    f"| saturation status | {metrics.saturation_status} |",
                    "| main exclusion reasons | see paper_exports/exclusion_reason_evolution.csv |",
                    "| major anomalies | none in mock run |",
                    f"| output directory | runs/{run_id} |",
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
                    "Live Retrieval Specialist integration.",
                    "",
                    "## Completed Modules",
                    "",
                    "- Phase 0 clean repository baseline.",
                    "- Configuration loader with runtime batch settings.",
                    "- Canonical query planner and compiler scaffold.",
                    "- Mock source adapters with paginated batch execution.",
                    "- Metadata normalization and provenance-aware deduplication scaffold.",
                    "- Rule-backed title/abstract screening scaffold.",
                    "- Explicit state machine, checkpoints, resume, and rollback.",
                    "- Durable Download Specialist handoff outbox scaffold.",
                    "- Memory telemetry and paper export CSV/Markdown scaffold.",
                    "- ECfinder `external_metadata_discovery_v1` runner boundary for "
                    "live metadata discovery.",
                    "",
                    "## Incomplete Modules",
                    "",
                    "- Strict four-source formal retrieval requires "
                    "`SEMANTIC_SCHOLAR_API_KEY`; degraded three-source retrieval "
                    "requires explicit acknowledgement.",
                    "- PDF acquisition by Download Specialist.",
                    "- Full paper-export data dictionaries.",
                    "- Formal unrestricted retrieval run.",
                    "",
                    "## Known Issues",
                    "",
                    "- SCIE registry remains a header-only placeholder; `scie_status = unknown`.",
                    "- Live metadata discovery uses ECfinder as the provider boundary; "
                    "ECMonitor must not import provider adapter modules.",
                    "",
                    "## Latest Test Status",
                    "",
                    "See the latest pushed live-integration validation report.",
                    "",
                    "## Latest Runtime Status",
                    "",
                    f"Latest mock run completed: `runs/{run_id}`.",
                    "",
                    "## Next Step",
                    "",
                    "Configure `SEMANTIC_SCHOLAR_API_KEY` before strict four-source "
                    "formal retrieval, or run explicit degraded validation only.",
                    "",
                ]
            ),
        )

    def _runtime_settings(self, runtime_config: dict[str, Any]) -> dict[str, Any]:
        return dict(runtime_config.get("runtime", {}))

    def _handoff_settings(self, runtime_config: dict[str, Any]) -> dict[str, Any]:
        return dict(runtime_config.get("handoff", {}))

    def _per_source_limit(self, stopping: dict[str, Any], target_novel_records: int | None) -> int:
        if target_novel_records is not None:
            return max(1, target_novel_records // len(SOURCE_NAMES))
        return int(stopping["evaluation"]["target_novel_records_per_source"])

    def _source_completeness_from_summary(self, source_summary: list[dict[str, Any]]) -> str:
        statuses = [str(result["status"]) for result in source_summary]
        if all(status == SourceExecutionStatus.SOURCE_SUCCESS.value for status in statuses):
            return "complete"
        if any(status == SourceExecutionStatus.SOURCE_SUCCESS.value for status in statuses):
            return "partial"
        return "failed"

    def _enrich_source_summary(
        self,
        source_summary: list[dict[str, Any]],
        records: list[NormalizedRecord],
        decisions: list[ScreeningDecision],
    ) -> list[dict[str, Any]]:
        decision_by_id = {decision.global_record_id: decision for decision in decisions}
        enriched = []
        for summary in source_summary:
            source = summary["source_name"]
            source_records = [record for record in records if source in record.retrieved_from]
            source_ids = {record.global_record_id for record in source_records}
            source_decisions = [
                decision_by_id[record_id] for record_id in source_ids if record_id in decision_by_id
            ]
            enriched.append(
                summary
                | {
                    "novel_eligible_records": sum(
                        1 for decision in source_decisions if decision.decision == "include"
                    ),
                    "duplicate_records": 0,
                    "missing_abstracts": sum(
                        1 for record in source_records if not record.abstract_original
                    ),
                    "deferred_records": sum(
                        1 for decision in source_decisions if decision.decision.startswith("defer")
                    ),
                }
            )
        return enriched

    def _scie_status(self, protocol: dict[str, Any]) -> str:
        registry_path = self.repo_root / str(protocol["scie"]["registry_path"])
        if not registry_path.exists():
            return "unknown"
        content = registry_path.read_text(encoding="utf-8").strip().splitlines()
        return "available" if len(content) > 1 else "unknown"

    def _git_sha(self) -> str:
        return self._git(["rev-parse", "HEAD"], default="unknown")

    def _git_branch(self) -> str:
        return self._git(["branch", "--show-current"], default="unknown")

    def _git_dirty_status(self) -> str:
        status = self._git(["status", "--porcelain"], default="")
        return "dirty" if status.strip() else "clean"

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

    def _lock_hash(self) -> str:
        lock_path = self.repo_root / "uv.lock"
        if not lock_path.exists():
            return "not_available"
        return hashlib.sha256(lock_path.read_bytes()).hexdigest()

    def _prompt_hash(self) -> str:
        prompt_dir = self.repo_root / "prompts" / "retrieval"
        digest = hashlib.sha256()
        for path in sorted(prompt_dir.glob("*.md")):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _scie_registry_hash(self) -> str:
        path = self.repo_root / "registry" / "scie_journals.csv"
        if not path.exists():
            return "unknown"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _control_db_path(self) -> Path:
        override = os.environ.get("ECMONITOR_CONTROL_DB_PATH", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        return self.repo_root / "state" / "ecmonitor_control.sqlite3"
