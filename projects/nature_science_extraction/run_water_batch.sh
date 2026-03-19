#!/usr/bin/env bash
set -u
set -o pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT" || exit 1

EXCEL_PATH="/mnt/c/Users/Administrator/Desktop/three_libraries_doi_table_filled.xlsx"
RUN_LOG_DIR="$PROJECT_ROOT/runs"
mkdir -p "$RUN_LOG_DIR"

timestamp="$(date +%Y%m%d_%H%M%S)"
log_path="$RUN_LOG_DIR/water_batch_${timestamp}.log"

python3 - <<'PY' "$EXCEL_PATH" | while IFS= read -r row_id; do
from openpyxl import load_workbook
import sys

excel_path = sys.argv[1]
workbook = load_workbook(excel_path, read_only=True, data_only=True)
sheet = workbook["All_Literature_DOI"]
headers = list(next(sheet.iter_rows(min_row=1, max_row=1, values_only=True)))
index = {name: position for position, name in enumerate(headers)}

for row in sheet.iter_rows(min_row=2, values_only=True):
    if row[index["source_block"]] == "Water2023":
        print(str(row[index["row_id"]]))
PY
  echo "[$(date '+%F %T')] START ${row_id}" | tee -a "$log_path"
  python3 workflow.py \
    --source-block Water2023 \
    --row-id "$row_id" \
    --extractor codex \
    --skip-existing-json \
    --max-source-chars 5000 \
    --codex-timeout-seconds 90 \
    --headless-browser >>"$log_path" 2>&1 || true
  echo "[$(date '+%F %T')] END ${row_id}" | tee -a "$log_path"
done

echo "Water batch finished. Log: $log_path"
