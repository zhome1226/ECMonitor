"""Validate every JSON Schema and parse every tracked YAML configuration."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from jsonschema import validators

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures: list[str] = []
    schema_paths = sorted((ROOT / "schemas").rglob("*.schema.json"))
    for schema_path in schema_paths:
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            validators.validator_for(schema).check_schema(schema)
        except Exception as exc:
            failures.append(f"{schema_path.relative_to(ROOT)}: {exc}")

    yaml_paths = sorted((ROOT / "configs").rglob("*.yaml"))
    for yaml_path in yaml_paths:
        try:
            yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append(f"{yaml_path.relative_to(ROOT)}: {exc}")

    if failures:
        for failure in failures:
            print(f"ERROR {failure}")
        return 1
    print(f"validated {len(schema_paths)} JSON Schemas and {len(yaml_paths)} YAML files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
