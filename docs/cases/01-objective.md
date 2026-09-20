# Case 01 · Preserve a financial objective through QUBO and Ising

[中文](01-objective_zh.md) · [Research map](../research.md) · [Notebook](../../notebooks/01_objective_identity.ipynb)

## Prerequisites

Read vectors and quadratic forms, expected return, covariance, and a binary selection variable. The fixed-cardinality example below uses equal weights; it can be followed without quantum mechanics.

## The question

A quantum optimizer can minimize the wrong financial objective perfectly. Before comparing optimizers, the return units, covariance horizon, asset order, previous holdings and cardinality must describe the same decision. This case tests that interface.

For binary selections $x_i\in\{0,1\}$ with $\sum_i x_i=K$, equal weights are $w=x/K$. Let $\mu$ be expected simple returns, $\Sigma$ a symmetric covariance matrix for the same horizon, $v$ the actual previous weights, $\lambda$ risk aversion and $c$ the cost per unit turnover:

$$J(x)=-\mu^Tx/K+\lambda x^T\Sigma x/K^2+c\sum_i|x_i/K-v_i|.$$

Keeping the factors $1/K$ and $1/K^2$ matters: changing them changes the tradeoff between mean and risk. A cash position or an asset that has left the eligible universe also changes the previous-holdings accounting.

## Exact transformation

Because $x_i$ takes only two values,

$$|x_i/K-v_i|=|v_i|+x_i\left(|1/K-v_i|-|v_i|\right).$$

Thus the cost term is affine in each binary variable. Add $\kappa(\sum_i x_i-K)^2$ when the solver works on the full binary domain. Using an upper-triangular QUBO convention,

$$C(x)=C_0+\sum_iQ_{ii}x_i+\sum_{i<j}Q_{ij}x_ix_j,$$

$$Q_{ii}=-\mu_i/K+\lambda\Sigma_{ii}/K^2+c(|1/K-v_i|-|v_i|)+\kappa(1-2K),$$

$$Q_{ij}=2\lambda\Sigma_{ij}/K^2+2\kappa,\quad C_0=c\sum_i|v_i|+\kappa K^2.$$

The factor two for off-diagonal covariance comes from the symmetric quadratic form. A full symmetric matrix representation requires a different storage convention; copying these coefficients without that distinction doubles interactions.

Substitute $x_i=(1-z_i)/2$, $z_i\in\{-1,1\}$. The Ising couplings are $J_{ij}=Q_{ij}/4$, fields are $h_i=-Q_{ii}/2-\sum_{j\ne i}Q_{\min(i,j),\max(i,j)}/4$, and the constant is $C_0+\sum_iQ_{ii}/2+\sum_{i<j}Q_{ij}/4$. Constants leave the optimizer unchanged but must remain when comparing numeric energies or objective gaps.

## A two-asset calculation

Take $K=1$, $\mu=(0.02,0.01)$, $\Sigma=\operatorname{diag}(0.04,0.01)$, $\lambda=0.5$, $c=0.001$, and previous weights $v=(1,0)$. Keeping asset one costs $-0.02+0.5(0.04)=0$. Switching to asset two costs $-0.01+0.5(0.01)+0.001(2)=-0.003$: selling the old position and buying the new one creates two units of turnover.

With $\kappa=1$, the QUBO coefficients are $Q_{11}=-1.001$, $Q_{22}=-1.004$, $Q_{12}=2$, and $C_0=1.001$. Substitution gives the same feasible costs: $C(1,0)=0$ and $C(0,1)=-0.003$. This toy comparison explains the encoding; its synthetic numbers say nothing about realized investment returns.

## What is checked

The [small example](../../examples/bridge_identity.py) constructs synthetic returns, a PSD covariance and explicit prior weights. It enumerates all 64 six-bit states, checks direct financial cost plus penalty against QUBO and Ising, then ranks the 20 states satisfying $K=3$. It requires no market data or quantum provider.

The archived [E01 result](../../evidence/E01.json) covers a larger registered set: 60 synthetic contract cases, 12,516 states, maximum identity error `4.163336342344337e-17`, and all recorded perturbation-bound checks passing. This demonstrates agreement in those finite checks; it does not measure financial outperformance.

## Follow the implementation

- [Bridge primitives](../../algorithm/qf_algorithm/legacy/bridge.py) build objective coefficients and classical solutions.
- [Pipeline](../../algorithm/qf_algorithm/pipeline.py) keeps the same risk/cost parameters through objective, exact enumeration, QUBO, Ising and candidate evaluation.
- [Quantum candidate evaluation](../../algorithm/qf_algorithm/quantum.py) writes circuit and optimization traces.

The notebook uses an independent compact derivation so a reader can check the algebra without installing the full numerical environment. Its synthetic case is separate from the 60 archived cases.

## Boundaries and next experiment

A penalty must dominate the relevant improvement available from breaking the constraint. Record its derivation and verify infeasible states rather than choosing an unexplained large number. For financial evaluation, preserve the asset identity through every permutation and compare actual trade costs under the same execution assumptions.

A useful contribution is a worked extension with one blocked previous holding and explicit cash. Its acceptance criterion is equality of the direct and encoded objective on every permitted state, with a clear statement of which positions are fixed. The extension is proposed work; the public example currently uses the equal-weight fixed-cardinality setting.
