#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def pdfinfo_pages(path: Path) -> int | None:
    try:
        proc = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def pdftotext_extractable(path: Path) -> bool:
    try:
        proc = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False
    if proc.returncode != 0:
        return False
    text = " ".join(proc.stdout.split())
    return len(text) >= 200


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    args = parser.parse_args()

    pages = pdfinfo_pages(args.pdf)
    extractable = pdftotext_extractable(args.pdf)
    if pages is None:
        status = "pdf_corrupted"
    elif pages <= 1:
        status = "pdf_one_page_only"
    elif not extractable:
        status = "abstract_only"
    else:
        status = "fulltext_ready"
    print(json.dumps({"pdf": str(args.pdf), "pages": pages, "extractable": extractable, "integrity_status": status}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
