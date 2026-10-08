"""Shared classification of model-agent and document-run failures.

Both the library runners (which decide whole-document retry) and the short-lived JSON command
adapters (which decide bounded, same-request transport retries) need to answer one question:
"is this failure retryable and, if so, which category is it?" Keeping the rules in one module
stops the two layers from drifting apart and lets a transport retry at the adapter level reuse
exactly the same vocabulary as a whole-document retry at the batch level.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

RETRYABLE_ERROR_CATEGORIES = frozenset(
    {
        "agent_deadline_exhausted",
        "agent_process_timeout",
        "model_gateway_error",
        "model_response_empty",
        "model_response_invalid",
        "model_transport_timeout",
        "rate_limited",
    }
)

# Categories that signal the model gateway itself is under load. Seeing these is a hint to
# back off concurrency before it piles more work onto a struggling upstream.
TRANSPORT_STRESS_CATEGORIES = frozenset(
    {
        "agent_deadline_exhausted",
        "agent_process_timeout",
        "model_gateway_error",
        "model_response_empty",
        "model_transport_timeout",
        "rate_limited",
    }
)


@dataclass(frozen=True, slots=True)
class ClassifiedDocumentError:
    category: str
    retryable: bool


def classify_document_error(exc: Exception) -> ClassifiedDocumentError:
    """Classify failures without coupling callers to one model adapter."""
    category = model_command_error_category(exc)
    return ClassifiedDocumentError(
        category=category,
        retryable=category in RETRYABLE_ERROR_CATEGORIES,
    )


def model_command_error_category(exc: Exception) -> str:
    """Return a stable category string for one raised model-command exception."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return "agent_process_timeout"
    message = str(exc).casefold()
    if "empty message content" in message or "response has no choices" in message:
        return "model_response_empty"
    if any(
        marker in message
        for marker in (
            "read timed out",
            "model transport failed",
            "no first content token within",
            "stream idle for",
            "stream deadline exhausted before completion",
        )
    ):
        return "model_transport_timeout"
    if "budget exhausted" in message:
        return "agent_deadline_exhausted"
    if "429" in message or "rate limit" in message:
        return "rate_limited"
    if "model gateway" in message:
        return "model_gateway_error"
    if any(
        marker in message
        for marker in (
            "model response failed local validation",
            "agent command did not return valid json",
            "model content was not valid json",
            "batch validator returned",
            "batch validator candidate_id mismatch",
        )
    ):
        return "model_response_invalid"
    return "non_retryable_error"


def is_transport_stress_category(category: str) -> bool:
    return category in TRANSPORT_STRESS_CATEGORIES
