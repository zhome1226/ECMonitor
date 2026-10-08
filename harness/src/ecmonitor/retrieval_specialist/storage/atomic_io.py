"""Atomic file I/O for resumable retrieval runs."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_text_atomic(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    last_error: OSError | None = None
    for write_attempt in range(3):
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", delete=False, dir=path.parent
        ) as handle:
            handle.write(content)
            temp_name = handle.name
        try:
            for replace_attempt in range(5):
                try:
                    os.replace(temp_name, path)
                    return
                except PermissionError as exc:
                    last_error = exc
                    if replace_attempt == 4:
                        break
                    time.sleep(0.05 * (replace_attempt + 1))
                except FileNotFoundError as exc:
                    last_error = exc
                    break
        finally:
            if os.path.exists(temp_name):
                with suppress(OSError):
                    os.unlink(temp_name)
        time.sleep(0.05 * (write_attempt + 1))
    if last_error:
        raise last_error


def write_json_atomic(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n")


def write_yaml_atomic(path: Path, payload: Any) -> None:
    write_text_atomic(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=False))


def write_csv_atomic(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", delete=False, dir=path.parent
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            {
                key: _csv_value(value)
                for key, value in row.items()
            }
            for row in rows
        )
        temp_name = handle.name
    os.replace(temp_name, path)


def _csv_value(value: Any) -> Any:
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return value


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(payload), ensure_ascii=True, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                payload = json.loads(stripped)
                if isinstance(payload, dict):
                    yield payload


def count_jsonl(path: Path) -> int:
    return sum(1 for _ in read_jsonl(path))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)
