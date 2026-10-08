"""Bounded route runner for Download Specialist plugins."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import islice
from typing import Any

from ecmonitor.download_specialist.models import (
    DownloadOutcome,
    FinalDownloadStatus,
    RouteAttempt,
)
from ecmonitor.download_specialist.routes import DownloadRoute
from ecmonitor.download_specialist.validator import validate_pdf


def acquire_document(
    request: dict[str, Any],
    routes: Iterable[DownloadRoute],
    *,
    require_pdf_parser: bool = True,
    maximum_route_attempts: int = 6,
) -> DownloadOutcome:
    """Try authorized routes in configured order and stop at a durable outcome."""
    global_record_id = str(request.get("global_record_id") or "")
    if not global_record_id:
        raise ValueError("download request requires global_record_id")

    if maximum_route_attempts < 1:
        raise ValueError("Route attempt limit must be positive")
    attempts: list[RouteAttempt] = []
    for route in islice(routes, maximum_route_attempts):
        attempt = route.acquire(request)
        attempts.append(attempt)
        if attempt.status == "user_action_required":
            return DownloadOutcome(
                global_record_id,
                "blocked_user_action",
                tuple(attempts),
                failure_reason=attempt.reason_code,
            )
        if attempt.status == "terminal_failure":
            continue
        if attempt.status != "success" or attempt.artifact_path is None:
            continue
        if attempt.artifact_format == "html":
            return DownloadOutcome(
                global_record_id,
                "web_fulltext_available",
                tuple(attempts),
                artifact_path=attempt.artifact_path,
            )
        validation = validate_pdf(attempt.artifact_path, require_parser=require_pdf_parser)
        if validation.valid:
            final_status: FinalDownloadStatus = (
                "matched_local" if route.name == "local_inventory" else "downloaded"
            )
            return DownloadOutcome(
                global_record_id,
                final_status,
                tuple(attempts),
                artifact_path=attempt.artifact_path,
                sha256=validation.sha256,
                page_count=validation.page_count,
            )
        attempts.append(
            RouteAttempt(
                route_name=f"{route.name}:validation",
                status="terminal_failure",
                reason_code=validation.reason_codes[0],
                artifact_path=attempt.artifact_path,
                artifact_format="pdf",
            )
        )

    retryable = any(attempt.status == "retryable_failure" for attempt in attempts)
    return DownloadOutcome(
        global_record_id,
        "retryable_failure" if retryable else "permanent_skip_no_authorized_access",
        tuple(attempts),
        failure_reason="routes_exhausted",
    )
