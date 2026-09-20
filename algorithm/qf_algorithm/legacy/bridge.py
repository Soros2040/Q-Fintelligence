"""Equal-weight portfolio objective and bounded deterministic swap search."""
from __future__ import annotations
import itertools
import time
import numpy as np


def objective(x, mu, sigma, k, risk_lambda, cost, previous):
    weights = np.asarray(x) / k
    return float(-mu @ weights + risk_lambda * weights @ sigma @ weights + cost * np.abs(weights - previous).sum())


def qubo(mu, sigma, k, risk_lambda, cost, previous, penalty=0):
    n = len(mu)
    matrix = risk_lambda * np.asarray(sigma, dtype=float) / k**2
    matrix = matrix.copy()
    matrix[np.diag_indices(n)] += -mu / k + cost * (np.abs(1 / k - previous) - previous)
    constant = float(cost * np.sum(previous))
    if penalty:
        matrix += penalty * np.ones((n, n))
        matrix[np.diag_indices(n)] -= 2 * penalty * k
        constant += penalty * k**2
    return matrix, constant


def ising(matrix, constant):
    matrix = (matrix + matrix.T) / 2
    off = matrix.copy()
    np.fill_diagonal(off, 0)
    fields = -.5 * np.diag(matrix) - .5 * off.sum(axis=1)
    coupling = off / 4
    offset = constant + .5 * np.trace(matrix) + .25 * off.sum()
    return fields, coupling, float(offset)


def exhaustive(mu, sigma, k, risk_lambda=1, cost=0, previous=None):
    previous = np.zeros(len(mu)) if previous is None else previous
    result = []
    for basket in itertools.combinations(range(len(mu)), k):
        x = np.zeros(len(mu)); x[list(basket)] = 1
        result.append((objective(x, mu, sigma, k, risk_lambda, cost, previous), basket))
    return min(result)


def solve(mu, sigma, k, risk_lambda, cost, previous, eligible=None, cpu_cap=1.0, max_passes=4):
    start = time.process_time()
    n = len(mu)
    eligible = np.ones(n, dtype=bool) if eligible is None else np.asarray(eligible, dtype=bool)
    candidates = np.flatnonzero(eligible)
    k_active = min(k, len(candidates))
    # Cash occupies any unfilled slot; each selected asset retains weight 1/k.
    matrix, constant = qubo(mu, sigma, k, risk_lambda, cost, previous)
    diagonal = np.diag(matrix)
    selected = []
    for _ in range(k_active):
        scores = diagonal[candidates] + (2 * matrix[np.ix_(candidates, selected)].sum(axis=1) if selected else 0)
        available = ~np.isin(candidates, selected)
        chosen = int(candidates[np.argmin(np.where(available, scores, np.inf))])
        selected.append(chosen)
    iterations = 0
    for _ in range(max_passes):
        if time.process_time() - start >= cpu_cap * .9:
            break
        outside = np.array([x for x in candidates if x not in selected], dtype=int)
        if not len(outside) or not selected:
            break
        chosen = np.array(selected)
        sums = matrix[:, chosen].sum(axis=1)
        removal = -diagonal[chosen] - 2 * (sums[chosen] - diagonal[chosen])
        addition = diagonal[outside] + 2 * sums[outside]
        delta = removal[:, None] + addition[None, :] - 2 * matrix[np.ix_(chosen, outside)]
        row, column = np.unravel_index(np.argmin(delta), delta.shape)
        if delta[row, column] >= -1e-12:
            break
        selected[row] = int(outside[column]); iterations += 1
    x = np.zeros(n); x[selected] = 1
    return x, {'objective': float(x @ matrix @ x + constant), 'cpu_seconds': time.process_time() - start,
               'swap_iterations': iterations, 'solver': 'GREEDY_PLUS_BEST_SWAP', 'cpu_cap': cpu_cap,
               'optimality_status': 'BOUNDED_HEURISTIC', 'selected_count': len(selected), 'cash_slots': k - len(selected)}
