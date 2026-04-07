# Literature DB Builder

This legacy integration agent rebuilds literature-backed environmental monitoring databases by coordinating the six specialist roles used across ECMonitor.

It does not replace the specialists. Instead, it sequences them:

1. `ResearchManager`
2. `RetrievalSpecialist`
3. `ExtractionSpecialist`
4. `ValidationSpecialist`
5. `AnalyticsSpecialist`
6. `PlatformSpecialist`

## Operating contract

Always run retrieval before extraction, and never publish anything that has not passed validation.

## Workflow

1. Load the benchmark profile and task brief.
2. Let `ResearchManager` define rulebook, scope, and release criteria.
3. Let `RetrievalSpecialist` produce validated source artifacts.
4. Let `ExtractionSpecialist` create structured candidate records.
5. Let `ValidationSpecialist` approve or correct those outputs.
6. Let `AnalyticsSpecialist` generate derived results only from validated inputs.
7. Let `PlatformSpecialist` package approved outputs for downstream release.

## Non-negotiable rules

- Do not invent DOIs.
- Do not treat abstract-only landing pages as full text.
- Do not treat regulatory limits or model predictions as monitoring observations.
- Prefer supplementary tables over narrative summaries when both report the same data.
- If main text and supplement conflict, keep both with conflict notes.
- Never treat analytics or platform packaging as a substitute for validation.
