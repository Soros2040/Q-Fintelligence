# Reproduction and validation

[中文](reproduction_zh.md) · [Home](../README.md) · [Validation status](status.md)

Choose the layer you want to verify. This repository contains source, curated historical evidence and independently executable teaching checks. Each layer has a different environment and evidentiary scope.

## 1. Public offline checks

Use Python 3.11 or later, from the repository root:

```bash
python examples/bridge_identity.py
python tools/verify_public_evidence.py
```

The first enumerates all 64 states of a synthetic six-asset problem, compares direct, upper-triangular QUBO and Ising energies, and checks the 20 feasible baskets. The second recomputes public-table means, sample counts, selected paired effects and endpoint arithmetic. Both require only the standard library. A `PASS` establishes these narrow checks, not a rerun of the archived experiments.

The [three notebooks](../notebooks/) use the same offline material with bilingual explanations. Open with local Jupyter; run from this repository or its notebook directory. Published notebooks have cleared outputs and execution metadata. Publication verification also executes their code cells without saving outputs into them.

## 2. Product workbench

The supported baseline is Linux or WSL2 with Node.js **24.x**, Python **3.11**, and `uv`. Several scripts use Unix shell syntax; `doctor` is WSL-oriented.

```bash
cd product
npm ci
uv sync --frozen
cp .env.example .env
npm run typecheck
npm test
npm run build
uv run pytest tests/python
npm run dev
```

Keep provider keys empty for the mock workflow. Source includes dependency locks and tests. Publication checks do not substitute for a clean installation and full product test run; see [status](status.md).

The optional OpenHands sidecar has its own Python **3.12.13** / `uv` **0.11.29** environment, vendored derivative wheel, and isolation requirements including `bwrap`/Docker depending on the path. Review its [configuration](../product/workers/openhands_sidecar/pyproject.toml) and [supply-chain instructions](../product/workers/openhands_sidecar/supply_chain/README.md) before enabling `QF_AGENT_MODE=openhands`. Basic mock success does not validate this optional service.

## 3. Algorithm package

The package declares Python **3.11** (`>=3.11,<3.12`). Use a new environment:

```bash
cd algorithm
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
qf-algorithm --version
```

Dependency versions and hashes come from the source snapshot. Fresh resolution and installation have not been certified by publication checks.

For a local synthetic method-chain run, choose a new output location outside the implementation package:

```bash
qf-algorithm run --epochs 1 --qaoa-mode fixed --shots 128 --output-dir ../outputs --run-id synthetic-review
```

This suggested development check fits models and simulates circuits locally; it was not executed during this publication. Synthetic results do not reproduce E01–E07. Review [CLI](../algorithm/qf_algorithm/cli.py), [configuration](../algorithm/qf_algorithm/config.py) and [schemas](../algorithm/qf_algorithm/schemas/) before using your own input.

Historical `statistics`, compatibility `verify`/`analyze`/`recompute`, and `--frozen` paths need the authorized source archive. Source market panels, full execution records and the complete archive are outside this edition. The available historical compact archive also has missing canonical records; alias restoration cannot create missing source files. Complete historical reproduction remains a preservation task.

## Report a result

Record the commit, OS, runtime versions, command, mode, input provenance, output hashes and outcome in the [contribution record](contributions.md#contribution-record). Identify whether you checked arithmetic, installed source, reran an experiment or used a device. Include failed steps and logs after removing secrets. Reproducible failures are useful contributions.
