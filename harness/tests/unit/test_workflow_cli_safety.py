from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecmonitor.orchestration import cli


def test_preflight_requires_source_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ECMONITOR_EXTRACTOR_MODEL", "ECMONITOR_VALIDATOR_MODEL"):
        monkeypatch.setenv(name, "test-setting")
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _: object())
    assert cli.main(["preflight", "--source-root", str(tmp_path / "missing")]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is False
    assert result["source_root_exists"] is False


def test_console_error_boundary_masks_env_credentials(monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    secret = "sk-" + "synthetic" * 5
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def fail() -> int:
        raise ValueError(secret)

    monkeypatch.setattr(cli, "main", fail)
    assert cli.entrypoint() == 1
    assert secret not in capsys.readouterr().out
