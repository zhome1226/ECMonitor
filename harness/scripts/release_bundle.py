"""Build a sanitized source release without Git history or local runtime artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tarfile
from pathlib import Path

ALLOWED_ROOTS = {"src", "scripts", "schemas", "configs", "registry", "prompts", "skills", "tests", "deploy", "docs", "examples"}
ALLOWED_FILES = {"README.md", "pyproject.toml", "requirements-lock.txt", ".env.example", ".gitignore", ".dockerignore", "SECURITY.md", "LICENSE"}
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "archive", "egg-info", "retrieval_experiments"}
TEXT_SUFFIXES = {".py", ".ps1", ".sh", ".md", ".yaml", ".yml", ".toml", ".json", ".jsonl", ".txt", ".csv", ".cfg", ".ini"}
CODE_SUFFIXES = {".py", ".ps1", ".sh", ".js", ".ts", ".css", ".html"}
SECRET_PATTERNS = {
    "credential": re.compile(r"\b(?:sk-|nvapi-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "authorization": re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    "machine_path": re.compile(r"(?i)(?:[A-Z]:[\\/](?:Users|CodexStorage|Documents)|/" + r"Users/[^/\s]+|/" + r"home/(?!runner\b)[^/\s]+)"),
    "personal_email": re.compile(r"[A-Za-z0-9._%+-]+@(?!(?:example\.(?:com|org|net)|localhost)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_ASSIGNMENT = re.compile(r"(?im)^[ \t]*[\"']?(?:[A-Za-z0-9_]*API_KEY|api_key|password|access_token|authorization)[\"']?[ \t]*[:=][ \t]*[\"']?([^\s\"',}]+)")


def release_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in EXCLUDED_PARTS and not name.startswith(".") and not name.endswith(".egg-info")]
        relative_dir = Path(directory).relative_to(root)
        if relative_dir.parts and relative_dir.parts[0] not in ALLOWED_ROOTS:
            dirs[:] = []
            continue
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root)
            if len(relative.parts) == 1 and name not in ALLOWED_FILES:
                continue
            if path.is_symlink() or path.suffix in {".pyc", ".pyo"} or name == ".gitkeep":
                continue
            if path.suffix in TEXT_SUFFIXES or name in ALLOWED_FILES or name in {"Dockerfile", "py.typed"}:
                files.append(path)
    return sorted(files)


def audit(root: Path) -> list[str]:
    """Return only file locations and categories, never sensitive values."""
    findings: list[str] = []
    for path in release_files(root):
        relative = path.relative_to(root)
        if path.stat().st_size > 10 * 1024 * 1024:
            findings.append(f"oversized:{relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{name}:{relative}")
        if path.suffix not in CODE_SUFFIXES:
            for match in _ASSIGNMENT.finditer(text):
                value = match.group(1).lower()
                if value not in {"null", "none", "[redacted]", "changeme"} and not value.startswith(("${", "$env:", "your-")):
                    findings.append(f"embedded_credential_setting:{relative}")
        if path.suffix in CODE_SUFFIXES and _CJK.search(text):
            findings.append(f"non_english_code:{relative}")
    return findings


def build(root: Path, destination: Path) -> dict[str, object]:
    findings = audit(root)
    if findings:
        raise ValueError("Release audit failed: " + "; ".join(findings))
    if destination.exists():
        raise ValueError("Release destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    checksums = {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in release_files(root)}
    with tarfile.open(destination, "w:gz") as archive:
        for path in release_files(root):
            def sanitized_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = 0o644
                return info
            archive.add(path, arcname="ecmonitor/" + str(path.relative_to(root)).replace("\\", "/"), filter=sanitized_info)
    return {"file_count": len(checksums), "archive_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "files": checksums, "git_history_included": False, "runtime_artifacts_included": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.output is None:
        findings = audit(root)
        print(json.dumps({"passed": not findings, "findings": findings}, indent=2))
        return 1 if findings else 0
    manifest_path = args.output.with_suffix(args.output.suffix + ".manifest.json")
    if manifest_path.exists():
        raise ValueError("Release manifest destination already exists")
    manifest = build(root, args.output.resolve())
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"file_count": manifest["file_count"], "archive_sha256": manifest["archive_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
