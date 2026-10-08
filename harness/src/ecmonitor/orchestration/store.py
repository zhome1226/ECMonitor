"""SQLite queue with fenced leases and atomic result/outbox transactions."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from ecmonitor.security import redact, require_secret_free

STAGES = {"retrieval", "download", "extraction", "validation", "commit"}
SCHEMA_VERSION = 1


class LeaseLost(RuntimeError):
    """A stale worker must never overwrite a newer lease."""


class WorkflowStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError("Workflow database requires a newer runtime")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, document_id TEXT,
                    stage TEXT NOT NULL, identity TEXT NOT NULL UNIQUE,
                    signature TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    maximum_attempts INTEGER NOT NULL, available_at REAL NOT NULL DEFAULT 0,
                    lease_token TEXT, lease_until REAL, result TEXT,
                    error_code TEXT, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,available_at,created_at);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_health (
                    id INTEGER PRIMARY KEY CHECK(id=1), updated_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS immutable_events_update
                BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'Immutable audit event'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_events_delete
                BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'Immutable audit event'); END;
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _event(self, db: sqlite3.Connection, task: sqlite3.Row, event: str, payload: Any) -> None:
        db.execute("INSERT INTO events(task_id,run_id,event,payload,created_at) VALUES(?,?,?,?,?)",
                   (task["id"], task["run_id"], event, json.dumps(redact(payload)), time.time()))

    def enqueue(self, *, run_id: str, stage: str, payload: dict[str, Any],
                document_id: str | None = None, identity: str | None = None,
                maximum_attempts: int = 3, db: sqlite3.Connection | None = None) -> str:
        if stage not in STAGES or maximum_attempts < 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
            raise ValueError("Invalid queue task")
        require_secret_free(payload)
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        signature = hashlib.sha256(encoded.encode()).hexdigest()
        identity = identity or f"{run_id}:{document_id or 'run'}:{stage}"
        if db is None:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.enqueue(run_id=run_id, stage=stage, payload=payload,
                                    document_id=document_id, identity=identity,
                                    maximum_attempts=maximum_attempts, db=connection)
        existing = db.execute("SELECT id,signature FROM tasks WHERE identity=?", (identity,)).fetchone()
        if existing is not None:
            if existing["signature"] != signature:
                raise ValueError("Idempotency key reused with different input; create a versioned run")
            return str(existing["id"])
        task_id = uuid.uuid4().hex
        db.execute("""INSERT INTO tasks(id,run_id,document_id,stage,identity,signature,payload,
                   maximum_attempts,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                   (task_id, run_id, document_id, stage, identity, signature, encoded,
                    maximum_attempts, time.time()))
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert task is not None
        self._event(db, task, "enqueued", {"stage": stage, "signature": signature})
        return task_id

    def claim(self, *, lease_seconds: float = 300) -> dict[str, Any] | None:
        if lease_seconds <= 0:
            raise ValueError("Lease duration must be positive")
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("SELECT * FROM tasks WHERE status='leased' AND lease_until<=?", (now,)).fetchall()
            for task in expired:
                status = "failed_terminal" if task["attempts"] >= task["maximum_attempts"] else "pending"
                db.execute("UPDATE tasks SET status=?,lease_token=NULL,lease_until=NULL WHERE id=?", (status, task["id"]))
                self._event(db, task, "lease_expired", {"status": status})
            row = db.execute("""SELECT * FROM tasks WHERE status='pending' AND available_at<=?
                              ORDER BY created_at,id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE tasks SET status='leased',attempts=attempts+1,lease_token=?,lease_until=? WHERE id=?",
                       (token, now + lease_seconds, row["id"]))
            self._event(db, row, "claimed", {"attempt": row["attempts"] + 1})
            task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())
            task["payload"] = json.loads(task["payload"])
            return task

    def _leased(self, db: sqlite3.Connection, task_id: str, token: str) -> sqlite3.Row:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None or task["status"] != "leased" or task["lease_token"] != token or task["lease_until"] <= time.time():
            raise LeaseLost("Lease expired or ownership changed")
        return cast(sqlite3.Row, task)

    def heartbeat(self, task_id: str, token: str, *, lease_seconds: float) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._leased(db, task_id, token)
            db.execute("UPDATE tasks SET lease_until=? WHERE id=?", (time.time() + lease_seconds, task_id))

    def finish(self, task_id: str, token: str, result: dict[str, Any], *,
               successors: list[dict[str, Any]] | None = None, status: str = "completed") -> None:
        if status not in {"completed", "paused_human_review", "blocked_access", "failed_terminal"}:
            raise ValueError("Invalid completion status")
        require_secret_free(result)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._leased(db, task_id, token)
            for successor in successors or []:
                self.enqueue(db=db, run_id=task["run_id"], **successor)
            db.execute("UPDATE tasks SET status=?,result=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                       (status, json.dumps(result), task_id))
            self._event(db, task, status, {"successors": len(successors or [])})

    def fail(self, task_id: str, token: str, *, error_code: str, retryable: bool = True,
             backoff_seconds: float = 5) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._leased(db, task_id, token)
            status = "pending" if retryable and task["attempts"] < task["maximum_attempts"] else "failed_terminal"
            db.execute("UPDATE tasks SET status=?,error_code=?,available_at=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                       (status, str(redact(error_code)), time.time() + backoff_seconds, task_id))
            self._event(db, task, "attempt_failed", {"error_code": error_code, "status": status})

    def resume(self, task_id: str, *, reason: str) -> None:
        if not reason.strip():
            raise ValueError("An audit reason is required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None or task["status"] not in {"paused_human_review", "blocked_access"}:
                raise ValueError("Only blocked or paused tasks may be resumed")
            if task["attempts"] >= task["maximum_attempts"]:
                raise ValueError("Retry budget exhausted; create a versioned task")
            db.execute("UPDATE tasks SET status='pending',available_at=0 WHERE id=?", (task_id,))
            self._event(db, task, "resumed", {"reason": reason})

    def status(self) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute("SELECT run_id,status,COUNT(*) AS count FROM tasks GROUP BY run_id,status").fetchall()
            return {"schema_version": SCHEMA_VERSION, "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
                    "counts": [dict(row) for row in rows],
                    "attention": [dict(row) for row in db.execute(
                        "SELECT id,run_id,stage,status,error_code FROM tasks WHERE status IN ('paused_human_review','blocked_access','failed_terminal')")]}

    def tick(self) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO worker_health(id,updated_at) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at", (time.time(),))
