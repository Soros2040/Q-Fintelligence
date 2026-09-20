# Case 02 · Quantum local representation and the classical reference

[中文](02-representations_zh.md) · [Research map](../research.md) · [Notebook](../../notebooks/02_representation_evidence.ipynb)

## A testable contribution

A quantum feature map transforms a small vector into observable features. It can be combined with classical graph aggregation and a learned prediction head. The relevant claim is that this representation improves a specified held-out task at an acceptable cost. It requires a comparison with the same input information, target, date partition and model-selection budget.

The project keeps the full asset graph in classical memory and uses a small local quantum register for features. Graph size and quantum-register size therefore describe different resources. Calls across assets, dates, training iterations and candidate maps must all count toward cost.

## Information clock and design

The 2.1.0 [normalized panel](../../algorithm/qf_algorithm/data.py) assigns each label an availability date. For its next-open-to-following-open return task, the label cannot be known before the exit open. The [pipeline](../../algorithm/qf_algorithm/pipeline.py) includes a training row only when that label has matured before the evaluation start, and fits scaling on the training rows.

This contract is important for future-risk targets too: shifting a five-session future label by one date does not make it observable. Historical experiments must keep their original identity and limitations. A revised information contract does not retroactively rerun a historical comparison.

To isolate the mean representation, hold the covariance estimator fixed. To isolate risk estimation, hold the mean estimator fixed. To test graph direction, compare directional, reversed, self-only and randomized structure under the same task. When multiple comparisons form one declared family, inspect the correction and the unit of resampling.

## Read two tasks separately

| Field | E02 mapping | E03 local message |
|---|---|---|
| Target | Five-session future downside risk, original annualized scale | Stock return under the registered next-open task |
| Observations | 576 dates, 3,456 asset-date pairs, 5 seeds | 485 shared dates, 5 seeds |
| Principal contrast | Selected map minus classical map | Local message minus validation-selected classical graph |
| MAE difference | `−0.0005830381` | `+0.0000883367` |
| Archived 95% interval | `[−0.0009172197, −0.0002712622]` | `[0.0000409374, 0.0001486703]` |
| Interpretation | Lower error for selected quantum features in this task | Higher error for the quantum local-message model in this task |

[E02](../../evidence/E02.json) and [E03](../../evidence/E03.json) preserve paired stationary time-block settings and seed aggregation. E02 selected and fixed maps produce the same endpoint. In E03 the selected and fixed directional implementations also coincide in the archived outputs. These observations constrain what credit can be assigned to architecture selection.

## From prediction to a portfolio

A prediction change can alter rankings, turnover and tail loss. The integrated E07 comparison holds the recorded trading assumptions and cost `0.001` fixed: mean seed CVaR95 is `0.0369492238` for the full method and `0.0275373056` for classical. Full-minus-classical is `+0.0094119182`, with archived 95% interval `[0.0059764443, 0.0133643372]`. On this loss endpoint, lower is better. The full and fixed-map results coincide.

The [public E07 summary](../../evidence/E07.json) records 485 intervals and 5 seeds. Its fractional, long-only, adjusted-price and execution-proxy assumptions define the scope; the record is not a live portfolio performance statement.

## Source and follow-up

Read [graph head](../../algorithm/qf_algorithm/legacy/graph_head.py), [feature computation](../../algorithm/qf_algorithm/legacy/quantum_features.py), [risk estimators](../../algorithm/qf_algorithm/risk.py), and then the [pipeline](../../algorithm/qf_algorithm/pipeline.py). The notebook reads the released summaries and checks contrast arithmetic; it does not retrain models or recreate the time-block intervals.

The next useful experiment is a matched capacity and cost comparison with one unchanged target, mature labels, identical held-out dates, fixed risk estimation and logged feature/train/search time. Publish all predeclared comparisons and explain any selected map that equals the fixed candidate. A feature-level gain should be carried to the portfolio endpoint before claiming financial value.
