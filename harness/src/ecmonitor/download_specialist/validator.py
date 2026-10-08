"""Content-based PDF validation used by every acquisition route."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_HTML_MARKERS = (
    b"<html",
    b"<!doctype html",
    b"sign in",
    b"log in",
    b"access denied",
    b"captcha",
    b"cloudflare",
    b"institutional access",
)


@dataclass(frozen=True, slots=True)
class PdfValidationResult:
    path: str
    valid: bool
    sha256: str | None
    byte_size: int
    page_count: int | None
    reason_codes: tuple[str, ...]
    parser: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parser_page_count(path: Path) -> tuple[int | None, str | None, str | None]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, None, None
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            return None, "pypdf", "encrypted_pdf"
        return len(reader.pages), "pypdf", None
    except Exception:
        return None, "pypdf", "unreadable_pdf"


def validate_pdf(
    path: str | Path,
    *,
    minimum_bytes: int = 1024,
    require_parser: bool = False,
    minimum_pages: int = 1,
) -> PdfValidationResult:
    """Reject missing, HTML, preview, truncated, encrypted, or unreadable artifacts."""
    pdf_path = Path(path)
    if not pdf_path.is_file():
        return PdfValidationResult(str(pdf_path), False, None, 0, None, ("missing_file",))

    byte_size = pdf_path.stat().st_size
    if byte_size == 0:
        return PdfValidationResult(str(pdf_path), False, None, 0, None, ("zero_byte_file",))

    with pdf_path.open("rb") as handle:
        header = handle.read(8192)
    lowered = header.lower()
    if not header.startswith(b"%PDF-") and any(marker in lowered for marker in _HTML_MARKERS):
        return PdfValidationResult(
            str(pdf_path), False, _sha256(pdf_path), byte_size, None, ("html_instead_of_pdf",)
        )
    if not header.startswith(b"%PDF-"):
        return PdfValidationResult(
            str(pdf_path), False, _sha256(pdf_path), byte_size, None, ("invalid_pdf_signature",)
        )
    page_count, parser, parser_error = _parser_page_count(pdf_path)
    if parser_error is not None:
        return PdfValidationResult(
            str(pdf_path), False, _sha256(pdf_path), byte_size, None, (parser_error,), parser
        )
    if require_parser and parser is None:
        return PdfValidationResult(
            str(pdf_path), False, _sha256(pdf_path), byte_size, None, ("pdf_parser_unavailable",)
        )
    # A parser-validated PDF may legitimately be very small (for example a generated
    # two-page test or a compact publisher artifact). Use the byte threshold only as a
    # signature-only fallback when no parser is available.
    if parser is None and byte_size < minimum_bytes:
        return PdfValidationResult(
            str(pdf_path), False, _sha256(pdf_path), byte_size, None, ("truncated_or_preview_pdf",)
        )
    if page_count is not None and page_count < minimum_pages:
        return PdfValidationResult(
            str(pdf_path), False, _sha256(pdf_path), byte_size, page_count, ("one_page_preview",), parser
        )

    reasons = ("validated_pdf",) if parser is not None else ("validated_pdf_signature_only",)
    return PdfValidationResult(
        str(pdf_path), True, _sha256(pdf_path), byte_size, page_count, reasons, parser
    )
