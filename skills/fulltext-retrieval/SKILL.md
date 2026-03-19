---
name: fulltext-retrieval
description: Retrieve literature full text for environmental database rebuilding. Use when a task requires finding, downloading, validating, and recording article PDFs, HTML full text, or supplements with a fixed priority order: open PDF, publisher API harvest, Zotero plus institutional access, then browser-assisted retrieval.
---

# Fulltext Retrieval

Use this skill whenever the job is to obtain a validated source document before extraction.

## Fixed retrieval order

Always try these in order and stop when one yields usable full text:

1. open or open-access PDF
2. publisher API harvest
3. Zotero plus campus or institutional access
4. browser-assisted retrieval

Do not skip ahead unless an earlier step is impossible for that item.

## Output expectations

For each target, record:

- `source_type`
- `source_url`
- `download_path` or `snapshot_path`
- `pdf_page_count` if applicable
- `integrity_status`
- `failure_reason`
- `supplement_paths`
- `manual_intervention_required`

## Integrity gate

Before handing anything to extraction, verify:

- article identity matches the target
- file is not corrupted
- PDF is not one-page junk or title-only
- text extraction is possible
- supplement links are captured when available

## When to load references

- Read `references/workflow.md` for the exact retrieval decision tree.
- Use `scripts/publisher_api_harvest.py` for DOI-first harvesting and publisher routing.
- Use `scripts/check_pdf_integrity.py` for PDF checks.
