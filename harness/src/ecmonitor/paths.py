"""Locate versioned runtime assets in a source release or configured deployment."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    configured = os.environ.get("ECMONITOR_PROJECT_ROOT")
    root = Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]
    if not (root / "schemas").is_dir() or not (root / "configs").is_dir():
        raise RuntimeError("Runtime assets not found; set ECMONITOR_PROJECT_ROOT to the extracted source release")
    return root
