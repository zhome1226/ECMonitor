# Batch Extraction Status

Project root:

- `/mnt/c/Users/Administrator/codex/projects/nature_science_extraction`

Current focus:

- Extract all `Nature2024` and `Science2020` records
- Skip `Lancet2023` for now
- Preserve manually curated pilot JSON files while batch-processing the remaining records

Batch commands in use:

```bash
python3 workflow.py --source-block Nature2024 --extractor codex --skip-existing-json --no-browser --max-source-chars 20000 --codex-timeout-seconds 300
python3 workflow.py --source-block Science2020 --extractor codex --skip-existing-json --no-browser --max-source-chars 20000 --codex-timeout-seconds 300
```

One-page PDF web-retry command:

```bash
export ELSEVIER_API_KEY='your_key_here'
python3 workflow.py --from-run Nature2024_20260316_191612_0d897a0e --status-filter failed --failure-reason-contains "pdf has one page or less" --extractor codex --max-source-chars 12000 --codex-timeout-seconds 240
```

Important behavior:

- `runs/` is stored inside this project directory so prior progress is easy to revisit
- Browser login / verification state is reused from `.browser_profiles/default`
- `--skip-existing-json` skips only meaningful existing JSON outputs; stub outputs are eligible for overwrite on reruns
- Exact DOI PDF matches are now preferred over title-similar but incorrect PDFs
- Browser fallback is currently disabled for batch mode, so incomplete local PDFs become explicit stub failures instead of blocking the run
- Web retry mode now supports Elsevier API lookup before browser fallback
- Google search fallback now auto-prefers `ScienceDirect`, then `PubMed/NIH`, then `Semantic Scholar`, then `ResearchGate`
