"""Download Specialist command line interface."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from jsonschema import Draft202012Validator

from ecmonitor.download_specialist.validator import validate_pdf
from ecmonitor.paths import project_root


def _project_root() -> Path:
    return project_root()


def _validate_request(request_path: Path) -> int:
    schema_path = _project_root() / "schemas" / "handoff" / "download_request.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        for error in errors:
            print(f"ERROR {list(error.path)}: {error.message}")
        return 1
    print("download request is valid")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecmonitor-download")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_file = subparsers.add_parser("validate-file", help="validate a local PDF")
    validate_file.add_argument("path", type=Path)
    validate_file.add_argument("--require-parser", action="store_true")
    validate_file.add_argument("--minimum-pages", type=int, default=1)
    validate_request = subparsers.add_parser("validate-request", help="validate a handoff request")
    validate_request.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-file":
        result = validate_pdf(
            args.path,
            require_parser=bool(args.require_parser),
            minimum_pages=int(args.minimum_pages),
        )
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0 if result.valid else 1
    if args.command == "validate-request":
        return _validate_request(args.path)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
