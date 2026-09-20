# Datawhale preinitiation preparation

[中文](datawhale-preinitiation_zh.md) · [Home](../README.md) · [Research route](research.md)

**Status: preparation draft, 20 September 2026.** This document maps the existing Q-Fintelligence project to Datawhale's official initiation fields. It records a proposed collaboration path. No project approval, repository transfer, Alpha/Beta status or initiation submission is recorded here.

Official references: [DOPMC guide](https://github.com/datawhalechina/DOPMC/blob/main/GUIDE.md), [initiation Issue form](https://github.com/datawhalechina/DOPMC/blob/main/.github/ISSUE_TEMPLATE/establishing.yaml), [repository template](https://github.com/datawhalechina/repo-template), [Alpha criteria](https://github.com/datawhalechina/DOPMC/blob/main/ALPHA.md).

## 1. Project introduction

**Working name:** Q-Fintelligence / 量融智枢. The lowercase hyphenated form `q-fintelligence` can be used where Datawhale naming conventions require it; repository naming or transfer will follow the review process.

The project is a quantum-computing and quantum-machine-learning learning/research repository built around three existing lines: quantum kernels and local representations, constrained quantum optimization, and hardware-aware circuit selection. Financial tasks provide concrete information clocks, prediction targets, objectives and classical references. A supporting workbench organizes experiments and artifacts.

Available materials include versioned algorithm and product source, E01–E07 evidence summaries, paired hardware/cloud tables, eight original figures, three bilingual cases and companion notebooks. Manuscripts are organized in [Julius' future / works](https://github.com/Soros2040/julius-future/tree/main/works).

## 2. Motivation

Learners can often find a quantum circuit demonstration but have difficulty tracing it through data availability, a meaningful classical comparison, a downstream decision and a physical execution record. This project makes that chain readable using a finite, inspectable archive. Favorable and unfavorable results both become teaching material: what a kernel comparison actually establishes, why a prediction change can worsen tail loss, and why an ideal feasible distribution can deteriorate on hardware.

The collaboration goal is to turn existing material into a reviewable learning route and a maintained research scaffold. Specific future hypotheses will be chosen through evidence review and joint discussion of feasibility and resources.

## 3. Intended audience and prerequisites

| Audience | Starting knowledge | Expected outcome |
|---|---|---|
| Undergraduates entering QML | Python, vectors/matrices, elementary probability | Explain a complete quantum-method comparison with units and evidence |
| Quantum-computing learners | States, gates and measurements | Connect circuits to optimization constraints, noise and resource accounting |
| Finance/ML learners | Regression, time splits, mean–variance ideas | Distinguish information leakage, prediction effects and decision endpoints |
| Research contributors | One relevant method or software stack | Review evidence, strengthen explanations or design a bounded next comparison |

Readers may begin with source and evidence review. Running product or full method chains additionally requires their documented environments. New hardware studies require an agreed task and budget.

## 4. Highlights and related projects

1. **A continuous method-to-evidence route.** Derivations, actual implementations, original images and result fields are linked in both languages.
2. **Separate quantum representations.** Anchored fidelity kernels and local observable features have different source paths, targets and cost models.
3. **Full sampling and transfer questions.** Feasibility, conditional quality, distribution distance and compiled resources are interpreted together.
4. **Inspectable limitations.** Identical selected/fixed outputs, stronger classical references, calibration scope and archive gaps are explicit learning objects.

| Related resource | Its role | Q-Fintelligence's proposed complement |
|---|---|---|
| [Qiskit learning](https://quantum.cloud.ibm.com/learning) | General quantum theory, algorithms and platform instruction | A source-linked benchmark archive following predictions through constrained decisions |
| [PennyLane demos](https://pennylane.ai/qml/demonstrations/) | Differentiable quantum programming and QML examples | A focused comparison of existing fidelity and observable-feature paths with financial information clocks |
| [OpenQAOA](https://github.com/entropicalabs/openqaoa) | Framework for variational combinatorial optimization | A small exact objective bridge, sampling-quality analysis and a matched archived hardware case |

These are complementary reference points, not a claim that the project supersedes their frameworks or a comprehensive feature survey.

## 5. Content and implementation plan

Relative checkpoints below are **proposed estimates after a maintainer kickoff**, not dates promised by an established team. Julius is the current experiment/product/model architecture designer and proposed content lead. Chapter implementation/review owners remain to be agreed. Existing writing does not imply that the whole proposed curriculum is complete.

| Chapter and second-level contents | Existing material | Proposed next delivery | Owner / estimate |
|---|---|---|---|
| 01 Foundations: 1.1 information clock; 1.2 targets and sample units; 1.3 classical references | [Research route](research.md), Cases 01–02 | Entry exercises with answer/evidence pointers | Proposed lead Julius; reviewer open; kickoff + 2 weeks |
| 02 Objective bridge: 2.1 transaction costs; 2.2 QUBO/Ising; 2.3 feasible distributions | [Case 01](cases/01-objective.md), original figures, notebook | Peer-reviewed derivation and exercise fixes | Proposed lead Julius; method reviewer open; + 2 weeks |
| 03 Quantum representations: 3.1 fidelity kernels; 3.2 local observables; 3.3 graph controls and decision endpoints | [Case 02](cases/02-representations.md), E02–E04/E07 | Source-to-field review and a clearly scoped research-question register | Proposed lead Julius; QML reviewer open; + 3 weeks |
| 04 Circuit selection: 4.1 proxy fit; 4.2 candidate holdout; 4.3 equal search budgets | [Case 03](cases/03-hardware.md), original proxy figures, E06 | Selection-contract worksheet and held-out design review | Proposed lead Julius; optimization reviewer open; + 3 weeks |
| 05 Hardware: 5.1 compiled resources; 5.2 matched counts; 5.3 calibration-aware inference | [Case 03](cases/03-hardware.md), hardware/cloud tables | A reviewer-checked accounting example and study-design checklist | Proposed lead Julius; hardware reviewer open; + 4 weeks |
| 06 Research practice: 6.1 environments; 6.2 evidence/versioning; 6.3 workbench boundaries and handoffs | [Architecture](architecture.md), [reproduction](reproduction.md), contribution records | Clean-environment validation assigned and documented | Implementation owner open; + 4 weeks subject to environment |

### Foreseeable difficulties

- **Prerequisite spread:** provide a short route through the three cases and optional deeper source sections; review both languages together.
- **Historical archive completeness:** selected public evidence is available, while some canonical records are absent. Keep full historical reproduction as an unresolved milestone with explicit scope.
- **Data redistribution:** source datasets remain governed by their original terms; use the existing public summaries and permitted synthetic teaching path.
- **Hardware resources:** the existing study observes one calibration; a new cross-calibration study needs independent design, resources and authorization.
- **Contributor continuity:** assign concrete chapter reviewers and implementation owners, keep Issue/PR records and use maintenance handoffs.

## 6. Completed public materials

- [Project home and bilingual navigation](../README.md).
- [Three worked research cases](research.md#worked-cases), [companion notebooks](../notebooks/), [eight original figures with provenance](../assets/original/README.md).
- [Algorithm source](../algorithm/), [supporting workbench source](../product/), [architecture map](architecture.md).
- [Selected evidence](../evidence/README.md) and [verification status](status.md).
- [Contribution workflow](../CONTRIBUTING.md), [record examples](../contributions/), [maintenance and handoffs](maintenance.md).

“Completed” here means these linked materials exist in the public edition. It does not mean pending environment validation, external review or future studies have been completed.

## 7. Review readiness and handoff

The official guide recommends initiating after roughly 50% of planned work is completed. Assess that threshold against the agreed teaching plan and acceptance criteria; no percentage is asserted in this draft. The planned route is: align chapter scope and owners → review actual completion and the official repository template → prepare the official Issue → respond to review → follow any approved transfer and subsequent stage requirements.

The official Issue includes conduct acknowledgement and contact fields. The eventual submitter must complete those deliberately in the appropriate channel; this public document contains no private contact details. Coordinate through Datawhale's stated helper/review process. The preparation document itself sends no messages or application.

## 8. Roles, collaboration and license

Julius: experiment design, model architecture, product architecture and proposed learning-content coordination. Implementation, deployment, execution and review contributions are recorded with their actual owners in [contribution scope](contributions.md). Additional names are added with permission and specific responsibilities.

Use Issues to discuss and claim bounded tasks; submit a bilingual PR with sources and a contribution record; obtain review and register the result. Original code uses MIT; original documentation and team-original figures use CC BY-NC-SA 4.0. Third-party materials retain their own rights. Any compatibility question raised during Datawhale review should be resolved explicitly while preserving upstream notices.
