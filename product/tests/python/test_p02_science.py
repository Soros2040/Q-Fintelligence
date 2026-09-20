from __future__ import annotations

import numpy as np
import pytest
from qf_finance_worker.science import (
    build_cqlib_circuits,
    generalized_fevd,
    prepare_bundle,
    qaoa_p1,
    quantum_node_features,
)


def test_generalized_fevd_is_finite_directed_and_row_normalized() -> None:
    generator = np.random.default_rng(20260721)
    values = np.zeros((420, 6), dtype=float)
    shocks = generator.normal(0, 0.01, size=values.shape)
    for row in range(1, len(values)):
        values[row, 0] = 0.72 * values[row - 1, 0] + shocks[row, 0]
        values[row, 1] = 0.58 * values[row - 1, 0] + 0.12 * values[row - 1, 1] + shocks[row, 1]
        values[row, 2:] = 0.18 * values[row - 1, 2:] + shocks[row, 2:]
    matrix = generalized_fevd(values, horizon=10)
    assert matrix.shape == (6, 6)
    assert np.isfinite(matrix).all()
    assert np.allclose(matrix.sum(axis=1), 1)
    assert matrix[1, 0] > matrix[0, 1]


def test_cqlib_and_local_statevector_paths_are_executable() -> None:
    edges = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 0)]
    weights = np.full((6, 6), 0.1, dtype=float)
    angles = np.linspace(-0.4, 0.4, 6)
    features = quantum_node_features(angles, edges, weights, True, 0.2)
    circuits = build_cqlib_circuits(edges, weights, angles)
    assert features.shape == (6,)
    assert np.isfinite(features).all()
    assert abs(sum(circuits["qgnn"]["probabilities"].values()) - 1) < 1e-9
    assert abs(sum(circuits["qaoa"]["probabilities"].values()) - 1) < 1e-9


def test_qaoa_p1_evaluates_all_6_choose_3_portfolios_reproducibly() -> None:
    costs = np.linspace(0.1, 1.0, 64)
    first = qaoa_p1(costs, shots=2048, seed=7)
    second = qaoa_p1(costs, shots=2048, seed=7)
    assert first == second
    assert first["feasible_portfolio_count"] == 20
    assert len(first["selected_qubits"]) == 3
    assert first["shots"] == 2048


def test_formal_test_rows_are_rejected_before_science_execution() -> None:
    bundle = {
        "formalTestSealed": True,
        "selected": [{"tsCode": "000001.SZ"}],
        "daily": [{"ts_code": "000001.SZ", "trade_date": "20240102", "close": 1.0, "amount": 1.0}],
        "adjustmentFactors": [
            {"ts_code": "000001.SZ", "trade_date": "20240102", "adj_factor": 1.0}
        ],
    }
    with pytest.raises(ValueError, match="formal test data"):
        prepare_bundle(bundle)
