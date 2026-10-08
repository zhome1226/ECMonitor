"""Pluggable, authorization-aware acquisition route interface."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

from ecmonitor.download_specialist.models import RouteAttempt


class DownloadRoute(Protocol):
    """A route must never automate credentials, MFA, CAPTCHA, or entitlement decisions."""

    @property
    def name(self) -> str: ...

    def acquire(self, request: dict[str, Any]) -> RouteAttempt: ...


@dataclass(frozen=True)
class LocalInventoryRoute:
    """Only read files inside the operator-configured source directory."""

    root: Path
    name: str = "local_inventory"

    def acquire(self, request: dict[str, Any]) -> RouteAttempt:
        source = request.get("local_path")
        if not isinstance(source, str) or not source:
            return RouteAttempt(self.name, "terminal_failure", "no_local_path")
        root = self.root.resolve()
        path = (root / source).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return RouteAttempt(self.name, "terminal_failure", "local_path_unavailable")
        return RouteAttempt(self.name, "success", "local_inventory_match", path, "pdf")


@dataclass(frozen=True)
class AuthorizedHttpsRoute:
    """Bounded public HTTPS acquisition; no cookies, credential automation, or redirects."""

    output_dir: Path
    allowed_hosts: frozenset[str]
    maximum_bytes: int = 50 * 1024 * 1024
    timeout_seconds: float = 30
    name: str = "authorized_https"

    def acquire(self, request: dict[str, Any]) -> RouteAttempt:
        url = request.get("authorized_url")
        if request.get("access_authorized") is not True or not isinstance(url, str):
            return RouteAttempt(self.name, "terminal_failure", "explicit_authorization_required")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.port not in {None, 443} or host not in self.allowed_hosts):
            return RouteAttempt(self.name, "terminal_failure", "url_policy_rejected")
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                return RouteAttempt(self.name, "terminal_failure", "nonpublic_address_rejected")
        except OSError:
            return RouteAttempt(self.name, "retryable_failure", "dns_unavailable")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.get(url, stream=True, allow_redirects=False,
                                 timeout=self.timeout_seconds) as response:
                    if response.status_code in {401, 403, 429}:
                        return RouteAttempt(self.name, "user_action_required", "access_or_rate_limit_blocked")
                    if response.is_redirect:
                        return RouteAttempt(self.name, "terminal_failure", "redirect_requires_explicit_route")
                    if response.status_code >= 500:
                        return RouteAttempt(self.name, "retryable_failure", "upstream_unavailable")
                    if response.status_code != 200:
                        return RouteAttempt(self.name, "terminal_failure", "http_not_found_or_rejected")
                    digest = hashlib.sha256()
                    total = 0
                    deadline = time.monotonic() + self.timeout_seconds
                    with tempfile.NamedTemporaryFile(dir=self.output_dir, suffix=".partial", delete=False) as handle:
                        temporary = handle.name
                        for block in response.iter_content(chunk_size=64 * 1024):
                            if time.monotonic() > deadline:
                                return RouteAttempt(self.name, "retryable_failure", "download_deadline_exhausted")
                            total += len(block)
                            if total > self.maximum_bytes:
                                return RouteAttempt(self.name, "terminal_failure", "artifact_size_limit")
                            digest.update(block)
                            handle.write(block)
                        handle.flush()
                        os.fsync(handle.fileno())
                    destination = self.output_dir / f"{digest.hexdigest()}.pdf"
                    os.replace(temporary, destination)
                    temporary = None
                    return RouteAttempt(self.name, "success", "authorized_public_download", destination, "pdf")
        except (requests.RequestException, OSError):
            return RouteAttempt(self.name, "retryable_failure", "transport_failure")
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
