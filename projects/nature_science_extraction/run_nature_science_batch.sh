#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

python3 workflow.py \
  --source-block Nature2024 \
  --extractor codex \
  --skip-existing-json \
  --no-browser \
  --max-source-chars 20000 \
  --codex-timeout-seconds 300

python3 workflow.py \
  --source-block Science2020 \
  --extractor codex \
  --skip-existing-json \
  --no-browser \
  --max-source-chars 20000 \
  --codex-timeout-seconds 300
