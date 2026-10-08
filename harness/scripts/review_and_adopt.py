"""Human review -> signoff -> rule adoption workflow for the extraction harness.

This script closes the quality loop described in
``docs/research/review_standards_research/industry_standards_synthesis.md``:

1. read a batch of human verdicts (JSONL or CSV) over pending human-review tasks;
2. resolve each task in ``control.sqlite3`` (``human_review_resolutions``) and append the signed
   decision to the signoff example store (``signed_review_examples.jsonl``);
3. aggregate accepted/rejected counts per reason code (``SignedExampleStore.reason_code_stats``);
4. propose which reason codes are safe to promote to deterministic gates for the next run and
   write an adoption report plus ``adopted_review_rules.json``.

Verdicts file format (JSONL)::

    {"task_id": "human-review-...", "disposition": "rejected",
     "note": "literature review value, not field measurement",
     "reason_codes": ["literature_summary_value", "default_reject"]}

CSV: columns ``task_id,disposition,note`` (reason codes optional column). Only ``accepted`` or
``rejected`` dispositions are allowed; every pending task must be signed exactly once.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.signoff import SignedExampleStore
from ecmonitor.fulltext_extraction.storage import FulltextControlPlane

_DEFAULT_MIN_SIGNOFFS = 3
_DEFAULT_REJECT_SHARE = 0.9
_DEFAULT_ACCEPT_SHARE = 0.9


def _load_verdicts(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("each JSONL verdict must be an object")
            rows.append(item)
    return rows


def _document_id_for_session(control: FulltextControlPlane, session_id: str) -> str:
    import sqlite3

    try:
        with control._connect() as connection:
            row = connection.execute(
                "SELECT document_id FROM document_sessions WHERE document_session_id = ?",
                (session_id,),
            ).fetchone()
        return str(row["document_id"]) if row is not None else ""
    except (sqlite3.Error, AttributeError):
        return ""


def _adopt_rule(
    code: str, stats: dict[str, int], *, min_signoffs: int, reject_share: float, accept_share: float
) -> dict[str, Any]:
    accepted = int(stats.get("accepted", 0))
    rejected = int(stats.get("rejected", 0))
    total = accepted + rejected
    if total < min_signoffs:
        return {
            "reason_code": code,
            "adopt": False,
            "reason": "insufficient_signoffs",
            "total": total,
            "accepted": accepted,
            "rejected": rejected,
        }
    reject_rate = rejected / total
    accept_rate = accepted / total
    if reject_rate >= reject_share:
        disposition = "reject"
    elif accept_rate >= accept_share:
        disposition = "accept"
    else:
        return {
            "reason_code": code,
            "adopt": False,
            "reason": "split_verdicts",
            "total": total,
            "accepted": accepted,
            "rejected": rejected,
        }
    return {
        "reason_code": code,
        "adopt": True,
        "disposition": disposition,
        "total": total,
        "accepted": accepted,
        "rejected": rejected,
        "reject_rate": round(reject_rate, 3),
        "accept_rate": round(accept_rate, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path, help="run directory containing state/control.sqlite3")
    parser.add_argument("--verdicts", type=Path, help="JSONL/CSV of human verdicts (task_id, disposition)")
    parser.add_argument("--signoff", type=Path, help="signed example store path (default <run_root>/state/signed_review_examples.jsonl)")
    parser.add_argument("--output-dir", type=Path, help="report output dir (default <run_root>/review_adoption)")
    parser.add_argument("--min-signoffs", type=int, default=_DEFAULT_MIN_SIGNOFFS)
    parser.add_argument("--reject-share", type=float, default=_DEFAULT_REJECT_SHARE)
    parser.add_argument("--accept-share", type=float, default=_DEFAULT_ACCEPT_SHARE)
    parser.add_argument("--dry-run", action="store_true", help="validate verdicts without writing")
    args = parser.parse_args()

    db_path = args.run_root / "state" / "control.sqlite3"
    if not db_path.is_file():
        parser.error(f"control db not found: {db_path}")
    verdict_path = args.verdicts
    if verdict_path is None:
        parser.error("--verdicts is required")
    if not verdict_path.is_file():
        parser.error(f"verdicts file not found: {verdict_path}")

    signoff_path = args.signoff or (args.run_root / "state" / "signed_review_examples.jsonl")
    output_dir = args.output_dir or (args.run_root / "review_adoption")
    output_dir.mkdir(parents=True, exist_ok=True)

    verdicts = _load_verdicts(verdict_path)
    control = FulltextControlPlane(db_path)
    store = SignedExampleStore(signoff_path)

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in verdicts:
        task_id = str(row.get("task_id") or row.get("candidate_id") or "").strip()
        disposition = str(row.get("disposition") or "").strip().lower()
        note = str(row.get("note") or "").strip() or None
        if not task_id or disposition not in {"accepted", "rejected"}:
            skipped.append({"task_id": task_id, "reason": "invalid_disposition_or_id"})
            continue
        task = control.get_human_review_task(task_id)
        if task is None:
            skipped.append({"task_id": task_id, "reason": "task_not_found"})
            continue
        session_id = task["document_session_id"]
        reason_codes = tuple(task.get("reason_codes") or ())
        if disposition == "accepted":
            signed_codes = tuple(str(c) for c in (row.get("reason_codes") or reason_codes))
        else:
            signed_codes = tuple(
                dict.fromkeys((*reason_codes, *(row.get("reason_codes") or ())))
            )
        if args.dry_run:
            applied.append(
                {
                    "task_id": task_id,
                    "disposition": disposition,
                    "candidate_id": task["candidate_id"],
                    "reason_codes": list(signed_codes),
                    "dry_run": True,
                }
            )
            continue
        control.resolve_human_review_task(task_id, disposition=disposition, note=note)
        store.record(
            document_id=_document_id_for_session(control, session_id) or session_id,
            document_session_id=session_id,
            candidate_id=task["candidate_id"],
            disposition=disposition,
            reason_codes=signed_codes,
            payload=task["payload"],
        )
        applied.append(
            {
                "task_id": task_id,
                "disposition": disposition,
                "candidate_id": task["candidate_id"],
                "reason_codes": list(signed_codes),
            }
        )

    stats = store.reason_code_stats() if not args.dry_run else {}
    adopted: list[dict[str, Any]] = []
    for code, bucket in sorted(stats.items()):
        adopted.append(
            _adopt_rule(
                code,
                bucket,
                min_signoffs=args.min_signoffs,
                reject_share=args.reject_share,
                accept_share=args.accept_share,
            )
        )

    generated_at = datetime.now(UTC).isoformat()
    report_lines = [
        "# Review & Adopt Report",
        "",
        f"- generated_at: `{generated_at}`",
        f"- run_root: `{args.run_root}`",
        f"- verdicts: `{verdict_path}`",
        f"- signoff store: `{signoff_path}`",
        f"- dry_run: `{args.dry_run}`",
        "",
        f"## Applied verdicts: {len(applied)}",
        "",
    ]
    accepted_count = sum(1 for a in applied if a["disposition"] == "accepted")
    rejected_count = len(applied) - accepted_count
    report_lines.append(f"- accepted: {accepted_count}")
    report_lines.append(f"- rejected: {rejected_count}")
    if skipped:
        report_lines.append(f"- skipped: {len(skipped)}")
        report_lines.append("")
        report_lines.append("| task_id | reason |")
        report_lines.append("|---|---|")
        for s in skipped:
            report_lines.append(f"| {s.get('task_id') or '-'} | {s['reason']} |")
    report_lines.append("")
    report_lines.append("## Reason-code adoption candidates")
    report_lines.append("")
    report_lines.append("| reason_code | total | accepted | rejected | adopt | disposition |")
    report_lines.append("|---|---:|---:|---:|:--:|---|")
    for rule in adopted:
        report_lines.append(
            f"| {rule['reason_code']} | {rule['total']} | {rule['accepted']} | "
            f"{rule['rejected']} | {'yes' if rule['adopt'] else 'no'} | "
            f"{rule.get('disposition', rule.get('reason', ''))} |"
        )
    report_lines.append("")
    report_lines.append(
        f"Adoption threshold: min_signoffs={args.min_signoffs}, "
        f"reject_share>={args.reject_share:.2f}, accept_share>={args.accept_share:.2f}."
    )
    report_lines.append("Promoted rules land in `adopted_review_rules.json`; the deterministic "
                        "gates in `src/ecmonitor/fulltext_extraction/quality.py` already cover the "
                        "core non-field classes.")
    report_text = "\n".join(report_lines)

    rules_payload = {
        "generated_at": generated_at,
        "run_root": str(args.run_root),
        "min_signoffs": args.min_signoffs,
        "reject_share": args.reject_share,
        "accept_share": args.accept_share,
        "applied_count": len(applied),
        "rules": adopted,
    }

    report_path = output_dir / "review_adoption_report.md"
    rules_path = output_dir / "adopted_review_rules.json"
    if not args.dry_run:
        report_path.write_text(report_text, encoding="utf-8")
        rules_path.write_text(json.dumps(rules_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(
        {
            "applied": len(applied),
            "accepted": accepted_count,
            "rejected": rejected_count,
            "skipped": len(skipped),
            "adopted_rules": sum(1 for r in adopted if r["adopt"]),
            "report": str(report_path),
            "rules": str(rules_path),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if not skipped else 2


if __name__ == "__main__":
    raise SystemExit(main())
