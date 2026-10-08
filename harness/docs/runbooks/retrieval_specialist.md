# Retrieval Specialist Runbook

1. Validate configuration.
2. Run source health checks.
3. Start with `mock-run` during Phase 1.
4. Inspect `runs/<run_id>/RUN_SUMMARY.md`, logs, checkpoints, and paper exports.
5. Resume interrupted runs with `resume --run-id <run_id>`.
6. Roll back rejected or unsafe query variants with
   `rollback --run-id <run_id> --query-id <query_id>`.
