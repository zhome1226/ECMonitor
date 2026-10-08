"""Executable document pipeline using the established scientific harness."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecmonitor.download_specialist.routes import AuthorizedHttpsRoute, LocalInventoryRoute
from ecmonitor.download_specialist.worker import acquire_document
from ecmonitor.fulltext_extraction.cli import _build_harness, build_parser
from ecmonitor.fulltext_extraction.storage import FulltextControlPlane
from ecmonitor.orchestration.store import LeaseLost, WorkflowStore
from ecmonitor.retrieval_specialist.orchestration.run_manager import RunManager


@dataclass(frozen=True)
class Runtime:
    project_root: Path
    data_root: Path
    source_root: Path
    allowed_hosts: frozenset[str] = frozenset()
    model_agents: bool = True
    lease_seconds: float = 300
    model_timeout_seconds: int = 300

    def signature(self) -> dict[str, Any]:
        """Pin policy, schemas, prompts, models, and paths across resumptions."""
        digest = hashlib.sha256()
        for directory in ("configs", "schemas", "prompts", "src", "scripts"):
            for path in sorted((self.project_root / directory).rglob("*")):
                if path.is_file() and path.suffix in {".py", ".yaml", ".json", ".md"} and "__pycache__" not in path.parts:
                    digest.update(str(path.relative_to(self.project_root)).encode())
                    digest.update(path.read_bytes())
        return {"runtime_digest": digest.hexdigest(), "model_agents": self.model_agents,
                "source_root": str(self.source_root.resolve()),
                "data_root": str(self.data_root.resolve()), "allowed_hosts": sorted(self.allowed_hosts),
                "extractor_model": os.environ.get("ECMONITOR_EXTRACTOR_MODEL", ""),
                "validator_model": os.environ.get("ECMONITOR_VALIDATOR_MODEL", ""),
                "model_timeout_seconds": self.model_timeout_seconds}


class WorkflowWorker:
    def __init__(self, store: WorkflowStore, runtime: Runtime) -> None:
        self.store = store
        self.runtime = runtime
        self.signature = runtime.signature()

    def run_once(self) -> bool:
        self.store.tick()
        task = self.store.claim(lease_seconds=self.runtime.lease_seconds)
        if task is None:
            return False
        stop = threading.Event()
        lost = threading.Event()

        def keep_lease() -> None:
            while not stop.wait(max(0.05, self.runtime.lease_seconds / 3)):
                try:
                    self.store.heartbeat(task["id"], task["lease_token"], lease_seconds=self.runtime.lease_seconds)
                    self.store.tick()
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=keep_lease, daemon=True)
        thread.start()
        try:
            if task["payload"].get("runtime") != self.signature:
                raise ValueError("Runtime signature changed; create a versioned workflow run")
            result, successors, status = self.dispatch(task)
            if lost.is_set():
                raise LeaseLost("Heartbeat failed")
            self.store.finish(task["id"], task["lease_token"], result, successors=successors, status=status)
        except LeaseLost:
            pass
        except Exception as exc:
            with suppress(LeaseLost):
                self.store.fail(task["id"], task["lease_token"], error_code=type(exc).__name__,
                                retryable=not isinstance(exc, (ValueError, FileNotFoundError)))
        finally:
            stop.set()
            thread.join(timeout=2)
        return True

    def _next(self, task: dict[str, Any], stage: str, payload: dict[str, Any],
              document_id: str | None = None) -> dict[str, Any]:
        return {"stage": stage, "document_id": document_id or task["document_id"],
                "payload": {**payload, "runtime": self.signature}}

    def dispatch(self, task: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        payload = task["payload"]
        stage = task["stage"]
        if stage == "retrieval":
            records = payload.get("records")
            if records is None:
                root = self._retrieval_workspace()
                manager = RunManager(root)
                existing_run = root / "runs" / task["run_id"]
                report = manager.resume(task["run_id"]) if existing_run.is_dir() else manager.live_run(run_id=task["run_id"], date_from=payload["date_from"],
                                          date_to=payload["date_to"], max_iterations=payload.get("max_iterations", 1),
                                          allow_degraded_sources=payload.get("allow_degraded_sources", False))
                if report.get("status") not in {"completed", "already_completed", "saturated_narrow", "saturated_broad"}:
                    return {"reason": "retrieval_requires_operator_action", "status": report.get("status")}, [], "blocked_access"
                database = root / "state/ecmonitor_control.sqlite3"
                with sqlite3.connect(database) as db:
                    records = [json.loads(row[0]) for row in db.execute(
                        "SELECT payload_json FROM download_outbox WHERE run_id=?", (task["run_id"],))]
            if not isinstance(records, list):
                raise ValueError("Retrieval output must contain a list of screened records")
            successors = []
            for record in records:
                if record.get("screening_decision") != "include" or not record.get("global_record_id"):
                    raise ValueError("Only explicitly included records may be queued for acquisition")
                successors.append(self._next(task, "download", {"request": record}, record["global_record_id"]))
            return {"screened_included_count": len(records)}, successors, "completed"
        if stage == "download":
            routes: list[Any] = [LocalInventoryRoute(self.runtime.source_root)]
            if self.runtime.allowed_hosts:
                routes.append(AuthorizedHttpsRoute(self.runtime.data_root / "downloads", self.runtime.allowed_hosts))
            outcome = acquire_document(payload["request"], routes)
            result = outcome.to_handoff(idempotency_key=task["identity"], download_job_id=task["id"])
            if outcome.final_status == "blocked_user_action":
                return result, [], "blocked_access"
            if outcome.final_status == "retryable_failure":
                raise RuntimeError("Acquisition transport failure")
            if outcome.final_status not in {"downloaded", "matched_local"}:
                return result, [], "failed_terminal"
            return result, [self._next(task, "extraction", {"artifact": result, "request": payload["request"]})], "completed"
        if stage == "extraction":
            artifact = payload["artifact"]
            path = Path(artifact["artifact_path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["PDF_SHA256"]:
                raise ValueError("Source artifact changed after acquisition")
            # Recovery reads the committed SQLite outbox, not a potentially missing JSON mirror.
            plane_path = self.runtime.data_root / "state/fulltext.sqlite3"
            cached = self._committed_report(plane_path, artifact["PDF_SHA256"], task["identity"], self.signature)
            if cached is None:
                argv = ["inspect-document", str(path), "--database", str(plane_path),
                        "--registry", str(self.runtime.data_root / "state/chemicals.sqlite3"),
                        "--output-dir", str(self.runtime.data_root / "documents"), "--no-geocode",
                        "--signed-examples-path", str(self.runtime.data_root / "state/signed_examples.jsonl"),
                        "--table-aware-pdf"]
                if self.runtime.model_agents:
                    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ECMONITOR_EXTRACTOR_MODEL", "ECMONITOR_VALIDATOR_MODEL"):
                        if not os.environ.get(name):
                            raise ValueError(f"Missing required environment setting: {name}")
                    argv += ["--model-agents", "--agent-timeout-seconds", str(self.runtime.model_timeout_seconds),
                             "--agent-request-timeout-seconds", str(self.runtime.model_timeout_seconds),
                             "--agent-audit-dir", str(self.runtime.data_root / "agent_audit")]
                harness = _build_harness(build_parser().parse_args(argv))
                metadata = {**payload["request"], "workflow_identity": task["identity"],
                            "workflow_signature": self.signature}
                cached = harness.run_document(path, bibliographic_metadata=metadata).to_dict()
            return cached, [self._next(task, "validation", {"report": cached})], "completed"
        if stage == "validation":
            report = payload["report"]
            session_id = report["document_session_id"]
            FulltextControlPlane(self.runtime.data_root / "state/fulltext.sqlite3").ensure_workflow_review_tasks(session_id)
            with sqlite3.connect(self.runtime.data_root / "state/fulltext.sqlite3") as db:
                committed = db.execute("SELECT status FROM document_sessions WHERE document_session_id=?", (session_id,)).fetchone()
                if committed is None or committed[0] != "committed":
                    raise ValueError("Validation requires a committed evidence session")
                pending = db.execute("SELECT COUNT(*) FROM human_review_tasks WHERE document_session_id=? AND status IN ('pending','assigned')", (session_id,)).fetchone()[0]
                pending_records = db.execute("SELECT COUNT(*) FROM observation_records WHERE document_session_id=? AND disposition='pending_human_review'", (session_id,)).fetchone()[0]
                pending = max(pending, pending_records)
            # Independent evidence validation already ran in the scientific harness.
            # This gate never substitutes a queue decision for scientific signoff.
            result = {"document_session_id": session_id, "pending_human_review_count": pending,
                      "policy": "independent-validator-plus-deterministic-gates", "report": report}
            if pending:
                return result, [], "paused_human_review"
            return result, [self._next(task, "commit", result)], "completed"
        if stage == "commit":
            return {"document_id": task["document_id"], "document_session_id": payload["document_session_id"],
                    "scientific_data_authority": "fulltext_sqlite",
                    "release_ready": self.runtime.model_agents}, [], "completed"
        raise ValueError("Unknown stage")

    def _retrieval_workspace(self) -> Path:
        import shutil

        root = self.runtime.data_root / "retrieval"
        for name in ("configs", "schemas", "prompts", "registry", "skills"):
            shutil.copytree(self.runtime.project_root / name, root / name, dirs_exist_ok=True)
        return root

    @staticmethod
    def _committed_report(database: Path, sha256: str, identity: str,
                          signature: dict[str, Any]) -> dict[str, Any] | None:
        if not database.exists():
            return None
        with sqlite3.connect(database) as db:
            rows = db.execute("""SELECT o.payload_json FROM extraction_outbox o
                JOIN document_sessions s USING(document_session_id)
                JOIN document_assets a USING(document_id)
                WHERE a.source_sha256=? AND s.status='committed'
                ORDER BY o.outbox_id DESC""", (sha256,)).fetchall()
        for row in rows:
            report = dict(json.loads(row[0]))
            if report.get("workflow_identity") == identity and report.get("workflow_signature") == signature:
                return report
        return None
