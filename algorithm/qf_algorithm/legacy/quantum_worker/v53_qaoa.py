"""Isolated, deterministic V5.3 generic QCIS compiler.

This module consumes only the public local-science JSON.  It does not import a
historical dispatcher, run, receipt, authorization, seal, provider, or hardware
adapter, and it performs no network access.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import sys
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

import numpy as np
from cqlib.circuits import Circuit

from qf_algorithm.legacy.quantum_worker.v53_jcs import V53JcsError, canonical_v53_json_bytes

PUBLIC_RESULT_SCHEMA = "qf.public-local-science-result.v1"
BUNDLE_SCHEMA = "qf.v53.generic-qcis-bundle.v1"
VARIANT_SCHEMA = "qf.v53.generic-qcis-variant.v1"
CANONICAL_IR_SCHEMA = "qf.v53.qaoa-canonical-ir.v1"
ERROR_SCHEMA = "qf.v53.qaoa-compile-error.v1"
VARIANTS = ("full", "rho_zero", "graph_free")
QUBITS = 6
DEPTH_P = 1
SHOTS = 4096
SEED = 20260820
FLOAT_TOLERANCE = 1e-10

# These paths are the irreducible public inputs needed to reconstruct gates and
# freeze the interpretation of their sampled output.  Redundant Ising and
# enumeration fields are checked when present, but are not needed to guess any
# circuit parameter.
MINIMUM_REQUIRED_FIELDS = (
    "schemaVersion",
    "dataset.selectedColumns",
    "reproducibility.providerCalls",
    "reproducibility.hardwareJobs",
    "reproducibility.networkCalls",
    "variants[].variant",
    "variants[].qubo.columnOrder",
    "variants[].qubo.quboPolynomial.offset",
    "variants[].qubo.quboPolynomial.linear",
    "variants[].qubo.quboPolynomial.quadraticUpperTriangular",
    "variants[].simulation.qubitCount",
    "variants[].simulation.p",
    "variants[].simulation.costScaling",
    "variants[].simulation.optimal.gamma",
    "variants[].simulation.optimal.beta",
    "variants[].simulation.sampling.seed",
    "variants[].simulation.sampling.shots",
    "variants[].simulation.bitOrder.primaryRepresentation",
    "variants[].simulation.bitOrder.mapping",
)


class V53QaoaCompileError(ValueError):
    """Stable fail-closed compiler error without external side effects."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def canonical_json_bytes(value: Any) -> bytes:
    """Return strict RFC 8785 UTF-8 bytes shared with the Node verifier."""

    try:
        return canonical_v53_json_bytes(value)
    except V53JcsError as error:
        raise V53QaoaCompileError(
            "V53_QAOA_JSON_INVALID",
            "the public result is not finite canonical JSON",
            details={"reason": type(error).__name__},
        ) from error


def _sha256(value: bytes | str) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def selection_vector_to_canonical_bitstring(vector: Sequence[int]) -> str:
    """Encode ``[x0,...,x5]`` as left-to-right ``x0...x5``."""

    values = tuple(vector)
    if len(values) != QUBITS or any(value not in (0, 1) for value in values):
        raise V53QaoaCompileError(
            "V53_QAOA_SELECTION_INVALID", "selection vector must contain exactly six bits"
        )
    return "".join(str(value) for value in values)


def canonical_bitstring_to_selection_vector(bitstring: str) -> list[int]:
    """Invert the canonical logical bitstring without an endian assumption."""

    if len(bitstring) != QUBITS or any(value not in "01" for value in bitstring):
        raise V53QaoaCompileError(
            "V53_QAOA_BITSTRING_INVALID", "canonical bitstring must contain exactly six bits"
        )
    return [int(value) for value in bitstring]


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise V53QaoaCompileError(
            "V53_QAOA_SOURCE_INVALID", f"{path} must be an object", details={"path": path}
        )
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise V53QaoaCompileError(
            "V53_QAOA_SOURCE_INVALID", f"{path} must be an array", details={"path": path}
        )
    return value


def _required(root: Mapping[str, Any], path: Sequence[str], label: str) -> Any:
    current: Any = root
    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            raise V53QaoaCompileError(
                "V53_QAOA_SOURCE_INCOMPLETE",
                "public local-science JSON lacks a required compiler field",
                details={
                    "missingFields": [label],
                    "minimumRequiredFields": list(MINIMUM_REQUIRED_FIELDS),
                },
            )
        current = current[segment]
    return current


def _finite_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise V53QaoaCompileError(
            "V53_QAOA_SOURCE_INVALID", f"{path} must be numeric", details={"path": path}
        )
    result = float(value)
    if not math.isfinite(result):
        raise V53QaoaCompileError(
            "V53_QAOA_SOURCE_INVALID", f"{path} must be finite", details={"path": path}
        )
    return result


def _fixed_columns(value: Any, path: str) -> tuple[str, ...]:
    columns = tuple(_sequence(value, path))
    if (
        len(columns) != QUBITS
        or len(set(columns)) != QUBITS
        or any(not isinstance(column, str) or not column for column in columns)
    ):
        raise V53QaoaCompileError(
            "V53_QAOA_COLUMN_ORDER_INVALID",
            f"{path} must freeze exactly six unique non-empty strings",
        )
    return columns


def _float_vector(value: Any, length: int, path: str) -> np.ndarray:
    rows = _sequence(value, path)
    if len(rows) != length:
        raise V53QaoaCompileError("V53_QAOA_SOURCE_INVALID", f"{path} must have length {length}")
    result = np.asarray(
        [_finite_float(item, f"{path}[{index}]") for index, item in enumerate(rows)]
    )
    return result.astype(np.float64)


def _float_matrix(value: Any, size: int, path: str) -> np.ndarray:
    rows = _sequence(value, path)
    if len(rows) != size:
        raise V53QaoaCompileError("V53_QAOA_SOURCE_INVALID", f"{path} must have {size} rows")
    result = np.stack(
        [_float_vector(row, size, f"{path}[{index}]") for index, row in enumerate(rows)]
    )
    return np.asarray(result, dtype=np.float64)


def _find_forbidden_query_id(value: Any, path: str = "$") -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            child_path = f"{path}.{key}"
            if normalized in {"queryid", "queryids"}:
                return child_path
            found = _find_forbidden_query_id(child, child_path)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            found = _find_forbidden_query_id(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _validate_no_external_evidence(public_result: Mapping[str, Any]) -> None:
    forbidden_path = _find_forbidden_query_id(public_result)
    if forbidden_path is not None:
        raise V53QaoaCompileError(
            "V53_QAOA_OLD_QUERY_FORBIDDEN",
            "public local-science input may not contain a Query ID",
            details={"path": forbidden_path},
        )
    reproducibility = _mapping(
        _required(public_result, ("reproducibility",), "reproducibility"),
        "reproducibility",
    )
    for field in ("providerCalls", "hardwareJobs", "networkCalls"):
        value = _required(reproducibility, (field,), f"reproducibility.{field}")
        if isinstance(value, bool) or value != 0:
            raise V53QaoaCompileError(
                "V53_QAOA_EXTERNAL_EVIDENCE_FORBIDDEN",
                f"reproducibility.{field} must equal zero",
            )


def _validate_bit_order(simulation: Mapping[str, Any], path: str) -> dict[str, Any]:
    bit_order = _mapping(
        _required(simulation, ("bitOrder",), f"{path}.bitOrder"),
        f"{path}.bitOrder",
    )
    representation = _required(
        bit_order,
        ("primaryRepresentation",),
        f"{path}.bitOrder.primaryRepresentation",
    )
    if representation != "selectionVector=[x0,x1,x2,x3,x4,x5]":
        raise V53QaoaCompileError(
            "V53_QAOA_BIT_ORDER_INVALID", "source selection-vector representation drifted"
        )
    mapping = _sequence(
        _required(bit_order, ("mapping",), f"{path}.bitOrder.mapping"),
        f"{path}.bitOrder.mapping",
    )
    expected = [{"qubit": index, "selectionIndex": index} for index in range(QUBITS)]
    if list(mapping) != expected:
        raise V53QaoaCompileError(
            "V53_QAOA_BIT_ORDER_INVALID", "source qubit-to-selection mapping is not identity"
        )
    return {
        "primaryRepresentation": representation,
        "qubitToSelectionIndex": expected,
        "selectionIndexToQubit": [
            {"selectionIndex": index, "qubit": index} for index in range(QUBITS)
        ],
        "qcisMeasurementOrder": list(range(QUBITS)),
        "canonicalBitstringOrder": "x0_x1_x2_x3_x4_x5_LEFT_TO_RIGHT",
        "inverseMapping": [
            {"canonicalBitPosition": index, "selectionIndex": index} for index in range(QUBITS)
        ],
        "inverseRule": "canonicalBitstring[i] -> selectionVector[i] -> logicalQubit[i]",
        "providerRawBitstringAssumption": "NONE_GENERIC_LOGICAL_CIRCUIT",
    }


def _validate_grid_angle(value: float, *, denominator: int, maximum_index: int, path: str) -> None:
    step = math.pi / denominator
    nearest = round(value / step)
    if nearest < 0 or nearest > maximum_index or abs(value - nearest * step) > 1e-12:
        raise V53QaoaCompileError(
            "V53_QAOA_GRID_PARAMETER_INVALID", f"{path} is not on the frozen inclusive grid"
        )


def _qubo_to_ising(
    offset: float, linear: np.ndarray, quadratic: np.ndarray
) -> tuple[float, np.ndarray, np.ndarray]:
    if np.any(np.tril(quadratic) != 0.0):
        raise V53QaoaCompileError(
            "V53_QAOA_QUBO_INVALID",
            "quadraticUpperTriangular must have exact zeros on and below its diagonal",
        )
    constant = float(offset + 0.5 * linear.sum())
    h = -0.5 * linear.copy()
    j = np.zeros_like(quadratic)
    for left in range(QUBITS):
        for right in range(left + 1, QUBITS):
            coefficient = float(quadratic[left, right])
            constant += coefficient / 4.0
            h[left] -= coefficient / 4.0
            h[right] -= coefficient / 4.0
            j[left, right] = coefficient / 4.0
    return constant, h, j


def _validate_redundant_ising(
    qubo: Mapping[str, Any], constant: float, h: np.ndarray, j: np.ndarray, path: str
) -> None:
    value = qubo.get("ising")
    if value is None:
        return
    ising = _mapping(value, f"{path}.ising")
    published_constant = _finite_float(ising.get("constant"), f"{path}.ising.constant")
    published_h = _float_vector(ising.get("h"), QUBITS, f"{path}.ising.h")
    published_j = _float_matrix(
        ising.get("jUpperTriangular"), QUBITS, f"{path}.ising.jUpperTriangular"
    )
    error = max(
        abs(published_constant - constant),
        float(np.max(np.abs(published_h - h))),
        float(np.max(np.abs(published_j - j))),
    )
    if error > FLOAT_TOLERANCE:
        raise V53QaoaCompileError(
            "V53_QAOA_ISING_MISMATCH",
            "published Ising coefficients disagree with the complete QUBO polynomial",
            details={"maximumAbsoluteDifference": error},
        )


def _rzz_parallel_depth(j: np.ndarray) -> int:
    layers: list[set[int]] = []
    for left in range(QUBITS):
        for right in range(left + 1, QUBITS):
            if j[left, right] == 0.0:
                continue
            for occupied in layers:
                if left not in occupied and right not in occupied:
                    occupied.update((left, right))
                    break
            else:
                layers.append({left, right})
    return len(layers)


def _compile_variant(
    variant_row: Mapping[str, Any],
    *,
    variant: str,
    columns: tuple[str, ...],
    source_sha256: str,
) -> dict[str, Any]:
    path = f"variants[{variant}]"
    qubo = _mapping(_required(variant_row, ("qubo",), f"{path}.qubo"), f"{path}.qubo")
    if qubo.get("variant") not in (None, variant):
        raise V53QaoaCompileError(
            "V53_QAOA_VARIANT_MISMATCH", f"{path}.qubo.variant does not match"
        )
    qubo_columns = _fixed_columns(
        _required(qubo, ("columnOrder",), f"{path}.qubo.columnOrder"),
        f"{path}.qubo.columnOrder",
    )
    if qubo_columns != columns:
        raise V53QaoaCompileError(
            "V53_QAOA_COLUMN_ORDER_INVALID", f"{path} column order drifted from the dataset"
        )
    polynomial = _mapping(
        _required(qubo, ("quboPolynomial",), f"{path}.qubo.quboPolynomial"),
        f"{path}.qubo.quboPolynomial",
    )
    offset = _finite_float(
        _required(polynomial, ("offset",), f"{path}.qubo.quboPolynomial.offset"),
        f"{path}.qubo.quboPolynomial.offset",
    )
    linear = _float_vector(
        _required(polynomial, ("linear",), f"{path}.qubo.quboPolynomial.linear"),
        QUBITS,
        f"{path}.qubo.quboPolynomial.linear",
    )
    quadratic = _float_matrix(
        _required(
            polynomial,
            ("quadraticUpperTriangular",),
            f"{path}.qubo.quboPolynomial.quadraticUpperTriangular",
        ),
        QUBITS,
        f"{path}.qubo.quboPolynomial.quadraticUpperTriangular",
    )
    constant, h, j = _qubo_to_ising(offset, linear, quadratic)
    _validate_redundant_ising(qubo, constant, h, j, f"{path}.qubo")

    simulation = _mapping(
        _required(variant_row, ("simulation",), f"{path}.simulation"),
        f"{path}.simulation",
    )
    if _required(simulation, ("qubitCount",), f"{path}.simulation.qubitCount") != QUBITS:
        raise V53QaoaCompileError("V53_QAOA_WIDTH_INVALID", "source qubit count must equal six")
    if _required(simulation, ("p",), f"{path}.simulation.p") != DEPTH_P:
        raise V53QaoaCompileError("V53_QAOA_DEPTH_INVALID", "source QAOA p must equal one")
    if _required(simulation, ("costScaling",), f"{path}.simulation.costScaling") != "NONE":
        raise V53QaoaCompileError(
            "V53_QAOA_COST_SCALING_INVALID", "source cost phase must use unscaled C(x)"
        )
    optimal = _mapping(
        _required(simulation, ("optimal",), f"{path}.simulation.optimal"),
        f"{path}.simulation.optimal",
    )
    gamma = _finite_float(
        _required(optimal, ("gamma",), f"{path}.simulation.optimal.gamma"),
        f"{path}.simulation.optimal.gamma",
    )
    beta = _finite_float(
        _required(optimal, ("beta",), f"{path}.simulation.optimal.beta"),
        f"{path}.simulation.optimal.beta",
    )
    _validate_grid_angle(
        gamma,
        denominator=24,
        maximum_index=24,
        path=f"{path}.simulation.optimal.gamma",
    )
    _validate_grid_angle(
        beta,
        denominator=32,
        maximum_index=16,
        path=f"{path}.simulation.optimal.beta",
    )
    sampling = _mapping(
        _required(simulation, ("sampling",), f"{path}.simulation.sampling"),
        f"{path}.simulation.sampling",
    )
    if _required(sampling, ("seed",), f"{path}.simulation.sampling.seed") != SEED:
        raise V53QaoaCompileError("V53_QAOA_SEED_INVALID", "source seed must equal 20260820")
    if _required(sampling, ("shots",), f"{path}.simulation.sampling.shots") != SHOTS:
        raise V53QaoaCompileError("V53_QAOA_SHOTS_INVALID", "source shots must equal 4096")
    bit_order = _validate_bit_order(simulation, f"{path}.simulation")

    circuit = Circuit(QUBITS)
    operations: list[dict[str, Any]] = []
    for qubit in range(QUBITS):
        circuit.h(qubit)
        operations.append({"op": "H", "qubits": [qubit]})
    for qubit, coefficient in enumerate(h):
        if coefficient == 0.0:
            continue
        angle = float(2.0 * gamma * coefficient)
        circuit.rz(qubit, angle)
        operations.append(
            {
                "op": "RZ",
                "qubits": [qubit],
                "angle": angle,
                "isingCoefficient": float(coefficient),
            }
        )
    rzz_count = 0
    for left in range(QUBITS):
        for right in range(left + 1, QUBITS):
            coefficient = float(j[left, right])
            if coefficient == 0.0:
                continue
            angle = float(2.0 * gamma * coefficient)
            circuit.cx(left, right)
            circuit.rz(right, angle)
            circuit.cx(left, right)
            operations.append(
                {
                    "op": "RZZ_DECOMPOSED_CX_RZ_CX",
                    "qubits": [left, right],
                    "angle": angle,
                    "isingCoefficient": coefficient,
                }
            )
            rzz_count += 1
    for qubit in range(QUBITS):
        angle = float(2.0 * beta)
        circuit.rx(qubit, angle)
        operations.append({"op": "RX", "qubits": [qubit], "angle": angle})
    circuit.measure_all()
    operations.append(
        {
            "op": "MEASURE_ALL",
            "qubits": list(range(QUBITS)),
            "canonicalBitPositions": list(range(QUBITS)),
        }
    )
    qcis = circuit.qcis
    if not isinstance(qcis, str) or not qcis.strip():
        raise V53QaoaCompileError("V53_QAOA_QCIS_EMPTY", "cqlib produced empty QCIS")
    canonical_ir = {
        "schemaVersion": CANONICAL_IR_SCHEMA,
        "sourcePublicResultSha256": source_sha256,
        "variant": variant,
        "width": QUBITS,
        "p": DEPTH_P,
        "selectExactly": 3,
        "columnOrder": list(columns),
        "bitOrder": bit_order,
        "parameters": {
            "gamma": gamma,
            "beta": beta,
            "seed": SEED,
            "shots": SHOTS,
            "costScaling": "NONE",
        },
        "ising": {
            "mapping": "x_i=(1-z_i)/2",
            "constantGlobalPhaseCoefficient": constant,
            "h": [float(value) for value in h],
            "jUpperTriangular": [[float(value) for value in row] for row in j],
        },
        "operations": operations,
        "externalExecution": "NONE_GENERIC_LOGICAL_COMPILE_ONLY",
    }
    canonical_ir_sha256 = _sha256(canonical_json_bytes(canonical_ir))
    logical_gate_count = QUBITS + int(np.count_nonzero(h)) + rzz_count + QUBITS
    decomposed_gate_count = QUBITS + int(np.count_nonzero(h)) + 3 * rzz_count + QUBITS
    qcis_instruction_count = len([line for line in qcis.splitlines() if line.strip()])
    variant_payload = {
        "schemaVersion": VARIANT_SCHEMA,
        "variant": variant,
        "genericLogicalCircuit": True,
        "targetBackend": None,
        "canonicalIr": canonical_ir,
        "canonicalIrSha256": canonical_ir_sha256,
        "qcis": qcis,
        "qcisSha256": _sha256(qcis.encode("utf-8")),
        "compile": {
            "compiler": "qf.v53.generic-qcis-compiler.v1",
            "cqlibVersion": importlib.metadata.version("cqlib"),
            "logicalGateCountExcludingMeasurements": logical_gate_count,
            "decomposedGateCountExcludingMeasurements": decomposed_gate_count,
            "measurementCount": QUBITS,
            "cqlibInstructionCountIncludingMeasurements": len(circuit.instruction_sequence),
            "qcisInstructionCountIncludingMeasurements": qcis_instruction_count,
            "cqlibDepthIncludingMeasurements": int(circuit.depth()),
            "logicalParallelDepthIncludingMeasurements": (
                1 + int(np.any(h != 0.0)) + _rzz_parallel_depth(j) + 1 + 1
            ),
            "logicalParallelDepthDefinition": (
                "parallel H, parallel RZ, greedy disjoint RZZ layers, parallel RX, parallel measure"
            ),
        },
        "externalActions": {
            "networkCalls": 0,
            "providerCalls": 0,
            "hardwareJobs": 0,
        },
    }
    variant_payload["canonicalSha256"] = _sha256(canonical_json_bytes(variant_payload))
    return variant_payload


def compile_public_result(public_result: Mapping[str, Any]) -> dict[str, Any]:
    """Compile all three frozen variants into deterministic generic QCIS."""

    if not isinstance(public_result, Mapping):
        raise V53QaoaCompileError(
            "V53_QAOA_SOURCE_INVALID", "public local-science result must be an object"
        )
    if public_result.get("schemaVersion") != PUBLIC_RESULT_SCHEMA:
        raise V53QaoaCompileError(
            "V53_QAOA_SCHEMA_INVALID", "unsupported public local-science schema"
        )
    _validate_no_external_evidence(public_result)
    dataset = _mapping(
        _required(public_result, ("dataset",), "dataset"),
        "dataset",
    )
    columns = _fixed_columns(
        _required(dataset, ("selectedColumns",), "dataset.selectedColumns"),
        "dataset.selectedColumns",
    )
    rows = _sequence(
        _required(public_result, ("variants",), "variants"),
        "variants",
    )
    indexed: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(rows):
        row = _mapping(value, f"variants[{index}]")
        variant = row.get("variant")
        if not isinstance(variant, str) or variant not in VARIANTS or variant in indexed:
            raise V53QaoaCompileError(
                "V53_QAOA_VARIANT_SET_INVALID",
                "variants must contain one full, rho_zero, and graph_free result",
            )
        indexed[variant] = row
    if set(indexed) != set(VARIANTS):
        raise V53QaoaCompileError(
            "V53_QAOA_VARIANT_SET_INVALID",
            "variants must contain one full, rho_zero, and graph_free result",
        )
    source_sha256 = _sha256(canonical_json_bytes(public_result))
    compiled = [
        _compile_variant(
            indexed[variant],
            variant=variant,
            columns=columns,
            source_sha256=source_sha256,
        )
        for variant in VARIANTS
    ]
    bit_order = compiled[0]["canonicalIr"]["bitOrder"]
    bundle = {
        "schemaVersion": BUNDLE_SCHEMA,
        "sourcePublicResultSha256": source_sha256,
        "variantOrder": list(VARIANTS),
        "variants": compiled,
        "bitOrder": bit_order,
        "frozenParameters": {
            "width": QUBITS,
            "p": DEPTH_P,
            "seed": SEED,
            "shots": SHOTS,
            "costScaling": "NONE",
        },
        "minimumRequiredSourceFields": list(MINIMUM_REQUIRED_FIELDS),
        "genericLogicalOnly": True,
        "backendMappingRequiredBeforeExternalValidation": True,
        "externalActions": {
            "networkCalls": 0,
            "providerCalls": 0,
            "hardwareJobs": 0,
        },
    }
    bundle["canonicalSha256"] = _sha256(canonical_json_bytes(bundle))
    return bundle


def _error_payload(error: V53QaoaCompileError) -> dict[str, Any]:
    return {
        "schemaVersion": ERROR_SCHEMA,
        "ok": False,
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        },
        "externalActions": {
            "networkCalls": 0,
            "providerCalls": 0,
            "hardwareJobs": 0,
        },
    }


def _write_stdout(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")
    sys.stdout.buffer.flush()


def _fail_cli(error: V53QaoaCompileError) -> NoReturn:
    print(f"[v53-qaoa] failed: {error.code}", file=sys.stderr, flush=True)
    _write_stdout(_error_payload(error))
    raise SystemExit(2)


def main() -> None:
    """Read one public result JSON from stdin and emit one compiled JSON object."""

    try:
        source = json.load(sys.stdin)
        result = compile_public_result(source)
        _write_stdout(result)
    except V53QaoaCompileError as error:
        _fail_cli(error)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        _fail_cli(
            V53QaoaCompileError(
                "V53_QAOA_INPUT_INVALID",
                "stdin must contain one valid public local-science JSON object",
                details={"reason": type(error).__name__},
            )
        )
    except Exception as error:
        _fail_cli(
            V53QaoaCompileError(
                "V53_QAOA_COMPILE_INTERNAL_ERROR",
                "generic QCIS compilation failed unexpectedly",
                details={"reason": type(error).__name__},
            )
        )


if __name__ == "__main__":
    main()
