#!/usr/bin/env python3
"""
EcoScan publisher API harvester.

This script is inspired by the DOI routing approach used in the upstream
`auto-paper-harvester` project, but renamed and narrowed for EcoScan's
literature-retrieval workflow.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import requests

DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
ELSEVIER_PREFIXES = ("10.1016", "10.1011")
SPRINGER_PREFIXES = ("10.1007", "10.1038", "10.1186")
WILEY_PREFIXES = ("10.1002", "10.1111")


@dataclass
class HarvestResult:
    doi: str
    publisher: str
    status: str
    source_type: str
    url: str | None
    download_path: str | None
    failure_reason: str | None


def classify_publisher(doi: str) -> str:
    lowered = doi.lower()
    if lowered.startswith(ELSEVIER_PREFIXES):
        return "Elsevier"
    if lowered.startswith(SPRINGER_PREFIXES):
        return "Springer"
    if lowered.startswith(WILEY_PREFIXES):
        return "Wiley"
    return "Unknown"


def doi_slug(doi: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", doi).strip("._")


def read_dois(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    seen: set[str] = set()
    out: list[str] = []
    for match in DOI_PATTERN.finditer(raw):
        doi = match.group(0)
        if doi not in seen:
            seen.add(doi)
            out.append(doi)
    return out


def try_elsevier(doi: str, output_dir: Path) -> HarvestResult:
    api_key = os.getenv("ELSEVIER_API_KEY")
    if not api_key:
        return HarvestResult(doi, "Elsevier", "failed", "publisher_api_failed", None, None, "missing ELSEVIER_API_KEY")
    url = f"https://api.elsevier.com/content/article/doi/{doi}"
    headers = {"X-ELS-APIKey": api_key, "Accept": "application/pdf"}
    response = requests.get(url, headers=headers, timeout=90)
    if response.status_code == 200 and response.content.startswith(b"%PDF"):
        article_dir = output_dir / doi_slug(doi)
        article_dir.mkdir(parents=True, exist_ok=True)
        dest = article_dir / f"{doi_slug(doi)}.pdf"
        dest.write_bytes(response.content)
        return HarvestResult(doi, "Elsevier", "ok", "publisher_api_pdf", url, str(dest), None)
    reason = f"http {response.status_code}"
    return HarvestResult(doi, "Elsevier", "failed", "publisher_api_failed", url, None, reason)


def try_springer(doi: str, output_dir: Path) -> HarvestResult:
    api_key = os.getenv("SPRINGER_API_KEY")
    if not api_key:
        return HarvestResult(doi, "Springer", "failed", "publisher_api_failed", None, None, "missing SPRINGER_API_KEY")
    url = f"https://api.springernature.com/openaccess/pdf/{doi}"
    params = {"api_key": api_key}
    response = requests.get(url, params=params, timeout=90)
    if response.status_code == 200 and response.content.startswith(b"%PDF"):
        article_dir = output_dir / doi_slug(doi)
        article_dir.mkdir(parents=True, exist_ok=True)
        dest = article_dir / f"{doi_slug(doi)}.pdf"
        dest.write_bytes(response.content)
        return HarvestResult(doi, "Springer", "ok", "publisher_api_pdf", response.url, str(dest), None)
    reason = f"http {response.status_code}"
    return HarvestResult(doi, "Springer", "failed", "publisher_api_failed", response.url, None, reason)


def try_wiley(doi: str, output_dir: Path) -> HarvestResult:
    token = os.getenv("WILEY_TDM_TOKEN")
    if not token:
        return HarvestResult(doi, "Wiley", "failed", "publisher_api_failed", None, None, "missing WILEY_TDM_TOKEN")
    url = f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}"
    headers = {"Wiley-TDM-Client-Token": token, "Accept": "application/pdf"}
    response = requests.get(url, headers=headers, timeout=90)
    if response.status_code == 200 and response.content.startswith(b"%PDF"):
        article_dir = output_dir / doi_slug(doi)
        article_dir.mkdir(parents=True, exist_ok=True)
        dest = article_dir / f"{doi_slug(doi)}.pdf"
        dest.write_bytes(response.content)
        return HarvestResult(doi, "Wiley", "ok", "publisher_api_pdf", url, str(dest), None)
    reason = f"http {response.status_code}"
    return HarvestResult(doi, "Wiley", "failed", "publisher_api_failed", url, None, reason)


def harvest_one(doi: str, output_dir: Path) -> HarvestResult:
    publisher = classify_publisher(doi)
    if publisher == "Elsevier":
        return try_elsevier(doi, output_dir)
    if publisher == "Springer":
        return try_springer(doi, output_dir)
    if publisher == "Wiley":
        return try_wiley(doi, output_dir)
    return HarvestResult(doi, publisher, "failed", "publisher_api_failed", None, None, "unsupported publisher")


def write_results(results: Iterable[HarvestResult], manifest_path: Path) -> None:
    rows = [asdict(r) for r in results]
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    csv_path = manifest_path.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["doi", "publisher", "status", "source_type", "url", "download_path", "failure_reason"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doi-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    dois = read_dois(args.doi_file)
    if not dois:
        print("No DOIs found.", file=sys.stderr)
        return 1
    results = [harvest_one(doi, args.output_dir) for doi in dois]
    write_results(results, args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
