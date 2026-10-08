"""Command-line interface for Retrieval Specialist."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ecmonitor.retrieval_specialist.orchestration.run_manager import RunManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecmonitor-retrieval")
    parser.add_argument("--repo-root", default=".", help="Repository root.")
    parser.add_argument("--config-dir", help="Retrieval configuration directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate-config")
    subparsers.add_parser("health-check")
    subparsers.add_parser("db-migrate")
    subparsers.add_parser("db-status")
    subparsers.add_parser("db-integrity-check")

    mock_run = subparsers.add_parser("mock-run")
    mock_run.add_argument("--date-from")
    mock_run.add_argument("--date-to", required=True)
    mock_run.add_argument("--run-id")
    mock_run.add_argument("--target-novel-records", type=int)
    mock_run.add_argument("--max-scan-depth", type=int)
    mock_run.add_argument("--max-iterations", type=int)
    mock_run.add_argument("--fail-after-operator")
    mock_run.add_argument("--fail-after-batch", type=int)
    mock_run.add_argument("--fail-after-source-page", type=int)
    mock_run.add_argument("--stress-records-per-source", type=int)
    mock_run.add_argument("--force-max-iterations", action="store_true")

    dry_run = subparsers.add_parser("dry-run")
    dry_run.add_argument("--date-to", required=True)
    dry_run.add_argument("--source", action="append")
    dry_run.add_argument("--no-llm", action="store_true")

    discovery_dry_run = subparsers.add_parser("discovery-dry-run")
    discovery_dry_run.add_argument("--date-from")
    discovery_dry_run.add_argument("--date-to", required=True)
    discovery_dry_run.add_argument("--max-records-per-provider", type=int, default=5)
    discovery_dry_run.add_argument("--run-id")
    discovery_dry_run.add_argument("--provider", action="append")

    live_preflight = subparsers.add_parser("live-preflight")
    live_preflight.add_argument("--provider", action="append")
    live_preflight.add_argument("--strict", action="store_true")

    run = subparsers.add_parser("run")
    run.add_argument("--date-from", default="2006-01-01")
    run.add_argument("--date-to", required=True)
    run.add_argument("--max-iterations", type=int)
    run.add_argument("--target-novel-records", type=int)
    run.add_argument("--max-scan-depth", type=int)
    run.add_argument("--run-id")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-llm", action="store_true")
    run.add_argument("--allow-degraded-sources", action="store_true")
    run.add_argument("--force-max-iterations", action="store_true")
    run.add_argument("--source", action="append")
    run.add_argument("--log-level", default="INFO")

    resume = subparsers.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--run-id", required=True)

    export = subparsers.add_parser("export-paper-data")
    export_group = export.add_mutually_exclusive_group(required=True)
    export_group.add_argument("--run-id")
    export_group.add_argument("--all-runs", action="store_true")

    audit_pool = subparsers.add_parser("evaluate-audit-pool")
    audit_pool.add_argument("--run-id", required=True)
    audit_pool.add_argument("--audit-pool")

    candidate_pool_audit = subparsers.add_parser("evaluate-candidate-pool-audit")
    candidate_pool_audit.add_argument("--pool-id", required=True)
    candidate_pool_audit.add_argument("--audit-pool")

    candidate_pool = subparsers.add_parser("build-candidate-pool")
    candidate_pool.add_argument("--pool-id")
    candidate_pool.add_argument("--source-run-id", action="append", required=True)

    high_recall_pool = subparsers.add_parser("build-high-recall-candidate-pool")
    high_recall_pool.add_argument("--pool-id")
    high_recall_pool.add_argument("--family-config-dir", action="append", required=True)
    high_recall_pool.add_argument("--date-from")
    high_recall_pool.add_argument("--date-to", required=True)
    high_recall_pool.add_argument("--max-records-per-provider", type=int, default=100)
    high_recall_pool.add_argument("--provider", action="append")

    screen_pool = subparsers.add_parser("screen-candidate-pool")
    screen_pool.add_argument("--pool-id", required=True)
    screen_pool.add_argument("--max-records", type=int)
    screen_pool.add_argument("--provider", action="append")
    screen_pool.add_argument("--query-family", action="append")

    reuse_pool_screening = subparsers.add_parser("reuse-candidate-pool-screening")
    reuse_pool_screening.add_argument("--pool-id", required=True)
    reuse_pool_screening.add_argument("--source-pool-id", action="append", required=True)

    safe_defer_pool_screening = subparsers.add_parser(
        "safe-defer-candidate-pool-screening"
    )
    safe_defer_pool_screening.add_argument("--pool-id", required=True)
    safe_defer_pool_screening.add_argument("--max-records", type=int)

    analyze_pool = subparsers.add_parser("analyze-candidate-pool")
    analyze_pool.add_argument("--pool-id", required=True)

    plan_pool = subparsers.add_parser("plan-query-family-construction")
    plan_pool.add_argument("--pool-id", required=True)

    post_review_plan = subparsers.add_parser("plan-candidate-pool-post-review")
    post_review_plan.add_argument("--pool-id", required=True)

    metadata_enrichment_plan = subparsers.add_parser(
        "plan-candidate-pool-metadata-enrichment"
    )
    metadata_enrichment_plan.add_argument("--pool-id", required=True)
    metadata_enrichment_plan.add_argument("--limit", type=int, default=500)

    metadata_enrichment = subparsers.add_parser("enrich-candidate-pool-metadata")
    metadata_enrichment.add_argument("--pool-id", required=True)
    metadata_enrichment.add_argument("--limit", type=int, default=25)
    metadata_enrichment.add_argument("--batch-size", type=int, default=10)

    rescreen_enriched = subparsers.add_parser(
        "rescreen-enriched-candidate-pool-metadata"
    )
    rescreen_enriched.add_argument("--pool-id", required=True)
    rescreen_enriched.add_argument("--max-records", type=int)

    audit_candidate_pool = subparsers.add_parser("audit-candidate-pool")
    audit_candidate_pool.add_argument("--pool-id", required=True)
    audit_candidate_pool.add_argument("--sample-size", type=int, default=40)

    review_candidate_pool_audit = subparsers.add_parser("review-candidate-pool-audit")
    review_candidate_pool_audit.add_argument("--pool-id", required=True)
    review_candidate_pool_audit.add_argument("--max-records", type=int)

    safe_defer_audit_review = subparsers.add_parser(
        "safe-defer-candidate-pool-audit-review"
    )
    safe_defer_audit_review.add_argument("--pool-id", required=True)
    safe_defer_audit_review.add_argument("--max-records", type=int)

    export_candidate_pool_review = subparsers.add_parser("export-candidate-pool-review")
    export_candidate_pool_review.add_argument("--pool-id", required=True)

    review_priority = subparsers.add_parser("build-candidate-pool-review-priority")
    review_priority.add_argument("--pool-id", required=True)
    review_priority.add_argument("--limit", type=int, default=300)

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--run-id", required=True)
    rollback.add_argument("--query-id", required=True)

    claim = subparsers.add_parser("mock-download-claim")
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--lease-seconds", type=int, default=300)

    complete = subparsers.add_parser("mock-download-complete")
    complete.add_argument("--idempotency-key", required=True)
    complete.add_argument(
        "--final-status",
        choices=["succeeded", "skipped_existing"],
        default="succeeded",
    )

    fail = subparsers.add_parser("mock-download-fail")
    fail.add_argument("--idempotency-key", required=True)
    fail.add_argument("--retryable", action="store_true")
    fail.add_argument("--failure-reason", default="mock_failure")

    subparsers.add_parser("download-queue-status")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    config_dir = Path(args.config_dir).resolve() if getattr(args, "config_dir", None) else None
    manager = RunManager(repo_root=repo_root, config_dir=config_dir)

    if args.command == "validate-config":
        result = manager.validate_config()
    elif args.command == "health-check":
        result = manager.health_check()
    elif args.command == "db-migrate":
        result = manager.db_migrate()
    elif args.command == "db-status":
        result = manager.db_status()
    elif args.command == "db-integrity-check":
        result = manager.db_integrity_check()
    elif args.command in {"mock-run", "dry-run"}:
        if args.command == "dry-run" and (args.source or args.no_llm):
            parser.error(
                "`dry-run` is the deterministic mock run. Use `discovery-dry-run "
                "--provider ...` for bounded live source discovery without GPT."
            )
        result = manager.mock_run(
            date_from=getattr(args, "date_from", None),
            date_to=args.date_to,
            run_id=getattr(args, "run_id", None),
            target_novel_records=getattr(args, "target_novel_records", None),
            max_scan_depth=getattr(args, "max_scan_depth", None),
            max_iterations=getattr(args, "max_iterations", None),
            fail_after_operator=getattr(args, "fail_after_operator", None),
            fail_after_batch=getattr(args, "fail_after_batch", None),
            fail_after_source_page=getattr(args, "fail_after_source_page", None),
            stress_records_per_source=getattr(args, "stress_records_per_source", None),
            force_max_iterations=getattr(args, "force_max_iterations", False),
        )
    elif args.command == "discovery-dry-run":
        result = manager.discovery_dry_run(
            date_from=args.date_from,
            date_to=args.date_to,
            max_records_per_provider=args.max_records_per_provider,
            run_id=args.run_id,
            providers=args.provider,
        )
    elif args.command == "live-preflight":
        result = manager.live_preflight(
            providers=args.provider,
            strict=args.strict,
        )
    elif args.command == "run":
        if args.dry_run:
            parser.error(
                "`run --dry-run` is not a supported live mode. Use "
                "`discovery-dry-run` for bounded source-only validation, or "
                "`mock-run` for deterministic harness validation."
            )
        if args.no_llm:
            parser.error(
                "`run --no-llm` would bypass required live screening. Use "
                "`discovery-dry-run` when GPT screening must not run."
            )
        if args.resume and args.run_id:
            result = manager.resume(args.run_id)
        else:
            result = manager.live_run(
                date_from=args.date_from,
                date_to=args.date_to,
                run_id=args.run_id,
                target_novel_records=args.target_novel_records,
                max_scan_depth=args.max_scan_depth,
                max_iterations=args.max_iterations,
                providers=args.source,
                allow_degraded_sources=args.allow_degraded_sources,
                force_max_iterations=args.force_max_iterations,
                require_stable_provider_auth=True,
            )
    elif args.command == "resume":
        result = manager.resume(args.run_id)
    elif args.command == "inspect":
        result = manager.inspect(args.run_id)
    elif args.command == "export-paper-data":
        if getattr(args, "all_runs", False):
            result = manager.export_all_runs()
        else:
            result = manager.export_paper_data(args.run_id)
    elif args.command == "evaluate-audit-pool":
        result = manager.evaluate_audit_pool(
            args.run_id,
            Path(args.audit_pool).resolve() if args.audit_pool else None,
        )
    elif args.command == "evaluate-candidate-pool-audit":
        result = manager.evaluate_candidate_pool_audit(
            pool_id=args.pool_id,
            audit_pool_path=Path(args.audit_pool).resolve() if args.audit_pool else None,
        )
    elif args.command == "build-candidate-pool":
        result = manager.build_candidate_pool(
            source_run_ids=args.source_run_id,
            pool_id=args.pool_id,
        )
    elif args.command == "build-high-recall-candidate-pool":
        result = manager.build_high_recall_candidate_pool(
            family_config_dirs=[
                Path(value).resolve() for value in args.family_config_dir
            ],
            date_from=args.date_from,
            date_to=args.date_to,
            pool_id=args.pool_id,
            max_records_per_provider=args.max_records_per_provider,
            providers=args.provider,
        )
    elif args.command == "screen-candidate-pool":
        result = manager.screen_candidate_pool(
            pool_id=args.pool_id,
            max_records=args.max_records,
            providers=args.provider,
            query_families=args.query_family,
        )
    elif args.command == "reuse-candidate-pool-screening":
        result = manager.reuse_candidate_pool_screening(
            pool_id=args.pool_id,
            source_pool_ids=args.source_pool_id,
        )
    elif args.command == "safe-defer-candidate-pool-screening":
        result = manager.safe_defer_candidate_pool_screening(
            pool_id=args.pool_id,
            max_records=args.max_records,
        )
    elif args.command == "analyze-candidate-pool":
        result = manager.analyze_candidate_pool(pool_id=args.pool_id)
    elif args.command == "plan-query-family-construction":
        result = manager.plan_query_family_construction(pool_id=args.pool_id)
    elif args.command == "plan-candidate-pool-post-review":
        result = manager.plan_candidate_pool_post_review(pool_id=args.pool_id)
    elif args.command == "plan-candidate-pool-metadata-enrichment":
        result = manager.plan_candidate_pool_metadata_enrichment(
            pool_id=args.pool_id,
            limit=args.limit,
        )
    elif args.command == "enrich-candidate-pool-metadata":
        result = manager.enrich_candidate_pool_metadata(
            pool_id=args.pool_id,
            limit=args.limit,
            batch_size=args.batch_size,
        )
    elif args.command == "rescreen-enriched-candidate-pool-metadata":
        result = manager.rescreen_enriched_candidate_pool_metadata(
            pool_id=args.pool_id,
            max_records=args.max_records,
        )
    elif args.command == "audit-candidate-pool":
        result = manager.audit_candidate_pool(
            pool_id=args.pool_id,
            sample_size=args.sample_size,
        )
    elif args.command == "review-candidate-pool-audit":
        result = manager.review_candidate_pool_audit(
            pool_id=args.pool_id,
            max_records=args.max_records,
        )
    elif args.command == "safe-defer-candidate-pool-audit-review":
        result = manager.safe_defer_candidate_pool_audit_review(
            pool_id=args.pool_id,
            max_records=args.max_records,
        )
    elif args.command == "export-candidate-pool-review":
        result = manager.export_candidate_pool_review(pool_id=args.pool_id)
    elif args.command == "build-candidate-pool-review-priority":
        result = manager.build_candidate_pool_review_priority(
            pool_id=args.pool_id,
            limit=args.limit,
        )
    elif args.command == "rollback":
        result = manager.rollback(args.run_id, args.query_id)
    elif args.command == "mock-download-claim":
        result = manager.mock_download_claim(
            worker_id=args.worker_id, lease_seconds=args.lease_seconds
        )
    elif args.command == "mock-download-complete":
        result = manager.mock_download_complete(
            idempotency_key=args.idempotency_key, final_status=args.final_status
        )
    elif args.command == "mock-download-fail":
        result = manager.mock_download_fail(
            idempotency_key=args.idempotency_key,
            retryable=args.retryable,
            failure_reason=args.failure_reason,
        )
    elif args.command == "download-queue-status":
        result = manager.download_queue_status()
    else:
        parser.error(f"Unsupported command: {args.command}")
        return 2
    _print_json(result)
    return 0


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
