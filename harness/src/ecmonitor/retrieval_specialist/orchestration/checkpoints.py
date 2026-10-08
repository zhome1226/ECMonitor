"""Step-level checkpoints and query rollback helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from ecmonitor.retrieval_specialist.operators.time_utils import utc_now_iso
from ecmonitor.retrieval_specialist.storage.atomic_io import (
    append_jsonl,
    ensure_dir,
    read_json,
    write_json_atomic,
)


class CheckpointManager:
    """Persist resumable step checkpoints."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.checkpoint_dir = ensure_dir(run_dir / "checkpoints")

    def save(self, state: str, payload: dict[str, Any]) -> None:
        checkpoint = {
            "state": state,
            "timestamp": utc_now_iso(),
            "payload": payload,
        }
        write_json_atomic(self.checkpoint_dir / f"{state}.json", checkpoint)
        write_json_atomic(self.checkpoint_dir / "latest.json", checkpoint)

    def latest(self) -> dict[str, Any]:
        return cast(dict[str, Any], read_json(self.checkpoint_dir / "latest.json"))

    def rollback_to_query(self, query_id: str) -> dict[str, Any]:
        query_dir = self.run_dir / "queries" / query_id
        query_path = query_dir / "canonical_query.yaml"
        if not query_path.exists():
            raise FileNotFoundError(f"Cannot roll back to missing query: {query_id}")
        rollback = {
            "timestamp": utc_now_iso(),
            "query_id": query_id,
            "query_path": str(query_path),
            "rollback_actor": "Codex",
        }
        write_json_atomic(self.run_dir / "active_query.json", rollback)
        append_jsonl(
            self.run_dir / "logs" / "decision_log.jsonl", rollback | {"decision": "rollback"}
        )
        self.save("ROLLBACK", rollback)
        return rollback
