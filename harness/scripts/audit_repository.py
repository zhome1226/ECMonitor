"""Fail closed on restricted binaries, oversized files, or likely plaintext credentials."""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from pathlib import Path

TEXT_SUFFIXES = {
    ".cfg", ".csv", ".ini", ".json", ".jsonl", ".md", ".ps1", ".py",
    ".toml", ".txt", ".yaml", ".yml",
}
RESTRICTED_SUFFIXES = {".pdf", ".ris", ".nbib", ".enl", ".enlx", ".cookie"}
IGNORED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tmp_flat_pycache",
    ".tmp_mypy_cache",
    ".tmp_pycache",
    ".tmp_pytest",
}
SECRET_PATTERNS = {
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "nvidia_key": re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    "bearer_token": re.compile(r"(?i)authorization\s*[:=]\s*[\"']?bearer\s+[A-Za-z0-9._~+/-]{20,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def _files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        yield path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--maximum-bytes", type=int, default=10 * 1024 * 1024)
    args = parser.parse_args()
    root = args.root.resolve()
    findings: list[str] = []
    scanned_text = 0

    for path in _files(root):
        relative = path.relative_to(root)
        suffix = path.suffix.casefold()
        size = path.stat().st_size
        if suffix in RESTRICTED_SUFFIXES:
            findings.append(f"restricted file type: {relative}")
        if size > args.maximum_bytes:
            findings.append(f"oversized file ({size} bytes): {relative}")
        if suffix not in TEXT_SUFFIXES:
            continue
        scanned_text += 1
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"non-UTF-8 tracked text candidate: {relative}")
            continue
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"likely {name}: {relative}")

    if findings:
        for finding in findings:
            print(f"ERROR {finding}")
        return 1
    print(f"repository audit passed: {scanned_text} text files scanned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
