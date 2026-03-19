# DOI and Web-Fallback Extraction Workflow

This workspace contains a script-first workflow for extracting structured JSON from literature listed in `three_libraries_doi_table_filled.xlsx`.

It supports:

- `Nature2024`
- `Lancet2023`
- `Science2020`

It does not process `Water2023`.

## What the workflow does

1. Reads the `All_Literature_DOI` sheet from the Excel workbook.
2. Filters records by `source_block`, `row_id`, or `limit`.
3. Looks for a matching local PDF in the corresponding library directory.
4. Probes each candidate PDF with `pdfinfo` and `pdftotext`.
5. Uses the local PDF only when it looks like a complete paper.
6. Falls back to DOI or title-based web resolution when the local PDF is missing or incomplete.
7. Runs extraction with `codex exec` or a stub extractor.
8. Writes one JSON file per paper, a merged JSON file, a run manifest, and a run summary.

## Output locations

- Nature: `/mnt/d/paper_data/pdf/Nature/extracted_json`
- Lancet: `/mnt/d/paper_data/pdf/Lancet/extracted_json`
- Science: `/mnt/d/paper_data/pdf/Science/extracted_json`

Run metadata is written under:

- `/mnt/c/Users/Administrator/codex/runs/<run_id>`

## Main files

- `/mnt/c/Users/Administrator/codex/workflow.py`
- `/mnt/c/Users/Administrator/codex/profiles.py`

## Usage

Print record counts:

```bash
python3 workflow.py --print-counts
```

Pilot run, one Nature paper:

```bash
python3 workflow.py --source-block Nature2024 --limit 1
```

Pilot run, one Lancet paper:

```bash
python3 workflow.py --source-block Lancet2023 --limit 1
```

Pilot run, one Science paper:

```bash
python3 workflow.py --source-block Science2020 --limit 1
```

Process one exact row:

```bash
python3 workflow.py --source-block Nature2024 --row-id DOI0001
```

Resume a previous run:

```bash
python3 workflow.py --resume-run <run_id>
```

Use the stub extractor instead of `codex exec`:

```bash
python3 workflow.py --source-block Nature2024 --limit 1 --extractor stub
```

Disable web fallback:

```bash
python3 workflow.py --source-block Nature2024 --limit 1 --no-browser
```

## Browser behavior

- DOI resolution is attempted first when a DOI exists.
- If DOI resolution does not land on a usable source page, the workflow opens Google Scholar.
- If Scholar still does not reach the paper page, the workflow opens Google search.
- When login, captcha, or manual result selection is needed, the script pauses and waits for you to finish in the browser before continuing.

Use `--headless-browser` only if you do not need manual intervention.

## Extraction notes

- `codex` extraction uses the prompt and schema rules defined in `profiles.py`.
- The workflow stores compacted source text snapshots in the run directory before calling Codex.
- If extraction fails, the script writes a schema-shaped stub JSON with failure notes instead of crashing the whole run.

## Tests

Run the lightweight tests with:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```
