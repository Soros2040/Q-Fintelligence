"""Explicit local QCIS density-matrix model; no provider/network interface.

Internal qubits follow first appearance; qubits[0] is the least-significant bit.
Output labels list reversed terminal measurement order, explicitly in bit_labels.
Channels are defined here, not inferred to equal TianYan's proprietary simulator.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Mapping

import numpy as np

MAX_QUBITS = 10
MAX_OPERATIONS = 10000
I = np.eye(2, dtype=complex)
X = np.array([[0, 1], [1, 0]], dtype=complex)
Y = np.array([[0, -1j], [1j, 0]], dtype=complex)
Z = np.diag([1, -1]).astype(complex)
H = np.array([[1, 1], [1, -1]], dtype=complex) / np.sqrt(2)
FIXED = {"I": I, "X": X, "Y": Y, "Z": Z, "H": H,
         "S": np.diag([1, 1j]), "S+": np.diag([1, -1j]),
         "T": np.diag([1, np.exp(1j * np.pi / 4)]),
         "T+": np.diag([1, np.exp(-1j * np.pi / 4)])}
TWO = {
    "CX": np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1], [0, 0, 1, 0]], complex),
    "CZ": np.diag([1, 1, 1, -1]).astype(complex),
    "SWAP": np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], complex),
}


@dataclass(frozen=True)
class Gate:
    name: str
    qubits: tuple[int, ...]
    angle: float | None = None


@dataclass(frozen=True)
class CircuitSpec:
    qubits: tuple[int, ...]
    operations: tuple[Gate, ...]
    measured_qubits: tuple[int, ...]


@dataclass(frozen=True)
class NoiseRule:
    channel: str
    p: float = 0.0
    qubits: tuple[int, ...] | None = None
    gates: tuple[str, ...] | None = None
    position: str = "after_gate"
    params: Mapping | None = None


@dataclass
class SimulationResult:
    rho: np.ndarray
    probabilities: np.ndarray
    bit_labels: tuple[str, ...]


def parse_qcis(text: str) -> CircuitSpec:
    """Parse a bounded numeric QCIS subset, rejecting unsupported semantics."""
    qubits, measured, operations = [], [], []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        name = parts[0]
        angle = None
        if name == "M":
            if len(parts) != 2:
                raise ValueError(f"line {line_no}: one terminal measurement per line required")
            operands = parts[1:]
        elif name in FIXED or name in TWO or name in {"RX", "RY", "RZ"}:
            if measured:
                raise ValueError("quantum gate after measurement is unsupported")
            arity = 2 if name in TWO else 1
            rotating = name in {"RX", "RY", "RZ"}
            if len(parts) != 1 + arity + int(rotating):
                raise ValueError(f"line {line_no}: invalid gate arity")
            operands = parts[1:1 + arity]
            if rotating:
                angle = float(parts[-1])
                if not math.isfinite(angle):
                    raise ValueError("angle must be finite; expressions are not evaluated")
        else:
            raise ValueError(f"line {line_no}: unsupported QCIS gate {name}")
        if not all(re.fullmatch(r"Q[0-9]+", q) for q in operands):
            raise ValueError("qubits must be nonnegative Q-number tokens")
        targets = tuple(int(q[1:]) for q in operands)
        if len(set(targets)) != len(targets):
            raise ValueError("gate cannot repeat a qubit")
        for q in targets:
            if q not in qubits:
                qubits.append(q)
        if len(qubits) > MAX_QUBITS:
            raise ValueError(f"local density model capped at {MAX_QUBITS} qubits")
        if name == "M":
            if targets[0] in measured:
                raise ValueError("duplicate measurement")
            measured.append(targets[0])
        else:
            operations.append(Gate(name, targets, angle))
            if len(operations) > MAX_OPERATIONS:
                raise ValueError("operation limit exceeded")
    if not qubits or not measured:
        raise ValueError("nonempty circuit with explicit terminal measurements required")
    return CircuitSpec(tuple(qubits), tuple(operations), tuple(measured))


def _prob(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("channel probabilities must be finite and in [0, 1]")
    return value


def _kraus(rule: NoiseRule) -> tuple[np.ndarray, ...]:
    p = _prob(rule.p)
    params = rule.params or {}
    channel = rule.channel
    if channel in {"bit_flip", "phase_flip", "bit_phase_flip"}:
        op = {"bit_flip": X, "phase_flip": Z, "bit_phase_flip": Y}[channel]
        return np.sqrt(1 - p) * I, np.sqrt(p) * op
    if channel in {"pauli", "depolarizing"}:
        if channel == "pauli":
            weights = [_prob(params.get(k, 0)) for k in ("px", "py", "pz")]
        else:
            # (1-p)rho + p I/2 on this subsystem, not total Pauli error p.
            weights = [p / 4] * 3
        remainder = 1 - sum(weights)
        if remainder < -1e-14:
            raise ValueError("Pauli probabilities sum exceeds one")
        return tuple(np.sqrt(w) * op for w, op in zip([max(0, remainder), *weights], [I, X, Y, Z]))
    if channel == "amplitude_damping":
        return np.diag([1, np.sqrt(1 - p)]), np.array([[0, np.sqrt(p)], [0, 0]])
    if channel == "phase_damping":
        return np.diag([1, np.sqrt(1 - p)]), np.diag([0, np.sqrt(p)])
    raise ValueError(f"unsupported local noise channel {channel}")


def _matrix(gate: Gate) -> np.ndarray:
    if gate.name in FIXED:
        return FIXED[gate.name]
    if gate.name in TWO:
        return TWO[gate.name]
    op = {"RX": X, "RY": Y, "RZ": Z}[gate.name]
    return np.cos(gate.angle / 2) * I - 1j * np.sin(gate.angle / 2) * op


def _apply_axes(tensor: np.ndarray, matrix: np.ndarray, axes: tuple[int, ...]) -> np.ndarray:
    moved = np.moveaxis(tensor, axes, tuple(range(len(axes))))
    transformed = (matrix @ moved.reshape(2 ** len(axes), -1)).reshape(moved.shape)
    return np.moveaxis(transformed, tuple(range(len(axes))), axes)


def _conjugate(rho: np.ndarray, matrix: np.ndarray, targets: tuple[int, ...], n: int) -> np.ndarray:
    axes = tuple(n - 1 - q for q in targets)
    tensor = _apply_axes(rho.reshape((2,) * (2 * n)), matrix, axes)
    tensor = _apply_axes(tensor, matrix.conj(), tuple(n + q for q in axes))
    return tensor.reshape(rho.shape)


def _readout(probabilities: np.ndarray, rule: NoiseRule, measured: tuple[int, ...]) -> np.ndarray:
    targets = measured if rule.qubits is None else rule.qubits
    if not targets or any(q not in measured for q in targets):
        raise ValueError("readout targets must be measured qubits")
    params = rule.params or {}
    tensor = probabilities.reshape((2,) * len(measured))
    if "matrix" in params:
        matrix = np.asarray(params["matrix"], dtype=float)
        size = 2 ** len(targets)
        if (matrix.shape != (size, size) or not np.isfinite(matrix).all()
                or np.any(matrix < 0) or np.any(matrix > 1)
                or not np.allclose(matrix.sum(axis=1), 1, rtol=0, atol=1e-12)):
            raise ValueError("readout matrix must be finite row-stochastic of dimension 2^targets")
        # rows = true, columns = observed; local target order is reversed here.
        axes = tuple(len(measured) - 1 - measured.index(q) for q in reversed(targets))
        return _apply_axes(tensor, matrix.T, axes).reshape(-1)
    p01, p10 = _prob(params.get("p01", rule.p)), _prob(params.get("p10", rule.p))
    matrix = np.array([[1 - p01, p10], [p01, 1 - p10]])
    for q in targets:
        tensor = _apply_axes(tensor, matrix, (len(measured) - 1 - measured.index(q),))
    return tensor.reshape(-1)


def simulate(spec: CircuitSpec, rules: tuple[NoiseRule, ...] = ()) -> SimulationResult:
    n = len(spec.qubits)
    if not 1 <= n <= MAX_QUBITS:
        raise ValueError("local qubit limit violated")
    if (len(set(spec.qubits)) != n or not spec.measured_qubits
            or len(set(spec.measured_qubits)) != len(spec.measured_qubits)
            or not set(spec.measured_qubits).issubset(spec.qubits)):
        raise ValueError("invalid circuit bit mapping")
    rules = tuple(rules)
    kraus = {}
    for i, rule in enumerate(rules):
        _prob(rule.p)
        if rule.qubits is not None and (not rule.qubits or len(set(rule.qubits)) != len(rule.qubits)
                                       or not set(rule.qubits).issubset(spec.qubits)):
            raise ValueError("invalid noise qubit selector")
        if rule.channel == "readout":
            if rule.position not in {"after_gate", "readout"} or rule.gates is not None:
                raise ValueError("readout rules do not select gates")
        else:
            if rule.position != "after_gate":
                raise ValueError("only after_gate insertion is supported")
            if rule.gates is not None and (not rule.gates or not set(rule.gates).issubset(set(FIXED) | set(TWO) | {"RX", "RY", "RZ"})):
                raise ValueError("unsupported gate selector")
            kraus[i] = _kraus(rule)
    rho = np.zeros((2 ** n, 2 ** n), dtype=complex)
    rho[0, 0] = 1
    for gate in spec.operations:
        positions = tuple(spec.qubits.index(q) for q in gate.qubits)
        rho = _conjugate(rho, _matrix(gate), positions, n)
        for i, rule in enumerate(rules):
            if rule.channel == "readout" or (rule.gates is not None and gate.name not in rule.gates):
                continue
            for q, position in zip(gate.qubits, positions):
                if rule.qubits is None or q in rule.qubits:
                    rho = sum((_conjugate(rho, k, (position,), n) for k in kraus[i]), start=np.zeros_like(rho))
    diagonal = rho.diagonal().real
    indexes = np.arange(2 ** n)
    measured_indexes = sum(((indexes >> spec.qubits.index(q)) & 1) << i
                           for i, q in enumerate(spec.measured_qubits))
    probabilities = np.bincount(measured_indexes, weights=diagonal, minlength=2 ** len(spec.measured_qubits))
    for rule in rules:
        if rule.channel == "readout":
            probabilities = _readout(probabilities, rule, spec.measured_qubits)
    if (not np.isfinite(probabilities).all() or probabilities.min() < -1e-10
            or abs(probabilities.sum() - 1) > 1e-9):
        raise ArithmeticError("probability conservation failed")
    probabilities = np.maximum(probabilities, 0)
    probabilities /= probabilities.sum()
    labels = tuple(format(i, f"0{len(spec.measured_qubits)}b") for i in range(len(probabilities)))
    return SimulationResult(rho, probabilities, labels)


def sample_counts(result: SimulationResult, shots: int, seed: int) -> dict[str, int]:
    if isinstance(shots, bool) or not isinstance(shots, int) or not 1 <= shots <= 5000:
        raise ValueError("shots must be an integer in [1, 5000], matching observed GUI range")
    counts = np.random.default_rng(seed).multinomial(shots, result.probabilities)
    return dict(zip(result.bit_labels, map(int, counts)))
