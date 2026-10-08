"""Workflow Orchestrator command line utilities."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from ecmonitor.fulltext_extraction.models import TerminalStatus
from ecmonitor.orchestration.router import route_validation_outcome
from ecmonitor.orchestration.store import WorkflowStore
from ecmonitor.orchestration.worker import Runtime, WorkflowWorker
from ecmonitor.paths import project_root
from ecmonitor.security import redact


def _project_root() -> Path:
    return project_root()


def _validate_layout(root: Path) -> int:
    required = (
        "configs/retrieval",
        "configs/extraction",
        "configs/workflows/ecmonitor_workflow_v1.yaml",
        "schemas/common/task_envelope.schema.json",
        "schemas/handoff/download_request.schema.json",
        "schemas/extraction/occurrence_observation.schema.json",
        "src/ecmonitor/retrieval_specialist",
        "src/ecmonitor/download_specialist",
        "src/ecmonitor/fulltext_extraction",
        "src/ecmonitor/validation_specialist",
        "src/ecmonitor/orchestration",
    )
    missing = [item for item in required if not (root / item).exists()]
    if missing:
        print(json.dumps({"valid": False, "missing": missing}, indent=2))
        return 1
    print(json.dumps({"valid": True, "root": str(root)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecmonitor-workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-layout", help="validate the self-contained harness layout")
    route = subparsers.add_parser("route-validation", help="show deterministic feedback routing")
    route.add_argument("--disposition", required=True)
    route.add_argument("--reason-code", action="append", default=[])
    route.add_argument("--human-review-required", action="store_true")
    route.add_argument("--attempts", type=int, default=0)
    for command in ("enqueue", "worker", "status", "resume", "preflight"):
        runtime = subparsers.add_parser(command)
        runtime.add_argument("--data-root", type=Path, default=Path(os.environ.get("ECMONITOR_DATA_ROOT", "runtime")))
        runtime.add_argument("--source-root", type=Path, default=Path(os.environ.get("ECMONITOR_SOURCE_ROOT", "sources")))
        runtime.add_argument("--allowed-host", action="append", default=[])
        if command == "enqueue":
            runtime.add_argument("--run-id", required=True)
            runtime.add_argument("--manifest", type=Path, help="JSON array of explicitly included acquisition requests")
            runtime.add_argument("--date-from", default="2006-01-01")
            runtime.add_argument("--date-to")
            runtime.add_argument("--max-iterations", type=int, default=1)
            runtime.add_argument("--allow-degraded-sources", action="store_true")
        if command == "worker":
            runtime.add_argument("--once", action="store_true")
            runtime.add_argument("--poll-seconds", type=float, default=5)
        if command == "resume":
            runtime.add_argument("--task-id", required=True)
            runtime.add_argument("--reason", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-layout":
        return _validate_layout(_project_root())
    if args.command == "route-validation":
        outcome = route_validation_outcome(
            cast(TerminalStatus, str(args.disposition)),
            reason_codes=tuple(str(item) for item in args.reason_code),
            human_review_required=bool(args.human_review_required),
            targeted_attempts=int(args.attempts),
        )
        print(json.dumps(outcome.to_dict(), indent=2))
        return 0
    if args.command in {"enqueue", "worker", "status", "resume", "preflight"}:
        runtime = Runtime(_project_root(), args.data_root.resolve(), args.source_root.resolve(),
                          frozenset(host.lower() for host in args.allowed_host))
        if args.command == "preflight":
            names = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ECMONITOR_EXTRACTOR_MODEL", "ECMONITOR_VALIDATOR_MODEL")
            missing = [name for name in names if not os.environ.get(name)]
            missing_modules = [name for name in ("fitz", "pypdf") if importlib.util.find_spec(name) is None]
            ready = not missing and not missing_modules and runtime.source_root.is_dir()
            print(json.dumps({"ready": ready, "missing_environment_settings": missing,
                              "missing_pdf_modules": missing_modules,
                              "source_root_exists": runtime.source_root.is_dir(),
                              "live_smoke_test_required": True}, indent=2))
            return 0 if ready else 1
        store = WorkflowStore(runtime.data_root / "state/workflow.sqlite3")
        if args.command == "status":
            print(json.dumps(store.status(), indent=2))
            return 0
        if args.command == "resume":
            store.resume(args.task_id, reason=args.reason)
            return 0
        if args.command == "enqueue":
            payload = {"runtime": runtime.signature()}
            if args.manifest is not None:
                payload["records"] = json.loads(args.manifest.read_text(encoding="utf-8"))
            else:
                if not args.date_to:
                    raise ValueError("--date-to is required for live retrieval")
                payload.update(date_from=args.date_from, date_to=args.date_to,
                               max_iterations=args.max_iterations,
                               allow_degraded_sources=args.allow_degraded_sources)
            task_id = store.enqueue(run_id=args.run_id, stage="retrieval", payload=payload)
            print(json.dumps({"task_id": task_id}))
            return 0
        if args.poll_seconds <= 0:
            raise ValueError("Poll interval must be positive")
        worker = WorkflowWorker(store, runtime)
        try:
            while True:
                worked = worker.run_once()
                if args.once:
                    return 0
                if not worked:
                    time.sleep(min(args.poll_seconds, 30))
        except KeyboardInterrupt:
            return 0
    raise AssertionError(f"unhandled command: {args.command}")


def entrypoint() -> int:
    """Apply the same safe error boundary to module and installed console usage."""
    try:
        return main()
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": redact(str(exc))}))
        return 1


if __name__ == "__main__":
    raise SystemExit(entrypoint())
