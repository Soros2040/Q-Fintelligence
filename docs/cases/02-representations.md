# Case 02 · Fidelity kernels, local quantum features and controlled comparisons

[中文](02-representations_zh.md) · [Research map](../research.md) · [Companion notebook](../../notebooks/02_representation_evidence.ipynb)

## What this case teaches

Trace two quantum representations from their mathematical definitions to the actual source: E02 uses **anchored fidelity-kernel features** for a five-session downside-risk task; E03 uses **local observable features and a classical graph head** for a stock-return task. Then inspect how representation-level effects behave when passed into portfolio decisions. The different targets, models and sample units matter more than the shared word “quantum.”

Prerequisites: vectors and inner products, basic quantum states, supervised regression, and train/validation/test separation. Read [Case 01](01-objective.md) first for the portfolio objective. The [public notebook](../../notebooks/02_representation_evidence.ipynb) reads selected evidence; it does not regenerate the historical model fits.

## 1. The circuit family already implemented

The [feature source](../../algorithm/qf_algorithm/legacy/quantum_features.py) defines twelve frozen maps on six qubits. The catalogue varies one/two layers, input scale in $\{0.5,1,1.5\}$ and a CZ/controlled-RY chain. Each layer applies data-dependent RY and RZ rotations; the first begins with Hadamard gates. Quantum parameters are frozen while the classical prediction head is trained.

For a normalized six-coordinate input $x$, write

$$
|\psi_\theta(x)\rangle=U_\theta(x)|0\rangle^{\otimes6},\qquad
\rho_\theta(x)=|\psi_\theta(x)\rangle\langle\psi_\theta(x)|.
$$

Here $\theta$ names a catalogue configuration. A finite catalogue search is an implemented form of model selection. A broader program of quantum-kernel self-design would additionally need explicit search spaces, trainable parameters or structural moves, objective choices, search-budget accounting and held-out evaluation; the present evidence supports the recorded catalogue comparison.

### E02: similarity through a fidelity kernel

The implemented `fidelity_kernel` computes

$$
k_\theta(x,x')=|\langle\psi_\theta(x)|\psi_\theta(x')\rangle|^2
=\operatorname{Tr}[\rho_\theta(x)\rho_\theta(x')].
$$

For ideal normalized states, $0\leq k\leq1$ and $k(x,x)=1$. Its Gram matrix is positive semidefinite because, for real coefficients $c_i$,

$$
\sum_{ij}c_ic_jk(x_i,x_j)=\left\|\sum_i c_i\rho(x_i)\right\|_{\mathrm{HS}}^2\geq0.
$$

Independent noisy estimates of pairwise entries need not preserve this property. A measured-kernel study must state symmetrization, diagonal treatment and any PSD correction, and count those choices as part of the method.

The [E02 implementation](../../algorithm/qf_algorithm/legacy/run_e02.py) uses 32 anchors selected from training rows and represents each sample as

$$
\Phi_\theta(x)=\big[k_\theta(x,a_1),\ldots,k_\theta(x,a_{32})\big].
$$

These are anchored fidelity features. The source feeds them into a standardized `SGDRegressor`; it does not apply an inverse-square-root anchor-Gram transform. The classical comparator uses $\exp(-\|x-a_j\|^2/6)$ with the same head. Three development folds select among twelve maps; the final head is evaluated with five seeds. Input standardization is fitted on training rows, followed by clipping to $[-3,3]$ and multiplication by $\pi/3$.

The original run computes exact statevectors and reuses their classical cache for pairwise overlaps. Its finite-shot analysis samples binomial successes from those exact overlaps at 256, 1,024 and 4,096 shots. These entries describe a **local sampling estimator**. A hardware-equivalent pair budget is a cost model and must be distinguished from actual device calls. The source records anchor-Gram eigenvalues and a zero PSD-correction norm for its exact calculation.

### E03: observable features passed to a graph head

The same state family also supplies a distinct representation:

$$
\phi_\theta(x)=\big(\langle Z_0\rangle,\ldots,\langle Z_5\rangle,
\langle Z_0Z_1\rangle,\ldots,\langle Z_4Z_5\rangle\big)\in\mathbb R^{11}.
$$

The eleven readouts are six single-qubit Z expectations and five neighboring ZZ expectations. They share a Z-basis measurement setting. The implementation explicitly distinguishes SDK basis strings `q5...q0` from readout order `q0...q5`; a reversal can silently change graph features while keeping array dimensions valid.

The [graph head](../../algorithm/qf_algorithm/legacy/graph_head.py) applies classical graph aggregation. One layer has the form

$$
H_1=\tanh(A\Phi W_1+b_1),\qquad \widehat y=H_1W_o+b_o,
$$

with another $A H_1$ aggregation in its two-layer version. Trainable weights belong to this prediction head. The full asset graph stays in classical memory; the local quantum register has six qubits. Asset count, register size, number of input encodings and measurement shots are separate resource dimensions.

## 2. Respect the information clock

A label is usable only once its outcome is known. E02's development folds filter by `label_end`; the five-session downside target belongs to its original task contract. The newer [normalized panel](../../algorithm/qf_algorithm/data.py) and [pipeline](../../algorithm/qf_algorithm/pipeline.py) explicitly track label availability for a next-open-to-following-open task and purge immature training labels before an evaluation window.

An illustrative clock is: form a signal at close $t$, enter at the next open, exit at the following open, and admit that label only after the exit. A five-session target needs its own longer availability horizon. The source/version and information contract must travel with each result; editing today's pipeline does not re-evaluate historical experiments.

A useful comparison fixes the input information, target, date partitions, development budget and head capacity. Test graph direction with directional, reversed, self-only and randomized structures. To isolate expected-return features, hold covariance estimation fixed; to isolate risk estimation, hold the return model fixed. Record all declared comparisons, their correction family and the resampling unit.

## 3. Read the archived results by task

| Field | E02 fidelity-kernel mapping | E03 local observable messages |
|---|---|---|
| Target | Five-session future downside risk, original annualized scale | Stock return under the registered next-open task |
| Quantum representation | Similarity to 32 training anchors | 11 observable features, classical graph prediction head |
| Observations | 576 dates, 3,456 asset-date pairs, 5 seeds | 485 shared dates, 5 seeds |
| Principal contrast | Selected map minus classical map | Local message minus validation-selected classical graph |
| MAE difference | `−0.0005830381` | `+0.0000883367` |
| Archived 95% interval | `[−0.0009172197, −0.0002712622]` | `[0.0000409374, 0.0001486703]` |
| Task-specific conclusion | Lower error for the selected quantum map | Higher error for the quantum local-message model |

[E02](../../evidence/E02.json) and [E03](../../evidence/E03.json) preserve the paired stationary time-block procedure and seed aggregation. E02 uses a frozen block length of 80 and 2,000 resamples. Dates are time-dependent; five model seeds are not five independent financial histories. Selected and fixed maps produce identical E02 endpoints. Selected and fixed directional implementations also coincide in E03. A favorable feature comparison therefore does not by itself establish value from searching the architecture.

![Original archived effects and intervals for fifteen comparisons](../../assets/original/effects.png)

*Original figure, copied unchanged from the archived statistical reanalysis. Filled positions are effect estimates and horizontal bars are archived intervals. The [figure guide](../../assets/original/README.md#numbered-comparisons-in-the-effects-figure--效应图编号) maps all fifteen numbered rows to experiment and comparison keys. MAE, CVaR95 and objective-gap panels use different endpoints; even E02 and E03 MAEs have different target scales. The zero line is the relevant within-row reference.*

The directional controls also constrain interpretation: E03 directional-minus-self is positive, directional-minus-reverse is positive, and directional-minus-random has an interval crossing zero. The archive does not establish that this directional quantum message improves those controls.

## 4. Prediction quality must survive the decision layer

For period loss $L_t=-r_t$, empirical CVaR at confidence $\alpha$ can be expressed as

$$
\widehat{\mathrm{CVaR}}_\alpha(L)=\min_\eta\left[\eta+
\frac{1}{(1-\alpha)T}\sum_t\max(L_t-\eta,0)\right].
$$

The precise sample convention should follow the registered implementation. Lower CVaR loss is favorable. A small MAE change can alter rankings, selected holdings, turnover and tail loss; its sign need not predict the financial effect.

[E04](../../evidence/E04.json) separates changes to the mean and risk estimators. At its primary endpoint, quantum-mean minus classical-mean CVaR is `+0.0094119182`, interval `[0.0059764443, 0.0133643372]`. Factor minus shrinkage is `+0.0014133302`, interval `[−0.0001619626, 0.0033556581]`. Quantum-risk minus factor is `−0.0015807364`, interval `[−0.0038498930, 0.0002968482]`. The latter two intervals cross zero; their point estimates alone do not establish improvements.

The integrated [E07](../../evidence/E07.json) uses 485 holding intervals and five seeds. At cost `0.001`, mean seed CVaR95 is `0.0369492238` for the full method and `0.0275373056` for classical, with the same adverse primary difference as the E04 mean comparison. Full and fixed-map outputs coincide. E07 reuses existing quantum features and records zero new quantum/hardware calls. Its fractional long-only holdings, adjusted-price total-return proxy and execution proxy define an offline evaluation scope.

## 5. A useful next contribution

Choose one of these bounded tasks and attach a [review record](../contributions.md#contribution-record):

1. Follow `fidelity_kernel` to E02's anchors, scaler and head. Explain which operations use training data and where a future measured-kernel estimator would enter.
2. Trace one E03 feature from Z-basis bit order through graph aggregation into an endpoint. Check units and identify the strongest matched classical reference.
3. Audit one E04 contrast: identify which mean/covariance component changes and which remains fixed, then connect it to turnover and CVaR fields.

**Reading checks.** Why are there eleven observables? Six Z plus five neighboring ZZ. Why can an exact fidelity Gram be PSD but an estimated one be indefinite? Entrywise sampling errors need not correspond to one common feature embedding. What does selected-equals-fixed show? The reported endpoint provides no gain attributable to catalogue selection. Can E02's MAE be compared numerically to E03's? Their target scales differ.

Potential research directions remain open: task-aware kernel design, matched-capacity local quantum features, search-budget controls, and hardware-aware representations. A new study should freeze its task and classical references, count state preparation/measurement/search resources, and preserve held-out dates before drawing conclusions.

## Sources and related writing

- [Quantum feature definitions](../../algorithm/qf_algorithm/legacy/quantum_features.py), [E02 source](../../algorithm/qf_algorithm/legacy/run_e02.py), [graph head](../../algorithm/qf_algorithm/legacy/graph_head.py), [risk estimators](../../algorithm/qf_algorithm/risk.py).
- Havlíček et al., *Supervised learning with quantum-enhanced feature spaces*, Nature 567 (2019), [DOI](https://doi.org/10.1038/s41586-019-0980-2).
- Schuld and Killoran, *Quantum Machine Learning in Feature Hilbert Spaces*, Physical Review Letters 122 (2019), [DOI](https://doi.org/10.1103/PhysRevLett.122.040504).
- [Original figure provenance](../../assets/original/README.md), [evidence scope](../../evidence/README.md), [manuscripts and research writing](https://github.com/Soros2040/julius-future/tree/main/works).
