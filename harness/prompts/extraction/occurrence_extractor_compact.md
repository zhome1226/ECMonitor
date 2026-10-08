# Surface-Water Occurrence Extractor — Compact Contract

Version: `occurrence-extractor-compact-v1.1`

Extract only observations produced by the current paper from **actual environmental surface water**. Treat every request as a fresh document; use no previous-document memory. Return only source-supported data in the response schema supplied by the system.

## Include

- Individually identified organic emerging contaminants, heavy metals/ions measured in the water column, and individually identified transformation/intermediate/metabolite products.
- Preserve the exact reported chemical name, raw value, raw unit, qualifier, and statistic.
- Every formal record represents exactly one chemical × site/matrix/time × statistic. In the compact transport, group all statistics sharing one chemical and context into one row's `measurements` array; the harness expands each measurement locally into its own formal record. Extract **all** supported occurrence statistics, not only the first number in a cell.
- A table cell written as `A–B (C ± D)` requires one compact chemical/context row with three measurement triples: `A` as `minimum`, `B` as `maximum`, and `C` as `mean`. `D` is a standard deviation descriptor and is not emitted as a standalone concentration record. For `n.d–B (C ± D)`, include the censored minimum triple only if useful for detection-limit annotation, plus the measured maximum and mean triples.
- For a transformation product, set row `kind` to `transformation_product` and give the parent name only when the paper states it.
- Use the paper's own abbreviation/full-name definition first, especially for PFAS. Preserve isomer, oxidation-state, homolog, salt/acid, and chain-length specificity. Never merge α/β/γ-HCH or other distinct isomers.
- Define shared matrix/location/time/method/page/table/evidence once in `contexts`; rows reference the context ID. Each compact row is one chemical/context and contains one or more `[raw_value, qualifier, statistic]` measurement triples. Create a new context when any shared binding changes.
- `page` means the PDF page indicated by `CHUNK ... pages=...`, not the printed journal page number.

## Exclude

- Wastewater, WWTP influent/effluent, secondary effluent, reclaimed water, groundwater, sediment, soil, air, biota/tissue, sludge, and concentrations attached to microplastic/material surfaces.
- Laboratory-spiked/synthetic water, treatment/removal experiments without proven field-water provenance, standards, calibration, recovery, blanks, toxicology doses, modeled values, risk thresholds, and regulatory limits.
- Values summarized or cited from another study, including comparison-table rows not explicitly marked as the current study.
- Chemical families/classes, mixtures/products, polymer categories, sums/totals such as PFAS, HCH, ΣPCB, total PAHs, or total metals. Do not emit an umbrella term as an individual chemical.
- LOD/LOQ/MDL/MQL as environmental concentrations. A censored field result may use `less_than`, `not_detected`, or `not_quantified`; do not substitute the method limit as a measured value.
- Classic non-contaminant water-quality parameters such as nutrients/major ions, dissolved oxygen, conductivity, turbidity, salinity, hardness, alkalinity, TDS/TSS, COD/BOD/TOC, Na, K, Ca, and Mg.

## Evidence and uncertainty

- The analyte, value, unit, statistic, matrix, location/time, and current-study provenance must bind to the same text/table relation.
- Keep a short evidence quote or table caption sufficient to verify the relation. Use optional row/column labels when they disambiguate flattened tables.
- Do not invent a missing site, date, method, unit, value, chemical identity, or parent compound. Use null/unknown where the schema permits it.
- Sampling time comes from Methods/site information. Do not replace it with publication year inside extraction unless the supplied document context explicitly instructs that fallback.
- If the relation cannot be reconstructed safely, omit the row rather than guess. The validator handles unresolved cases.

Output JSON only. Do not add explanations or Markdown fences.
