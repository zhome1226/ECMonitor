"""Load versioned Retrieval Specialist configuration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecmonitor.retrieval_specialist.storage.atomic_io import read_yaml


@dataclass(frozen=True)
class LoadedConfig:
    protocol: dict[str, Any]
    scoring: dict[str, Any]
    sources: dict[str, Any]
    model: dict[str, Any]
    stopping: dict[str, Any]
    logging: dict[str, Any]
    runtime: dict[str, Any]
    config_hash: str


class ProtocolLoader:
    """Load YAML config files from a retrieval config directory."""

    required_files = {
        "protocol": "protocol.yaml",
        "scoring": "scoring.yaml",
        "sources": "sources.yaml",
        "model": "model.yaml",
        "stopping": "stopping.yaml",
        "logging": "logging.yaml",
        "runtime": "runtime.yaml",
    }

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir

    def load(self) -> LoadedConfig:
        payloads: dict[str, dict[str, Any]] = {}
        digest = hashlib.sha256()
        for key, filename in self.required_files.items():
            path = self.config_dir / filename
            if not path.exists():
                raise FileNotFoundError(f"Missing retrieval config: {path}")
            raw = path.read_bytes()
            digest.update(filename.encode("utf-8"))
            digest.update(raw)
            loaded = read_yaml(path)
            if not isinstance(loaded, dict):
                raise TypeError(f"Config must be a mapping: {path}")
            payloads[key] = loaded
        return LoadedConfig(
            protocol=payloads["protocol"],
            scoring=payloads["scoring"],
            sources=payloads["sources"],
            model=payloads["model"],
            stopping=payloads["stopping"],
            logging=payloads["logging"],
            runtime=payloads["runtime"],
            config_hash=digest.hexdigest(),
        )
