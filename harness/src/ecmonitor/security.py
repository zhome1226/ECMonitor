"""Credential-safe diagnostics and runtime payload validation."""

from __future__ import annotations

import os
import re
from typing import Any

_TOKEN = re.compile(r"\b(?:sk-|nvapi-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}\b")
_AUTH = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_QUERY = re.compile(r"(?i)((?:api[_-]?key|token|password|secret|signature)=)[^&\s]+")
_SENSITIVE_FIELD = re.compile(r"(?i)^(?:api[_-]?key|access_token|refresh_token|password|secret|authorization|cookie)$")


def redact(value: Any) -> Any:
    """Mask secrets without including the matched value in diagnostics."""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SENSITIVE_FIELD.fullmatch(str(key)) else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if not isinstance(value, str):
        return value
    result = _QUERY.sub(r"\1[REDACTED]", _AUTH.sub(r"\1[REDACTED]", _TOKEN.sub("[REDACTED]", value)))
    for name, secret in os.environ.items():
        if any(word in name.upper() for word in ("API_KEY", "TOKEN", "PASSWORD", "SECRET")) and len(secret) >= 8:
            result = result.replace(secret, "[REDACTED]")
    return result


def require_secret_free(value: Any) -> None:
    """Do not persist embedded credentials; use environment variables instead."""
    if redact(value) != value:
        raise ValueError("Embedded credentials are not allowed in persisted workflow payloads")
