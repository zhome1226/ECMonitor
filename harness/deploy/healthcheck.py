"""Read-only durable worker liveness and SQLite integrity check."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path


if __name__ == "__main__":
    path = Path(os.environ.get("ECMONITOR_DATA_ROOT", "runtime")) / "state/workflow.sqlite3"
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5) as db:
            row = db.execute("SELECT updated_at FROM worker_health WHERE id=1").fetchone()
            healthy = row is not None and time.time() - row[0] < 180
            healthy = healthy and db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except (OSError, sqlite3.Error, ValueError):
        healthy = False
    raise SystemExit(0 if healthy else 1)
