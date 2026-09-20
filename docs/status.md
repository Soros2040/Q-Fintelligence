# Project and validation status

[中文](status_zh.md) · [Home](../README.md) · [Reproduction](reproduction.md)

Last source/publication review: **20 September 2026**. This table separates checks executed for the public edition from historical records and remaining development validation.

| Layer | Evidence available | Verification status |
|---|---|---|
| Public objective example | Six synthetic assets, 64 states, 20 feasible baskets | Executed; direct/QUBO/Ising maximum discrepancy `2.60e-15`; feasible minimum confirmed |
| Public evidence arithmetic | Seven experiment summaries, 1,000 hardware rows, 999 cloud rows | Executed; 1,171 numerical comparisons pass at absolute tolerance `1e-10` |
| Teaching notebooks | Three bilingual notebooks, nine code cells, cleared outputs | All code cells executed successfully in fresh namespaces; published metadata remains clear |
| Source syntax and formats | 99 Python files, 32 JSON files, five TOML files, 17 JavaScript modules | Python AST, JSON/TOML parsing and JavaScript syntax checks pass |
| Bundled OpenHands derivative | Wheel, patch and NOTICE against source lock | All three hashes match; upstream rebuild not performed |
| Product tests | 28 TypeScript, 11 main Python and two sidecar test files | Present in source; full suite and clean dependency installation not run in this review |
| Algorithm 2.1.0 | Python source, dependency locks and local synthetic path | Source checked; fresh installation and full model/circuit chain remain to be validated |
| Deployed product | Public entry responds with the workbench title | Entry-page availability checked; authenticated/business flows not revalidated |
| Historical E01–E07 | Archived registry/result identities and selected summaries | Source-result hashes checked; no new training or historical experiment rerun |
| Historical hardware/cloud | Recorded six-qubit results under one calibration | Selected public point estimates recomputed; no new device or cloud jobs |
| Full historical archive | Compact source archive with missing canonical records | Complete end-to-end historical reproduction is not currently established |

The archive contains a software acceptance record reporting version **2.0.0**. It must not be treated as a fresh acceptance run of the source currently labeled **2.1.0**.

## Next validation milestones

1. Install the product on clean Linux/WSL2 with the pinned environment; run the included suites and one mock task.
2. Install the algorithm in clean Python 3.11; record a bounded synthetic method-chain run and its artifacts.
3. Document and test one product-to-algorithm adapter, including incompatible fields and execution modes.
4. Preserve missing historical records where original sources can be recovered; keep source-dataset redistribution rights separate.
5. Design a new evaluation only after its information set, classical references, resource accounting and data permissions are explicit.

Use a [contribution record](contributions.md#contribution-record) to advance a milestone. Update this table with commit, environment and evidence links when a new check is completed.
