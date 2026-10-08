"""Transactional per-document persistence for full-text extraction."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.models import (
    EvidenceChunk,
    HumanReviewTask,
    ObservationDisposition,
    ParsedDocument,
    RetryEvent,
    ValidationDecision,
)
from ecmonitor.validation_specialist.service import finalize_candidate_validation

SCHEMA_VERSION = 3


class FulltextControlPlane:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT OR IGNORE INTO schema_migrations(version) VALUES (1);
                INSERT OR IGNORE INTO schema_migrations(version) VALUES (2);

                CREATE TABLE IF NOT EXISTS document_assets (
                    document_id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL UNIQUE,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS document_sessions (
                    document_session_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES document_assets(document_id),
                    registry_snapshot_version INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running','committed','failed')),
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS parsed_documents (
                    document_session_id TEXT PRIMARY KEY
                        REFERENCES document_sessions(document_session_id),
                    parser_name TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence_chunks (
                    chunk_row_id TEXT PRIMARY KEY,
                    chunk_id TEXT NOT NULL,
                    document_session_id TEXT NOT NULL
                        REFERENCES document_sessions(document_session_id),
                    ordinal INTEGER NOT NULL,
                    page_start INTEGER NOT NULL,
                    page_end INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(document_session_id, ordinal),
                    UNIQUE(document_session_id, chunk_id)
                );

                CREATE TABLE IF NOT EXISTS extraction_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    document_session_id TEXT NOT NULL
                        REFERENCES document_sessions(document_session_id),
                    chunk_row_id TEXT NOT NULL REFERENCES evidence_chunks(chunk_row_id),
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chemical_resolution_events (
                    resolution_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_session_id TEXT NOT NULL
                        REFERENCES document_sessions(document_session_id),
                    candidate_id TEXT,
                    raw_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS review_decisions (
                    review_id TEXT PRIMARY KEY,
                    document_session_id TEXT NOT NULL
                        REFERENCES document_sessions(document_session_id),
                    candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS retry_events (
                    retry_id TEXT PRIMARY KEY,
                    document_session_id TEXT NOT NULL
                        REFERENCES document_sessions(document_session_id),
                    chunk_row_id TEXT NOT NULL REFERENCES evidence_chunks(chunk_row_id),
                    attempt INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS observation_records (
                    record_id TEXT PRIMARY KEY,
                    document_session_id TEXT NOT NULL
                        REFERENCES document_sessions(document_session_id),
                    candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),
                    disposition TEXT NOT NULL CHECK(
                        disposition IN ('accepted','rejected','pending_human_review')
                    ),
                    canonical_name TEXT,
                    reported_name TEXT,
                    replacement_name TEXT,
                    terminal_status TEXT NOT NULL DEFAULT 'accepted_main',
                    output_stream TEXT NOT NULL DEFAULT 'accepted_observations',
                    policy_rule_id TEXT NOT NULL DEFAULT 'OBS-01',
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS human_review_tasks (
                    task_id TEXT PRIMARY KEY,
                    document_session_id TEXT NOT NULL
                        REFERENCES document_sessions(document_session_id),
                    candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),
                    priority TEXT NOT NULL CHECK(priority IN ('low','medium','high','critical')),
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(
                        status IN ('pending','assigned','resolved','dismissed')
                    ),
                    reason_codes_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS human_review_resolutions (
                    resolution_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES human_review_tasks(task_id),
                    document_session_id TEXT NOT NULL
                        REFERENCES document_sessions(document_session_id),
                    candidate_id TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK(disposition IN ('accepted','rejected')),
                    note TEXT,
                    reason_codes_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    resolved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS extraction_outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_session_id TEXT NOT NULL
                        REFERENCES document_sessions(document_session_id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    exported_at TEXT
                );
                """
            )
            observation_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(observation_records)")
            }
            for column_name, ddl in (
                ("terminal_status", "TEXT NOT NULL DEFAULT 'accepted_main'"),
                ("output_stream", "TEXT NOT NULL DEFAULT 'accepted_observations'"),
                ("policy_rule_id", "TEXT NOT NULL DEFAULT 'OBS-01'"),
            ):
                if column_name not in observation_columns:
                    connection.execute(
                        f"ALTER TABLE observation_records ADD COLUMN {column_name} {ddl}"
                    )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )

    def begin_session(
        self,
        *,
        document_id: str,
        document_session_id: str,
        source_path: Path,
        source_sha256: str,
        registry_snapshot_version: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO document_assets(document_id, source_path, source_sha256)
                VALUES (?, ?, ?)""",
                (document_id, str(source_path.resolve()), source_sha256),
            )
            connection.execute(
                """INSERT INTO document_sessions(
                    document_session_id, document_id, registry_snapshot_version, status
                ) VALUES (?, ?, ?, 'running')""",
                (document_session_id, document_id, registry_snapshot_version),
            )

    def commit_document(
        self,
        *,
        document_session_id: str,
        parsed_document: ParsedDocument,
        chunks: list[EvidenceChunk],
        candidates: list[tuple[str, str, dict[str, Any]]],
        resolutions: list[tuple[str | None, dict[str, Any]]],
        reviews: list[tuple[str, str, ValidationDecision]],
        outbox_payload: dict[str, Any],
        retry_events: list[RetryEvent] | None = None,
        observation_records: list[ObservationDisposition] | None = None,
        human_review_tasks: list[HumanReviewTask] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT status FROM document_sessions WHERE document_session_id = ?",
                (document_session_id,),
            ).fetchone()
            if session is None or session["status"] != "running":
                raise ValueError("document session is not in running state")
            connection.execute(
                """INSERT INTO parsed_documents(
                    document_session_id, parser_name, parser_version, page_count, payload_json
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    document_session_id,
                    parsed_document.parser_name,
                    parsed_document.parser_version,
                    parsed_document.page_count,
                    _json(parsed_document.to_dict()),
                ),
            )
            chunk_row_ids = {
                chunk.chunk_id: f"{document_session_id}:{chunk.chunk_id}" for chunk in chunks
            }
            connection.executemany(
                """INSERT INTO evidence_chunks(
                    chunk_row_id, chunk_id, document_session_id, ordinal,
                    page_start, page_end, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        chunk_row_ids[chunk.chunk_id],
                        chunk.chunk_id,
                        document_session_id,
                        chunk.ordinal,
                        chunk.page_start,
                        chunk.page_end,
                        _json(chunk.to_dict()),
                    )
                    for chunk in chunks
                ],
            )
            connection.executemany(
                """INSERT INTO extraction_candidates(
                    candidate_id, document_session_id, chunk_row_id, payload_json
                ) VALUES (?, ?, ?, ?)""",
                [
                    (
                        candidate_id,
                        document_session_id,
                        chunk_row_ids[chunk_id],
                        _json(payload),
                    )
                    for candidate_id, chunk_id, payload in candidates
                ],
            )
            connection.executemany(
                """INSERT INTO chemical_resolution_events(
                    document_session_id, candidate_id, raw_name, payload_json
                ) VALUES (?, ?, ?, ?)""",
                [
                    (
                        document_session_id,
                        candidate_id,
                        str(payload.get("raw_name", "")),
                        _json(payload),
                    )
                    for candidate_id, payload in resolutions
                ],
            )
            connection.executemany(
                """INSERT INTO review_decisions(
                    review_id, document_session_id, candidate_id, payload_json
                ) VALUES (?, ?, ?, ?)""",
                [
                    (review_id, document_session_id, candidate_id, _json(decision.to_dict()))
                    for review_id, candidate_id, decision in reviews
                ],
            )
            retry_events = retry_events or []
            observation_records = observation_records or []
            human_review_tasks = human_review_tasks or []
            connection.executemany(
                """INSERT INTO retry_events(
                    retry_id, document_session_id, chunk_row_id, attempt, payload_json
                ) VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        event.retry_id,
                        document_session_id,
                        chunk_row_ids[event.chunk_id],
                        event.attempt,
                        _json(event.to_dict()),
                    )
                    for event in retry_events
                ],
            )
            connection.executemany(
                """INSERT INTO observation_records(
                    record_id, document_session_id, candidate_id, disposition,
                    canonical_name, reported_name, replacement_name, terminal_status,
                    output_stream, policy_rule_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        record.record_id,
                        document_session_id,
                        record.candidate_id,
                        record.disposition,
                        record.canonical_name,
                        record.reported_name,
                        record.replacement_name,
                        record.terminal_status,
                        record.output_stream,
                        record.policy_rule_id,
                        _json(record.payload),
                    )
                    for record in observation_records
                ],
            )
            connection.executemany(
                """INSERT INTO human_review_tasks(
                    task_id, document_session_id, candidate_id, priority,
                    reason_codes_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        task.task_id,
                        document_session_id,
                        task.candidate_id,
                        task.priority,
                        _json(task.reason_codes),
                        _json(task.payload),
                    )
                    for task in human_review_tasks
                ],
            )
            connection.execute(
                """INSERT INTO extraction_outbox(document_session_id, event_type, payload_json)
                VALUES (?, 'document_committed', ?)""",
                (document_session_id, _json(outbox_payload)),
            )
            connection.execute(
                """UPDATE document_sessions
                SET status = 'committed', completed_at = CURRENT_TIMESTAMP
                WHERE document_session_id = ?""",
                (document_session_id,),
            )
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {result}")
            connection.commit()

    def fail_session(self, document_session_id: str, error_message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE document_sessions
                SET status = 'failed', completed_at = CURRENT_TIMESTAMP, error_message = ?
                WHERE document_session_id = ? AND status = 'running'""",
                (error_message[:4000], document_session_id),
            )


    def get_human_review_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT task_id, document_session_id, candidate_id, priority, status,
                          reason_codes_json, payload_json
                FROM human_review_tasks WHERE task_id = ?""",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "task_id": row["task_id"],
            "document_session_id": row["document_session_id"],
            "candidate_id": row["candidate_id"],
            "priority": row["priority"],
            "status": row["status"],
            "reason_codes": json.loads(row["reason_codes_json"]),
            "payload": json.loads(row["payload_json"]),
        }

    def ensure_workflow_review_tasks(self, document_session_id: str) -> None:
        """Expose deferred evidence as operator work before publication, not acceptance."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            records = connection.execute(
                """SELECT o.candidate_id,o.payload_json FROM observation_records o
                   LEFT JOIN human_review_tasks h ON o.candidate_id=h.candidate_id
                   WHERE o.document_session_id=? AND o.disposition='pending_human_review'
                   AND h.task_id IS NULL""", (document_session_id,),
            ).fetchall()
            for record in records:
                payload = json.loads(record["payload_json"])
                reasons = payload.get("decision", {}).get("reason_codes") or ["workflow_evidence_resolution_required"]
                connection.execute(
                    """INSERT INTO human_review_tasks(task_id,document_session_id,candidate_id,
                       priority,reason_codes_json,payload_json) VALUES(?,?,?,'high',?,?)""",
                    (f"workflow-review-{uuid.uuid4().hex}", document_session_id,
                     record["candidate_id"], _json(reasons), record["payload_json"]),
                )

    def resolve_human_review_task(
        self, task_id: str, *, disposition: str, note: str | None = None
    ) -> dict[str, Any]:
        """Mark a pending human review task resolved with a signed disposition.

        The candidate payload and its original reason codes are preserved in a new
        ``human_review_resolutions`` row so the signed decision can feed the few-shot
        extractor examples later. Raises ``ValueError`` when the task is unknown or already
        resolved.
        """
        if disposition not in {"accepted", "rejected"}:
            raise ValueError("disposition must be 'accepted' or 'rejected'")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                """SELECT document_session_id, candidate_id, status,
                          reason_codes_json, payload_json
                FROM human_review_tasks WHERE task_id = ?""",
                (task_id,),
            ).fetchone()
            if task is None:
                raise ValueError(f"no human review task {task_id}")
            if task["status"] != "pending":
                raise ValueError(f"human review task {task_id} is {task['status']}, not pending")
            resolution_id = f"resolution-{uuid.uuid4().hex[:12]}"
            connection.execute(
                """INSERT INTO human_review_resolutions(
                    resolution_id, task_id, document_session_id, candidate_id,
                    disposition, note, reason_codes_json, payload_json, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    resolution_id,
                    task_id,
                    task["document_session_id"],
                    task["candidate_id"],
                    disposition,
                    (note or "")[:2000],
                    task["reason_codes_json"],
                    task["payload_json"],
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute(
                """UPDATE human_review_tasks
                SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP
                WHERE task_id = ?""",
                (task_id,),
            )
            if disposition == "rejected":
                connection.execute(
                    """UPDATE observation_records SET disposition='rejected',
                       output_stream='human_rejected_observations', policy_rule_id='HUMAN-REJECT-01'
                       WHERE candidate_id=? AND document_session_id=? AND disposition='pending_human_review'""",
                    (task["candidate_id"], task["document_session_id"]),
                )
            else:
                payload = json.loads(task["payload_json"])
                decision = payload.get("decision", {})
                # Only the trusted policy wrapper sets eligibility after both gates pass.
                # An operator signature is not permission to bypass unresolved evidence.
                if (decision.get("pilot_accept_eligible") is True
                        and decision.get("action") == "escalate"
                        and "pilot_human_signoff_required" in decision.get("reason_codes", [])
                        and not decision.get("failed_json_pointers")
                        and not decision.get("requested_context")):
                    accepted = ValidationDecision(action="accept", reason_codes=("human_pilot_signoff",))
                    outcome = finalize_candidate_validation(payload["candidate"], accepted)
                    payload.update(candidate=outcome.candidate, decision=accepted.to_dict(),
                                   terminal_status=outcome.terminal_status, output_stream=outcome.output_stream,
                                   policy_rule_id=outcome.policy_rule_id, human_resolution_id=resolution_id)
                    connection.execute(
                        """UPDATE observation_records SET disposition='accepted', terminal_status=?,
                           output_stream=?, policy_rule_id=?, payload_json=?
                           WHERE candidate_id=? AND document_session_id=? AND disposition='pending_human_review'""",
                        (outcome.terminal_status, outcome.output_stream, outcome.policy_rule_id,
                         _json(payload), task["candidate_id"], task["document_session_id"]),
                    )
        return {
            "resolution_id": resolution_id,
            "task_id": task_id,
            "candidate_id": task["candidate_id"],
            "document_session_id": task["document_session_id"],
            "disposition": disposition,
        }

    def has_committed_sha256(self, source_sha256: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT 1
                FROM document_assets a
                JOIN document_sessions s ON s.document_id = a.document_id
                WHERE a.source_sha256 = ? AND s.status = 'committed'
                LIMIT 1""",
                (source_sha256,),
            ).fetchone()
        return row is not None

    def has_committed_path(self, source_path: Path) -> bool:
        resolved = str(source_path.resolve())
        with self._connect() as connection:
            row = connection.execute(
                """SELECT 1
                FROM document_assets a
                JOIN document_sessions s ON s.document_id = a.document_id
                WHERE a.source_path = ? AND s.status = 'committed'
                LIMIT 1""",
                (resolved,),
            ).fetchone()
        return row is not None

    def status(self) -> dict[str, Any]:
        tables = [
            "document_assets",
            "document_sessions",
            "parsed_documents",
            "evidence_chunks",
            "extraction_candidates",
            "chemical_resolution_events",
            "review_decisions",
            "retry_events",
            "observation_records",
            "human_review_tasks",
            "human_review_resolutions",
            "extraction_outbox",
        ]
        with self._connect() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        return {
            "schema_version": SCHEMA_VERSION,
            "database": str(self.database_path),
            "integrity": integrity,
            "table_counts": counts,
        }


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)
