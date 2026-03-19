#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/mnt/c/Users/Administrator/codex/projects/nature_science_extraction"
cd "$PROJECT_ROOT"

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <run_id> [limit]" >&2
  exit 1
fi

RUN_ID="$1"
LIMIT_ARGS=()
if [[ $# -eq 2 ]]; then
  LIMIT_ARGS=(--limit "$2")
fi

python3 workflow.py \
  --from-run "$RUN_ID" \
  --status-filter failed \
  --failure-reason-contains "pdf has one page or less" \
  --extractor codex \
  --max-source-chars 12000 \
  --codex-timeout-seconds 240 \
  "${LIMIT_ARGS[@]}"
