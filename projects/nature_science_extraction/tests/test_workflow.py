from __future__ import annotations

import unittest
import tempfile
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

    def test_extract_doi_candidate_from_citation_text(self) -> None:
        doi = workflow.extract_doi_candidate(
            "Evlampidou et al. https://doi.org/10.1289/EHP4495"
        )
        self.assertEqual(doi, "10.1289/EHP4495")

    def test_infer_title_from_citation(self) -> None:
        title = workflow.infer_title_from_citation(
            "Abbas S, Hashmi I, Rehman MSU, Qazi IA, Awan MA, Nasir H. 2015. "
            "Monitoring of chlorination disinfection by-products and their associated health risks in drinking water of Pakistan. "
            "J Water Health. 13(1):270-84."
        )
        self.assertEqual(
            title,
            "Monitoring of chlorination disinfection by-products and their associated health risks in drinking water of Pakistan",
        )

    def test_infer_title_from_author_title_journal_citation(self) -> None:
        title = workflow.infer_title_from_citation(
            "Andreola R, Mannigel AR, Bido G de S, et al. Levels of Trihalomethanes in Stored Water from High and Fundamental Schools: Comparison between Two Temporal Data Sets. J Water Resour Prot. 2018;10(06):577-86."
        )
        self.assertEqual(
            title,
            "Levels of Trihalomethanes in Stored Water from High and Fundamental Schools: Comparison between Two Temporal Data Sets",
        )

    def test_enrich_paper_metadata_uses_crossref_for_water_records(self) -> None:
        paper = workflow.PaperInput(
            row_id="DOI0399",
            library_bucket="bucket",
            record_id="REF0411",
            source_block="Water2023",
            study_title="Water Research 2023 - THM global assessment",
            entry_type="source",
            title_hint=None,
            citation_text="Abbas S, Hashmi I, Rehman MSU, Qazi IA, Awan MA, Nasir H. Monitoring of chlorination disinfection by-products and their associated health risks in drinking water of Pakistan. J Water Health. 2015;13(1):270-84.",
            retrieval_key=None,
            doi=None,
        )
        candidate = {
            "DOI": "10.2166/wh.2014.096",
            "title": [
                "Monitoring of chlorination disinfection by-products and their associated health risks in drinking water of Pakistan"
            ],
            "author": [{"family": "Abbas"}],
            "published-print": {"date-parts": [[2015]]},
        }
        with patch("workflow.fetch_crossref_metadata", return_value=candidate):
            enriched = workflow.enrich_paper_metadata(paper)

        self.assertEqual(enriched.doi, "10.2166/wh.2014.096")
        self.assertEqual(
            enriched.title_hint,
            "Monitoring of chlorination disinfection by-products and their associated health risks in drinking water of Pakistan",
        )
        self.assertEqual(enriched.retrieval_key, "10.2166/wh.2014.096")

    def test_crossref_seed_url_prefers_primary_resource(self) -> None:
        work_item = {
            "resource": {"primary": {"URL": "https://ehp.niehs.nih.gov/doi/10.1289/EHP4495"}},
            "link": [{"URL": "https://ehp.niehs.nih.gov/doi/pdf/10.1289/EHP4495"}],
            "URL": "https://doi.org/10.1289/EHP4495",
        }
        with patch("workflow.fetch_crossref_work_by_doi", return_value=work_item):
            seed = workflow.crossref_seed_url_for_doi("10.1289/EHP4495")
        self.assertEqual(seed, "https://ehp.niehs.nih.gov/doi/10.1289/EHP4495")

    def test_select_crossref_candidate_rejects_topically_weak_mismatch(self) -> None:
        items = [
            {
                "DOI": "10.1021/acsestwater.3c00549.s001",
                "title": [
                    "Potential disinfection byproducts-related risks to drinking water? Molecular insights into the dissolved organic matter from photodegradation of polyethylene microplastics"
                ],
                "author": [{"family": "Zhang"}],
                "published-online": {"date-parts": [[2024]]},
            }
        ]
        candidate = workflow.select_crossref_candidate(
            items,
            target_text="Cancer Risks from Disinfection Byproducts of Drinking Water: The Neglected Issue in the Global South",
            first_author="Ambelu",
            year_hint=2017,
        )
        self.assertIsNone(candidate)

    def test_focus_source_text_for_water_keeps_thm_windows(self) -> None:
        text = (
            "header nav footer " * 500
            + "Abstract This research estimated the THMs concentration. "
            + "The total concentration of THMs ranged between 64-84.5 ppb and 74.3-105.9 ppb in drinking water samples. "
            + "footer nav links " * 500
        )
        focused = workflow.focus_source_text("Water2023", text, 1200)
        self.assertIn("THMs", focused)
        self.assertIn("64-84.5 ppb", focused)
        self.assertLessEqual(len(focused), 1200)

    def test_should_fast_exclude_water_source_for_preview_without_values(self) -> None:
        resolution = workflow.SourceResolution(
            run_id="run",
            row_id="DOI0001",
            record_id="REF0001",
            source_block="Water2023",
            status="source_page_ready",
            selected_source_type="web_http_html",
            selected_source_path_or_url="https://example.com/article",
            manual_intervention=False,
            failure_reason=None,
            pdf_candidates=[],
            page_title="Preview",
            notes="Preview text only",
            source_text_path=None,
            page_html_path=None,
            search_strategy=["crossref_http_html"],
        )
        source_text = (
            "Preview of subscription content Abstract Trihalomethanes in drinking water were investigated. "
            "The article discusses THM formation and monitoring context."
        )
        self.assertTrue(workflow.should_fast_exclude_water_source(source_text, resolution))

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

    def test_select_row_ids_from_manifest_items_filters_failed_one_page(self) -> None:
        items = [
            {"row_id": "DOI0001", "status": "failed", "failure_reason": "pdf has one page or less"},
            {"row_id": "DOI0002", "status": "failed", "failure_reason": "pdfinfo failed"},
            {"row_id": "DOI0003", "status": "extracted", "failure_reason": None},
        ]
        selected = workflow.select_row_ids_from_manifest_items(
            items,
            statuses={"failed"},
            failure_reason_contains="pdf has one page or less",
        )
        self.assertEqual(selected, {"DOI0001"})

    def test_navigated_to_source_page_rejects_search_pages(self) -> None:
        self.assertFalse(workflow.navigated_to_source_page("https://doi.org/10.1000/example"))
        self.assertFalse(workflow.navigated_to_source_page("https://scholar.google.com/scholar?q=test"))
        self.assertFalse(workflow.navigated_to_source_page("https://www.google.com/search?q=test"))
        self.assertTrue(workflow.navigated_to_source_page("https://link.springer.com/article/10.1007/example"))

    def test_looks_like_challenge_detects_sciencedirect_error_page(self) -> None:
        text = (
            "There was a problem providing the content you requested. "
            "Reference number: abc123. Enable JavaScript and cookies to continue."
        )
        self.assertTrue(
            workflow.looks_like_challenge(
                "Just a moment...",
                text,
                "https://www.sciencedirect.com/science/article/pii/S0043135419309194",
            )
        )

    def test_choose_preferred_search_result_prioritizes_sciencedirect_then_pubmed(self) -> None:
        results = [
            {
                "href": "https://pubmed.ncbi.nlm.nih.gov/24814036/",
                "text": "PubMed",
                "base_url": "https://www.google.com/search?q=test",
            },
            {
                "href": "https://www.sciencedirect.com/science/article/pii/S0048969714005762",
                "text": "ScienceDirect",
                "base_url": "https://www.google.com/search?q=test",
            },
        ]
        chosen = workflow.choose_preferred_search_result(results)
        self.assertIsNotNone(chosen)
        self.assertIn("sciencedirect.com", chosen["resolved_href"])

    def test_normalize_search_result_target_unwraps_google_redirect(self) -> None:
        href = "/url?q=https://www.sciencedirect.com/science/article/pii/S0048969714005762&sa=U"
        normalized = workflow.normalize_search_result_target(href, "https://www.google.com/search?q=test")
        self.assertEqual(normalized, "https://www.sciencedirect.com/science/article/pii/S0048969714005762")

    def test_resolve_source_prefers_elsevier_api_before_web(self) -> None:
        paper = workflow.PaperInput(
            row_id="DOI0001",
            library_bucket="bucket",
            record_id="REF0001",
            source_block="Nature2024",
            study_title="Example study",
            entry_type="source",
            title_hint="Example title",
            citation_text=None,
            retrieval_key=None,
            doi="10.1016/j.scitotenv.2014.04.066",
        )
        failed_probe = workflow.PdfProbe(
            path="/tmp/example.pdf",
            pages=1,
            size_bytes=1234,
            text_excerpt="sciencedirect elsevier",
            text_length=1000,
            body_hint_count=0,
            usable=False,
            reason="pdf has one page or less",
            title_score=1,
            doi_score=1,
            dup_penalty=0,
        )
        api_resolution = workflow.SourceResolution(
            run_id="run",
            row_id=paper.row_id,
            record_id=paper.record_id,
            source_block=paper.source_block,
            status="source_page_ready",
            selected_source_type="elsevier_api_xml",
            selected_source_path_or_url="https://api.elsevier.com/content/article/pii/S0048969714005762",
            manual_intervention=False,
            failure_reason=None,
            pdf_candidates=[],
            page_title=None,
            notes="Resolved via Elsevier API full-text XML.",
            source_text_path="/tmp/source.txt",
            search_strategy=["elsevier_api"],
        )
        with patch("workflow.find_pdf_candidates", return_value=[Path("/tmp/example.pdf")]), patch(
            "workflow.select_best_pdf", return_value=(failed_probe, [failed_probe])
        ), patch("workflow.get_elsevier_api_key", return_value="secret"), patch(
            "workflow.resolve_via_elsevier_api", return_value=api_resolution
        ) as api_mock, patch("workflow.resolve_via_web") as web_mock:
            resolution = workflow.resolve_source(
                paper,
                Path("/tmp/run"),
                allow_browser=True,
                headless_browser=True,
                browser_timeout_ms=1000,
            )
        self.assertEqual(resolution.selected_source_type, "elsevier_api_xml")
        api_mock.assert_called_once()
        web_mock.assert_not_called()

    def test_resolve_source_uses_http_seed_before_browser_for_non_elsevier(self) -> None:
        paper = workflow.PaperInput(
            row_id="DOI0400",
            library_bucket="bucket",
            record_id="REF0412",
            source_block="Water2023",
            study_title="Water Research 2023 - THM global assessment",
            entry_type="source",
            title_hint="Mutagenicity and genotoxicity of drinking water in Guelma region, Algeria",
            citation_text=None,
            retrieval_key="10.1007/s10661-014-4223-6",
            doi="10.1007/s10661-014-4223-6",
        )
        http_resolution = workflow.SourceResolution(
            run_id="run",
            row_id=paper.row_id,
            record_id=paper.record_id,
            source_block=paper.source_block,
            status="source_page_ready",
            selected_source_type="web_http_html",
            selected_source_path_or_url="https://link.springer.com/article/10.1007/s10661-014-4223-6",
            manual_intervention=False,
            failure_reason=None,
            pdf_candidates=[],
            page_title="Example",
            notes="Resolved via direct HTTP HTML fetch.",
            source_text_path="/tmp/source.txt",
            page_html_path="/tmp/source.html",
            search_strategy=["crossref_http_html"],
        )
        with patch("workflow.find_pdf_candidates", return_value=[]), patch(
            "workflow.crossref_seed_url_for_doi",
            return_value="https://link.springer.com/article/10.1007/s10661-014-4223-6",
        ), patch("workflow.resolve_via_http_seed", return_value=http_resolution) as http_mock, patch(
            "workflow.resolve_via_web"
        ) as web_mock:
            resolution = workflow.resolve_source(
                paper,
                Path("/tmp/run"),
                allow_browser=True,
                headless_browser=True,
                browser_timeout_ms=1000,
            )
        self.assertEqual(resolution.selected_source_type, "web_http_html")
        http_mock.assert_called_once()
        web_mock.assert_not_called()

    def test_resolve_source_falls_through_to_web_after_elsevier_metadata_only(self) -> None:
        paper = workflow.PaperInput(
            row_id="DOI0001",
            library_bucket="bucket",
            record_id="REF0001",
            source_block="Nature2024",
            study_title="Example study",
            entry_type="source",
            title_hint="Example title",
            citation_text=None,
            retrieval_key=None,
            doi="10.1016/j.scitotenv.2014.04.066",
        )
        failed_probe = workflow.PdfProbe(
            path="/tmp/example.pdf",
            pages=1,
            size_bytes=1234,
            text_excerpt="sciencedirect elsevier",
            text_length=1000,
            body_hint_count=0,
            usable=False,
            reason="pdf has one page or less",
            title_score=1,
            doi_score=1,
            dup_penalty=0,
        )
        api_resolution = workflow.SourceResolution(
            run_id="run",
            row_id=paper.row_id,
            record_id=paper.record_id,
            source_block=paper.source_block,
            status="failed",
            selected_source_type="elsevier_api_metadata_only",
            selected_source_path_or_url="https://www.sciencedirect.com/science/article/pii/S0048969714005762",
            manual_intervention=False,
            failure_reason="elsevier_api_no_fulltext",
            pdf_candidates=[],
            page_title=None,
            notes="Elsevier API returned metadata or abstract only.",
            search_strategy=["elsevier_api"],
        )
        web_resolution = workflow.SourceResolution(
            run_id="run",
            row_id=paper.row_id,
            record_id=paper.record_id,
            source_block=paper.source_block,
            status="source_page_ready",
            selected_source_type="web_html",
            selected_source_path_or_url="https://pubmed.ncbi.nlm.nih.gov/24814036/",
            manual_intervention=False,
            failure_reason=None,
            pdf_candidates=[],
            page_title="PubMed",
            notes="Resolved via web fallback.",
            source_text_path="/tmp/web.txt",
            search_strategy=["elsevier_api", "google"],
        )
        with patch("workflow.find_pdf_candidates", return_value=[Path("/tmp/example.pdf")]), patch(
            "workflow.select_best_pdf", return_value=(failed_probe, [failed_probe])
        ), patch("workflow.get_elsevier_api_key", return_value="secret"), patch(
            "workflow.resolve_via_elsevier_api", return_value=api_resolution
        ), patch("workflow.resolve_via_web", return_value=web_resolution) as web_mock:
            resolution = workflow.resolve_source(
                paper,
                Path("/tmp/run"),
                allow_browser=True,
                headless_browser=True,
                browser_timeout_ms=1000,
        )
        self.assertEqual(resolution.selected_source_type, "web_html")
        self.assertEqual(web_mock.call_args.kwargs["seed_url"], api_resolution.selected_source_path_or_url)

    def test_resolve_source_recovers_elsevier_metadata_when_web_fails(self) -> None:
        paper = workflow.PaperInput(
            row_id="DOI0001",
            library_bucket="bucket",
            record_id="REF0001",
            source_block="Nature2024",
            study_title="Example study",
            entry_type="source",
            title_hint="Example title",
            citation_text=None,
            retrieval_key=None,
            doi="10.1016/j.scitotenv.2014.04.066",
        )
        failed_probe = workflow.PdfProbe(
            path="/tmp/example.pdf",
            pages=1,
            size_bytes=1234,
            text_excerpt="sciencedirect elsevier",
            text_length=1000,
            body_hint_count=0,
            usable=False,
            reason="pdf has one page or less",
            title_score=1,
            doi_score=1,
            dup_penalty=0,
        )
        api_resolution = workflow.SourceResolution(
            run_id="run",
            row_id=paper.row_id,
            record_id=paper.record_id,
            source_block=paper.source_block,
            status="failed",
            selected_source_type="elsevier_api_metadata_only",
            selected_source_path_or_url="https://www.sciencedirect.com/science/article/pii/S0048969714005762",
            manual_intervention=False,
            failure_reason="elsevier_api_no_fulltext",
            pdf_candidates=[],
            page_title=None,
            notes="Elsevier API returned metadata or abstract only.",
            source_text_path="/tmp/api.txt",
            search_strategy=["elsevier_api"],
        )
        web_resolution = workflow.SourceResolution(
            run_id="run",
            row_id=paper.row_id,
            record_id=paper.record_id,
            source_block=paper.source_block,
            status="failed",
            selected_source_type="web_html",
            selected_source_path_or_url="https://www.sciencedirect.com/science/article/pii/S0048969714005762",
            manual_intervention=False,
            failure_reason="publisher_access_blocked",
            pdf_candidates=[],
            page_title="Just a moment...",
            notes="Publisher page remained access-blocked.",
            search_strategy=["elsevier_api", "google"],
        )
        with patch("workflow.find_pdf_candidates", return_value=[Path("/tmp/example.pdf")]), patch(
            "workflow.select_best_pdf", return_value=(failed_probe, [failed_probe])
        ), patch("workflow.get_elsevier_api_key", return_value="secret"), patch(
            "workflow.resolve_via_elsevier_api", return_value=api_resolution
        ), patch("workflow.resolve_via_web", return_value=web_resolution):
            resolution = workflow.resolve_source(
                paper,
                Path("/tmp/run"),
                allow_browser=True,
                headless_browser=True,
                browser_timeout_ms=1000,
            )
        self.assertEqual(resolution.selected_source_type, "elsevier_api_metadata_only")
        self.assertEqual(resolution.failure_reason, None)
        self.assertEqual(resolution.source_text_path, "/tmp/api.txt")
        self.assertIn("continuing with Elsevier API metadata or abstract", resolution.notes)

    def test_resolve_source_recovers_incomplete_local_pdf_text_when_web_fails(self) -> None:
        paper = workflow.PaperInput(
            row_id="DOI0001",
            library_bucket="bucket",
            record_id="REF0001",
            source_block="Nature2024",
            study_title="Example study",
            entry_type="source",
            title_hint="Example title",
            citation_text=None,
            retrieval_key=None,
            doi=None,
        )
        failed_probe = workflow.PdfProbe(
            path="/tmp/example.pdf",
            pages=1,
            size_bytes=1234,
            text_excerpt="abstract intro results concentration",
            text_length=1000,
            body_hint_count=1,
            usable=False,
            reason="pdf has one page or less",
            title_score=1,
            doi_score=0,
            dup_penalty=0,
        )
        web_resolution = workflow.SourceResolution(
            run_id="run",
            row_id=paper.row_id,
            record_id=paper.record_id,
            source_block=paper.source_block,
            status="failed",
            selected_source_type="web_html",
            selected_source_path_or_url="https://www.google.com/search?q=test",
            manual_intervention=False,
            failure_reason="web_search_no_source_page",
            pdf_candidates=[],
            page_title="Google",
            notes="Web fallback ended on google_search.",
            search_strategy=["google"],
        )
        with patch("workflow.find_pdf_candidates", return_value=[Path("/tmp/example.pdf")]), patch(
            "workflow.select_best_pdf", return_value=(failed_probe, [failed_probe])
        ), patch("workflow.extract_pdf_text", return_value="Abstract Results concentration in river water 10 ng/L"), patch(
            "workflow.resolve_via_web", return_value=web_resolution
        ):
            resolution = workflow.resolve_source(
                paper,
                Path("/tmp/run"),
                allow_browser=True,
                headless_browser=True,
                browser_timeout_ms=1000,
            )
        self.assertEqual(resolution.selected_source_type, "local_pdf_excerpt")
        self.assertEqual(resolution.failure_reason, None)
        self.assertIn("continuing with incomplete local PDF text", resolution.notes)

    def test_load_payloads_from_output_dir_ignores_merged_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.json").write_text('{"paper_title":"A","records":[]}', encoding="utf-8")
            (root / "b.json").write_text('{"paper_title":"B","records":[]}', encoding="utf-8")
            (root / "all_records.json").write_text('{"source_block":"Nature2024"}', encoding="utf-8")
            payloads = workflow.load_payloads_from_output_dir(root, "all_records.json")
        self.assertEqual([payload["paper_title"] for payload in payloads], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
