from __future__ import annotations

from ecmonitor.orchestration.cli import main


def test_self_contained_workflow_layout() -> None:
    assert main(["validate-layout"]) == 0
