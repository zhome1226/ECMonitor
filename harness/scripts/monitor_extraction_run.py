"""Snapshot progress of a full-text extraction run from its SQLite control DB + events."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    args = parser.parse_args()
    db = args.run_root / "state" / "control.sqlite3"
    if not db.is_file():
        print("no control db:", db)
        return 0
    con = sqlite3.connect(db)
    sessions = con.execute(
        "SELECT status, COUNT(*) FROM document_sessions GROUP BY status"
    ).fetchall()
    candidates = con.execute("SELECT COUNT(*) FROM extraction_candidates").fetchone()[0]
    obs = con.execute("SELECT disposition, COUNT(*) FROM observation_records GROUP BY disposition").fetchall()
    reviews = con.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
    last_session = con.execute(
        "SELECT s.started_at, d.source_path FROM document_sessions s JOIN document_assets d "
        "ON d.document_id=s.document_id ORDER BY s.started_at DESC LIMIT 1"
    ).fetchone()
    con.close()
    events = []
    ev_path = args.run_root / "events.jsonl"
    if ev_path.is_file():
        for line in ev_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    ev_counts = Counter(str(e.get("status")) for e in events)
    recent_fail = [e for e in events if e.get("status") == "failed"]
    audit_path = args.run_root / "model_audit" / "model_calls.jsonl"
    audit_n = 0
    if audit_path.is_file():
        with audit_path.open(encoding="utf-8", errors="replace") as handle:
            audit_n = sum(1 for _ in handle)
    print(json.dumps({
        "sessions_by_status": dict(sessions),
        "extraction_candidates": candidates,
        "observations": dict(obs),
        "reviews": reviews,
        "last_session": (last_session[1].split("\\")[-1], last_session[0]) if last_session else None,
        "events_status": dict(ev_counts),
        "recent_failed_dois": [e.get("source_path","").split("\\")[-1].replace(".pdf","") for e in recent_fail[-5:]],
        "model_calls": audit_n,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
