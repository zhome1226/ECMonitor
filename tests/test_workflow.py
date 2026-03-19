from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import workflow
from profiles import coerce_payload, validate_payload


class WorkflowTests(unittest.TestCase):
    def test_slugify_doi(self) -> None:
        self.assertEqual(
            workflow.slugify_doi("10.1016/j.scitotenv.2014.04.066"),
            "10.1016_j.scitotenv.2014.04.066",
        )

    def test_counts_match_expected_blocks(self) -> None:
        counts = workflow.summarize_counts(workflow.DEFAULT_EXCEL_PATH)
        self.assertEqual(counts["Nature2024"], 327)
        self.assertEqual(counts["Lancet2023"], 144)
        self.assertEqual(counts["Science2020"], 82)

    def test_lancet_payload_validation_requires_evidence(self) -> None:
        payload = coerce_payload(
            "Lancet2023",
            {
                "paper_title": "Example",
                "reported_statistics": [
                    {
                        "pollutant_original": "ciprofloxacin",
                        "matrix": "surface water",
                        "value_original": "12 ng/L",
                        "evidence_snippet": "",
                        "evidence_page": "",
                    }
                ],
            },
        )
        errors = validate_payload("Lancet2023", payload)
        self.assertTrue(any("missing evidence_snippet" in error for error in errors))
        self.assertTrue(any("missing evidence_page" in error for error in errors))

    def test_build_merged_payload_for_science_flattens_results(self) -> None:
        merged = workflow.build_merged_payload(
            "Science2020",
            [
                {
                    "paper_title": "Paper A",
                    "doi": "10.1000/example",
                    "results": [{"pollutant_original": "As", "evidence_snippet": "A", "evidence_page": "1"}],
                }
            ],
        )
        self.assertEqual(merged["source_block"], "Science2020")
        self.assertEqual(len(merged["results"]), 1)
        self.assertEqual(merged["results"][0]["paper_title"], "Paper A")

    def test_best_pdf_prefers_usable_non_dup_with_more_pages(self) -> None:
        paper = workflow.PaperInput(
            row_id="DOI0001",
            library_bucket="bucket",
            record_id="REF0001",
            source_block="Nature2024",
            study_title="Example study",
            entry_type="source",
            title_hint="River contamination study",
            citation_text=None,
            retrieval_key=None,
            doi="10.1000/example",
        )
        candidates = [
            Path("/tmp/10.1000_example__dup1.pdf"),
            Path("/tmp/10.1000_example__main.pdf"),
        ]

        def fake_probe(path_str: str, title_hint: str = "", doi_hint: str = "") -> workflow.PdfProbe:
            if path_str.endswith("__main.pdf"):
                return workflow.PdfProbe(
                    path=path_str,
                    pages=10,
                    size_bytes=500000,
                    text_excerpt="abstract introduction results discussion table figure",
                    text_length=5000,
                    body_hint_count=6,
                    usable=True,
                    reason="usable",
                    title_score=3,
                    doi_score=1,
                    dup_penalty=0,
                )
            return workflow.PdfProbe(
                path=path_str,
                pages=12,
                size_bytes=600000,
                text_excerpt="abstract introduction results discussion table figure",
                text_length=5000,
                body_hint_count=6,
                usable=True,
                reason="usable",
                title_score=3,
                doi_score=1,
                dup_penalty=1,
            )

        with patch("workflow.probe_pdf", side_effect=fake_probe):
            best, _ = workflow.select_best_pdf(paper, candidates)

        self.assertIsNotNone(best)
        self.assertTrue(best.path.endswith("__main.pdf"))

    def test_location_style_title_hint_is_not_used_as_real_title_match(self) -> None:
        paper = workflow.PaperInput(
            row_id="DOI0001",
            library_bucket="bucket",
            record_id="REF0001",
            source_block="Nature2024",
            study_title="Nature Geoscience 2024 - Underestimated burden of PFAS",
            entry_type="source",
            title_hint="TOKYO_2014",
            citation_text="http://doi.org/10.1016/j.scitotenv.2014.04.066",
            retrieval_key="http://doi.org/10.1016/j.scitotenv.2014.04.066",
            doi="10.1016/j.scitotenv.2014.04.066",
        )

        with patch(
            "workflow.list_pdf_files",
            return_value=[Path("/tmp/10.1021_es5006187__Some_Other_Paper.pdf")],
        ):
            candidates = workflow.find_pdf_candidates(paper, Path("/tmp"))

        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
