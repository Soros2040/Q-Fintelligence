from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from cqlib.circuits import Circuit
from cqlib.simulator import StatevectorSimulator
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.neural_network import MLPRegressor

TRAIN_END = pd.Timestamp("2022-12-31")
VALIDATION_START = pd.Timestamp("2023-01-01")
VALIDATION_END = pd.Timestamp("2023-12-31")
FORMAL_TEST_START = pd.Timestamp("2024-01-01")


def _sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, np.floating):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.ndarray):
        return [_finite(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(key): _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def generalized_fevd(
    returns: np.ndarray, ridge_alpha: float = 1e-3, horizon: int = 10
) -> np.ndarray:
    """Fit a six-dimensional Ridge VAR(1) and return row-normalized generalized FEVD."""
    if returns.ndim != 2 or returns.shape[0] < 20:
        raise ValueError("VAR window is too small")
    if not np.isfinite(returns).all():
        raise ValueError("VAR window contains non-finite values")
    x = returns[:-1]
    y = returns[1:]
    x_augmented = np.column_stack([np.ones(len(x)), x])
    penalty = np.eye(x_augmented.shape[1]) * ridge_alpha
    penalty[0, 0] = 0
    coefficients = np.linalg.solve(x_augmented.T @ x_augmented + penalty, x_augmented.T @ y)
    transition = coefficients[1:].T
    residuals = y - x_augmented @ coefficients
    covariance = np.cov(residuals, rowvar=False)
    covariance = np.atleast_2d(covariance) + np.eye(y.shape[1]) * 1e-10
    spectral_radius = max(abs(np.linalg.eigvals(transition)))
    if not np.isfinite(spectral_radius) or spectral_radius >= 1.05:
        transition = transition / max(float(spectral_radius) + 0.02, 1.0)
    variables = y.shape[1]
    numerator = np.zeros((variables, variables), dtype=float)
    denominator = np.zeros(variables, dtype=float)
    phi = np.eye(variables)
    for _ in range(horizon):
        transformed = phi @ covariance
        for affected in range(variables):
            denominator[affected] += float(transformed[affected] @ phi[affected])
            for source in range(variables):
                variance = max(float(covariance[source, source]), 1e-12)
                numerator[affected, source] += float(transformed[affected, source] ** 2 / variance)
        phi = phi @ transition
    fevd = numerator / np.maximum(denominator[:, None], 1e-12)
    fevd /= np.maximum(fevd.sum(axis=1, keepdims=True), 1e-12)
    return fevd


def rolling_fevd(
    returns: pd.DataFrame, window: int = 120
) -> tuple[dict[pd.Timestamp, np.ndarray], list[str]]:
    matrices: dict[pd.Timestamp, np.ndarray] = {}
    failures: list[str] = []
    values = returns.to_numpy(dtype=float)
    for position in range(window, len(returns)):
        date = returns.index[position]
        sample = values[position - window : position]
        try:
            matrices[date] = generalized_fevd(sample)
        except (ValueError, np.linalg.LinAlgError) as error:
            failures.append(f"{date.date().isoformat()}:{type(error).__name__}")
    if not matrices:
        raise ValueError("no rolling FEVD window succeeded")
    return matrices, failures


def fixed_topology(
    matrices: dict[pd.Timestamp, np.ndarray],
    dates: pd.DatetimeIndex,
    node_codes: list[str],
    top_k: int = 2,
) -> list[tuple[int, int]]:
    train_matrices = [
        matrix for date, matrix in matrices.items() if date in dates and date <= TRAIN_END
    ]
    if not train_matrices:
        raise ValueError("training FEVD matrices are missing")
    average = np.mean(train_matrices, axis=0)
    edges: list[tuple[int, int]] = []
    for affected in range(len(node_codes)):
        candidates = [source for source in range(len(node_codes)) if source != affected]
        candidates.sort(key=lambda source: (-float(average[affected, source]), node_codes[source]))
        edges.extend((source, affected) for source in candidates[:top_k])
    return edges


def _apply_ry(state: np.ndarray, qubit: int, theta: float, qubits: int) -> np.ndarray:
    cosine = math.cos(theta / 2)
    sine = math.sin(theta / 2)
    result = state.copy()
    mask = 1 << qubit
    for basis in range(1 << qubits):
        if basis & mask:
            continue
        other = basis | mask
        zero = state[basis]
        one = state[other]
        result[basis] = cosine * zero - sine * one
        result[other] = sine * zero + cosine * one
    return result


def _apply_cry(
    state: np.ndarray, control: int, target: int, theta: float, qubits: int
) -> np.ndarray:
    cosine = math.cos(theta / 2)
    sine = math.sin(theta / 2)
    result = state.copy()
    control_mask = 1 << control
    target_mask = 1 << target
    for basis in range(1 << qubits):
        if not basis & control_mask or basis & target_mask:
            continue
        other = basis | target_mask
        zero = state[basis]
        one = state[other]
        result[basis] = cosine * zero - sine * one
        result[other] = sine * zero + cosine * one
    return result


def quantum_node_features(
    angles: np.ndarray,
    edges: list[tuple[int, int]],
    affected_source_weights: np.ndarray,
    include_graph: bool,
    trainable_offset: float,
) -> np.ndarray:
    qubits = len(angles)
    state = np.zeros(1 << qubits, dtype=np.complex128)
    state[0] = 1
    for qubit, angle in enumerate(angles):
        state = _apply_ry(state, qubit, float(angle + trainable_offset), qubits)
    if include_graph:
        for source, affected in edges:
            weight = float(affected_source_weights[affected, source])
            state = _apply_cry(state, source, affected, weight * math.pi / 2, qubits)
    for qubit in range(qubits):
        state = _apply_ry(state, qubit, trainable_offset / 2, qubits)
    probabilities = np.abs(state) ** 2
    z = np.zeros(qubits, dtype=float)
    for qubit in range(qubits):
        mask = 1 << qubit
        z[qubit] = sum(
            probability if not basis & mask else -probability
            for basis, probability in enumerate(probabilities)
        )
    return z


def build_cqlib_circuits(
    edges: list[tuple[int, int]], weights: np.ndarray, angles: np.ndarray
) -> dict[str, Any]:
    qgnn = Circuit(6)
    for qubit in range(6):
        qgnn.ry(qubit, float(angles[qubit]))
    for source, affected in edges:
        if weights[affected, source] <= 0:
            continue
        qgnn.cry(source, affected, float(weights[affected, source] * math.pi / 2))
    qgnn.measure_all()

    qaoa = Circuit(6)
    for qubit in range(6):
        qaoa.h(qubit)
    for qubit in range(6):
        qaoa.rz(qubit, float((qubit + 1) * 0.071))
    for left, right in [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (0, 5)]:
        qaoa.cz(left, right)
    for qubit in range(6):
        qaoa.rx(qubit, 0.43)
    qaoa.measure_all()

    qgnn_simulator = StatevectorSimulator(qgnn, omp_threads=1)
    qaoa_simulator = StatevectorSimulator(qaoa, omp_threads=1)
    qgnn_probabilities = {key: float(value) for key, value in qgnn_simulator.probs().items()}
    qaoa_probabilities = {key: float(value) for key, value in qaoa_simulator.probs().items()}
    return {
        "qgnn": {
            "qcis": qgnn.qcis,
            "qcis_sha256": _sha256(qgnn.qcis),
            "depth": qgnn.depth(),
            "instruction_count": len(qgnn.instruction_sequence),
            "probabilities": qgnn_probabilities,
        },
        "qaoa": {
            "qcis": qaoa.qcis,
            "qcis_sha256": _sha256(qaoa.qcis),
            "depth": qaoa.depth(),
            "instruction_count": len(qaoa.instruction_sequence),
            "probabilities": qaoa_probabilities,
        },
        "cqlib_version": "1.3.11",
        "backend": "cqlib.StatevectorSimulator",
    }


def _future_downside_risk(returns: pd.DataFrame, horizon: int = 5) -> pd.DataFrame:
    values = np.full_like(returns.to_numpy(dtype=float), np.nan)
    raw = returns.to_numpy(dtype=float)
    for position in range(len(returns) - horizon):
        future = raw[position + 1 : position + horizon + 1]
        values[position] = np.sqrt((252 / horizon) * np.square(np.minimum(future, 0)).sum(axis=0))
    return pd.DataFrame(values, index=returns.index, columns=returns.columns)


def _empirical_cdf(train: pd.Series, values: pd.Series) -> pd.Series:
    reference = np.sort(train.dropna().to_numpy(dtype=float))
    ranks = np.searchsorted(reference, values.to_numpy(dtype=float), side="right") / max(
        len(reference), 1
    )
    return pd.Series(np.clip(ranks, 0, 1), index=values.index)


def _metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    return {
        "mae": float(mean_absolute_error(actual, predicted)),
        "rmse": float(math.sqrt(mean_squared_error(actual, predicted))),
    }


def _apply_rx_mixer(state: np.ndarray, beta: float, qubits: int) -> np.ndarray:
    result = state
    for qubit in range(qubits):
        cosine = math.cos(beta)
        sine = -1j * math.sin(beta)
        next_state = result.copy()
        mask = 1 << qubit
        for basis in range(1 << qubits):
            if basis & mask:
                continue
            other = basis | mask
            zero = result[basis]
            one = result[other]
            next_state[basis] = cosine * zero + sine * one
            next_state[other] = sine * zero + cosine * one
        result = next_state
    return result


def qaoa_p1(costs: np.ndarray, shots: int = 4096, seed: int = 20260721) -> dict[str, Any]:
    qubits = int(round(math.log2(len(costs))))
    best: tuple[float, float, float, np.ndarray] | None = None
    initial = np.full(len(costs), 1 / math.sqrt(len(costs)), dtype=np.complex128)
    cost_scale = max(float(np.std(costs)), 1e-9)
    scaled = (costs - float(np.min(costs))) / cost_scale
    for gamma in np.linspace(0, math.pi, 25):
        phased = initial * np.exp(-1j * gamma * scaled)
        for beta in np.linspace(0, math.pi / 2, 17):
            state = _apply_rx_mixer(phased, float(beta), qubits)
            probabilities = np.abs(state) ** 2
            expectation = float(probabilities @ costs)
            if best is None or expectation < best[0]:
                best = (expectation, float(gamma), float(beta), probabilities)
    assert best is not None
    expectation, gamma, beta, probabilities = best
    rng = np.random.default_rng(seed)
    samples = rng.multinomial(shots, probabilities / probabilities.sum())
    distribution = {
        format(index, f"0{qubits}b"): int(count) for index, count in enumerate(samples) if count > 0
    }
    feasible = np.asarray([int(index).bit_count() == 3 for index in range(len(costs))])
    feasible_rate = float(samples[feasible].sum() / shots)
    feasible_costs = np.where(feasible, costs, np.inf)
    exact_index = int(np.argmin(feasible_costs))
    best_sample_index = min(
        (index for index, count in enumerate(samples) if count > 0 and feasible[index]),
        key=lambda index: costs[index],
    )
    return {
        "depth_p": 1,
        "shots": shots,
        "seed": seed,
        "feasible_portfolio_count": int(feasible.sum()),
        "selected_qubits": [qubit for qubit in range(qubits) if best_sample_index & (1 << qubit)],
        "gamma": gamma,
        "beta": beta,
        "probabilities": {
            format(index, f"0{qubits}b"): float(value) for index, value in enumerate(probabilities)
        },
        "sample_counts": distribution,
        "feasible_rate": feasible_rate,
        "expected_objective": expectation,
        "exact_optimal_bitstring": format(exact_index, f"0{qubits}b"),
        "exact_optimal_probability": float(probabilities[exact_index]),
        "best_sample_bitstring": format(best_sample_index, f"0{qubits}b"),
        "best_sample_gap": float(costs[best_sample_index] - costs[exact_index]),
    }


@dataclass
class PreparedData:
    bundle_hash: str
    node_codes: list[str]
    prices: pd.DataFrame
    amounts: pd.DataFrame
    returns: pd.DataFrame


def prepare_bundle(bundle: dict[str, Any]) -> PreparedData:
    if bundle.get("formalTestSealed") is not True:
        raise ValueError("formal test seal is missing")
    serialized = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    daily = pd.DataFrame(bundle["daily"])
    factors = pd.DataFrame(bundle["adjustmentFactors"])
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], format="%Y%m%d")
    factors["trade_date"] = pd.to_datetime(factors["trade_date"], format="%Y%m%d")
    if daily["trade_date"].max() >= FORMAL_TEST_START:
        raise ValueError("formal test data entered the P02 science runner")
    merged = daily.merge(factors, on=["ts_code", "trade_date"], how="inner", validate="one_to_one")
    merged["adjusted_close"] = merged["close"].astype(float) * merged["adj_factor"].astype(float)
    node_codes = [item["tsCode"] for item in bundle["selected"]]
    prices = merged.pivot(
        index="trade_date", columns="ts_code", values="adjusted_close"
    ).sort_index()[node_codes]
    amounts = merged.pivot(index="trade_date", columns="ts_code", values="amount").sort_index()[
        node_codes
    ]
    prices = prices.dropna(how="any")
    amounts = amounts.reindex(prices.index).ffill()
    returns = np.log(prices).diff().dropna()
    if (
        len(returns.loc[:TRAIN_END]) < 600
        or len(returns.loc[VALIDATION_START:VALIDATION_END]) < 200
    ):
        raise ValueError("training or validation data is incomplete")
    return PreparedData(_sha256(serialized), node_codes, prices, amounts, returns)


def run_science_pipeline(bundle: dict[str, Any], seed: int = 20260721) -> dict[str, Any]:
    prepared = prepare_bundle(bundle)
    matrices, fevd_failures = rolling_fevd(prepared.returns)
    edges = fixed_topology(matrices, prepared.returns.index, prepared.node_codes)
    risk = _future_downside_risk(prepared.returns)
    percentiles = risk.copy()
    for code in prepared.node_codes:
        train = risk.loc[:TRAIN_END, code]
        percentiles[code] = _empirical_cdf(train, risk[code])

    features: dict[str, pd.DataFrame] = {
        "ret5": prepared.returns.rolling(5).sum(),
        "ret20": prepared.returns.rolling(20).sum(),
        "downside5": np.sqrt(252 / 5 * np.square(prepared.returns.clip(upper=0)).rolling(5).sum()),
        "downside20": np.sqrt(
            252 / 20 * np.square(prepared.returns.clip(upper=0)).rolling(20).sum()
        ),
        "amount_change20": np.log(prepared.amounts).diff(20).reindex(prepared.returns.index),
    }
    net_spillover = pd.DataFrame(
        index=prepared.returns.index, columns=prepared.node_codes, dtype=float
    )
    for date, matrix in matrices.items():
        incoming = matrix.sum(axis=1) - np.diag(matrix)
        outgoing = matrix.sum(axis=0) - np.diag(matrix)
        net_spillover.loc[date] = incoming - outgoing
    features["net_spillover"] = net_spillover

    common_index = percentiles.dropna().index
    for frame in features.values():
        common_index = common_index.intersection(frame.dropna().index)
    common_index = common_index[common_index < FORMAL_TEST_START]
    x_frame = pd.concat({name: frame.loc[common_index] for name, frame in features.items()}, axis=1)
    y_frame = percentiles.loc[common_index]
    train_mask = x_frame.index <= TRAIN_END
    validation_mask = (x_frame.index >= VALIDATION_START) & (x_frame.index <= VALIDATION_END)
    x_train = x_frame.loc[train_mask].to_numpy(dtype=float)
    x_validation = x_frame.loc[validation_mask].to_numpy(dtype=float)
    y_train = y_frame.loc[train_mask].to_numpy(dtype=float)
    y_validation = y_frame.loc[validation_mask].to_numpy(dtype=float)
    means = x_train.mean(axis=0)
    scales = x_train.std(axis=0)
    scales[scales < 1e-12] = 1
    x_train_scaled = (x_train - means) / scales
    x_validation_scaled = (x_validation - means) / scales

    ridge = Ridge(alpha=1.0).fit(x_train_scaled, y_train)
    ridge_prediction = np.clip(ridge.predict(x_validation_scaled), 0, 1)
    persistence = np.clip(
        y_frame.shift(1).loc[x_frame.loc[validation_mask].index].to_numpy(dtype=float), 0, 1
    )
    mlp = MLPRegressor(hidden_layer_sizes=(24,), activation="tanh", max_iter=300, random_state=seed)
    mlp.fit(x_train_scaled, y_train)
    mlp_prediction = np.clip(mlp.predict(x_validation_scaled), 0, 1)

    angle_feature = features["downside20"].loc[common_index]
    angle_train = angle_feature.loc[train_mask]
    angle_mean = angle_train.mean(axis=0).to_numpy(dtype=float)
    angle_scale = angle_train.std(axis=0).replace(0, 1).to_numpy(dtype=float)
    angles = np.clip((angle_feature.to_numpy(dtype=float) - angle_mean) / angle_scale, -3, 3) * (
        math.pi / 3
    )
    quantum_train: list[np.ndarray] = []
    graph_free_train: list[np.ndarray] = []
    quantum_validation: list[np.ndarray] = []
    graph_free_validation: list[np.ndarray] = []
    best_offset = 0.2
    for row_index, date in enumerate(common_index):
        matrix = matrices.get(date)
        if matrix is None:
            earlier = [candidate for candidate in matrices if candidate <= date]
            matrix = matrices[max(earlier)]
        qgnn_features = quantum_node_features(angles[row_index], edges, matrix, True, best_offset)
        pqc_features = quantum_node_features(angles[row_index], [], matrix, False, best_offset)
        if date <= TRAIN_END:
            quantum_train.append(qgnn_features)
            graph_free_train.append(pqc_features)
        elif VALIDATION_START <= date <= VALIDATION_END:
            quantum_validation.append(qgnn_features)
            graph_free_validation.append(pqc_features)
    qgnn_readout = Ridge(alpha=0.1).fit(np.asarray(quantum_train), y_train)
    pqc_readout = Ridge(alpha=0.1).fit(np.asarray(graph_free_train), y_train)
    qgnn_prediction = np.clip(qgnn_readout.predict(np.asarray(quantum_validation)), 0, 1)
    pqc_prediction = np.clip(pqc_readout.predict(np.asarray(graph_free_validation)), 0, 1)

    topology_matrix = np.zeros((6, 6), dtype=float)
    training_matrix = np.mean(
        [matrix for date, matrix in matrices.items() if date <= TRAIN_END], axis=0
    )
    for source, affected in edges:
        topology_matrix[affected, source] = training_matrix[affected, source]
    graph_projection = np.eye(6) + topology_matrix
    graph_projection /= graph_projection.sum(axis=1, keepdims=True)
    graph_train = np.asarray([graph_projection @ row for row in y_train])
    graph_validation = np.asarray([graph_projection @ row for row in ridge_prediction])
    graph_readout = Ridge(alpha=0.5).fit(graph_train, y_train)
    graph_prediction = np.clip(graph_readout.predict(graph_validation), 0, 1)

    validation_metrics = {
        "persistence": _metrics(y_validation, persistence),
        "ridge": _metrics(y_validation, ridge_prediction),
        "mlp": _metrics(y_validation, mlp_prediction),
        "graph_ridge": _metrics(y_validation, graph_prediction),
        "graph_free_pqc": _metrics(y_validation, pqc_prediction),
        "qgnn": _metrics(y_validation, qgnn_prediction),
    }

    decision_date = x_frame.loc[validation_mask].index[-1]
    latest_returns = prepared.returns.loc[:decision_date].tail(120)
    covariance = latest_returns.cov().to_numpy(dtype=float)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    covariance = eigenvectors @ np.diag(np.clip(eigenvalues, 1e-10, None)) @ eigenvectors.T
    expected_returns = latest_returns.tail(20).mean().to_numpy(dtype=float) * 5
    rho = qgnn_prediction[-1]
    directed = matrices[decision_date]
    symmetric_risk = (directed + directed.T) / 2
    np.fill_diagonal(symmetric_risk, 0)
    alpha, risk_weight, spillover_weight, node_risk_weight = 1.0, 1.0, 0.2, 0.5
    unconstrained_bound = (
        abs(alpha) * float(np.abs(expected_returns).sum())
        + risk_weight * float(np.abs(covariance).sum())
        + spillover_weight * float(np.abs(symmetric_risk).sum())
        + node_risk_weight * float(np.abs(rho).sum())
    )
    cardinality_penalty = unconstrained_bound + 1.0
    costs = np.zeros(64, dtype=float)
    components: list[dict[str, Any]] = []
    for index in range(64):
        x = np.asarray([(index >> qubit) & 1 for qubit in range(6)], dtype=float)
        return_term = -alpha * float(expected_returns @ x)
        covariance_term = risk_weight * float(x @ covariance @ x)
        spillover_term = spillover_weight * float(x @ symmetric_risk @ x)
        node_term = node_risk_weight * float(rho @ x)
        penalty_term = cardinality_penalty * float((x.sum() - 3) ** 2)
        costs[index] = return_term + covariance_term + spillover_term + node_term + penalty_term
        if int(x.sum()) == 3:
            components.append(
                {
                    "bitstring": format(index, "06b"),
                    "selected": [prepared.node_codes[qubit] for qubit in range(6) if x[qubit] == 1],
                    "return": return_term,
                    "covariance": covariance_term,
                    "spillover": spillover_term,
                    "node_risk": node_term,
                    "total_without_penalty": return_term
                    + covariance_term
                    + spillover_term
                    + node_term,
                }
            )
    components.sort(key=lambda item: (item["total_without_penalty"], item["bitstring"]))
    if len(components) != 20:
        raise AssertionError("6 choose 3 exact enumeration did not produce 20 portfolios")
    qaoa = qaoa_p1(costs, seed=seed)
    local_circuits = build_cqlib_circuits(edges, directed, angles[-1])

    result = {
        "schema_version": "qf.science-result.v1",
        "bundle_sha256": prepared.bundle_hash,
        "formal_test": {
            "sealed": True,
            "downloaded_max_date": prepared.returns.index.max().date().isoformat(),
            "metrics_max_date": decision_date.date().isoformat(),
            "test_metrics_emitted": False,
        },
        "universe": {"node_order": prepared.node_codes, "count": len(prepared.node_codes)},
        "data": {
            "training_rows": int(train_mask.sum()),
            "validation_rows": int(validation_mask.sum()),
            "return_rows": len(prepared.returns),
        },
        "risk_graph": {
            "window": 120,
            "lag": 1,
            "fevd_horizon": 10,
            "direction": "shock_source_to_affected_target",
            "edges": [
                {
                    "source": prepared.node_codes[source],
                    "affected": prepared.node_codes[affected],
                    "training_average_weight": float(training_matrix[affected, source]),
                }
                for source, affected in edges
            ],
            "failed_windows": fevd_failures,
        },
        "models": {
            "validation_only_metrics": validation_metrics,
            "qgnn": {
                "qubits": 6,
                "layers": 1,
                "readout": "per_qubit_z_to_node_risk_with_train_only_ridge_calibration",
                "validation_output_shape": list(qgnn_prediction.shape),
            },
        },
        "qubo": {
            "decision_date": decision_date.date().isoformat(),
            "choose_k": 3,
            "equal_weight": True,
            "cardinality_penalty": cardinality_penalty,
            "exact_portfolios": components,
            "exact_best": components[0],
        },
        "qaoa": qaoa,
        "cqlib_local": local_circuits,
        "seed": seed,
    }
    return _finite(result)


def run_reproducibility_sweep(science_result: dict[str, Any], seed: int) -> dict[str, Any]:
    exact = science_result["qubo"]["exact_portfolios"]
    base_cost = {item["bitstring"]: float(item["total_without_penalty"]) for item in exact}
    ordered = sorted(base_cost)
    vector = np.asarray([base_cost[bitstring] for bitstring in ordered], dtype=float)
    rng = np.random.default_rng(seed)
    bootstrap = []
    for _ in range(2_000):
        sample = rng.choice(vector, size=len(vector), replace=True)
        bootstrap.append(float(sample.mean()))
    return {
        "schema_version": "qf.reproducibility-sweep.v1",
        "seed": seed,
        "portfolio_count": len(ordered),
        "bootstrap_repetitions": len(bootstrap),
        "mean_objective": float(np.mean(vector)),
        "bootstrap_mean_std": float(np.std(bootstrap)),
        "qaoa_exact_probability": float(science_result["qaoa"]["exact_optimal_probability"]),
        "source_bundle_sha256": science_result["bundle_sha256"],
    }


def run_active_reproducibility_block(
    science_result: dict[str, Any],
    seed_start: int,
    minimum_compute_seconds: int,
) -> dict[str, Any]:
    if minimum_compute_seconds < 1 or minimum_compute_seconds > 300:
        raise ValueError("active reproducibility block must be between 1 and 300 seconds")
    exact = science_result["qubo"]["exact_portfolios"]
    vector = np.asarray([float(item["total_without_penalty"]) for item in exact], dtype=float)
    started = time.perf_counter()
    seed = seed_start
    batch_summaries: list[dict[str, float | int]] = []
    digest = hashlib.sha256()
    while time.perf_counter() - started < minimum_compute_seconds:
        rng = np.random.default_rng(seed)
        samples = rng.choice(vector, size=(50_000, len(vector)), replace=True)
        means = samples.mean(axis=1)
        summary = {
            "seed": seed,
            "bootstrap_mean": float(means.mean()),
            "bootstrap_std": float(means.std()),
            "minimum": float(means.min()),
            "maximum": float(means.max()),
        }
        digest.update(json.dumps(summary, sort_keys=True).encode("utf-8"))
        if len(batch_summaries) < 64:
            batch_summaries.append(summary)
        seed += 1
    elapsed = time.perf_counter() - started
    return {
        "schema_version": "qf.active-reproducibility-block.v1",
        "seed_start": seed_start,
        "seed_end_exclusive": seed,
        "batches": seed - seed_start,
        "bootstrap_repetitions_per_batch": 50_000,
        "active_compute_seconds": elapsed,
        "summary_digest": digest.hexdigest(),
        "representative_batches": batch_summaries,
        "source_bundle_sha256": science_result["bundle_sha256"],
    }


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(_finite(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)
    return _sha256(serialized)
