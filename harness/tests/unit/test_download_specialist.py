from __future__ import annotations

from pathlib import Path

import pytest

from ecmonitor.download_specialist.models import RouteAttempt
from ecmonitor.download_specialist.validator import validate_pdf
from ecmonitor.download_specialist.worker import acquire_document


class LocalRoute:
    name = "local_inventory"

    def __init__(self, path: Path) -> None:
        self.path = path

    def acquire(self, request: dict[str, object]) -> RouteAttempt:
        return RouteAttempt(
            route_name=self.name,
            status="success",
            reason_code="local_match",
            artifact_path=self.path,
            artifact_format="pdf",
        )


def test_validator_rejects_html_renamed_as_pdf(tmp_path: Path) -> None:
    path = tmp_path / "login.pdf"
    path.write_text("<html><body>Sign in</body></html>", encoding="utf-8")
    result = validate_pdf(path, minimum_bytes=1)
    assert not result.valid
    assert result.reason_codes == ("html_instead_of_pdf",)


def test_validator_rejects_missing_file(tmp_path: Path) -> None:
    result = validate_pdf(tmp_path / "missing.pdf")
    assert not result.valid
    assert result.reason_codes == ("missing_file",)


def test_local_route_accepts_readable_pdf(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    path = tmp_path / "article.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)

    outcome = acquire_document(
        {"global_record_id": "doc-1"},
        [LocalRoute(path)],
        require_pdf_parser=True,
    )
    assert outcome.final_status == "matched_local"
    assert outcome.sha256 is not None
    assert outcome.page_count == 2
