# Q-Fintelligence · 量融智枢

**Quantum computing and quantum machine learning, studied through transparent financial benchmarks.**

[简体中文](README_zh.md) · [Start learning](docs/research.md) · [Source and architecture](docs/architecture.md) · [Evidence](evidence/README.md) · [Manuscripts](https://github.com/Soros2040/julius-future/tree/main/works)

Q-Fintelligence studies how quantum representations and circuit choices affect a well-defined task: prediction, constrained optimization, or execution on a noisy device. The repository brings together QF Algorithm 2.1.0, selected historical experiment records, original figures, three worked cases and a supporting research workbench.

**Current stage:** source and evidence edition; research continues. Existing experiments contain both favorable and unfavorable comparisons. The [Datawhale preinitiation draft](docs/datawhale-preinitiation.md) organizes a proposed educational collaboration; it is preparation for review. [Validation status](docs/status.md) distinguishes previous checks, archived experiments and outstanding clean-environment validation.

## Three research lines

| Line | Concrete question | Existing source and evidence | What remains open |
|---|---|---|---|
| Quantum kernels and local features | Does a quantum representation improve a matched held-out prediction task? | Twelve frozen maps; anchored fidelity features in E02; local observables and a graph head in E03 | Task-aware kernel design, matched capacity and complete search/measurement cost |
| Quantum constrained optimization | How does a circuit's full distribution compare with exact and classical solutions? | Portfolio → QUBO → Ising derivation; E01 identity checks; E05 QAOA comparisons | Reliable feasible quality, stronger references and scaling beyond small exact problems |
| Hardware-aware circuit selection | Can a candidate choice retain quality after compilation and noise? | Local proxy example, E06, 1,000 six-qubit hardware circuits and 999 matched cloud records | Generalization across tasks and independent calibration windows |

Financial data give these questions decision times, targets, risk constraints and strong classical references. The quantum methods and their resource/quality tradeoffs are the research focus. The agent/workbench layer supports task organization, human review and artifact management.

## Start with one complete case

| Order | Prerequisites | Worked material | Observable learning outcome |
|---|---|---|---|
| 1 | Linear algebra, binary variables | [Objective → QUBO → Ising](docs/cases/01-objective.md), [notebook](notebooks/01_objective_identity.ipynb) | Derive constants and cardinality penalties; read an original six-asset example and sampling distribution |
| 2 | Inner products, regression, time splits | [Fidelity kernels and local features](docs/cases/02-representations.md), [notebook](notebooks/02_representation_evidence.ipynb) | Distinguish kernel similarity from observable features; follow prediction effects into portfolio endpoints |
| 3 | Probability, circuits, held-out evaluation | [Candidate selection and hardware transfer](docs/cases/03-hardware.md), [notebook](notebooks/03_hardware_transfer.ipynb) | Interpret a proxy's held-out error, resource costs, distribution distances and feasibility |

Every case connects equations, the actual implementation, original evidence, limitations and a bounded contribution task. The English and Chinese editions carry the same technical substance. [Eight original figures](assets/original/README.md) have file hashes, source-result hashes and execution-mode labels.

![Original calculated and sampled probabilities for one six-qubit demonstration](assets/original/probabilities.png)

*Original archived local demonstration: B01_L0.1, fixed qas_00, gamma 0.6, beta 0.25 and 1,024 local shots. Filled bars are calculated probabilities; outlined bars are sampled frequencies. [Case 01](docs/cases/01-objective.md) explains bit order, feasible mass and conditional quality. This figure describes this demonstration, separately from the hardware study.*

## Read at your own depth

- **Five minutes:** read the three lines above and the evidence table below; choose one question you can explain.
- **Thirty minutes:** follow one worked case from equation to source and JSON field; check a reading question.
- **A research contribution:** review a comparison contract, improve a bilingual explanation, or propose a bounded next study using the [contribution record](docs/contributions.md#contribution-record).
- **A reproduction session:** use the [environment guide](docs/reproduction.md); record runtime, commit, mode and outcomes. Full historical reproduction has archive gaps described in the status page.

## Materials and actual progress

| Module | Public entry | Available now | Next useful contribution |
|---|---|---|---|
| Quantum methods | [Algorithm](algorithm/) and [source map](docs/architecture.md) | Versioned Python/CLI, local synthetic path, frozen-map and circuit-selection source | Clean-environment method-chain validation |
| Research evidence | [E01–E07, hardware/cloud](evidence/) | Selected summaries, per-circuit tables and source identities | Review one task, effect direction, unit and limitation |
| Learning cases | [Research route](docs/research.md), [original figures](assets/original/README.md) | Three bilingual cases and three companion notebooks | Improve one explanation with an evidence-linked exercise |
| Supporting workbench | [Product source](product/), [product entry](https://qfintelligence.yuxuan.wiki/) | Web, control plane, workers and contracts | Clean Linux/WSL2 mock installation and adapter review |
| Research writing | [Julius' future / works](https://github.com/Soros2040/julius-future/tree/main/works) | Central writing and manuscript navigation | Register document version, contribution and supporting evidence |
| Educational collaboration | [Datawhale preparation](docs/datawhale-preinitiation.md) | Bilingual draft using official initiation fields | Review chapter ownership, schedule and readiness |

## Selected evidence

The numerical archive below covers experiments recorded on 7–9 September 2026. Values describe those frozen tasks. They are neither a return forecast nor a general quantum-advantage claim.

| Study | Evidence | Interpretation |
|---|---|---|
| E01 objective identity | 60 synthetic cases, 12,516 binary states; maximum error `4.16e-17` | Numerical agreement of the registered objective representations |
| E02 feature mapping | 576 dates × 5 seeds; selected-minus-classical MAE `−0.0005830` | Improvement on this five-session downside-risk task; selected and fixed maps coincide |
| E03 local graph message | 485 dates × 5 seeds; quantum-minus-classical MAE `+0.00008834` | The classical reference performs better on this stock-return task |
| E05 small portfolio search | 27 fixed objectives; QAOA gap difference `−0.0016276` vs uniform, `+0.0024316` vs local search | Better than uniform sampling, worse than the classical search reference |
| E06 noise-aware selection | 8 base problems; interval overlaps zero vs fixed selection | The archive does not establish an improvement over fixed selection |
| E07 financial endpoint | CVaR95 `0.0369492` for the integrated method vs `0.0275373` for classical, at cost `0.001` | Higher tail loss in this comparison; integrated and fixed-map outcomes coincide |
| Hardware transfer | 1,000 six-qubit circuits, 1,024,000 hardware shots; 999 valid matched cloud simulations | Real-device transfer can be studied under one observed calibration snapshot |

See the [evidence guide](evidence/README.md) for sample units, uncertainty, field selection and the distinction between archived and newly executed checks. A device name indicates the platform; the tested circuits here use six qubits.

## Your first contribution

Choose one comparison in [Case 02](docs/cases/02-representations.md), open its linked JSON, and record the task, compared methods, effect direction, sample unit, interval and conclusion. Submit an Issue with the exact file/field and a proposed correction or confirmation. A bilingual documentation PR can follow once the scope is agreed. A useful first review does not require a GPU or a quantum-provider account.

| Task | Deliverable | Acceptance | Ownership |
|---|---|---|---|
| Review E02 kernel features | Issue plus source-linked explanation | Anchors, frozen maps, classical head and local finite-shot scope distinguished | Open; claim in an Issue |
| Review one original figure | Caption or teaching improvement | File identity, axes, execution mode and inference boundary agree | Open; claim in an Issue |
| Audit one comparison | [Contribution record](contributions/) | Exact evidence fields, sign, units and uncertainty checked | Open; claim in an Issue |

The [reproduction guide](docs/reproduction.md) provides optional small offline arithmetic examples and installation steps. This documentation revision restores original figures and expands source explanations; it does not add new experiment results.

## Design, collaboration and maintenance

Julius designed the experiments, product architecture and model architecture. Implementation, deployment and experiment execution are recorded separately through the team's implementation workflow. [Contribution scope](docs/contributions.md) explains individual roles and attribution.

Follow [CONTRIBUTING](CONTRIBUTING.md) for Issue discussion → task claim → branch → PR review → [outcome registration](contributions/). [Maintenance](docs/maintenance.md) covers review responsibilities and handoffs. Source-linked corrections, stronger explanations and careful examination of unfavorable results are all valuable contributions.

## License and sources

Original code: [MIT](LICENSE). Original documentation and team-original figures: [CC BY-NC-SA 4.0](LICENSE-DOCS.txt). Third-party components retain their licenses in [Third-party notices](THIRD_PARTY_NOTICES.md). Source datasets and complete execution archives remain subject to their original permissions; the selected public records have a [data notice](evidence/DATA_NOTICE.md).
