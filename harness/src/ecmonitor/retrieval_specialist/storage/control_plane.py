"""SQLite control plane for Phase 1.1 Retrieval Specialist state."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.storage.atomic_io import ensure_dir

SCHEMA_VERSION = "1.1.0"
MIGRATION_ID = "001_phase_1_1_control_plane"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    code_commit_sha TEXT NOT NULL,
    checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    completeness TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    current_iteration INTEGER NOT NULL DEFAULT 0,
    current_query_id TEXT,
    accepted_query_id TEXT,
    current_state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    code_commit_sha TEXT NOT NULL,
    git_branch TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    scoring_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    source_status_json TEXT NOT NULL,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS query_iterations (
    query_iteration_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    iteration INTEGER NOT NULL,
    query_id TEXT NOT NULL,
    parent_query_id TEXT,
    branch_id TEXT NOT NULL,
    query_status TEXT NOT NULL,
    acceptance_status TEXT NOT NULL,
    score REAL,
    score_delta REAL,
    saturation_status TEXT NOT NULL,
    query_known_cutoff INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finalized_at TEXT,
    completion_checksum TEXT,
    UNIQUE (run_id, query_id)
);

CREATE TABLE IF NOT EXISTS operator_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    operator TEXT NOT NULL,
    source_name TEXT,
    batch_id TEXT NOT NULL,
    page_cursor TEXT,
    next_page_cursor TEXT,
    input_checksum TEXT,
    output_checksum TEXT,
    processed_count INTEGER NOT NULL DEFAULT 0,
    persisted_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_batch
ON operator_checkpoints (
    run_id,
    query_id,
    iteration,
    operator,
    COALESCE(source_name, ''),
    batch_id
);

CREATE TABLE IF NOT EXISTS source_records (
    source_record_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    source_rank INTEGER NOT NULL,
    retrieval_page INTEGER NOT NULL,
    retrieval_cursor TEXT,
    raw_metadata_path TEXT NOT NULL,
    raw_payload_checksum TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    normalized_at TEXT,
    UNIQUE (run_id, query_id, source_name, source_record_id)
);

CREATE INDEX IF NOT EXISTS ix_source_records_run_query
ON source_records(run_id, query_id, source_name, retrieval_page);

CREATE TABLE IF NOT EXISTS documents (
    global_record_id TEXT PRIMARY KEY,
    normalized_doi TEXT,
    pmid TEXT,
    openalex_id TEXT,
    semantic_scholar_id TEXT,
    crossref_id TEXT,
    canonical_title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    publication_year INTEGER,
    first_author TEXT,
    journal_title TEXT,
    abstract_original TEXT,
    abstract_source TEXT,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    authors_json TEXT NOT NULL DEFAULT '[]',
    issn_json TEXT NOT NULL DEFAULT '[]',
    eissn_json TEXT NOT NULL DEFAULT '[]',
    document_type TEXT,
    language TEXT,
    sampled_matrices_json TEXT NOT NULL DEFAULT '[]',
    study_type TEXT,
    has_real_field_sample INTEGER,
    has_concentration_evidence INTEGER,
    article_ec_scope TEXT NOT NULL DEFAULT 'uncertain',
    first_seen_run_id TEXT NOT NULL,
    first_seen_query_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    latest_metadata_version TEXT NOT NULL,
    current_screening_status TEXT,
    current_download_status TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_normalized_doi
ON documents(normalized_doi) WHERE normalized_doi IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_pmid
ON documents(pmid) WHERE pmid IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_openalex
ON documents(openalex_id) WHERE openalex_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_semantic_scholar
ON documents(semantic_scholar_id) WHERE semantic_scholar_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_crossref
ON documents(crossref_id) WHERE crossref_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_documents_screening_status
ON documents(current_screening_status);

CREATE TABLE IF NOT EXISTS document_sources (
    document_source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    global_record_id TEXT NOT NULL REFERENCES documents(global_record_id),
    source_name TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_rank INTEGER NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    raw_metadata_path TEXT NOT NULL,
    metadata_version TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    UNIQUE (global_record_id, source_name, source_record_id, run_id, query_id)
);

CREATE TABLE IF NOT EXISTS document_query_membership (
    membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    global_record_id TEXT NOT NULL REFERENCES documents(global_record_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    source_name TEXT NOT NULL,
    source_rank INTEGER NOT NULL,
    first_seen_in_query INTEGER NOT NULL,
    already_known_before_query INTEGER NOT NULL,
    included_in_novelty_sample INTEGER NOT NULL,
    novelty_sample_position INTEGER,
    screening_status_at_iteration TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (global_record_id, run_id, query_id, source_name)
);

CREATE INDEX IF NOT EXISTS ix_membership_run_query
ON document_query_membership(run_id, query_id, included_in_novelty_sample);

CREATE TABLE IF NOT EXISTS candidate_duplicate_clusters (
    cluster_id TEXT PRIMARY KEY,
    record_a TEXT NOT NULL,
    record_b TEXT NOT NULL,
    confidence REAL NOT NULL,
    matched_fields_json TEXT NOT NULL,
    conflicting_fields_json TEXT NOT NULL,
    merge_status TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS screening_decisions (
    screening_decision_id TEXT PRIMARY KEY,
    global_record_id TEXT NOT NULL REFERENCES documents(global_record_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    screening_pass TEXT NOT NULL,
    decision TEXT NOT NULL,
    confidence REAL,
    reason_codes_json TEXT NOT NULL,
    evidence_spans_json TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    raw_response_path TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes_decision_id TEXT,
    is_current INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_current_screening_decision
ON screening_decisions(global_record_id, run_id, query_id)
WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS download_outbox (
    event_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    global_record_id TEXT NOT NULL REFERENCES documents(global_record_id),
    document_version TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_checksum TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE (idempotency_key, event_type)
);

CREATE TABLE IF NOT EXISTS download_jobs (
    download_job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    global_record_id TEXT NOT NULL REFERENCES documents(global_record_id),
    job_state TEXT NOT NULL,
    claimed_by TEXT,
    claimed_at TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    completed_at TEXT,
    result_reference TEXT,
    failure_reason TEXT
);

CREATE INDEX IF NOT EXISTS ix_download_jobs_state
ON download_jobs(job_state);

CREATE TABLE IF NOT EXISTS metric_values (
    metric_value_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    metric_unit TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, query_id, iteration, metric_name)
);

CREATE TABLE IF NOT EXISTS term_ledger (
    term_event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    term TEXT NOT NULL,
    concept_block TEXT NOT NULL,
    action TEXT NOT NULL,
    previous_status TEXT,
    new_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    supporting_positive_documents TEXT NOT NULL DEFAULT '[]',
    supporting_negative_documents TEXT NOT NULL DEFAULT '[]',
    discriminative_score REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saturation_counters (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    consecutive_low_novelty_rounds INTEGER NOT NULL DEFAULT 0,
    consecutive_low_yield_rounds INTEGER NOT NULL DEFAULT 0,
    consecutive_low_score_improvement_rounds INTEGER NOT NULL DEFAULT 0,
    consecutive_no_effective_term_rounds INTEGER NOT NULL DEFAULT 0,
    consecutive_known_noise_dominance_rounds INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_event_id TEXT PRIMARY KEY,
    run_id TEXT,
    query_id TEXT,
    global_record_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_audit_events_run
ON audit_events(run_id, event_type);
"""


class ControlPlane:
    """Small, transaction-scoped SQLite helper for harness state."""

    def __init__(self, db_path: Path, code_commit_sha: str = "unknown") -> None:
        self.db_path = db_path
        self.code_commit_sha = code_commit_sha

    def connect(self) -> sqlite3.Connection:
        ensure_dir(self.db_path.parent)
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> dict[str, Any]:
        checksum = hashlib.sha256(SCHEMA_SQL.encode()).hexdigest()
        with self.transaction() as connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (
                    migration_id,
                    schema_version,
                    applied_at,
                    code_commit_sha,
                    checksum
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (MIGRATION_ID, SCHEMA_VERSION, utc_now_iso(), self.code_commit_sha, checksum),
            )
        return {
            "status": "ok",
            "schema_version": SCHEMA_VERSION,
            "migration_id": MIGRATION_ID,
            "database": str(self.db_path),
            "checksum": checksum,
        }

    def status(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"status": "missing", "database": str(self.db_path)}
        with self.connect() as connection:
            migrations = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM schema_migrations ORDER BY applied_at"
                )
            ]
            table_counts = self.table_counts(connection)
        return {
            "status": "ok",
            "database": str(self.db_path),
            "schema_version": migrations[-1]["schema_version"] if migrations else None,
            "migrations": migrations,
            "table_counts": table_counts,
            "database_checksum": self.database_checksum(),
        }

    def integrity_check(self) -> dict[str, Any]:
        with self.connect() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_errors = [
                dict(row) for row in connection.execute("PRAGMA foreign_key_check")
            ]
        return {
            "status": "ok" if result == "ok" and not foreign_key_errors else "failed",
            "integrity_check": result,
            "foreign_key_errors": foreign_key_errors,
        }

    def table_counts(self, connection: sqlite3.Connection | None = None) -> dict[str, int]:
        owns_connection = connection is None
        if connection is None:
            connection = self.connect()
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
            ]
            return {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in tables
            }
        finally:
            if owns_connection:
                connection.close()

    def database_checksum(self) -> str:
        if not self.db_path.exists():
            return "missing"
        digest = hashlib.sha256()
        with self.db_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
