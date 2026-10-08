import importlib.util
from pathlib import Path


def test_no_committed_machine_specific_paths() -> None:
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("release_reproducibility", root / "scripts/release_bundle.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    findings = module.audit(root)
    assert [finding for finding in findings if finding.startswith("machine_path:")] == []
