# Manuscript data and analysis code

ECMonitor: Closing Environmental Monitoring Data Gaps with AI

This archive contains the query texts and execution metadata for the literature search and code and fixed inputs for the exploratory recognition–study-count analysis.

## Contents

- `retrieval/`: 14 executed query families, their recorded timestamps and provider metadata.
- `analysis/input/`: the 386 chemical identities used in the analysis, in Table S5 order.
- `analysis/recognition_study_count.py`: Dirichlet summaries and three negative binomial models.
- `analysis/reference_results/`: the reported summaries, rate-ratio posterior draws and Table S6 values.

## Run the analysis

The verification environment was Python 3.12.14, NumPy 2.5.3 and SciPy 1.18.1. From the unzipped directory:

```bash
python -m pip install -r requirements.txt
python analysis/recognition_study_count.py
python analysis/verify_results.py
```

The script reads the supplied CSV and writes to `analysis/results/`. Alternate locations can be supplied with `--input` and `--output`. No network access or workbook software is needed to run the analysis. MCMC results can differ slightly across numerical library versions or platforms; the saved posterior draws also permit direct calculation of the reported intervals.

## Input and model

The analysis unit is one standardized chemical identity in the numerical monitoring database. The outcome, `n_studies`, is its number of distinct supporting studies. `row_order` preserves the order in Table S5. Recognition group codes are:

| Code | Status | Identities |
|---|---|---:|
| 0 | formally_listed_or_prioritized | 45 |
| 1 | surface_water_monitoring_concern | 16 |
| 2 | drinking_water_candidate_or_monitoring_concern | 32 |
| 3 | supporting_ecotoxicity_evidence_only | 112 |
| 4 | no_identified_list_coverage | 181 |

`broad_category` retains Pharmaceuticals, Plant protection products, PFAS and Industrial chemicals as separate levels; all other display categories map to Other. Group 4 is the recognition reference and Other is the category reference.

The study count follows a negative binomial distribution with mean mu and shape phi, with variance mu + mu²/phi. The log mean is an intercept plus recognition-group indicators; the adjusted model adds four broad-category indicators. The intercept and log(phi) have independent Normal(0, 2²) priors. Recognition coefficients have Normal(0, 1²) priors in the primary and adjusted models, and Normal(0, 2.5²) priors in the prior-sensitivity model. Category coefficients have Normal(0, 1²) priors.

Posterior sampling uses two random-walk Metropolis chains per model. Each chain runs for 44,000 iterations, discards 12,000 warm-up iterations and retains every fourth subsequent draw (8,000 retained draws per chain). The proposal is tuned during warm-up and fixed afterward. Seeds are 20261002 (status only, SD 1), 20261003 (status only, SD 2.5) and 20261004 (category adjusted). Intervals are pooled posterior 2.5th and 97.5th percentiles; rate-ratio point estimates are posterior medians. Split R-hat, effective sample size and acceptance rates are reported.

For evidence-depth proportions, counts in the 1, 2–4, 5–9 and ≥10 study categories receive symmetric Dirichlet priors with alpha = 1, 0.5 or 0.1. Marginal probabilities for at least two or five studies use the corresponding beta distributions.

These are exploratory associations within the assembled database. They do not estimate differences across all environmental chemicals.

## Verification

The supplied code was run once against the fixed input. All three model summaries and saved posterior rate-ratio arrays exactly reproduced the archived results in the verification environment. All 35 Table S6 summary and diagnostic rows matched at their reported precision. Input row order, group assignments and category mapping were checked against Table S5. Details and checksums are in `verification.json` and `manifest.json`.

## Retrieval coverage

The 14 query families were executed on 31 July 2026, from 03:16:52 to 03:41:53 UTC, with a publication-date constraint of 1 January 2006 through 31 July 2026. Requested providers were Crossref and PubMed. The provider availability recorded for the quaternary-ammonium/disinfectant-residue family was Crossref only; the other families recorded both. The family outputs were combined by union before deduplication and screening.

The trace records each family as `partial`, with 200–203 candidates. Full provider pagination, cursor and request-limit parameters are unavailable in this archive; the query text and metadata do not reconstruct every individual provider request.
