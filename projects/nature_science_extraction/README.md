# DOI and Web-Fallback Extraction Workflow

This project contains a script-first workflow for extracting structured JSON from literature listed in `three_libraries_doi_table_filled.xlsx`.

Project root:

- `/mnt/c/Users/Administrator/codex/projects/nature_science_extraction`

It supports:

- `Nature2024`
- `Water2023`
- `Lancet2023`
- `Science2020`
- `NatComm2017`

Current batch work has been focused on `Nature2024` and `Science2020`, but the workflow now also supports `Water2023` and `NatComm2017` profiles.

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
- Water: `/mnt/d/paper_data/pdf/Water/extracted_json`
- Lancet: `/mnt/d/paper_data/pdf/Lancet/extracted_json`
- Science: `/mnt/d/paper_data/pdf/Science/extracted_json`
- NatComm: `/mnt/d/paper_data/pdf/NatComm/extracted_json`

Run metadata is written under:

- `/mnt/c/Users/Administrator/codex/projects/nature_science_extraction/runs/<run_id>`

## Main files

- `/mnt/c/Users/Administrator/codex/projects/nature_science_extraction/workflow.py`
- `/mnt/c/Users/Administrator/codex/projects/nature_science_extraction/profiles.py`
- `/mnt/c/Users/Administrator/codex/projects/nature_science_extraction/run_nature_science_batch.sh`

## Usage

Print record counts:

```bash
cd /mnt/c/Users/Administrator/codex/projects/nature_science_extraction
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

Pilot run, one Water paper:

```bash
python3 workflow.py --source-block Water2023 --limit 1
```

Pilot run, one Science paper:

```bash
python3 workflow.py --source-block Science2020 --limit 1
```

Pilot run, one Nature Communications paper:

```bash
python3 workflow.py --source-block NatComm2017 --limit 1
```

Process one exact row:

```bash
python3 workflow.py --source-block Nature2024 --row-id DOI0001
```

Retry one-page failures from a previous run with browser fallback enabled:

```bash
python3 workflow.py --from-run Nature2024_20260316_191612_0d897a0e --status-filter failed --failure-reason-contains "pdf has one page or less" --extractor codex --max-source-chars 12000 --codex-timeout-seconds 240
```

Enable the Elsevier API for ScienceDirect / Elsevier DOI fallback:

```bash
export ELSEVIER_API_KEY='your_key_here'
```

The browser session is now persistent by default and reuses cookies and verification state from:

```bash
/mnt/c/Users/Administrator/codex/projects/nature_science_extraction/.browser_profiles/default
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

Batch run Nature and Science without overwriting existing JSON files:

```bash
bash run_nature_science_batch.sh
```

Manual batch commands:

```bash
python3 workflow.py --source-block Nature2024 --extractor codex --skip-existing-json --no-browser --max-source-chars 20000 --codex-timeout-seconds 300
python3 workflow.py --source-block Science2020 --extractor codex --skip-existing-json --no-browser --max-source-chars 20000 --codex-timeout-seconds 300
```

## Browser behavior

- For Elsevier papers, the workflow first tries the Elsevier API using `ELSEVIER_API_KEY`.
- If the Elsevier API only returns metadata or abstract text, the workflow keeps that snapshot and continues to browser fallback.
- Browser fallback reuses one persistent profile for the whole run instead of launching a fresh browser per paper.
- DOI resolution is attempted first when a DOI exists.
- If DOI landing fails with a transient browser/network error, the workflow retries and then falls back to Scholar or Google instead of stopping immediately.
- If DOI resolution does not land on a usable source page, the workflow opens Google Scholar.
- Search-result pages are auto-resolved when possible. The Google preference order is `ScienceDirect` first, then `PubMed/NIH`, then `Semantic Scholar`, then `ResearchGate`.
- When login, captcha, publisher access, or an unresolved search page needs help, the script pauses and waits for you to finish in the browser before continuing.
- The workflow is intended for DOI landing pages, publisher pages, open-access mirrors, institutional access, and supplementary-material links. It does not integrate unauthorized full-text download sources.

Override the browser profile location if needed:

```bash
python3 workflow.py --source-block Nature2024 --browser-profile-dir /tmp/nature-science-profile
```

Use `--headless-browser` only if you do not need manual intervention.

## Extraction notes

- `codex` extraction uses the prompt and schema rules defined in `profiles.py`.
- The workflow stores compacted source text snapshots in the run directory before calling Codex.
- If extraction fails, the script writes a schema-shaped stub JSON with failure notes instead of crashing the whole run.
- `Water2023` and `NatComm2017` write one JSON per paper plus both a merged JSON file and a merged CSV file.

## Tests

Run the lightweight tests with:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```
