"""File-based GPT title/abstract screening workflow.

The Retrieval Specialist harness uses durable one-document requests and
one-document results for live title/abstract screening. Each request is handled
as a fresh ``TitleAbstractScreeningWorker`` conversation so document context is
not reused across records.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from ecmonitor.retrieval_specialist.models import NormalizedRecord, ScreeningDecision
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.storage.atomic_io import (
    ensure_dir,
    read_json,
    write_json_atomic,
)

PROMPT_VERSION = "title-abstract-screening-worker-v1.0"
MODEL_NAME = "codex-gpt"
MODEL_VERSION = "codex-current"


class ScreeningWorkerBlocked(RuntimeError):
    """Raised when live screening needs external Codex worker results."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("TitleAbstractScreeningWorker result is required.")
        self.payload = payload


class TitleAbstractScreeningWorkerExecutor:
    """Reads/writes isolated one-document GPT screening requests and results."""

    def __init__(
        self,
        *,
        repo_root: Path,
        run_dir: Path,
        schema_path: Path,
        max_schema_retries: int = 1,
    ) -> None:
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.schema_path = schema_path
        self.max_schema_retries = max_schema_retries
        self.schema = read_json(schema_path)

    def screen_one(
        self,
        record: NormalizedRecord,
        *,
        run_id: str,
        query_id: str,
        iteration: int,
        audit_batch_id: str,
        protocol: dict[str, Any],
        allowed_reason_codes: dict[str, Any],
        scie_status: str,
    ) -> ScreeningDecision:
        request_ref = self._request_path(query_id, record.global_record_id)
        result_ref = self._result_path(query_id, record.global_record_id)
        raw_ref = self._raw_response_path(query_id, record.global_record_id)
        if not result_ref.exists():
            self._write_request(
                request_ref=request_ref,
                record=record,
                run_id=run_id,
                query_id=query_id,
                iteration=iteration,
                audit_batch_id=audit_batch_id,
                protocol=protocol,
                allowed_reason_codes=allowed_reason_codes,
                scie_status=scie_status,
                attempt=1,
                previous_error=None,
            )
            raise ScreeningWorkerBlocked(
                {
                    "status": "paused_screening_worker_required",
                    "worker_name": "TitleAbstractScreeningWorker",
                    "request_ref": self._display_path(request_ref),
                    "result_ref": self._display_path(result_ref),
                    "isolation": "one_document_per_conversation",
                    "global_record_id": record.global_record_id,
                }
            )

        try:
            raw_payload = self._read_result_payload(result_ref)
        except json.JSONDecodeError as exc:
            ensure_dir(raw_ref.parent)
            raw_ref.write_text(
                result_ref.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
            retry_count = self._increment_invalid_counter(
                query_id,
                record.global_record_id,
                {"result_ref": self._display_path(result_ref)},
                str(exc),
            )
            if retry_count <= self.max_schema_retries:
                self._write_request(
                    request_ref=request_ref,
                    record=record,
                    run_id=run_id,
                    query_id=query_id,
                    iteration=iteration,
                    audit_batch_id=audit_batch_id,
                    protocol=protocol,
                    allowed_reason_codes=allowed_reason_codes,
                    scie_status=scie_status,
                    attempt=retry_count + 1,
                    previous_error=str(exc),
                )
                result_ref.unlink(missing_ok=True)
                raise ScreeningWorkerBlocked(
                    {
                        "status": "paused_screening_worker_retry_required",
                        "worker_name": "TitleAbstractScreeningWorker",
                        "request_ref": self._display_path(request_ref),
                        "result_ref": self._display_path(result_ref),
                        "schema_error": str(exc),
                        "global_record_id": record.global_record_id,
                    }
                ) from exc
            decision_payload = self._safe_defer_payload(
                record=record,
                run_id=run_id,
                query_id=query_id,
                iteration=iteration,
                audit_batch_id=audit_batch_id,
                scie_status=scie_status,
                raw_ref=raw_ref,
                schema_error=str(exc),
            )
            validate(instance=decision_payload, schema=self.schema)
            return ScreeningDecision(**decision_payload)
        write_json_atomic(raw_ref, raw_payload)
        decision_payload = self._decision_payload(raw_payload)
        decision_payload = decision_payload | {
            "screening_schema_version": "1.1.0",
            "screening_decision_id": self._decision_id(
                run_id, query_id, iteration, record.global_record_id, decision_payload
            ),
            "global_record_id": record.global_record_id,
            "run_id": run_id,
            "query_id": query_id,
            "iteration": iteration,
            "screening_pass": "SCREEN_PASS_2",
            "prompt_version": PROMPT_VERSION,
            "prompt_hash": self.prompt_hash(),
            "model_name": str(raw_payload.get("model_name") or MODEL_NAME),
            "model_version": str(raw_payload.get("model_version") or MODEL_VERSION),
            "model_parameters": {"temperature": 0, "structured_output": True},
            "raw_model_response_path": self._display_path(raw_ref),
            "screening_timestamp": utc_now_iso(),
            "decision_actor": "TitleAbstractScreeningWorker",
            "audit_status": "not_audited",
            "audit_sampled": False,
            "audit_batch_id": audit_batch_id,
            "manager_decision": None,
            "manager_reason": None,
            "screening_disagreement": False,
            "original_agent_decision": None,
            "override_decision": None,
            "override_actor": None,
            "override_reason": None,
            "override_timestamp": None,
            "article_ec_scope": str(decision_payload.get("article_ec_scope") or "uncertain"),
            "screening_version": "live-gpt-1.0",
        }
        try:
            validate(instance=decision_payload, schema=self.schema)
        except ValidationError as exc:
            retry_count = self._increment_invalid_counter(
                query_id, record.global_record_id, raw_payload, str(exc)
            )
            if retry_count <= self.max_schema_retries:
                self._write_request(
                    request_ref=request_ref,
                    record=record,
                    run_id=run_id,
                    query_id=query_id,
                    iteration=iteration,
                    audit_batch_id=audit_batch_id,
                    protocol=protocol,
                    allowed_reason_codes=allowed_reason_codes,
                    scie_status=scie_status,
                    attempt=retry_count + 1,
                    previous_error=str(exc),
                )
                result_ref.unlink(missing_ok=True)
                raise ScreeningWorkerBlocked(
                    {
                        "status": "paused_screening_worker_retry_required",
                        "worker_name": "TitleAbstractScreeningWorker",
                        "request_ref": self._display_path(request_ref),
                        "result_ref": self._display_path(result_ref),
                        "schema_error": str(exc),
                        "global_record_id": record.global_record_id,
                    }
                ) from exc
            decision_payload = self._safe_defer_payload(
                record=record,
                run_id=run_id,
                query_id=query_id,
                iteration=iteration,
                audit_batch_id=audit_batch_id,
                scie_status=scie_status,
                raw_ref=raw_ref,
                schema_error=str(exc),
            )
            validate(instance=decision_payload, schema=self.schema)
        return ScreeningDecision(**decision_payload)

    def _read_result_payload(self, result_ref: Path) -> dict[str, Any]:
        payload = json.loads(result_ref.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise json.JSONDecodeError("Result payload must be a JSON object", "", 0)
        return payload

    def prompt_hash(self) -> str:
        prompt = (
            "TitleAbstractScreeningWorker|temperature=0|structured_output|"
            "one_document_per_conversation|schema=screening_decision.schema.json"
        )
        return hashlib.sha256(prompt.encode()).hexdigest()

    def _write_request(
        self,
        *,
        request_ref: Path,
        record: NormalizedRecord,
        run_id: str,
        query_id: str,
        iteration: int,
        audit_batch_id: str,
        protocol: dict[str, Any],
        allowed_reason_codes: dict[str, Any],
        scie_status: str,
        attempt: int,
        previous_error: str | None,
    ) -> None:
        ensure_dir(request_ref.parent)
        write_json_atomic(
            request_ref,
            {
                "worker_name": "TitleAbstractScreeningWorker",
                "isolation": "one_document_per_conversation",
                "instruction": (
                    "Open a fresh conversation for this one document only. Return one "
                    "JSON object conforming to schemas/retrieval/"
                    "screening_decision.schema.json. Do not use PDF-only facts. "
                    "Use a high-recall but primary-data screening posture: include "
                    "records only when the title/abstract indicates direct natural "
                    "surface-water occurrence, monitoring, field sampling, abundance, "
                    "or concentration evidence for emerging contaminants. Do not include "
                    "reviews, meta-analyses, critical assessments, bibliometric papers, "
                    "policy papers, method-only papers, removal-strategy papers, or "
                    "risk-assessment-only papers unless the title/abstract also shows "
                    "new direct ambient surface-water measurements. Mixed matrices are "
                    "not automatically excluded if surface-water data appear separately "
                    "extractable; mark these with "
                    "I_EXTRACTABLE_SURFACE_WATER_IN_MIXED_MATRIX when clear, or "
                    "defer_metadata with D_AMBIGUOUS_SURFACE_WATER_EXTRACTABILITY when "
                    "not clear. Exclude when title/abstract clearly lacks direct "
                    "natural surface-water evidence, is review/assessment/method-only, "
                    "is treatment/lab/model-only, or is an excluded publication type. "
                    "While deciding, extract query_term_evidence terms that are useful "
                    "for natural-water emerging-contaminant retrieval: pollutant class, "
                    "natural waterbody, field monitoring, occurrence, sampling, or "
                    "concentration phrases. For repeated off-scope signals such as review, "
                    "meta-analysis, critical assessment, bibliometric analysis, correction, "
                    "fish-only, biota-only, sediment-only, treatment-only, removal-only, "
                    "or method-only, use the exclusion_candidate_terms concept block. "
                    "Do not return generic words or terms already unsupported by the "
                    "title/abstract/keywords."
                ),
                "attempt": attempt,
                "previous_schema_error": previous_error,
                "run_id": run_id,
                "query_id": query_id,
                "iteration": iteration,
                "audit_batch_id": audit_batch_id,
                "scie_status": scie_status,
                "allowed_reason_codes": allowed_reason_codes,
                "protocol": protocol,
                "document": {
                    "global_record_id": record.global_record_id,
                    "title": record.title_original,
                    "abstract": record.abstract_original,
                    "keywords": record.keywords,
                    "doi": record.normalized_doi,
                    "pmid": record.pmid,
                    "openalex_id": record.openalex_id,
                    "semantic_scholar_id": record.semantic_scholar_id,
                    "journal_title": record.journal_title,
                    "publication_year": record.publication_year,
                    "document_type": record.document_type,
                    "retrieved_from": record.retrieved_from,
                },
                "decision_policy": {
                    "clear_match": "include",
                    "clear_mismatch": "exclude",
                    "missing_or_ambiguous_metadata": "defer_metadata",
                    "unresolved_after_enrichment": "defer_not_downloaded",
                },
                "result_ref": self._display_path(
                    self._result_path(query_id, record.global_record_id)
                ),
            },
        )

    def _safe_defer_payload(
        self,
        *,
        record: NormalizedRecord,
        run_id: str,
        query_id: str,
        iteration: int,
        audit_batch_id: str,
        scie_status: str,
        raw_ref: Path,
        schema_error: str,
    ) -> dict[str, Any]:
        evidence = [record.title_original]
        if record.abstract_original:
            evidence.append(record.abstract_original[:500])
        evidence.append(f"schema_validation_error={schema_error[:300]}")
        return {
            "screening_schema_version": "1.1.0",
            "screening_decision_id": self._decision_id(
                run_id,
                query_id,
                iteration,
                record.global_record_id,
                {"decision": "defer_metadata", "reason_codes": ["D_METADATA_MISSING"]},
            ),
            "global_record_id": record.global_record_id,
            "run_id": run_id,
            "query_id": query_id,
            "iteration": iteration,
            "screening_pass": "SCREEN_PASS_2",
            "decision": "defer_metadata",
            "confidence": 0.0,
            "article_type_ok": None,
            "date_ok": None,
            "scie_status": scie_status,
            "emerging_contaminant_context": None,
            "surface_water_sample": None,
            "included_waterbody_types": [],
            "excluded_sample_matrices_present": False,
            "excluded_sample_matrices": [],
            "water_treatment_plant_samples_present": False,
            "mixed_eligible_ineligible_matrices": False,
            "field_environmental_samples": None,
            "concentration_evidence": "likely_but_not_explicit",
            "study_type": None,
            "reason_codes": ["D_METADATA_MISSING"],
            "evidence_spans": evidence,
            "query_term_evidence": [],
            "prompt_version": PROMPT_VERSION,
            "prompt_hash": self.prompt_hash(),
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "model_parameters": {"temperature": 0, "structured_output": True},
            "raw_model_response_path": self._display_path(raw_ref),
            "screening_timestamp": utc_now_iso(),
            "decision_actor": "TitleAbstractScreeningWorker",
            "audit_status": "not_audited",
            "audit_sampled": False,
            "audit_batch_id": audit_batch_id,
            "manager_decision": None,
            "manager_reason": None,
            "screening_disagreement": False,
            "original_agent_decision": None,
            "override_decision": None,
            "override_actor": None,
            "override_reason": None,
            "override_timestamp": None,
            "article_ec_scope": "uncertain",
            "screening_version": "live-gpt-1.0",
        }

    def _decision_payload(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(raw_payload.get("screening_decision") or raw_payload)
        concentration_evidence = str(payload.get("concentration_evidence") or "")
        if concentration_evidence == "present":
            payload["concentration_evidence"] = "explicit_quantified"
        elif concentration_evidence in {"uncertain", "unknown", "not_clear"}:
            payload["concentration_evidence"] = "likely_but_not_explicit"
        defaults: dict[str, Any] = {
            "confidence": None,
            "article_type_ok": None,
            "date_ok": None,
            "scie_status": "unknown",
            "emerging_contaminant_context": None,
            "surface_water_sample": None,
            "included_waterbody_types": [],
            "excluded_sample_matrices_present": False,
            "excluded_sample_matrices": [],
            "water_treatment_plant_samples_present": False,
            "mixed_eligible_ineligible_matrices": False,
            "field_environmental_samples": None,
            "concentration_evidence": "likely_but_not_explicit",
            "study_type": None,
            "reason_codes": [],
            "evidence_spans": [],
            "query_term_evidence": [],
            "article_ec_scope": "uncertain",
        }
        return defaults | payload

    def _increment_invalid_counter(
        self, query_id: str, global_record_id: str, payload: Any, error: str
    ) -> int:
        path = self._invalid_path(query_id, global_record_id)
        rows: list[dict[str, Any]] = []
        if path.exists():
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        rows.append({"timestamp": utc_now_iso(), "payload": payload, "error": error})
        ensure_dir(path.parent)
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=True, sort_keys=True) for row in rows)
            + "\n",
            encoding="utf-8",
        )
        return len(rows)

    def _request_path(self, query_id: str, global_record_id: str) -> Path:
        return (
            self.run_dir
            / "screening"
            / "worker_requests"
            / query_id
            / f"{self._safe_id(global_record_id)}.json"
        )

    def _result_path(self, query_id: str, global_record_id: str) -> Path:
        return (
            self.run_dir
            / "screening"
            / "worker_results"
            / query_id
            / f"{self._safe_id(global_record_id)}.json"
        )

    def _raw_response_path(self, query_id: str, global_record_id: str) -> Path:
        return (
            self.run_dir
            / "screening"
            / "raw_responses"
            / query_id
            / f"{self._safe_id(global_record_id)}.json"
        )

    def _invalid_path(self, query_id: str, global_record_id: str) -> Path:
        return (
            self.run_dir
            / "screening"
            / "invalid_responses"
            / query_id
            / f"{self._safe_id(global_record_id)}.jsonl"
        )

    def _safe_id(self, value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:24]

    def _decision_id(
        self,
        run_id: str,
        query_id: str,
        iteration: int,
        global_record_id: str,
        payload: dict[str, Any],
    ) -> str:
        key = (
            f"{run_id}|{query_id}|{iteration}|{global_record_id}|"
            f"{json.dumps(payload, ensure_ascii=True, sort_keys=True)}"
        )
        return f"screen_{hashlib.sha256(key.encode()).hexdigest()[:24]}"

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.repo_root.resolve()))
        except ValueError:
            return str(path)
