"""Download Specialist value objects."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

RouteStatus = Literal["success", "retryable_failure", "terminal_failure", "user_action_required"]
FinalDownloadStatus = Literal[
    "downloaded",
    "matched_local",
    "web_fulltext_available",
    "retryable_failure",
    "blocked_user_action",
    "permanent_skip_no_authorized_access",
    "invalid_artifact",
]


@dataclass(frozen=True, slots=True)
class RouteAttempt:
    route_name: str
    status: RouteStatus
    reason_code: str
    artifact_path: Path | None = None
    artifact_format: Literal["pdf", "html", "none"] = "none"
    source_identity: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifact_path"] = None if self.artifact_path is None else str(self.artifact_path)
        return payload


@dataclass(frozen=True, slots=True)
class DownloadOutcome:
    global_record_id: str
    final_status: FinalDownloadStatus
    attempts: tuple[RouteAttempt, ...]
    artifact_path: Path | None = None
    sha256: str | None = None
    page_count: int | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifact_path"] = None if self.artifact_path is None else str(self.artifact_path)
        payload["attempts"] = [attempt.to_dict() for attempt in self.attempts]
        return payload

    def to_handoff(self, *, idempotency_key: str, download_job_id: str) -> dict[str, Any]:
        """Convert an acquisition result into the versioned retrieval handoff."""
        return {
            **self.to_dict(),
            "schema_version": "download-result-v1",
            "event_id": f"download-result-{uuid.uuid4().hex}",
            "idempotency_key": idempotency_key,
            "download_job_id": download_job_id,
            "local_inventory_record_id": self.global_record_id if self.final_status == "matched_local" else None,
            "PDF_SHA256": self.sha256,
            "completed_at": datetime.now(UTC).isoformat(),
        }
