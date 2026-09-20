# Research map

[中文](research_zh.md) · [Home](../README.md)

The central question is whether a quantum component contributes useful information or search quality after accounting for the data clock, a strong classical reference and total computational cost. The project separates three outputs: financial predictions, portfolio decisions and physical circuit behavior. An improvement in one does not automatically improve the other two.

## Prerequisites and route

1. Learn train/validation/test separation, label availability and paired comparison.
2. Read mean–variance objectives, fixed-cardinality constraints, transaction costs and covariance PSD.
3. Work through binary-to-Ising substitution and probability distributions over feasible portfolios.
4. Read local quantum features, graph aggregation, parameter learning and circuit compilation.
5. Examine finite-shot variation, noise models and paired hardware analysis.

Only Python and basic linear algebra are needed for the small teaching examples. Full source exploration additionally uses TypeScript, React, SQLite, Python numerical tools and Linux process isolation.

## Experiment map

| ID | Question | Comparison and unit | Public source |
|---|---|---|---|
| E01 | Do return, risk and cost survive the binary/Ising transformation? | Synthetic contract case; all binary states | [E01](../evidence/E01.json) |
| E02 | Does the selected quantum feature map improve a fixed prediction task? | Selected, fixed and classical; date, aggregated across seeds | [E02](../evidence/E02.json) |
| E03 | Does quantum local messaging add value to a shared stock-return task? | Classical graph and graph-direction controls; date | [E03](../evidence/E03.json) |
| E04 | What changes when mean or covariance estimation changes? | Fixed trading semantics, cost and paired holding intervals | [E04](../evidence/E04.json) |
| E05 | How good is the full sampling distribution? | Exact optimum, uniform feasible and local search; frozen objective | [E05](../evidence/E05.json) |
| E06 | Does noise-aware candidate selection improve quality? | Fixed, resource-only and noise-aware selection; base problem | [E06](../evidence/E06.json) |
| E07 | Does the integrated method improve the financial endpoint, and how do circuits transfer? | Financial holding intervals and a separate circuit/calibration analysis | [E07](../evidence/E07.json), [hardware](../evidence/hardware.json) |

## Worked cases

- [01 · Objective → QUBO → Ising](cases/01-objective.md): derive the exact transaction-cost term, preserve scale and enumerate every state.
- [02 · Quantum local representation vs classical](cases/02-representations.md): align the information clock, distinguish task endpoints and interpret favorable and unfavorable effects.
- [03 · Hardware migration](cases/03-hardware.md): compare ideal, finite-shot, local-noise, cloud-noise and hardware records under a single calibration.

Each case includes a notebook, source links, a result and a next experiment. The next experiments are research proposals, not completed milestones.

## An appropriate next research cycle

Freeze a common data/label contract; select all model and circuit settings using development data; record feature, training, search and measurement costs; evaluate on held-out dates; report paired effects and uncertainty; then transfer only frozen circuits to hardware. Use the same candidate pool and search budget when comparing selection methods. Judge each module by its own endpoint before assigning credit for portfolio-level performance.
