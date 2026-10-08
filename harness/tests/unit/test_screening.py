from ecmonitor.retrieval_specialist.models import NormalizedRecord
from ecmonitor.retrieval_specialist.operators.screener import TitleAbstractScreener


def record(**overrides: object) -> NormalizedRecord:
    base = {
        "global_record_id": "r1",
        "source_records": [],
        "doi": None,
        "normalized_doi": None,
        "pmid": None,
        "openalex_id": None,
        "semantic_scholar_id": None,
        "crossref_id": None,
        "title_original": "Emerging contaminants",
        "title_normalized": "emerging contaminants",
        "abstract_original": "Measured concentrations in field samples.",
        "abstract_source": "mock",
        "keywords": [],
        "authors": [],
        "first_author": None,
        "publication_date": None,
        "publication_year": 2020,
        "journal_title": None,
        "issn": [],
        "eissn": [],
        "document_type": "journal article",
        "language": "en",
        "source_rank": 1,
        "source_relevance_score": None,
        "retrieved_from": ["mock"],
        "retrieval_timestamp": "2026-07-09T00:00:00Z",
        "raw_metadata_path": None,
        "sampled_matrices": ["river"],
        "study_type": "field monitoring",
        "has_real_field_sample": True,
        "has_concentration_evidence": True,
        "article_ec_scope": "true",
    }
    base.update(overrides)
    return NormalizedRecord(**base)


def decision_for(**overrides: object):
    return TitleAbstractScreener().screen_one(record(**overrides))


def test_mixed_surface_water_and_effluent_is_included_when_surface_water_extractable() -> None:
    decision = decision_for(sampled_matrices=["river", "effluent"])
    assert decision.decision == "include"
    assert decision.reason_codes == ["I_EXTRACTABLE_SURFACE_WATER_IN_MIXED_MATRIX"]


def test_title_review_artifact_is_excluded_even_when_provider_type_is_article() -> None:
    decision = decision_for(
        title_original=(
            "Contaminants of Emerging Concern in Water Resources: "
            "An Introductory Meta-Review of their Occurrence, Impacts, "
            "Management and Treatment"
        ),
        document_type="journal article",
        sampled_matrices=["river"],
    )
    assert decision.decision == "exclude"
    assert decision.reason_codes == ["E_REVIEW_OR_ARTIFACT"]


def test_preface_artifact_is_excluded_even_when_provider_type_is_article() -> None:
    decision = decision_for(
        title_original="Chemicals of Emerging Arctic Concern: Preface",
        document_type="journal article",
        sampled_matrices=["surface water"],
    )
    assert decision.decision == "exclude"
    assert decision.reason_codes == ["E_REVIEW_OR_ARTIFACT"]


def test_prefilter_handles_artifacts_and_high_confidence_primary_records() -> None:
    screener = TitleAbstractScreener()
    artifact = record(
        title_original="Chemicals of Emerging Arctic Concern: Preface",
        document_type="journal article",
    )
    primary = record(
        title_original="Occurrence of emerging contaminants in river surface water",
        document_type="journal article",
    )

    artifact_decision = screener.prefilter_one(artifact)
    assert artifact_decision is not None
    assert artifact_decision.reason_codes == ["E_REVIEW_OR_ARTIFACT"]
    primary_decision = screener.prefilter_one(primary)
    assert primary_decision is not None
    assert primary_decision.decision == "include"


def test_prefilter_includes_title_only_surface_water_occurrence_record() -> None:
    screener = TitleAbstractScreener()
    title_only = record(
        title_original=(
            "The impact of discharge reduction activities on the occurrence "
            "of contaminants of emerging concern in surface water from the Pearl River"
        ),
        abstract_original=None,
        document_type="journal article",
        sampled_matrices=[],
    )

    decision = screener.prefilter_one(title_only)
    assert decision is not None
    assert decision.decision == "include"
    assert decision.reason_codes == ["I_SURFACE_WATER_CONCENTRATION"]


def test_prefilter_includes_title_only_waterbody_occurrence_record() -> None:
    screener = TitleAbstractScreener()
    title_only = record(
        title_original=(
            "Occurrence and distribution of steroid hormones and contaminants "
            "of emerging concern in a south indian water body"
        ),
        abstract_original=None,
        document_type="journal article",
        sampled_matrices=[],
    )

    decision = screener.prefilter_one(title_only)
    assert decision is not None
    assert decision.decision == "include"


def test_prefilter_includes_title_only_antibiotics_surface_water_record() -> None:
    screener = TitleAbstractScreener()
    title_only = record(
        title_original=(
            "Occurrence, spatial distribution, source apportionment, and risk "
            "assessment of antibiotics in Yangtze river surface water"
        ),
        abstract_original=None,
        document_type="journal article",
        sampled_matrices=[],
    )

    decision = screener.prefilter_one(title_only)
    assert decision is not None
    assert decision.decision == "include"


def test_prefilter_includes_abstract_river_monitoring_model_interpretation_record() -> None:
    screener = TitleAbstractScreener()
    monitoring_record = record(
        title_original=(
            "Data-based interpretation of emerging contaminants occurrence "
            "in rivers using a simple advection-reaction model"
        ),
        abstract_original=(
            "The model was tested in the Llobregat River basin with 70 emerging "
            "contaminants. The monitoring network included 14 river sites and "
            "was monitored in 2 campaigns."
        ),
        document_type="journal article",
        sampled_matrices=[],
    )

    decision = screener.prefilter_one(monitoring_record)
    assert decision is not None
    assert decision.decision == "include"


def test_prefilter_excludes_title_only_treatment_noise_without_surface_water_anchor() -> None:
    screener = TitleAbstractScreener()
    title_only = record(
        title_original=(
            "Carbon-derived adsorbent to remove contaminants of emerging concern "
            "from water"
        ),
        abstract_original=None,
        document_type="journal article",
        sampled_matrices=[],
    )

    decision = screener.prefilter_one(title_only)
    assert decision is not None
    assert decision.decision == "exclude"
    assert decision.reason_codes == ["E_LAB_STUDY"]


def test_prefilter_defers_title_only_microplastic_without_surface_water_anchor() -> None:
    screener = TitleAbstractScreener()
    title_only = record(
        title_original="Microplastic-an emerging contaminant of potential concern?",
        abstract_original=None,
        document_type="journal article",
        sampled_matrices=[],
    )

    decision = screener.prefilter_one(title_only)
    assert decision is not None
    assert decision.decision == "defer_metadata"
    assert decision.reason_codes == ["D_METADATA_MISSING"]


def test_prefilter_excludes_title_only_molecular_simulation_noise() -> None:
    screener = TitleAbstractScreener()
    title_only = record(
        title_original=(
            "Exploring the binding mechanism of contaminants of emerging concern "
            "in personal care products to transthyretin using molecular dynamics simulations"
        ),
        abstract_original=None,
        document_type="journal article",
        sampled_matrices=[],
    )

    decision = screener.prefilter_one(title_only)
    assert decision is not None
    assert decision.decision == "exclude"


def test_prefilter_excludes_abstract_adsorption_noise_without_surface_water_anchor() -> None:
    screener = TitleAbstractScreener()
    treatment_record = record(
        title_original=(
            "Management of ciprofloxacin as a contaminant of emerging concern "
            "in water using microalgae bioremediation"
        ),
        abstract_original=(
            "Biosorption is used for managing pharmaceutical wastes in water. "
            "The adsorption process followed a kinetic model."
        ),
        document_type="journal article",
        sampled_matrices=[],
    )

    decision = screener.prefilter_one(treatment_record)
    assert decision is not None
    assert decision.decision == "exclude"


def test_prefilter_excludes_toxicology_biota_noise_without_natural_water_anchor() -> None:
    screener = TitleAbstractScreener()
    toxicology_record = record(
        title_original=(
            "Texture analysis as a discriminating tool in response to a "
            "contaminant of emerging concern"
        ),
        abstract_original=(
            "PFAS exposure groups in common carp kidney were assessed with "
            "rodlet cell degranulation and immunotoxicity endpoints."
        ),
        document_type="journal article",
        sampled_matrices=[],
    )

    decision = screener.prefilter_one(toxicology_record)
    assert decision is not None
    assert decision.decision == "exclude"
    assert decision.reason_codes == ["E_LAB_STUDY"]


def test_surface_water_occurrence_article_with_review_word_is_not_artifact_excluded() -> None:
    decision = decision_for(
        title_original=(
            "Annual review of monitoring data for emerging contaminants "
            "in river surface water"
        ),
        abstract_original=(
            "This primary monitoring study reports measured concentrations "
            "and occurrence patterns from river surface water field samples."
        ),
        document_type="journal article",
        sampled_matrices=["river"],
        study_type="field monitoring",
    )
    assert decision.decision == "include"


def test_downstream_wwtp_river_only_is_not_automatically_excluded() -> None:
    decision = decision_for(
        title_original="River water downstream of wastewater treatment plants",
        sampled_matrices=["river"],
    )
    assert decision.decision == "include"


def test_groundwater_is_excluded() -> None:
    assert decision_for(sampled_matrices=["groundwater"]).reason_codes == ["E_GROUNDWATER"]


def test_drinking_water_plant_is_excluded() -> None:
    assert decision_for(sampled_matrices=["drinking-water treatment plants"]).reason_codes == [
        "E_WATER_PLANT"
    ]


def test_laboratory_degradation_is_excluded() -> None:
    assert decision_for(study_type="laboratory degradation").reason_codes == ["E_LAB_STUDY"]


def test_no_real_field_sample_is_excluded() -> None:
    assert decision_for(has_real_field_sample=False, study_type="modeling").reason_codes == [
        "E_NO_FIELD_SAMPLE"
    ]


def test_no_concentration_evidence_is_excluded() -> None:
    assert decision_for(has_concentration_evidence=False).reason_codes == ["E_NO_CONCENTRATION"]


def test_semiquantitative_nontarget_study_can_be_included() -> None:
    decision = decision_for(study_type="nontarget screening", sampled_matrices=["lake"])
    assert decision.decision == "include"


def test_ambiguous_concentration_evidence_is_deferred() -> None:
    decision = decision_for(has_concentration_evidence=None)
    assert decision.decision == "defer_metadata"
    assert decision.reason_codes == ["D_AMBIGUOUS_CONCENTRATION"]


def test_missing_abstract_is_deferred_not_downloaded_placeholder() -> None:
    decision = decision_for(has_concentration_evidence=None, abstract_original=None)
    assert decision.decision == "defer_metadata"
    assert decision.reason_codes == ["D_METADATA_MISSING"]
