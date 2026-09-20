# Public evidence guide

[中文](README_zh.md) · [Research](../docs/research.md) · [Data notice](DATA_NOTICE.md)

These files are selected summaries from the 7–9 September 2026 experiment archive. Each summary records its source result's SHA-256. A hash identifies the reviewed artifact; it is not external replication. The private preservation record maps original files, transformations and published hashes.

| File | Question | Unit / interpretation |
|---|---|---|
| [E01](E01.json) | Equivalent objective representations? | 60 synthetic cases; 12,516 binary states |
| [E02](E02.json) | Selected mapping for downside risk? | 576 dates, 3,456 asset-date pairs; date-block inference |
| [E03](E03.json) | Local quantum messages for stock returns? | Common 485 dates and five seeds; validation-selected classical reference |
| [E04](E04.json) | Risk interface and portfolio tail loss? | Shared financial endpoint and execution proxy |
| [E05](E05.json) | QAOA on fixed small objectives? | 27 objectives from nine base dates; conditional feasible expected gap |
| [E06](E06.json) | Noise-aware circuit selection? | Eight bases after candidate/seed/noise aggregation |
| [E07](E07.json) | Integrated financial endpoint? | 485 intervals, five seeds, cost 0.001; CVaR95 |
| [Hardware](hardware.json) / [table](hardware-per-circuit.csv) | Device vs ideal/local noise references? | 1,000 six-qubit circuits; one calibration |
| [Cloud](cloud.json) / [table](cloud-per-circuit.csv) | Matched cloud reference? | 999 valid of 1,000 accepted; one terminal failure |
| [Numerical audit](independent-numerical-check.json) | Agreement of a second numerical implementation? | Same analysis task; 31,464 reported recomputations; not external replication |

## Reading a result

Check `task_id`: E02's five-session downside-risk task and E03's next-open stock task have different targets. Read `statistical_unit`, endpoint and baseline. Negative loss differences can be favorable without implying return gains. Inspect both uniform and local-search baselines in E05. E06's interval against fixed selection crosses zero.

Hardware intervals are conditional descriptive intervals for one calibration, using original five-circuit groups stratified by family. Submission groups are not independent calibration replications. Public tables omit private job identifiers and retain metrics, so the verifier checks point estimates rather than reconstructing the full grouped bootstrap or raw device probabilities.

The hardware table has 500 QAOA circuits; the cloud-matched table has 499. Their hardware feasibility means therefore differ slightly. Preserve the denominator when quoting a value.

## Transformations and checks

Publication selected experiment fields and paired units, removed job/batch identifiers from circuit tables, retained scientific values and source hashes, and excluded market panels, model files and execution credentials. [index.json](index.json) lists public evidence.

Run `python tools/verify_public_evidence.py` from the repository root to check selected arithmetic. Confidence intervals and p-values remain archived outputs. Full inference reproduction needs dated observations, grouping and configuration.
