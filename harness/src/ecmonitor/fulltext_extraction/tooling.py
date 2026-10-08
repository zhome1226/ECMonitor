"""Read-only diagnostics for optional full-text extraction tooling."""

from __future__ import annotations

import importlib.util
import shutil
from typing import Any

_PYTHON_MODULES = {
    "pymupdf": "fast native-text parser (baseline)",
    "pypdf": "PDF integrity and metadata fallback",
    "pdfplumber": "table-oriented PDF inspection fallback",
    "docling": "layout/table/OCR parser benchmark",
    "ocrmypdf": "searchable OCR PDF generation",
    "pytesseract": "Tesseract Python bridge",
    "rdkit": "structure validation and standardization",
    "pint": "dimension-safe concentration unit conversion",
    "jsonschema": "candidate and observation schema validation",
}
_EXECUTABLES = {
    "tesseract": "OCR engine",
    "ocrmypdf": "OCR pipeline CLI",
    "docker": "GROBID sidecar option",
    "java": "local GROBID service option",
    "pdftotext": "Poppler text fallback",
    "mutool": "MuPDF diagnostics",
}


def tool_availability() -> dict[str, Any]:
    modules = {
        name: {
            "available": importlib.util.find_spec(name) is not None,
            "purpose": purpose,
        }
        for name, purpose in _PYTHON_MODULES.items()
    }
    executables = {
        name: {"available": bool(path := shutil.which(name)), "path": path, "purpose": purpose}
        for name, purpose in _EXECUTABLES.items()
    }
    return {
        "python_modules": modules,
        "executables": executables,
        "external_services": {
            "pubchem_pug_rest": {
                "configured_by": "--enable-pubchem",
                "purpose": "name/synonym/structure candidate lookup",
            },
            "grobid": {
                "configured_by": "future adapter URL",
                "purpose": "scholarly TEI, sections, references, coordinates",
            },
            "comptox_dsstox": {
                "configured_by": "future API adapter",
                "purpose": "CASRN/DTXSID and environmental identifier cross-check",
            },
            "opsin": {
                "configured_by": "future service adapter",
                "purpose": "systematic chemical name to structure fallback",
            },
        },
    }
