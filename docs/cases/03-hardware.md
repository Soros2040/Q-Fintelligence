# Case 03 · Explain the gap between ideal circuits and hardware

[中文](03-hardware_zh.md) · [Research map](../research.md) · [Notebook](../../notebooks/03_hardware_transfer.ipynb)

## The matching problem

A hardware result is interpretable only when its logical circuit, parameters, physical mapping, measurement order, shots and device calibration are identified. A device's advertised register size says little about the size or quality of the experiment. This study uses six-qubit circuits on the Tianyan-287 platform.

The archived transfer study has 500 local-message circuits and 500 QAOA circuits, each measured 1,024 times. Real-hardware coverage is therefore 1,024,000 shots. Matching cloud noisy simulation returned 999 valid circuits and one terminal failure, yielding 1,022,976 valid cloud shots. Its comparisons retain the 999 matched hardware records rather than pretending both samples contain 1,000 circuits.

## Five distributions answer different questions

| Distribution | What it holds fixed | What changes |
|---|---|---|
| Ideal statevector | Frozen compiled task and parameters | Exact probability reference |
| Finite-shot ideal | Same ideal probabilities | Sampling at the recorded shot count |
| Local noise model | Recorded noise assumptions | Channel evolution without device execution |
| Cloud noise model | Matched submitted circuit | Provider implementation and finite shots |
| Real hardware | Submitted physical circuit and mapping | Device behavior under the observed calibration |

For normalized distributions $p,q$, total variation is $\mathrm{TV}(p,q)=\frac12\sum_x|p(x)-q(x)|$. Hellinger distance is $H(p,q)=\sqrt{\frac12\sum_x(\sqrt{p(x)}-\sqrt{q(x)})^2}$. The public tables distinguish $H$ from $H^2$. For the three-holding constraint, feasibility is the total probability of bitstrings with exactly three ones; an optimal sample alone does not characterize the distribution.

## What the archive shows

| Metric, mean over family | Message (500 hardware circuits) | QAOA (500 hardware circuits) |
|---|---:|---:|
| Compiled depth | 48.00 | 419.08 |
| Two-qubit gates | 29.00 | 179.88 |
| Hardware vs ideal TV | 0.16652 | 0.88700 |
| Hardware vs local-noise TV | 0.15623 | 0.23501 |

The QAOA family's hardware feasibility is about **31.51%**, versus **100%** for its ideal reference and **35.22%** for the local-noise model. The larger compiled depth and two-qubit workload motivate a noise analysis, but this table alone does not establish a causal depth effect.

The [hardware table](../../evidence/hardware-per-circuit.csv) retains all 1,000 rows. The [cloud table](../../evidence/cloud-per-circuit.csv) retains 999 matched rows, including 499 QAOA rows. Means over those 499 records are slightly different from the full 500-row hardware summaries. Always choose the population before quoting a value.

## Statistical scope and provenance

The archive observed one hardware calibration snapshot. Its 2,000-resample bootstrap uses original five-circuit submission blocks within family. These are conditional descriptive intervals for this circuit collection and calibration; submission batches are not independent device calibrations.

The original records retain request/receipt lineage and counts reconciliation. The public edition contains selected numeric fields and source-summary SHA-256 values; provider handles and private execution metadata remain in the source archive. An archived [independent numerical implementation check](../../evidence/independent-numerical-check.json) reports differences below `1e-11`, with observed maxima around `5.6e-16`. That was a second implementation in the same analysis task, not an external replication.

The notebook and [public evidence checker](../../tools/verify_public_evidence.py) independently aggregate published rows. They do not query a device or regenerate provider receipts. See [reproduction status](../status.md) for the current archive limits.

## Source and next experiment

The product's [hardware policy](../../product/apps/control-plane/src/hardware-policy.ts) separates read-only checks from submission permission. The [Tianyan worker](../../product/workers/quantum/src/qf_quantum_worker/tianyan_job.py) keeps submission and querying explicit; the algorithm's [receipt audit](../../algorithm/qf_algorithm/governance.py) validates archived contracts. The [historical analysis implementation](../../algorithm/qf_algorithm/legacy/hardware_matched_analysis.py) documents the paired analysis.

A stronger next experiment would freeze a matched low/deep circuit set, repeat across independent calibration windows, retain all failed jobs, and compare target quality alongside TV, feasibility and resource cost. It requires a new execution budget and has not been performed by this publication workflow.
