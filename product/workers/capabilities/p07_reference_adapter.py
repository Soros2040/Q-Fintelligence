from __future__ import annotations

import hashlib
import itertools
import json
import math
import sys
from typing import Any


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _classical_reference(request: dict[str, Any]) -> dict[str, Any]:
    import numpy as np
    import pandas as pd
    from pypfopt.base_optimizer import portfolio_performance

    expected = np.asarray(request["expected_returns"], dtype=float)
    covariance = np.asarray(request["covariance"], dtype=float)
    tickers = [str(value) for value in request["tickers"]]
    risk_aversion = float(request["risk_aversion"])
    choose_k = int(request["choose_k"])
    if expected.shape != (6,) or covariance.shape != (6, 6) or len(tickers) != 6:
        raise ValueError("classical reference requires one six-asset instance")
    if choose_k != 3:
        raise ValueError("P07 reference fixes 6 choose 3")
    covariance_frame = pd.DataFrame(covariance, index=tickers, columns=tickers)
    expected_series = pd.Series(expected, index=tickers)
    portfolios: list[dict[str, Any]] = []
    for selected in itertools.combinations(range(6), choose_k):
        weights = np.zeros(6, dtype=float)
        weights[list(selected)] = 1 / choose_k
        annual_return, volatility, _ = portfolio_performance(
            weights,
            expected_series,
            covariance_frame,
            verbose=False,
            risk_free_rate=0.0,
        )
        objective = risk_aversion * float(volatility**2) - float(annual_return)
        portfolios.append(
            {
                "bitstring": "".join("1" if index in selected else "0" for index in range(6)),
                "selected": [tickers[index] for index in selected],
                "equal_weights": weights.tolist(),
                "expected_return": float(annual_return),
                "variance": float(volatility**2),
                "objective": objective,
            }
        )
    portfolios.sort(key=lambda item: (item["objective"], item["bitstring"]))
    if len(portfolios) != math.comb(6, 3):
        raise AssertionError("reference enumeration did not produce 20 portfolios")
    return {
        "schema_version": "qf.p07.classical-reference.v1",
        "package": "pyportfolioopt",
        "package_version": "1.6.0",
        "portfolio_count": len(portfolios),
        "portfolios": portfolios,
        "best": portfolios[0],
        "input_sha256": _sha256(request),
    }


def _qiskit_reference(request: dict[str, Any]) -> dict[str, Any]:
    from qiskit import QuantumCircuit
    from qiskit.quantum_info import Statevector
    from qiskit.transpiler import CouplingMap, generate_preset_pass_manager

    circuit_ir = request["circuit_ir"]
    qubits = int(circuit_ir["qubits"])
    if qubits != 6:
        raise ValueError("P07 Qiskit reference requires six qubits")
    circuit = QuantumCircuit(qubits)
    measured = False
    for operation in circuit_ir["operations"]:
        gate = operation["gate"]
        targets = [int(value) for value in operation["targets"]]
        angle = operation.get("angle")
        if gate == "H":
            circuit.h(targets[0])
        elif gate == "X":
            circuit.x(targets[0])
        elif gate == "RX":
            circuit.rx(float(angle), targets[0])
        elif gate == "RY":
            circuit.ry(float(angle), targets[0])
        elif gate == "RZ":
            circuit.rz(float(angle), targets[0])
        elif gate == "CZ":
            circuit.cz(targets[0], targets[1])
        elif gate == "MEASURE_ALL":
            measured = True
        else:
            raise ValueError(f"unsupported canonical gate {gate}")
    probabilities = Statevector.from_instruction(circuit).probabilities_dict()
    coupling = CouplingMap(couplinglist=[tuple(edge) for edge in request["coupling_map"]])
    pass_manager = generate_preset_pass_manager(
        optimization_level=2,
        coupling_map=coupling,
        basis_gates=["rz", "rx", "h", "cz"],
        seed_transpiler=20260723,
    )
    transpiled = pass_manager.run(circuit)
    return {
        "schema_version": "qf.p07.qiskit-reference.v1",
        "package": "qiskit",
        "package_version": "2.5.0",
        "logical_depth": circuit.depth(),
        "transpiled_depth": transpiled.depth(),
        "transpiled_gate_counts": dict(transpiled.count_ops()),
        "probabilities": {str(key): float(value) for key, value in probabilities.items()},
        "probability_sum": float(sum(probabilities.values())),
        "measurement_deferred_for_statevector": measured,
        "input_sha256": _sha256(request),
    }


def main() -> int:
    request = json.load(sys.stdin)
    action = request.get("action")
    if action == "classical_reference":
        result = _classical_reference(request)
    elif action == "qiskit_reference":
        result = _qiskit_reference(request)
    else:
        raise ValueError("unregistered P07 capability action")
    result["result_sha256"] = _sha256(result)
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - bounded adapter envelope.
        json.dump(
            {
                "schema_version": "qf.p07.capability-error.v1",
                "status": "FAILED",
                "error_type": type(error).__name__,
                "message": str(error)[:1000],
            },
            sys.stdout,
            ensure_ascii=False,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        raise SystemExit(1) from None
