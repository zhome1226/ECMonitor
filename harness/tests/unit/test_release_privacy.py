from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path

import pytest


def exporter():
    path = Path(__file__).resolve().parents[2] / "scripts/release_bundle.py"
    spec = importlib.util.spec_from_file_location("release_exporter", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_excludes_runtime_credentials_and_history(tmp_path: Path) -> None:
    module = exporter()
    for directory in ("src", "runtime", "state", "releases", ".git", "docs/archive"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
        (tmp_path / directory / "private.json").write_text("{}")
    (tmp_path / ".env").write_text("PRIVATE_SETTING=ignored")
    output = tmp_path / "releases/source.tar.gz"
    manifest = module.build(tmp_path, output)
    assert manifest["git_history_included"] is False
    with tarfile.open(output) as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == ["ecmonitor/src/private.json"]
        assert all(member.uname == "" and member.gname == "" for member in members)


def test_release_rejects_secret_and_non_english_code(tmp_path: Path) -> None:
    module = exporter()
    source = tmp_path / "src"
    source.mkdir()
    secret = "sk-" + "synthetic" * 5
    (source / "bad.py").write_text(f"# {chr(0x4e00)}\nvalue={secret!r}", encoding="utf-8")
    findings = module.audit(tmp_path)
    assert len(findings) == 2
    assert secret not in str(findings)
    with pytest.raises(ValueError, match="audit failed"):
        module.build(tmp_path, tmp_path / "source.tar.gz")


def test_candidate_source_release_passes_privacy_gate() -> None:
    assert exporter().audit(Path(__file__).resolve().parents[2]) == []
