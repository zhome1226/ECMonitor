#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import shlex
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from workflow import (
    DEFAULT_EXCEL_PATH,
    PaperInput,
    enrich_paper_metadata,
    extract_doi_candidate,
    load_inputs,
    normalize_whitespace,
    slugify_doi,
)


PROJECT_ROOT = Path("/mnt/c/Users/Administrator/codex/projects/nature_science_extraction")
HARVEST_ROOT = Path("/home/zhome/auto-paper-harvester")
DEFAULT_AUDIT_ROOT = PROJECT_ROOT / "water_download_audit"
DEFAULT_DOWNLOAD_ROOT = Path("/mnt/d/paper_data/pdf/Water/harvester_downloads")
DOI_SCRIPT = HARVEST_ROOT / ".claude/skills/paper-download/scripts/download_multiple_dois.py"
URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Water2023 papers via auto-paper-harvester and audit PDF page counts.")
    parser.add_argument("--excel-path", type=Path, default=DEFAULT_EXCEL_PATH)
    parser.add_argument("--audit-root", type=Path, default=DEFAULT_AUDIT_ROOT)
    parser.add_argument("--download-root", type=Path, default=DEFAULT_DOWNLOAD_ROOT)
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--batch-index", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--targets-csv", type=Path, help="Reuse an existing water2023_targets.csv file instead of rebuilding targets.")
    parser.add_argument("--checkpoint-base", help="Reuse an existing downloader checkpoint base when auditing prior results.")
    return parser.parse_args()


def extract_urls(*values: str | None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_whitespace(value)
        if not text:
            continue
        for match in URL_RE.finditer(text):
            url = match.group(0).rstrip(".,);]")
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def doi_source_for_record(original: PaperInput, enriched: PaperInput) -> str:
    original_doi = normalize_whitespace(original.doi)
    resolved_doi = normalize_whitespace(enriched.doi)
    if not resolved_doi:
        return ""
    if original_doi and original_doi.lower() == resolved_doi.lower():
        return "original"
    extracted = extract_doi_candidate(original.doi, original.retrieval_key, original.citation_text)
    if extracted and normalize_whitespace(extracted).lower() == resolved_doi.lower():
        return "citation"
    return "crossref"


def pdfinfo_pages(path: Path) -> tuple[int | None, str]:
    command = ["pdfinfo", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None, normalize_whitespace(result.stderr or result.stdout or "pdfinfo failed")
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        return None, "missing Pages field"
    return int(match.group(1)), ""


def choose_primary_pdf(files: list[Path], doi_slug: str) -> Path | None:
    if not files:
        return None
    normalized_slug = doi_slug.lower()

    def sort_key(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        score = 0
        if normalized_slug and path.stem.lower() == normalized_slug:
            score += 100
        if normalized_slug and normalized_slug in name:
            score += 30
        return (-score, -path.stat().st_size, path.name.lower())

    return sorted(files, key=sort_key)[0]


def parse_status_file(path: Path) -> dict[str, list[str]]:
    statuses: dict[str, list[str]] = {}
    if not path.exists():
        return statuses
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        doi, _, payload = line.partition("\t")
        statuses.setdefault(doi.strip(), []).append(payload.strip())
    return statuses


def build_targets(records: list[PaperInput], audit_dir: Path) -> list[dict[str, Any]]:
    records_for_enrichment = [replace(record) for record in records]
    with ThreadPoolExecutor(max_workers=4) as executor:
        enriched_records = list(executor.map(enrich_paper_metadata, records_for_enrichment))
    targets: list[dict[str, Any]] = []
    for original, enriched in zip(records, enriched_records, strict=True):
        resolved_doi = normalize_whitespace(enriched.doi)
        urls = extract_urls(original.doi, original.retrieval_key, original.citation_text)
        targets.append(
            {
                "row_id": original.row_id,
                "record_id": original.record_id,
                "title": normalize_whitespace(enriched.title_hint or enriched.best_title),
                "citation_text": normalize_whitespace(original.citation_text),
                "original_doi": normalize_whitespace(original.doi),
                "resolved_doi": resolved_doi,
                "doi_source": doi_source_for_record(original, enriched),
                "url_candidates": urls,
                "download_target": f"https://doi.org/{resolved_doi}" if resolved_doi else "",
                "download_attempted": bool(resolved_doi),
            }
        )

    targets_csv = audit_dir / "water2023_targets.csv"
    with targets_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "row_id",
                "record_id",
                "title",
                "citation_text",
                "original_doi",
                "resolved_doi",
                "doi_source",
                "download_target",
                "download_attempted",
                "url_candidates",
            ],
        )
        writer.writeheader()
        for row in targets:
            serializable = dict(row)
            serializable["url_candidates"] = "; ".join(row["url_candidates"])
            writer.writerow(serializable)

    doi_file = audit_dir / "water2023_dois.txt"
    seen: set[str] = set()
    ordered_dois: list[str] = []
    for row in targets:
        doi = row["resolved_doi"]
        if doi and doi not in seen:
            seen.add(doi)
            ordered_dois.append(doi)
    doi_file.write_text("\n".join(ordered_dois) + ("\n" if ordered_dois else ""), encoding="utf-8")
    return targets


def load_targets_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    targets: list[dict[str, Any]] = []
    for row in rows:
        targets.append(
            {
                "row_id": row["row_id"],
                "record_id": row["record_id"],
                "title": row["title"],
                "citation_text": row.get("citation_text", ""),
                "original_doi": row["original_doi"],
                "resolved_doi": row["resolved_doi"],
                "doi_source": row["doi_source"],
                "url_candidates": [part.strip() for part in row.get("url_candidates", "").split(";") if part.strip()],
                "download_target": row["download_target"],
                "download_attempted": row["download_attempted"].lower() == "true",
            }
        )
    return targets


def run_downloader(
    audit_dir: Path,
    doi_file: Path,
    download_root: Path,
    *,
    delay: float,
    batch_size: int | None,
    batch_index: int,
    resume: bool,
    overwrite: bool,
) -> str:
    checkpoint_path = audit_dir / f"{audit_dir.name}.checkpoint.json"
    checkpoint_base = checkpoint_path.stem.replace(".checkpoint", "")
    successes_path = HARVEST_ROOT / "downloads/state" / f"{checkpoint_base}_successes.txt"
    failures_path = HARVEST_ROOT / "downloads/state" / f"{checkpoint_base}_failures.txt"
    if not resume:
        for path in (checkpoint_path, successes_path, failures_path):
            if path.exists():
                path.unlink()
    command = [
        "bash",
        "-lc",
        "cd {root} && uv run python {script} --doi-file {doi_file} --output-dir {output_dir} --delay {delay} "
        "{resume} {overwrite} {batch_size} {batch_index} --checkpoint-file {checkpoint}".format(
            root=shlex.quote(str(HARVEST_ROOT)),
            script=shlex.quote(str(DOI_SCRIPT)),
            doi_file=shlex.quote(str(doi_file)),
            output_dir=shlex.quote(str(download_root)),
            delay=shlex.quote(str(delay)),
            resume="--resume" if resume else "",
            overwrite="--overwrite" if overwrite else "",
            batch_size=f"--batch-size {int(batch_size)}" if batch_size else "",
            batch_index=f"--batch-index {int(batch_index)}" if batch_size else "",
            checkpoint=shlex.quote(str(checkpoint_path)),
        ),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    (audit_dir / "download_stdout.log").write_text(result.stdout, encoding="utf-8")
    (audit_dir / "download_stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(
            "auto-paper-harvester downloader failed with exit code "
            f"{result.returncode}. See {audit_dir / 'download_stderr.log'}"
        )
    return checkpoint_base


def audit_downloads(targets: list[dict[str, Any]], audit_dir: Path, download_root: Path, checkpoint_base: str) -> list[dict[str, Any]]:
    failures_path = HARVEST_ROOT / "downloads/state" / f"{checkpoint_base}_failures.txt"
    successes_path = HARVEST_ROOT / "downloads/state" / f"{checkpoint_base}_successes.txt"
    failure_map = parse_status_file(failures_path)
    success_map = parse_status_file(successes_path)

    rows: list[dict[str, Any]] = []
    for target in targets:
        doi = target["resolved_doi"]
        doi_slug = slugify_doi(doi)
        paper_dir = download_root / doi_slug if doi_slug else None
        pdf_files = sorted((paper_dir.glob("*.pdf") if paper_dir and paper_dir.exists() else []))
        primary_pdf = choose_primary_pdf(pdf_files, doi_slug) if pdf_files else None
        pdf_pages, pdfinfo_error = (pdfinfo_pages(primary_pdf) if primary_pdf else (None, ""))

        if not doi:
            status = "no_resolved_doi"
            notes = "No DOI available for auto-paper-harvester API download."
        elif pdf_files:
            if primary_pdf and pdf_pages is None:
                status = "downloaded_invalid_pdf"
                notes = "Downloader saved a file, but pdfinfo could not parse it as a valid PDF."
            else:
                status = "downloaded"
                notes = ""
        elif doi in failure_map:
            status = "download_failed"
            notes = " | ".join(failure_map[doi])
        elif doi in success_map:
            status = "reported_success_no_pdf"
            notes = "Downloader reported success but no PDF directory was found."
        else:
            status = "not_downloaded"
            notes = "Downloader produced no success/failure entry for this DOI."

        if pdfinfo_error:
            notes = f"{notes} | pdfinfo: {pdfinfo_error}".strip(" |")

        rows.append(
            {
                "row_id": target["row_id"],
                "record_id": target["record_id"],
                "title": target["title"],
                "original_doi": target["original_doi"],
                "resolved_doi": doi,
                "doi_source": target["doi_source"],
                "download_target": target["download_target"],
                "download_status": status,
                "download_attempted": "yes" if target["download_attempted"] else "no",
                "download_dir": str(paper_dir) if paper_dir else "",
                "pdf_count": len(pdf_files),
                "primary_pdf": str(primary_pdf) if primary_pdf else "",
                "primary_pdf_pages": pdf_pages if pdf_pages is not None else "",
                "one_page_pdf": "yes" if pdf_pages == 1 else "no" if pdf_pages else "",
                "all_pdfs": "; ".join(str(path) for path in pdf_files),
                "url_candidates": "; ".join(target["url_candidates"]),
                "notes": notes,
            }
        )
    return rows


def write_report(rows: list[dict[str, Any]], audit_dir: Path) -> dict[str, Any]:
    report_csv = audit_dir / "water2023_download_report.csv"
    report_json = audit_dir / "water2023_download_report.json"
    with report_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "row_id",
                "record_id",
                "title",
                "original_doi",
                "resolved_doi",
                "doi_source",
                "download_target",
                "download_status",
                "download_attempted",
                "download_dir",
                "pdf_count",
                "primary_pdf",
                "primary_pdf_pages",
                "one_page_pdf",
                "all_pdfs",
                "url_candidates",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    report_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = Counter(row["download_status"] for row in rows)
    one_page_count = sum(1 for row in rows if row["one_page_pdf"] == "yes")
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_records": len(rows),
        "download_status_counts": dict(sorted(counts.items())),
        "downloaded_records": sum(1 for row in rows if row["download_status"] == "downloaded"),
        "one_page_pdfs": one_page_count,
        "report_csv": str(report_csv),
        "report_json": str(report_json),
    }
    (audit_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_dir = args.audit_root / timestamp
    audit_dir.mkdir(parents=True, exist_ok=True)
    args.download_root.mkdir(parents=True, exist_ok=True)

    if args.targets_csv:
        targets = load_targets_csv(args.targets_csv)
        reused_targets = audit_dir / "water2023_targets.csv"
        reused_targets.write_text(args.targets_csv.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        records = load_inputs(args.excel_path, source_block="Water2023")
        targets = build_targets(records, audit_dir)

    checkpoint_base = f"{audit_dir.name}"
    if args.checkpoint_base:
        checkpoint_base = args.checkpoint_base

    if not args.skip_download:
        checkpoint_base = run_downloader(
            audit_dir,
            audit_dir / "water2023_dois.txt",
            args.download_root,
            delay=args.delay,
            batch_size=args.batch_size,
            batch_index=args.batch_index,
            resume=args.resume,
            overwrite=args.overwrite,
        )

    rows = audit_downloads(targets, audit_dir, args.download_root, checkpoint_base)
    summary = write_report(rows, audit_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
