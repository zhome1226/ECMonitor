#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a run scaffold for an EcoScan benchmark workflow.")
    parser.add_argument("--benchmark", required=True, help="Benchmark profile name, e.g. Nature, Science, EST_TFA")
    parser.add_argument("--targets", required=True, type=Path, help="Input target list file")
    parser.add_argument("--run-root", required=True, type=Path, help="Root directory for run outputs")
    args = parser.parse_args()

    run_id = f"{args.benchmark}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir = args.run_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "run_id": run_id,
        "benchmark": args.benchmark,
        "targets": str(args.targets),
        "run_dir": str(run_dir),
        "next_steps": [
            "1. Execute fulltext-retrieval over the target list.",
            "2. Filter to validated fulltext_ready sources.",
            "3. Execute llm-extraction with the correct profile prompt.",
            "4. Merge outputs and unresolved items.",
        ],
        "artifacts_to_create": [
            "retrieval_manifest.json",
            "reference_audit.csv",
            "unresolved.csv",
            "summary.json",
        ],
    }

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
