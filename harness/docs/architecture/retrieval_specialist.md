# Retrieval Specialist Architecture

Retrieval Specialist is a harnessed runtime agent. It is not an unrestricted LLM
agent. Deterministic code owns state transitions, scoring, deduplication,
checkpointing, rollback, and persistence. LLM calls, when enabled in a later
phase, are limited to semantic judgment and candidate query proposals with
structured outputs.

The target operator chain is:

ProtocolLoader -> QueryPlanner -> CanonicalQueryCompiler -> SourceAdapters ->
MetadataNormalizer -> GlobalDocumentRegistry -> Deduplicator -> RulePrefilter ->
TitleAbstractScreener -> MetadataEnricher -> SecondPassScreener ->
QueryEvaluator -> CandidateTermMiner -> QueryVariantGenerator -> QuerySelector ->
Accept/Reject/Rollback -> SaturationDetector -> Exporter.
