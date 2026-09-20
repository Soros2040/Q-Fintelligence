from __future__ import annotations

import hashlib
import json
import math
import sys
from typing import Any

from cqlib.circuits import Circuit
from cqlib.simulator import StatevectorSimulator

LINEAR = [-0.24, -0.61, -0.78, -0.19, -0.72, -0.31]
RING = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 0)]
OPTIMAL = "011010"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    source = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _objective(bitstring: str) -> float:
    risk = [
        [0.0, 0.07, 0.05, 0.04, 0.08, 0.03],
        [0.07, 0.0, 0.02, 0.06, 0.03, 0.05],
        [0.05, 0.02, 0.0, 0.04, 0.02, 0.07],
        [0.04, 0.06, 0.04, 0.0, 0.05, 0.03],
        [0.08, 0.03, 0.02, 0.05, 0.0, 0.04],
        [0.03, 0.05, 0.07, 0.03, 0.04, 0.0],
    ]
    bits = [int(value) for value in bitstring]
    value = sum(weight * bit for weight, bit in zip(LINEAR, bits, strict=True))
    value += sum(risk[i][j] * bits[i] * bits[j] for i in range(6) for j in range(i + 1, 6))
    return float(value + 1.5 * (sum(bits) - 3) ** 2)


def _build_ir(index: int, generation_index: int = 0) -> dict[str, Any]:
    layers = (
        (1 if index < 25 else 2)
        if generation_index == 0
        else 1 + ((index // 20 + generation_index) % 3)
    )
    gamma = 0.17 + 0.011 * ((index * 7 + 3 + generation_index * 11) % 23)
    beta = 0.29 + 0.008 * ((index * 5 + 7 + generation_index * 13) % 19)
    operations: list[dict[str, Any]] = []
    for qubit in range(6):
        operations.append({"gate": "H", "targets": [qubit]})
    for layer in range(layers):
        layer_gamma = gamma * (1 + 0.07 * layer)
        layer_beta = beta * (1 - 0.05 * layer)
        for qubit, coefficient in enumerate(LINEAR):
            operations.append(
                {"gate": "RZ", "targets": [qubit], "angle": layer_gamma * coefficient}
            )
        for left, right in RING:
            operations.append({"gate": "CZ", "targets": [left, right]})
        for qubit in range(6):
            operations.append({"gate": "RX", "targets": [qubit], "angle": layer_beta})
    # A tiny final phase preserves probabilities while giving every registered
    # candidate a distinct, scientifically traceable parameterization.
    operations.append({"gate": "RZ", "targets": [0], "angle": (index + 1) * 1e-6})
    operations.append({"gate": "MEASURE_ALL", "targets": list(range(6))})
    circuit_ir = {
        "schema_version": "qf.circuit-ir.v1",
        "name": (
            f"p07-qaoa-{index:02d}"
            if generation_index == 0
            else f"p15-g{generation_index:03d}-qaoa-{index:02d}"
        ),
        "qubits": 6,
        "classical_bits": 6,
        "endianness": "little_endian_qubit_index_with_displayed_msb_left",
        "operations": operations,
        "parameters": {"layers": layers, "gamma": gamma, "beta": beta},
        "generation_index": generation_index,
        "formal_test_sealed": True,
    }
    circuit_ir["circuit_hash"] = _hash(circuit_ir)
    return circuit_ir


def _compile(circuit_ir: dict[str, Any]) -> tuple[Circuit, str]:
    circuit = Circuit(6)
    for operation in circuit_ir["operations"]:
        gate = operation["gate"]
        targets = operation["targets"]
        if gate == "H":
            circuit.h(targets[0])
        elif gate == "RZ":
            circuit.rz(targets[0], float(operation["angle"]))
        elif gate == "CZ":
            circuit.cz(targets[0], targets[1])
        elif gate == "RX":
            circuit.rx(targets[0], float(operation["angle"]))
        elif gate == "MEASURE_ALL":
            circuit.measure_all()
        else:
            raise ValueError(f"unregistered Circuit IR gate {gate}")
    return circuit, circuit.qcis


def generate(generation_index: int = 0) -> dict[str, Any]:
    circuits: list[dict[str, Any]] = []
    for index in range(50):
        circuit_ir = _build_ir(index, generation_index)
        circuit, qcis = _compile(circuit_ir)
        probabilities = {
            str(key): float(value)
            for key, value in StatevectorSimulator(circuit, omp_threads=1).probs().items()
        }
        feasible = sum(
            probability for state, probability in probabilities.items() if state.count("1") == 3
        )
        expectation = sum(
            _objective(state) * probability for state, probability in probabilities.items()
        )
        if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-9):
            raise ValueError("local cqlib statevector is not normalized")
        circuits.append(
            {
                "circuit_ir": circuit_ir,
                "qcis": qcis,
                "qcis_sha256": _hash(qcis),
                "manifest": {
                    "schema_version": (
                        "qf.p07.circuit-manifest.v1"
                        if generation_index == 0
                        else "qf.p15.circuit-manifest.v1"
                    ),
                    "family": f"qaoa_p{circuit_ir['parameters']['layers']}",
                    "candidate_index": index,
                    "shots": 100,
                    "depth": circuit.depth(),
                    "instruction_count": len(circuit.instruction_sequence),
                    "gamma": circuit_ir["parameters"]["gamma"],
                    "beta": circuit_ir["parameters"]["beta"],
                    "local_preflight": {
                        "probability_sum": sum(probabilities.values()),
                        "feasible_probability": feasible,
                        "optimal_probability": probabilities.get(OPTIMAL, 0.0),
                        "expected_objective": expectation,
                    },
                    "formal_test_sealed": True,
                },
            }
        )
    hashes = [item["circuit_ir"]["circuit_hash"] for item in circuits]
    if len(set(hashes)) != 50:
        raise AssertionError("P07 Circuit IR candidates must be distinct")
    return {
        "schema_version": (
            "qf.p07.qaoa-batch.v1"
            if generation_index == 0
            else "qf.p15.qaoa-generation.v1"
        ),
        "circuit_count": 50,
        "generation_index": generation_index,
        "circuits": circuits,
        "formal_test_sealed": True,
        "batch_hash": _hash(hashes),
    }


def main() -> int:
    request = json.load(sys.stdin)
    if request.get("action") != "generate_batch":
        raise ValueError("unregistered P07 QAOA action")
    generation_index = int(request.get("generation_index", 0))
    if generation_index < 0:
        raise ValueError("generation_index must be non-negative")
    json.dump(generate(generation_index), sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - bounded adapter envelope.
        json.dump(
            {
                "schema_version": "qf.p07.qaoa-error.v1",
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
