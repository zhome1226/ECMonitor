"""Durable asynchronous handoff from Retrieval Specialist to Download Specialist."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecmonitor.retrieval_specialist.models import NormalizedRecord, ScreeningDecision
from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.storage.atomic_io import (
    append_jsonl,
    ensure_dir,
    read_jsonl,
    write_csv_atomic,
    write_text_atomic,
)

DOWNLOAD_JOB_STATES = {
    "pending",
    "claimed",
    "checking_local_inventory",
    "downloading",
    "succeeded",
    "skipped_existing",
    "failed_retryable",
    "failed_terminal",
    "cancelled",
    "eligibility_revoked",
    "dead_letter",
}


@dataclass(frozen=True)
class HandoffResult:
    emitted: int
    duplicate_suppressed: int
    failures: int
    pending_jobs: int
    backpressure_status: str


class DownloadHandoffOutbox:
    """Append-only transactional-outbox style download handoff."""

    handoff_schema_version = "0.1.0"
    document_version = "metadata-v1"

    def __init__(self, repo_root: Path, run_dir: Path, config: dict[str, Any]) -> None:
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.config = config
        self.root = ensure_dir(repo_root / "handoff" / "download")
        self.outbox = ensure_dir(self.root / "outbox")
        self.queue = ensure_dir(self.root / "queue")
        self.acks = ensure_dir(self.root / "acknowledgements")
        self.results = ensure_dir(self.root / "results")
        self.dead_letter = ensure_dir(self.root / "dead_letter")
        self.snapshots = ensure_dir(self.root / "snapshots")
        ensure_dir(run_dir / "handoff" / "download")
        self._ensure_audit_logs()

    @property
    def event_log(self) -> Path:
        return self.outbox / "download_events.jsonl"

    @property
    def queue_snapshot(self) -> Path:
        return self.snapshots / "download_queue_snapshot.csv"

    @property
    def acknowledgement_log(self) -> Path:
        return self.acks / "download_acknowledgements.jsonl"

    @property
    def result_log(self) -> Path:
        return self.results / "download_results.jsonl"

    @property
    def dead_letter_log(self) -> Path:
        return self.dead_letter / "dead_letter_events.jsonl"

    def emit_for_batch(
        self,
        *,
        run_id: str,
        iteration: int,
        query_id: str,
        records: list[NormalizedRecord],
        decisions: list[ScreeningDecision],
        scie_status: str,
        screening_evidence_path: str,
    ) -> HandoffResult:
        if not self.config.get("enabled", True):
            return HandoffResult(0, 0, 0, self.pending_jobs(), "disabled")
        records_by_id = {record.global_record_id: record for record in records}
        existing = self._existing_idempotency_keys()
        emitted = 0
        duplicates = 0
        failures = 0
        for decision in decisions:
            if decision.decision != "include":
                continue
            record = records_by_id.get(decision.global_record_id)
            if record is None or not record.global_record_id:
                failures += 1
                continue
            idempotency_key = self.idempotency_key(record.global_record_id)
            if idempotency_key in existing:
                duplicates += 1
                self._write_status(
                    run_id,
                    iteration,
                    query_id,
                    record.global_record_id,
                    "handoff_duplicate_suppressed",
                )
                continue
            event = self._event(
                run_id=run_id,
                iteration=iteration,
                query_id=query_id,
                record=record,
                decision=decision,
                scie_status=scie_status,
                screening_evidence_path=screening_evidence_path,
            )
            append_jsonl(self.event_log, event)
            append_jsonl(self.run_dir / "handoff" / "download" / "download_events.jsonl", event)
            self._write_status(
                run_id, iteration, query_id, record.global_record_id, "handoff_emitted"
            )
            existing.add(idempotency_key)
            emitted += 1
        self._write_queue_snapshot(existing)
        pending = self.pending_jobs()
        return HandoffResult(
            emitted=emitted,
            duplicate_suppressed=duplicates,
            failures=failures,
            pending_jobs=pending,
            backpressure_status=self.backpressure_status(pending),
        )

    def idempotency_key(self, global_record_id: str) -> str:
        return f"download:{global_record_id}:{self.document_version}"

    def pending_jobs(self) -> int:
        return sum(
            1 for row in read_jsonl(self.event_log) if row.get("event_type") == "DOWNLOAD_REQUESTED"
        )

    def backpressure_status(self, pending_jobs: int | None = None) -> str:
        pending = self.pending_jobs() if pending_jobs is None else pending_jobs
        hard = int(self.config.get("max_pending_jobs_hard", 2000))
        soft = int(self.config.get("max_pending_jobs_soft", 500))
        if pending >= hard:
            return "hard_limit"
        if pending >= soft:
            return "soft_limit"
        return "ok"

    def _event(
        self,
        *,
        run_id: str,
        iteration: int,
        query_id: str,
        record: NormalizedRecord,
        decision: ScreeningDecision,
        scie_status: str,
        screening_evidence_path: str,
    ) -> dict[str, Any]:
        event_id = self._event_id(run_id, query_id, record.global_record_id)
        payload: dict[str, Any] = {
            "handoff_schema_version": self.handoff_schema_version,
            "event_id": event_id,
            "event_type": "DOWNLOAD_REQUESTED",
            "idempotency_key": self.idempotency_key(record.global_record_id),
            "run_id": run_id,
            "iteration": iteration,
            "query_id": query_id,
            "global_record_id": record.global_record_id,
            "document_version": self.document_version,
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
            "scie_status": scie_status,
            "source_metadata_paths": [record.raw_metadata_path] if record.raw_metadata_path else [],
            "source_provenance": record.source_records,
            "candidate_fulltext_urls": [],
            "open_access_status": None,
            "screening_decision": decision.decision,
            "screening_confidence": None,
            "screening_reason_codes": decision.reason_codes,
            "screening_evidence_path": screening_evidence_path,
            "retrieval_lane": "mock_phase1",
            "priority": 50,
            "requested_at": utc_now_iso(),
            "payload_checksum": "",
        }
        payload["payload_checksum"] = self._payload_checksum(payload)
        return payload

    def _write_status(
        self,
        run_id: str,
        iteration: int,
        query_id: str,
        global_record_id: str,
        status: str,
    ) -> None:
        event = {
            "run_id": run_id,
            "iteration": iteration,
            "query_id": query_id,
            "global_record_id": global_record_id,
            "handoff_status": status,
            "timestamp": utc_now_iso(),
        }
        append_jsonl(self.run_dir / "handoff" / "download" / "handoff_status.jsonl", event)

    def _existing_idempotency_keys(self) -> set[str]:
        return {
            str(row["idempotency_key"])
            for row in read_jsonl(self.event_log)
            if row.get("event_type") == "DOWNLOAD_REQUESTED" and row.get("idempotency_key")
        }

    def _write_queue_snapshot(self, keys: set[str]) -> None:
        rows = [
            {
                "idempotency_key": key,
                "job_state": "pending",
                "updated_at": utc_now_iso(),
            }
            for key in sorted(keys)
        ]
        write_csv_atomic(
            self.queue_snapshot,
            rows,
            ["idempotency_key", "job_state", "updated_at"],
        )

    def _event_id(self, run_id: str, query_id: str, global_record_id: str) -> str:
        digest = hashlib.sha256(
            f"{run_id}|{query_id}|{global_record_id}|{self.document_version}".encode()
        ).hexdigest()
        return f"download_event_{digest[:24]}"

    def _payload_checksum(self, payload: dict[str, Any]) -> str:
        unsigned = dict(payload)
        unsigned["payload_checksum"] = ""
        return hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()

    def _ensure_audit_logs(self) -> None:
        for path in [
            self.event_log,
            self.acknowledgement_log,
            self.result_log,
            self.dead_letter_log,
        ]:
            if not path.exists():
                write_text_atomic(path, "")
