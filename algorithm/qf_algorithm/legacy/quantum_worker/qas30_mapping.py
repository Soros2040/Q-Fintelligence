"""K0-bound mapping production for the QAS30 experiment.

This module is deliberately local-only.  It rebuilds the complete production
candidate bundle, compiles every candidate against one frozen K0 machine
configuration, selects a common physical measurement order for a five-circuit
batch, and consumes separately persisted one-request regularity observations.
It does not authenticate, contact Tianyan, or submit a circuit.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from itertools import product
from typing import Any

from qf_algorithm.legacy.quantum_worker import qas30_protocol, qas30_tianyan
from qf_algorithm.legacy.quantum_worker.v53_tianyan import (
    _deterministic_transpile,
    _FrozenMappingPlatform,
)

MAPPING_POOL_SCHEMA_VERSION = "qf.qas30.k0-mapping-pool.v1"
MAPPING_CANDIDATE_SCHEMA_VERSION = "qf.qas30.mapping-candidate.v1"
MAPPING_SELECTION_SCHEMA_VERSION = "qf.qas30.batch-mapping-selection.v1"
REGULARITY_OBSERVATION_SCHEMA_VERSION = "qf.qas30.qcis-regularity-observation.v1"
REGULARITY_RECEIPT_SCHEMA_VERSION = "qf.qas30.qcis-regularity-receipt.v1"
MAPPING_FEATURE_ROW_SCHEMA_VERSION = "qf.qas30.mapping-feature-row.v1"
MAPPING_VERIFICATION_SCHEMA_VERSION = "qf.qas30.batch-mapping-verification.v1"
NORMALIZED_TOPOLOGY_SCHEMA_VERSION = "qf.qas30.normalized-topology.v1"
CALIBRATION_NOISE_DEFINITION_ID = "MAPPED_QUBIT_AND_ACTUAL_TWO_QUBIT_EDGE_EQUAL_MEANS_V1"
MAPPING_STRATEGY = "BATCH_COMMON_MEASUREMENT_ORDER_DEPTH_V1"
REGULARITY_METHOD = "TIANYAN_QCIS_CHECK_REGULAR_SINGLE_REQUEST"
MAPPING_SEEDS = tuple(range(10))
LOGICAL_QUBITS = tuple(f"Q{index}" for index in range(6))
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
COMMIT_PATTERN = re.compile(r"[a-f0-9]{40}")
PHYSICAL_QUBIT_PATTERN = re.compile(r"Q\d{1,4}")
MEASUREMENT_PATTERN = re.compile(r"(?mi)^\s*M\s+(Q\d+)\s*$")
SCIENTIFIC_STATUSES = {
    qas30_tianyan.OBSERVED_SCIENTIFIC_STATUS,
    qas30_tianyan.FIXTURE_SCIENTIFIC_STATUS,
}

# ``mapping_circuit_rows`` is intentionally an evidence-rich intermediate
# representation.  It must never be passed to Tianyan directly: the latter
# accepts only the ten fields in ``qas30_tianyan.CIRCUIT_FIELDS``.  Keep this
# contract local to the projector so accidental envelope leakage is rejected
# before batch assembly.
MAPPING_CIRCUIT_ROW_FIELDS = {
    "schemaVersion",
    "candidateId",
    "qcis",
    "qcisSha256",
    "measurementPhysicalOrder",
    "regularityReceipt",
    "regularityReceiptSha256",
    "mappingFeatureRow",
    "mappingFeatureRowSha256",
    "executionMappingFeatures",
    "executionMappingFeatureSha256",
    "mappingCircuitRowSha256",
}
MAPPING_FEATURE_ROW_FIELDS = {
    "schemaVersion",
    "state",
    "candidateId",
    "sourceCommitSha",
    "dataEpoch",
    "freezeManifestSha256",
    "mappedQcisSha256",
    "mappingCandidateSha256",
    "mappingSnapshotSha256",
    "k0ConfigSha256",
    "activeCalibrationSha256",
    "normalizedTopologySha256",
    "regularityReceiptSha256",
    "mappingDerivedFeatures",
    "calibrationNoiseComponents",
    "scientificStatus",
    "mappingFeatureRowSha256",
}
REGULARITY_RECEIPT_FIELDS = {
    "schemaVersion",
    "state",
    "candidateId",
    "target",
    "sourceCommitSha",
    "dataEpoch",
    "freezeManifestSha256",
    "qcisSha256",
    "k0ConfigSha256",
    "activeCalibrationSha256",
    "mappingSnapshotSha256",
    "measurementPhysicalOrder",
    "logicalToPhysical",
    "mappingDerivedFeatures",
    "regular",
    "scientificStatus",
    "regularityObservationSha256",
    "transportRequestCount",
}


class Qas30MappingError(ValueError):
    """One K0 mapping, common-order, or regularity invariant failed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _entity_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _require_sha256(value: Any, name: str) -> str:
    if not _valid_sha256(value):
        raise Qas30MappingError("QAS30_MAPPING_BINDING_INVALID", f"{name} is invalid")
    return str(value)


def _require_identity(value: Any, name: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value) is None
    ):
        raise Qas30MappingError("QAS30_MAPPING_IDENTITY_INVALID", f"{name} is invalid")
    return value


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise Qas30MappingError("QAS30_CALIBRATION_FIELD_INVALID", f"{name} is invalid")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise Qas30MappingError("QAS30_CALIBRATION_FIELD_INVALID", f"{name} is invalid") from error
    if not math.isfinite(numeric) or numeric < 0.0:
        raise Qas30MappingError("QAS30_CALIBRATION_FIELD_INVALID", f"{name} is invalid")
    return numeric


def _csv_identities(value: Any, name: str) -> set[str]:
    if not isinstance(value, str):
        raise Qas30MappingError("QAS30_K0_CONFIG_INVALID", f"{name} is invalid")
    identities = {item.strip().upper() for item in value.split(",") if item.strip()}
    if any(re.fullmatch(r"[QG]\d{1,4}", item) is None for item in identities):
        raise Qas30MappingError("QAS30_K0_CONFIG_INVALID", f"{name} is invalid")
    return identities


def _topology_parts(
    machine_config: Mapping[str, Any], normalized_topology: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Validate normalized topology identity against the frozen machine config."""

    if machine_config.get("computerId") != qas30_protocol.TARGET:
        raise Qas30MappingError("QAS30_K0_TARGET_INVALID", "K0 machine config target changed")
    overview = machine_config.get("overview")
    config_couplers = overview.get("coupler_map") if isinstance(overview, Mapping) else None
    if not isinstance(config_couplers, Mapping) or not config_couplers:
        raise Qas30MappingError("QAS30_K0_CONFIG_INVALID", "K0 machine config has no coupler map")
    disabled_qubits = _csv_identities(machine_config.get("disabledQubits", ""), "disabledQubits")
    disabled_couplers = _csv_identities(
        machine_config.get("disabledCouplers", ""), "disabledCouplers"
    )
    expected_pairs: dict[str, tuple[str, str]] = {}
    config_qubits: set[str] = set()
    for raw_coupler, raw_pair in config_couplers.items():
        coupler = str(raw_coupler).upper()
        if re.fullmatch(r"G\d{1,4}", coupler) is None:
            raise Qas30MappingError("QAS30_K0_CONFIG_INVALID", "K0 coupler identity is invalid")
        if (
            not isinstance(raw_pair, Sequence)
            or isinstance(raw_pair, str | bytes)
            or len(raw_pair) != 2
        ):
            raise Qas30MappingError("QAS30_K0_CONFIG_INVALID", "K0 coupler endpoints are invalid")
        pair = tuple(str(item).upper() for item in raw_pair)
        if pair[0] == pair[1] or any(
            PHYSICAL_QUBIT_PATTERN.fullmatch(item) is None for item in pair
        ):
            raise Qas30MappingError("QAS30_K0_CONFIG_INVALID", "K0 coupler endpoints are invalid")
        expected_pairs[coupler] = pair
        config_qubits.update(pair)

    topology_required = {
        "schemaVersion",
        "target",
        "sourceConfigSha256",
        "qubits",
        "couplers",
        "normalizedTopologySha256",
    }
    if set(normalized_topology) != topology_required:
        raise Qas30MappingError(
            "QAS30_NORMALIZED_TOPOLOGY_INVALID",
            "normalized topology fields changed",
        )
    stored_topology_sha = normalized_topology.get("normalizedTopologySha256")
    topology_unhashed = {
        key: value
        for key, value in normalized_topology.items()
        if key != "normalizedTopologySha256"
    }
    if (
        normalized_topology.get("schemaVersion") != NORMALIZED_TOPOLOGY_SCHEMA_VERSION
        or normalized_topology.get("target") != qas30_protocol.TARGET
        or stored_topology_sha != _entity_sha256(topology_unhashed)
    ):
        raise Qas30MappingError(
            "QAS30_NORMALIZED_TOPOLOGY_INVALID",
            "normalized topology hash or identity changed",
        )
    qubits = normalized_topology.get("qubits")
    couplers = normalized_topology.get("couplers")
    if (
        not isinstance(qubits, Mapping)
        or set(qubits) != config_qubits
        or not isinstance(couplers, Mapping)
        or {str(key).upper() for key in couplers} != set(expected_pairs)
    ):
        raise Qas30MappingError(
            "QAS30_NORMALIZED_TOPOLOGY_INVALID",
            "normalized topology does not cover the frozen config",
        )
    validated_qubits: dict[str, Any] = {}
    for qubit in sorted(config_qubits, key=lambda name: int(name[1:])):
        row = qubits.get(qubit)
        expected_active = qubit not in disabled_qubits
        if (
            not isinstance(row, Mapping)
            or set(row) != {"active", "readoutError", "singleGateError"}
            or row.get("active") is not expected_active
        ):
            raise Qas30MappingError(
                "QAS30_NORMALIZED_TOPOLOGY_INVALID",
                f"normalized topology qubit {qubit} changed",
            )
        validated = dict(row)
        if expected_active:
            validated["readoutError"] = _finite_nonnegative(
                row.get("readoutError"), f"{qubit}.readoutError"
            )
            validated["singleGateError"] = _finite_nonnegative(
                row.get("singleGateError"), f"{qubit}.singleGateError"
            )
        validated_qubits[qubit] = validated

    validated_couplers: dict[str, Any] = {}
    for coupler in sorted(expected_pairs, key=lambda name: int(name[1:])):
        row = couplers.get(coupler)
        source, target = expected_pairs[coupler]
        expected_active = (
            coupler not in disabled_couplers
            and source not in disabled_qubits
            and target not in disabled_qubits
        )
        if (
            not isinstance(row, Mapping)
            or set(row) != {"source", "target", "active", "twoQubitError"}
            or (str(row.get("source")).upper(), str(row.get("target")).upper())
            not in {(source, target), (target, source)}
            or row.get("active") is not expected_active
        ):
            raise Qas30MappingError(
                "QAS30_NORMALIZED_TOPOLOGY_INVALID",
                f"normalized topology coupler {coupler} changed",
            )
        validated = dict(row)
        validated["source"] = str(row["source"]).upper()
        validated["target"] = str(row["target"]).upper()
        if expected_active:
            validated["twoQubitError"] = _finite_nonnegative(
                row.get("twoQubitError"), f"{coupler}.twoQubitError"
            )
        validated_couplers[coupler] = validated
    active_qubits = [name for name, row in validated_qubits.items() if row["active"]]
    if len(active_qubits) < 6:
        raise Qas30MappingError(
            "QAS30_NORMALIZED_TOPOLOGY_INVALID",
            "normalized topology has fewer than six active qubits",
        )
    indices = [int(name[1:]) for name in config_qubits]
    shift_from_one_based = 0 not in indices and min(indices) >= 1
    return validated_qubits, validated_couplers, shift_from_one_based


def validate_k0_calibration_receipt(
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one K0 receipt including raw config and normalized topology."""

    if not isinstance(receipt, Mapping):
        raise Qas30MappingError("QAS30_K0_RECEIPT_INVALID", "K0 receipt is not an object")
    required = {
        "schemaVersion",
        "runId",
        "dataEpoch",
        "calibrationLabel",
        "target",
        "sourceCommitSha",
        "freezeManifestSha256",
        "authorizationEnvelopeSha256",
        "resourceSampleSha256",
        "snapshot",
        "activeCalibrationSha256",
        "transportRequestCount",
        "capturedAt",
        "scientificStatus",
    }
    if set(receipt) != required:
        raise Qas30MappingError("QAS30_K0_RECEIPT_INVALID", "K0 receipt fields changed")
    snapshot = receipt.get("snapshot")
    status = receipt.get("scientificStatus")
    if (
        receipt.get("schemaVersion") != "qf.qas30.calibration-receipt.v1"
        or receipt.get("calibrationLabel") != "K0"
        or receipt.get("target") != qas30_protocol.TARGET
        or status not in SCIENTIFIC_STATUSES
        or receipt.get("transportRequestCount") != 1
        or not isinstance(snapshot, Mapping)
    ):
        raise Qas30MappingError("QAS30_K0_RECEIPT_INVALID", "K0 receipt identity changed")
    _require_identity(receipt.get("runId"), "runId")
    _require_identity(receipt.get("dataEpoch"), "dataEpoch")
    source_commit = receipt.get("sourceCommitSha")
    if not isinstance(source_commit, str) or COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise Qas30MappingError("QAS30_K0_RECEIPT_INVALID", "K0 source commit is invalid")
    for name in (
        "freezeManifestSha256",
        "authorizationEnvelopeSha256",
        "resourceSampleSha256",
    ):
        _require_sha256(receipt.get(name), name)
    snapshot_required = {
        "schemaVersion",
        "calibrationLabel",
        "target",
        "providerMachineId",
        "capturedAt",
        "capabilities",
        "topology",
        "machineConfig",
        "machineConfigSha256",
        "scientificStatus",
    }
    if set(snapshot) != snapshot_required:
        raise Qas30MappingError("QAS30_K0_RECEIPT_INVALID", "K0 snapshot fields changed")
    machine_config = snapshot.get("machineConfig")
    normalized_topology = snapshot.get("topology")
    if (
        snapshot.get("schemaVersion") != "qf.qas30.calibration-snapshot.v1"
        or snapshot.get("calibrationLabel") != "K0"
        or snapshot.get("target") != qas30_protocol.TARGET
        or snapshot.get("providerMachineId") != qas30_protocol.TARGET
        or snapshot.get("scientificStatus") != status
        or not isinstance(snapshot.get("capabilities"), Mapping)
        or not isinstance(machine_config, Mapping)
        or not isinstance(normalized_topology, Mapping)
    ):
        raise Qas30MappingError("QAS30_K0_RECEIPT_INVALID", "K0 snapshot identity changed")
    if not isinstance(receipt.get("capturedAt"), str) or receipt.get("capturedAt") != snapshot.get(
        "capturedAt"
    ):
        raise Qas30MappingError("QAS30_K0_RECEIPT_INVALID", "K0 capture timestamp binding changed")
    machine_config_sha = _entity_sha256(machine_config)
    if (
        snapshot.get("machineConfigSha256") != machine_config_sha
        or normalized_topology.get("sourceConfigSha256") != machine_config_sha
        or snapshot.get("capabilities", {}).get("machineConfigSha256") != machine_config_sha
    ):
        raise Qas30MappingError(
            "QAS30_K0_CONFIG_HASH_MISMATCH",
            "K0 machine config hash binding changed",
        )
    _topology_parts(machine_config, normalized_topology)
    active_calibration_sha = _entity_sha256(snapshot)
    if receipt.get("activeCalibrationSha256") != active_calibration_sha:
        raise Qas30MappingError(
            "QAS30_K0_CALIBRATION_HASH_MISMATCH",
            "K0 active calibration hash changed",
        )
    return dict(receipt)


def _provider_qcis(qcis: str, *, shift_from_one_based: bool) -> str:
    normalized = qcis.upper()
    if not shift_from_one_based:
        return normalized
    return re.sub(
        r"\bQ(\d+)\b",
        lambda match: f"Q{int(match.group(1)) + 1}",
        normalized,
    )


def _integer_mapping(value: Any, name: str) -> dict[int, int]:
    if not isinstance(value, Mapping):
        raise Qas30MappingError("QAS30_MAPPED_LAYOUT_INVALID", f"{name} is invalid")
    result: dict[int, int] = {}
    for raw_key, raw_value in value.items():
        try:
            key = int(str(raw_key).upper().removeprefix("Q"))
            item = int(str(raw_value).upper().removeprefix("Q"))
        except ValueError as error:
            raise Qas30MappingError("QAS30_MAPPED_LAYOUT_INVALID", f"{name} is invalid") from error
        if key in result or key < 0 or item < 0:
            raise Qas30MappingError("QAS30_MAPPED_LAYOUT_INVALID", f"{name} is invalid")
        result[key] = item
    return result


def _qcis_tokens(qcis: str) -> list[list[str]]:
    return [line.strip().upper().split() for line in qcis.splitlines() if line.strip()]


def _two_qubit_edges(qcis: str) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for tokens in _qcis_tokens(qcis):
        qubits = [token for token in tokens[1:] if PHYSICAL_QUBIT_PATTERN.fullmatch(token)]
        if len(qubits) == 2 and qubits[0] != qubits[1]:
            edges.append(tuple(sorted((qubits[0], qubits[1]), key=lambda item: int(item[1:]))))
    return edges


def _swap_count(qcis: str) -> int:
    """Count explicit SWAPs and cqlib's frozen nine-instruction decomposition."""

    rows = _qcis_tokens(qcis)
    count = sum(1 for row in rows if row and row[0] == "SWAP")
    cursor = 0
    while cursor + 8 < len(rows):
        window = rows[cursor : cursor + 9]
        if (
            len(window[0]) == 2
            and len(window[1]) == 3
            and window[0][0] == "Y2M"
            and window[1][0] == "CZ"
        ):
            target = window[0][1]
            source = window[1][1]
            expected = [
                ["Y2M", target],
                ["CZ", source, target],
                ["Y2P", target],
                ["Y2M", source],
                ["CZ", target, source],
                ["Y2P", source],
                ["Y2M", target],
                ["CZ", source, target],
                ["Y2P", target],
            ]
            if window == expected:
                count += 1
                cursor += 9
                continue
        cursor += 1
    return count


def _native_swap_rows(source: str, target: str) -> list[str]:
    """Return cqlib's native nine-instruction SWAP decomposition."""

    return [
        f"Y2M {target}",
        f"CZ {source} {target}",
        f"Y2P {target}",
        f"Y2M {source}",
        f"CZ {target} {source}",
        f"Y2P {source}",
        f"Y2M {target}",
        f"CZ {source} {target}",
        f"Y2P {target}",
    ]


def _restoration_swaps(
    logical_to_physical: Mapping[str, str],
    active_couplers: Mapping[str, Any],
) -> tuple[list[str], list[tuple[str, str]]] | None:
    """Find a shortest deterministic in-set SWAP path to numeric physical order."""

    current = tuple(logical_to_physical[item] for item in LOGICAL_QUBITS)
    target = tuple(sorted(current, key=lambda item: int(item[1:])))
    if current == target:
        return list(target), []
    physical_set = set(current)
    edges = sorted(
        {
            tuple(
                sorted(
                    (str(row["source"]), str(row["target"])),
                    key=lambda item: int(item[1:]),
                )
            )
            for row in active_couplers.values()
            if isinstance(row, Mapping)
            and row.get("active") is True
            and {str(row.get("source")), str(row.get("target"))} <= physical_set
        },
        key=lambda edge: (int(edge[0][1:]), int(edge[1][1:])),
    )
    queue: deque[tuple[str, ...]] = deque([current])
    predecessor: dict[
        tuple[str, ...], tuple[tuple[str, ...], tuple[str, str]] | None
    ] = {current: None}
    while queue:
        state = queue.popleft()
        if state == target:
            swaps: list[tuple[str, str]] = []
            cursor = state
            while predecessor[cursor] is not None:
                previous, edge = predecessor[cursor]
                swaps.append(edge)
                cursor = previous
            swaps.reverse()
            return list(target), swaps
        positions = {physical: index for index, physical in enumerate(state)}
        for edge in edges:
            left = positions[edge[0]]
            right = positions[edge[1]]
            next_state = list(state)
            next_state[left], next_state[right] = next_state[right], next_state[left]
            normalized = tuple(next_state)
            if normalized not in predecessor:
                predecessor[normalized] = (state, edge)
                queue.append(normalized)
    return None


def _restore_numeric_measurement_order(
    mapped_qcis: str,
    logical_to_physical: Mapping[str, str],
    active_couplers: Mapping[str, Any],
) -> tuple[str, dict[str, str], list[str]]:
    """Restore logical states to one physical order before terminal measurements.

    Reordering measurement statements alone would change the interpretation of
    provider bit positions.  The native SWAP network preserves the logical
    readout contract while making the physical order common and charges every
    added gate to the execution mapping metrics.
    """

    rows = [line.strip().upper() for line in mapped_qcis.splitlines() if line.strip()]
    measurement_rows = [
        index
        for index, tokens in enumerate(_qcis_tokens(mapped_qcis))
        if tokens and tokens[0] == "M"
    ]
    current_order = [logical_to_physical[item] for item in LOGICAL_QUBITS]
    if (
        len(rows) < 6
        or measurement_rows != list(range(len(rows) - 6, len(rows)))
        or [row.split()[1] for row in rows[-6:]] != current_order
    ):
        raise Qas30MappingError(
            "QAS30_MAPPED_MEASUREMENT_ORDER_INVALID",
            "mapped measurements are not six terminal logical-order rows",
        )
    restoration = _restoration_swaps(logical_to_physical, active_couplers)
    if restoration is None:
        return mapped_qcis, dict(logical_to_physical), current_order
    common_order, swaps = restoration
    if not swaps:
        return mapped_qcis, dict(logical_to_physical), common_order
    restored_rows = rows[:-6]
    for source, target in swaps:
        restored_rows.extend(_native_swap_rows(source, target))
    restored_rows.extend(f"M {physical}" for physical in common_order)
    restored_mapping = {
        logical: common_order[index] for index, logical in enumerate(LOGICAL_QUBITS)
    }
    return "\n".join(restored_rows), restored_mapping, common_order


def _mapping_status(transpiler: Callable[..., Any] | None, k0_status: str) -> str:
    if transpiler is None:
        return k0_status
    injected_status = getattr(transpiler, "scientific_status", None)
    if (
        k0_status != qas30_tianyan.FIXTURE_SCIENTIFIC_STATUS
        or injected_status != qas30_tianyan.FIXTURE_SCIENTIFIC_STATUS
    ):
        raise Qas30MappingError(
            "QAS30_PRODUCTION_TRANSPILER_INJECTION_FORBIDDEN",
            "production mapping uses the frozen cqlib transpiler",
        )
    return qas30_tianyan.FIXTURE_SCIENTIFIC_STATUS


def build_k0_mapping_pool(
    *,
    production_bundle: Mapping[str, Any],
    parent_manifest_bytes: bytes,
    parent_canonical_ir_bytes: bytes,
    market_data_sha256: str,
    k0_calibration_receipt: Mapping[str, Any],
    transpiler: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Compile all 30 candidates for seeds 0..9 against one frozen K0 config."""

    try:
        bundle = qas30_protocol.validate_production_candidate_bundle(
            production_bundle,
            parent_manifest_bytes=parent_manifest_bytes,
            parent_canonical_ir_bytes=parent_canonical_ir_bytes,
            market_data_sha256=market_data_sha256,
        )
    except qas30_protocol.Qas30ProtocolError as error:
        raise Qas30MappingError(
            "QAS30_PRODUCTION_BUNDLE_INVALID",
            "production candidate bundle did not rebuild from its parent artifacts",
        ) from error
    k0 = validate_k0_calibration_receipt(k0_calibration_receipt)
    snapshot = k0["snapshot"]
    machine_config = snapshot["machineConfig"]
    normalized_topology = snapshot["topology"]
    _, calibrated_couplers, shift_from_one_based = _topology_parts(
        machine_config, normalized_topology
    )
    scientific_status = _mapping_status(transpiler, str(k0["scientificStatus"]))
    compile_once = transpiler or _deterministic_transpile
    platform = _FrozenMappingPlatform(
        dict(machine_config), shift_from_one_based=shift_from_one_based
    )
    config_sha = snapshot["machineConfigSha256"]
    topology_sha = normalized_topology["normalizedTopologySha256"]
    active_calibration_sha = k0["activeCalibrationSha256"]
    source_commit = k0["sourceCommitSha"]
    freeze_sha = k0["freezeManifestSha256"]
    data_epoch = k0["dataEpoch"]
    bundle_sha = bundle["bundleSha256"]
    rows: list[dict[str, Any]] = []
    for circuit in bundle["logicalCircuits"]:
        candidate_id = circuit["candidateId"]
        source_qcis = circuit["qcis"]
        for seed in MAPPING_SEEDS:
            mapped_circuit, initial_raw, swap_raw, final_raw = compile_once(
                source_qcis, platform, seed=seed
            )
            mapped_qcis = _provider_qcis(
                str(mapped_circuit.qcis), shift_from_one_based=shift_from_one_based
            )
            if not mapped_qcis.strip():
                raise Qas30MappingError("QAS30_MAPPED_QCIS_INVALID", "mapped QCIS is empty")
            initial = _integer_mapping(initial_raw, "initialLayout")
            _integer_mapping(swap_raw, "swapMapping")
            final = _integer_mapping(final_raw, "virtualToFinal")
            if set(initial) != set(range(6)) or set(final) != set(range(6)):
                raise Qas30MappingError(
                    "QAS30_MAPPED_LAYOUT_INVALID",
                    "mapped layout does not cover Q0-Q5",
                )
            offset = 1 if shift_from_one_based else 0
            logical_to_physical = {
                f"Q{logical}": f"Q{final[logical] + offset}" for logical in range(6)
            }
            measurement_order = [item.upper() for item in MEASUREMENT_PATTERN.findall(mapped_qcis)]
            if (
                len(measurement_order) != 6
                or len(set(measurement_order)) != 6
                or measurement_order != [logical_to_physical[item] for item in LOGICAL_QUBITS]
            ):
                raise Qas30MappingError(
                    "QAS30_MAPPED_MEASUREMENT_ORDER_INVALID",
                    "mapped measurement order is not the Q0-Q5 final layout",
                )
            mapped_qcis, logical_to_physical, measurement_order = (
                _restore_numeric_measurement_order(
                    mapped_qcis,
                    logical_to_physical,
                    calibrated_couplers,
                )
            )
            try:
                compiled_depth = int(type(mapped_circuit).load(mapped_qcis).depth())
            except (AttributeError, TypeError, ValueError) as error:
                raise Qas30MappingError(
                    "QAS30_MAPPED_METRIC_INVALID", "mapped depth is invalid"
                ) from error
            two_qubit_edges = _two_qubit_edges(mapped_qcis)
            if compiled_depth < 0 or not two_qubit_edges:
                raise Qas30MappingError(
                    "QAS30_MAPPED_METRIC_INVALID",
                    "mapped circuit metrics are incomplete",
                )
            row: dict[str, Any] = {
                "schemaVersion": MAPPING_CANDIDATE_SCHEMA_VERSION,
                "candidateId": candidate_id,
                "seed": seed,
                "target": qas30_protocol.TARGET,
                "sourceCommitSha": source_commit,
                "dataEpoch": data_epoch,
                "freezeManifestSha256": freeze_sha,
                "candidateBundleSha256": bundle_sha,
                "sourceQcisSha256": circuit["qcisSha256"],
                "mappedQcis": mapped_qcis,
                "mappedQcisSha256": _text_sha256(mapped_qcis),
                "logicalToPhysical": logical_to_physical,
                "measurementPhysicalOrder": measurement_order,
                "compiledDepth": compiled_depth,
                "twoQubitGates": len(two_qubit_edges),
                "swapCount": _swap_count(mapped_qcis),
                "k0ConfigSha256": config_sha,
                "activeCalibrationSha256": active_calibration_sha,
                "normalizedTopologySha256": topology_sha,
                "scientificStatus": scientific_status,
            }
            row["mappingCandidateSha256"] = _entity_sha256(row)
            rows.append(row)
    if len(rows) != len(qas30_protocol.CANDIDATE_IDS) * len(MAPPING_SEEDS):
        raise Qas30MappingError("QAS30_MAPPING_POOL_INCOMPLETE", "mapping pool is incomplete")
    pool: dict[str, Any] = {
        "schemaVersion": MAPPING_POOL_SCHEMA_VERSION,
        "state": "K0_MAPPING_POOL_FROZEN",
        "target": qas30_protocol.TARGET,
        "runId": k0["runId"],
        "dataEpoch": data_epoch,
        "sourceCommitSha": source_commit,
        "freezeManifestSha256": freeze_sha,
        "candidateBundleSha256": bundle_sha,
        "activeCalibrationSha256": active_calibration_sha,
        "k0ConfigSha256": config_sha,
        "normalizedTopologySha256": topology_sha,
        "strategy": MAPPING_STRATEGY,
        "mappingSeeds": list(MAPPING_SEEDS),
        "compiler": {
            "algorithm": "cqlib.mapping.transpile_qcis",
            "cqlibVersion": importlib.metadata.version("cqlib"),
            "deterministicWrapper": "qf.v53_tianyan._deterministic_transpile",
        },
        "scientificStatus": scientific_status,
        "mappingCandidates": rows,
        "externalRequestsIssued": 0,
    }
    pool["mappingPoolSha256"] = _entity_sha256(pool)
    return pool


def _validate_mapping_pool(pool: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schemaVersion",
        "state",
        "target",
        "runId",
        "dataEpoch",
        "sourceCommitSha",
        "freezeManifestSha256",
        "candidateBundleSha256",
        "activeCalibrationSha256",
        "k0ConfigSha256",
        "normalizedTopologySha256",
        "strategy",
        "mappingSeeds",
        "compiler",
        "scientificStatus",
        "mappingCandidates",
        "externalRequestsIssued",
        "mappingPoolSha256",
    }
    if not isinstance(pool, Mapping) or set(pool) != required:
        raise Qas30MappingError("QAS30_MAPPING_POOL_INVALID", "mapping pool fields changed")
    stored_sha = pool.get("mappingPoolSha256")
    unhashed = {key: value for key, value in pool.items() if key != "mappingPoolSha256"}
    if (
        pool.get("schemaVersion") != MAPPING_POOL_SCHEMA_VERSION
        or pool.get("state") != "K0_MAPPING_POOL_FROZEN"
        or pool.get("target") != qas30_protocol.TARGET
        or pool.get("strategy") != MAPPING_STRATEGY
        or pool.get("mappingSeeds") != list(MAPPING_SEEDS)
        or pool.get("scientificStatus") not in SCIENTIFIC_STATUSES
        or pool.get("externalRequestsIssued") != 0
        or stored_sha != _entity_sha256(unhashed)
    ):
        raise Qas30MappingError("QAS30_MAPPING_POOL_INVALID", "mapping pool identity changed")
    _require_identity(pool.get("runId"), "runId")
    _require_identity(pool.get("dataEpoch"), "dataEpoch")
    if (
        not isinstance(pool.get("sourceCommitSha"), str)
        or COMMIT_PATTERN.fullmatch(pool["sourceCommitSha"]) is None
    ):
        raise Qas30MappingError(
            "QAS30_MAPPING_POOL_INVALID", "mapping pool source commit is invalid"
        )
    for name in (
        "freezeManifestSha256",
        "candidateBundleSha256",
        "activeCalibrationSha256",
        "k0ConfigSha256",
        "normalizedTopologySha256",
    ):
        _require_sha256(pool.get(name), name)
    compiler = pool.get("compiler")
    if (
        not isinstance(compiler, Mapping)
        or set(compiler) != {"algorithm", "cqlibVersion", "deterministicWrapper"}
        or compiler.get("algorithm") != "cqlib.mapping.transpile_qcis"
        or not isinstance(compiler.get("cqlibVersion"), str)
        or not compiler.get("cqlibVersion")
        or compiler.get("deterministicWrapper") != "qf.v53_tianyan._deterministic_transpile"
    ):
        raise Qas30MappingError("QAS30_MAPPING_POOL_INVALID", "mapping compiler identity changed")
    rows = pool.get("mappingCandidates")
    if not isinstance(rows, list) or len(rows) != 300:
        raise Qas30MappingError("QAS30_MAPPING_POOL_INCOMPLETE", "mapping pool is incomplete")
    expected_pairs = {
        (candidate_id, seed)
        for candidate_id in qas30_protocol.CANDIDATE_IDS
        for seed in MAPPING_SEEDS
    }
    observed_pairs: set[tuple[str, int]] = set()
    candidate_required = {
        "schemaVersion",
        "candidateId",
        "seed",
        "target",
        "sourceCommitSha",
        "dataEpoch",
        "freezeManifestSha256",
        "candidateBundleSha256",
        "sourceQcisSha256",
        "mappedQcis",
        "mappedQcisSha256",
        "logicalToPhysical",
        "measurementPhysicalOrder",
        "compiledDepth",
        "twoQubitGates",
        "swapCount",
        "k0ConfigSha256",
        "activeCalibrationSha256",
        "normalizedTopologySha256",
        "scientificStatus",
        "mappingCandidateSha256",
    }
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != candidate_required:
            raise Qas30MappingError(
                "QAS30_MAPPING_CANDIDATE_INVALID", "mapping candidate fields changed"
            )
        pair = (row.get("candidateId"), row.get("seed"))
        if pair not in expected_pairs or pair in observed_pairs:
            raise Qas30MappingError(
                "QAS30_MAPPING_CANDIDATE_INVALID", "mapping candidate identity changed"
            )
        candidate_unhashed = {
            key: value for key, value in row.items() if key != "mappingCandidateSha256"
        }
        mapped_qcis = row.get("mappedQcis")
        order = row.get("measurementPhysicalOrder")
        logical = row.get("logicalToPhysical")
        if (
            row.get("schemaVersion") != MAPPING_CANDIDATE_SCHEMA_VERSION
            or row.get("target") != pool.get("target")
            or row.get("sourceCommitSha") != pool.get("sourceCommitSha")
            or row.get("dataEpoch") != pool.get("dataEpoch")
            or row.get("freezeManifestSha256") != pool.get("freezeManifestSha256")
            or row.get("candidateBundleSha256") != pool.get("candidateBundleSha256")
            or row.get("k0ConfigSha256") != pool.get("k0ConfigSha256")
            or row.get("activeCalibrationSha256") != pool.get("activeCalibrationSha256")
            or row.get("normalizedTopologySha256") != pool.get("normalizedTopologySha256")
            or row.get("scientificStatus") != pool.get("scientificStatus")
            or row.get("mappingCandidateSha256") != _entity_sha256(candidate_unhashed)
            or not _valid_sha256(row.get("sourceQcisSha256"))
            or not isinstance(mapped_qcis, str)
            or row.get("mappedQcisSha256") != _text_sha256(mapped_qcis)
            or not isinstance(order, list)
            or len(order) != 6
            or len(set(order)) != 6
            or any(PHYSICAL_QUBIT_PATTERN.fullmatch(item) is None for item in order)
            or [item.upper() for item in MEASUREMENT_PATTERN.findall(mapped_qcis)] != order
            or not isinstance(logical, Mapping)
            or set(logical) != set(LOGICAL_QUBITS)
            or [logical[item] for item in LOGICAL_QUBITS] != order
            or any(
                not isinstance(row.get(name), int)
                or isinstance(row.get(name), bool)
                or row.get(name) < 0
                for name in ("compiledDepth", "twoQubitGates", "swapCount")
            )
        ):
            raise Qas30MappingError(
                "QAS30_MAPPING_CANDIDATE_INVALID", "mapping candidate binding changed"
            )
        observed_pairs.add(pair)
    if observed_pairs != expected_pairs:
        raise Qas30MappingError("QAS30_MAPPING_POOL_INCOMPLETE", "mapping pool is incomplete")
    return dict(pool)


def select_common_order_mapping(
    mapping_pool: Mapping[str, Any], ordered_candidate_ids: Sequence[str]
) -> dict[str, Any]:
    """Select the frozen lexicographic optimum common-order five-circuit mapping."""

    pool = _validate_mapping_pool(mapping_pool)
    candidate_ids = list(ordered_candidate_ids)
    if (
        len(candidate_ids) != qas30_protocol.BATCH_SIZE
        or len(set(candidate_ids)) != qas30_protocol.BATCH_SIZE
        or any(item not in qas30_protocol.CANDIDATE_IDS for item in candidate_ids)
    ):
        raise Qas30MappingError(
            "QAS30_BATCH_CANDIDATE_ORDER_INVALID",
            "mapping selection requires five unique ordered candidates",
        )
    rows_by_candidate_and_order: dict[str, dict[tuple[str, ...], list[dict[str, Any]]]] = {
        candidate_id: {} for candidate_id in candidate_ids
    }
    for row in pool["mappingCandidates"]:
        candidate_id = row["candidateId"]
        if candidate_id in rows_by_candidate_and_order:
            order = tuple(row["measurementPhysicalOrder"])
            rows_by_candidate_and_order[candidate_id].setdefault(order, []).append(row)
    common_orders = set.intersection(
        *(set(rows_by_candidate_and_order[candidate_id]) for candidate_id in candidate_ids)
    )
    if not common_orders:
        raise Qas30MappingError(
            "QAS30_BATCH_MAPPING_INFEASIBLE",
            "the five candidates have no common physical measurement order",
        )
    optimum: tuple[tuple[Any, ...], list[dict[str, Any]], tuple[str, ...], str] | None = None
    for order in common_orders:
        candidate_options = [
            sorted(
                rows_by_candidate_and_order[candidate_id][order],
                key=lambda row: (row["seed"], row["mappingCandidateSha256"]),
            )
            for candidate_id in candidate_ids
        ]
        for combination in product(*candidate_options):
            selected_rows = list(combination)
            depths = [row["compiledDepth"] for row in selected_rows]
            two_qubit = [row["twoQubitGates"] for row in selected_rows]
            batch_hash = _entity_sha256(
                {
                    "candidateIds": candidate_ids,
                    "mappingCandidateSha256": [
                        row["mappingCandidateSha256"] for row in selected_rows
                    ],
                    "measurementPhysicalOrder": list(order),
                }
            )
            rank = (
                max(depths),
                sum(depths),
                max(two_qubit),
                sum(two_qubit),
                sum(row["swapCount"] for row in selected_rows),
                batch_hash,
                tuple(row["seed"] for row in selected_rows),
                order,
            )
            candidate = (rank, selected_rows, order, batch_hash)
            if optimum is None or candidate[0] < optimum[0]:
                optimum = candidate
    if optimum is None:
        raise Qas30MappingError(
            "QAS30_BATCH_MAPPING_INFEASIBLE",
            "the five candidates have no common physical measurement order",
        )
    rank, selected_rows, order, batch_hash = optimum
    selection: dict[str, Any] = {
        "schemaVersion": MAPPING_SELECTION_SCHEMA_VERSION,
        "state": "COMMON_ORDER_MAPPING_SELECTED",
        "target": pool["target"],
        "runId": pool["runId"],
        "dataEpoch": pool["dataEpoch"],
        "sourceCommitSha": pool["sourceCommitSha"],
        "freezeManifestSha256": pool["freezeManifestSha256"],
        "candidateBundleSha256": pool["candidateBundleSha256"],
        "mappingPoolSha256": pool["mappingPoolSha256"],
        "activeCalibrationSha256": pool["activeCalibrationSha256"],
        "k0ConfigSha256": pool["k0ConfigSha256"],
        "normalizedTopologySha256": pool["normalizedTopologySha256"],
        "strategy": MAPPING_STRATEGY,
        "candidateIds": candidate_ids,
        "commonMeasurementPhysicalOrder": list(order),
        "mappings": selected_rows,
        "selectionObjective": {
            "maximumCompiledDepth": rank[0],
            "totalCompiledDepth": rank[1],
            "maximumTwoQubitGates": rank[2],
            "totalTwoQubitGates": rank[3],
            "totalSwapCount": rank[4],
            "orderedMappingHash": batch_hash,
            "orderedSeeds": list(rank[6]),
            "measurementPhysicalOrder": list(order),
        },
        "scientificStatus": pool["scientificStatus"],
        "externalRequestsIssued": 0,
    }
    selection["mappingSnapshotSha256"] = _entity_sha256(selection)
    return selection


def _validate_selection(selection: Mapping[str, Any], pool: Mapping[str, Any]) -> dict[str, Any]:
    verified_pool = _validate_mapping_pool(pool)
    if not isinstance(selection, Mapping):
        raise Qas30MappingError(
            "QAS30_MAPPING_SELECTION_INVALID", "mapping selection is not an object"
        )
    stored_sha = selection.get("mappingSnapshotSha256")
    unhashed = {key: value for key, value in selection.items() if key != "mappingSnapshotSha256"}
    if (
        selection.get("schemaVersion") != MAPPING_SELECTION_SCHEMA_VERSION
        or selection.get("state") != "COMMON_ORDER_MAPPING_SELECTED"
        or stored_sha != _entity_sha256(unhashed)
        or selection.get("mappingPoolSha256") != verified_pool.get("mappingPoolSha256")
        or selection.get("sourceCommitSha") != verified_pool.get("sourceCommitSha")
        or selection.get("dataEpoch") != verified_pool.get("dataEpoch")
        or selection.get("freezeManifestSha256") != verified_pool.get("freezeManifestSha256")
        or selection.get("activeCalibrationSha256") != verified_pool.get("activeCalibrationSha256")
        or selection.get("k0ConfigSha256") != verified_pool.get("k0ConfigSha256")
        or selection.get("normalizedTopologySha256")
        != verified_pool.get("normalizedTopologySha256")
        or selection.get("scientificStatus") != verified_pool.get("scientificStatus")
        or not isinstance(selection.get("candidateIds"), list)
        or not isinstance(selection.get("mappings"), list)
        or len(selection["candidateIds"]) != 5
        or len(selection["mappings"]) != 5
        or [row.get("candidateId") for row in selection["mappings"]] != selection["candidateIds"]
        or any(
            row.get("measurementPhysicalOrder") != selection.get("commonMeasurementPhysicalOrder")
            for row in selection["mappings"]
        )
    ):
        raise Qas30MappingError(
            "QAS30_MAPPING_SELECTION_INVALID", "mapping selection binding changed"
        )
    pool_hashes = {row["mappingCandidateSha256"] for row in verified_pool["mappingCandidates"]}
    if any(row.get("mappingCandidateSha256") not in pool_hashes for row in selection["mappings"]):
        raise Qas30MappingError(
            "QAS30_MAPPING_SELECTION_INVALID", "selection contains an unknown mapping"
        )
    expected = select_common_order_mapping(verified_pool, selection["candidateIds"])
    if _canonical_json(selection) != _canonical_json(expected):
        raise Qas30MappingError(
            "QAS30_MAPPING_SELECTION_INVALID",
            "mapping selection is not the frozen lexicographic optimum",
        )
    return dict(selection)


def _calibration_noise_proxy(
    mapped_qcis: str,
    measurement_order: Sequence[str],
    qubits: Mapping[str, Any],
    couplers: Mapping[str, Any],
) -> tuple[float, dict[str, Any]]:
    if len(measurement_order) != 6 or len(set(measurement_order)) != 6:
        raise Qas30MappingError(
            "QAS30_MAPPING_NOISE_INPUT_INVALID", "mapped qubit order is invalid"
        )
    readout_errors: list[float] = []
    single_gate_errors: list[float] = []
    for qubit in measurement_order:
        row = qubits.get(qubit)
        if not isinstance(row, Mapping) or row.get("active") is not True:
            raise Qas30MappingError(
                "QAS30_MAPPING_NOISE_FIELD_MISSING",
                f"active mapped qubit {qubit} has no calibration row",
            )
        readout_errors.append(_finite_nonnegative(row.get("readoutError"), f"{qubit}.readoutError"))
        single_gate_errors.append(
            _finite_nonnegative(row.get("singleGateError"), f"{qubit}.singleGateError")
        )
    actual_edges = sorted(
        set(_two_qubit_edges(mapped_qcis)),
        key=lambda pair: (int(pair[0][1:]), int(pair[1][1:])),
    )
    if not actual_edges:
        raise Qas30MappingError(
            "QAS30_MAPPING_NOISE_FIELD_MISSING",
            "mapped QCIS has no actual two-qubit edge",
        )
    error_by_edge: dict[tuple[str, str], float] = {}
    coupler_by_edge: dict[tuple[str, str], str] = {}
    for coupler, row in couplers.items():
        if not isinstance(row, Mapping) or row.get("active") is not True:
            continue
        edge = tuple(
            sorted(
                (str(row.get("source")).upper(), str(row.get("target")).upper()),
                key=lambda item: int(item[1:]),
            )
        )
        if edge in error_by_edge:
            raise Qas30MappingError(
                "QAS30_MAPPING_NOISE_FIELD_AMBIGUOUS",
                "active topology contains duplicate physical edges",
            )
        error_by_edge[edge] = _finite_nonnegative(
            row.get("twoQubitError"), f"{coupler}.twoQubitError"
        )
        coupler_by_edge[edge] = str(coupler)
    missing = [edge for edge in actual_edges if edge not in error_by_edge]
    if missing:
        raise Qas30MappingError(
            "QAS30_MAPPING_NOISE_FIELD_MISSING",
            "an actual mapped two-qubit edge has no active calibration",
        )
    readout_mean = sum(readout_errors) / len(readout_errors)
    single_mean = sum(single_gate_errors) / len(single_gate_errors)
    two_qubit_mean = sum(error_by_edge[edge] for edge in actual_edges) / len(actual_edges)
    proxy = (readout_mean + single_mean + two_qubit_mean) / 3.0
    components = {
        "definitionId": CALIBRATION_NOISE_DEFINITION_ID,
        "mappedQubits": list(measurement_order),
        "actualTwoQubitEdges": [
            {
                "source": edge[0],
                "target": edge[1],
                "coupler": coupler_by_edge[edge],
                "twoQubitError": error_by_edge[edge],
            }
            for edge in actual_edges
        ],
        "mappedQubitReadoutErrorMean": readout_mean,
        "mappedQubitSingleGateErrorMean": single_mean,
        "actualTwoQubitEdgeErrorMean": two_qubit_mean,
        "componentWeights": {
            "mappedQubitReadoutErrorMean": 1.0 / 3.0,
            "mappedQubitSingleGateErrorMean": 1.0 / 3.0,
            "actualTwoQubitEdgeErrorMean": 1.0 / 3.0,
        },
    }
    return proxy, components


def finalize_common_order_mapping(
    *,
    mapping_pool: Mapping[str, Any],
    mapping_selection: Mapping[str, Any],
    k0_calibration_receipt: Mapping[str, Any],
    regularity_observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Consume five official observations and emit regularity/feature receipts."""

    pool = _validate_mapping_pool(mapping_pool)
    selection = _validate_selection(mapping_selection, pool)
    k0 = validate_k0_calibration_receipt(k0_calibration_receipt)
    if (
        k0.get("sourceCommitSha") != selection.get("sourceCommitSha")
        or k0.get("dataEpoch") != selection.get("dataEpoch")
        or k0.get("freezeManifestSha256") != selection.get("freezeManifestSha256")
        or k0.get("activeCalibrationSha256") != selection.get("activeCalibrationSha256")
        or k0["snapshot"].get("machineConfigSha256") != selection.get("k0ConfigSha256")
        or k0["snapshot"]["topology"].get("normalizedTopologySha256")
        != selection.get("normalizedTopologySha256")
        or k0.get("scientificStatus") != selection.get("scientificStatus")
    ):
        raise Qas30MappingError(
            "QAS30_K0_SELECTION_BINDING_INVALID",
            "K0 receipt does not bind the mapping selection",
        )
    observations = list(regularity_observations)
    if len(observations) != 5:
        raise Qas30MappingError(
            "QAS30_BATCH_REGULARITY_INCOMPLETE",
            "five regularity observations are required",
        )
    by_candidate: dict[str, dict[str, Any]] = {}
    for observation in observations:
        if not isinstance(observation, Mapping):
            raise Qas30MappingError(
                "QAS30_REGULARITY_OBSERVATION_INVALID",
                "regularity observation is not an object",
            )
        required = {
            "schemaVersion",
            "candidateId",
            "target",
            "sourceCommitSha",
            "dataEpoch",
            "freezeManifestSha256",
            "mappedQcisSha256",
            "mappingCandidateSha256",
            "k0ConfigSha256",
            "activeCalibrationSha256",
            "validationMethod",
            "providerRequestSha256",
            "providerResponseSha256",
            "providerHttpStatus",
            "transportRequestCount",
            "regular",
            "scientificStatus",
            "observedAt",
            "regularityObservationSha256",
        }
        candidate_id = observation.get("candidateId")
        unhashed = {
            key: value for key, value in observation.items() if key != "regularityObservationSha256"
        }
        if (
            set(observation) != required
            or observation.get("schemaVersion") != REGULARITY_OBSERVATION_SCHEMA_VERSION
            or candidate_id not in selection["candidateIds"]
            or candidate_id in by_candidate
            or observation.get("target") != qas30_protocol.TARGET
            or observation.get("sourceCommitSha") != selection.get("sourceCommitSha")
            or observation.get("dataEpoch") != selection.get("dataEpoch")
            or observation.get("freezeManifestSha256") != selection.get("freezeManifestSha256")
            or observation.get("k0ConfigSha256") != selection.get("k0ConfigSha256")
            or observation.get("activeCalibrationSha256")
            != selection.get("activeCalibrationSha256")
            or observation.get("validationMethod") != REGULARITY_METHOD
            or observation.get("providerHttpStatus") != 200
            or observation.get("transportRequestCount") != 1
            or not isinstance(observation.get("regular"), bool)
            or observation.get("scientificStatus") != selection.get("scientificStatus")
            or not _valid_sha256(observation.get("providerRequestSha256"))
            or not _valid_sha256(observation.get("providerResponseSha256"))
            or observation.get("regularityObservationSha256") != _entity_sha256(unhashed)
        ):
            raise Qas30MappingError(
                "QAS30_REGULARITY_OBSERVATION_INVALID",
                "regularity observation binding changed",
            )
        mapping = selection["mappings"][selection["candidateIds"].index(candidate_id)]
        if observation.get("mappedQcisSha256") != mapping.get(
            "mappedQcisSha256"
        ) or observation.get("mappingCandidateSha256") != mapping.get("mappingCandidateSha256"):
            raise Qas30MappingError(
                "QAS30_REGULARITY_OBSERVATION_INVALID",
                "regularity observation does not bind the mapped QCIS",
            )
        by_candidate[str(candidate_id)] = dict(observation)
    if set(by_candidate) != set(selection["candidateIds"]):
        raise Qas30MappingError(
            "QAS30_BATCH_REGULARITY_INCOMPLETE",
            "regularity observations do not cover the selected batch",
        )
    if not all(observation.get("regular") is True for observation in by_candidate.values()):
        raise Qas30MappingError(
            "QAS30_BATCH_REGULARITY_INCOMPLETE",
            "all five official observations must classify the mapped QCIS as regular",
        )
    request_hashes = [observation["providerRequestSha256"] for observation in by_candidate.values()]
    if len(set(request_hashes)) != 5:
        raise Qas30MappingError(
            "QAS30_REGULARITY_OBSERVATION_INVALID",
            "regularity observations do not bind five distinct requests",
        )
    qubits, couplers, _ = _topology_parts(
        k0["snapshot"]["machineConfig"], k0["snapshot"]["topology"]
    )
    regularity_receipts: list[dict[str, Any]] = []
    mapping_feature_rows: list[dict[str, Any]] = []
    for mapping in selection["mappings"]:
        candidate_id = mapping["candidateId"]
        observation = by_candidate[candidate_id]
        calibration_noise_proxy, noise_components = _calibration_noise_proxy(
            mapping["mappedQcis"],
            mapping["measurementPhysicalOrder"],
            qubits,
            couplers,
        )
        features = {
            "compiledDepth": mapping["compiledDepth"],
            "twoQubitGates": mapping["twoQubitGates"],
            "swapCount": mapping["swapCount"],
            "calibrationNoiseProxy": calibration_noise_proxy,
        }
        regularity_receipt = {
            "schemaVersion": REGULARITY_RECEIPT_SCHEMA_VERSION,
            "state": "REGULARITY_VERIFIED_AFTER_K0",
            "candidateId": candidate_id,
            "target": qas30_protocol.TARGET,
            "sourceCommitSha": selection["sourceCommitSha"],
            "dataEpoch": selection["dataEpoch"],
            "freezeManifestSha256": selection["freezeManifestSha256"],
            "qcisSha256": mapping["mappedQcisSha256"],
            "k0ConfigSha256": selection["k0ConfigSha256"],
            "activeCalibrationSha256": selection["activeCalibrationSha256"],
            "mappingSnapshotSha256": selection["mappingSnapshotSha256"],
            "measurementPhysicalOrder": mapping["measurementPhysicalOrder"],
            "logicalToPhysical": mapping["logicalToPhysical"],
            "mappingDerivedFeatures": features,
            "regular": True,
            "scientificStatus": selection["scientificStatus"],
            "regularityObservationSha256": observation["regularityObservationSha256"],
            "transportRequestCount": 1,
        }
        regularity_receipt_sha = _entity_sha256(regularity_receipt)
        feature_row = {
            "schemaVersion": MAPPING_FEATURE_ROW_SCHEMA_VERSION,
            "state": "MAPPING_FEATURES_FROZEN_FOR_BATCH",
            "candidateId": candidate_id,
            "sourceCommitSha": selection["sourceCommitSha"],
            "dataEpoch": selection["dataEpoch"],
            "freezeManifestSha256": selection["freezeManifestSha256"],
            "mappedQcisSha256": mapping["mappedQcisSha256"],
            "mappingCandidateSha256": mapping["mappingCandidateSha256"],
            "mappingSnapshotSha256": selection["mappingSnapshotSha256"],
            "k0ConfigSha256": selection["k0ConfigSha256"],
            "activeCalibrationSha256": selection["activeCalibrationSha256"],
            "normalizedTopologySha256": selection["normalizedTopologySha256"],
            "regularityReceiptSha256": regularity_receipt_sha,
            "mappingDerivedFeatures": features,
            "calibrationNoiseComponents": noise_components,
            "scientificStatus": selection["scientificStatus"],
        }
        feature_row["mappingFeatureRowSha256"] = _entity_sha256(feature_row)
        regularity_receipts.append(
            {
                **regularity_receipt,
                "regularityReceiptSha256": regularity_receipt_sha,
            }
        )
        mapping_feature_rows.append(feature_row)
    verification: dict[str, Any] = {
        "schemaVersion": MAPPING_VERIFICATION_SCHEMA_VERSION,
        "state": "BATCH_MAPPING_REGULARITY_VERIFIED",
        "target": qas30_protocol.TARGET,
        "runId": selection["runId"],
        "dataEpoch": selection["dataEpoch"],
        "sourceCommitSha": selection["sourceCommitSha"],
        "freezeManifestSha256": selection["freezeManifestSha256"],
        "candidateBundleSha256": selection["candidateBundleSha256"],
        "mappingPoolSha256": selection["mappingPoolSha256"],
        "mappingSnapshotSha256": selection["mappingSnapshotSha256"],
        "k0ConfigSha256": selection["k0ConfigSha256"],
        "activeCalibrationSha256": selection["activeCalibrationSha256"],
        "normalizedTopologySha256": selection["normalizedTopologySha256"],
        "candidateIds": selection["candidateIds"],
        "commonMeasurementPhysicalOrder": selection["commonMeasurementPhysicalOrder"],
        "regularityReceipts": regularity_receipts,
        "mappingFeatureRows": mapping_feature_rows,
        "officialObservationCount": 5,
        "transportRequestCount": 5,
        "scientificStatus": selection["scientificStatus"],
    }
    verification["batchMappingVerificationSha256"] = _entity_sha256(verification)
    return verification


def mapping_circuit_rows(
    *,
    production_bundle: Mapping[str, Any],
    parent_manifest_bytes: bytes,
    parent_canonical_ir_bytes: bytes,
    market_data_sha256: str,
    k0_calibration_receipt: Mapping[str, Any],
    mapping_pool: Mapping[str, Any],
    mapping_selection: Mapping[str, Any],
    mapping_verification: Mapping[str, Any],
    regularity_observations: Sequence[Mapping[str, Any]],
    transpiler: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Project verified mapping entities for assembly into live circuit rows.

    The returned regularity entity excludes its sibling hash, matching the
    strict ``qas30_tianyan`` circuit contract.  The caller adds the separately
    frozen predictor feature receipt before constructing a live batch.  The
    complete parent/K0/official-observation chain is rebuilt here so a set of
    mutually self-signed mapping JSON objects cannot authorize a live batch.
    """

    rebuilt_pool = build_k0_mapping_pool(
        production_bundle=production_bundle,
        parent_manifest_bytes=parent_manifest_bytes,
        parent_canonical_ir_bytes=parent_canonical_ir_bytes,
        market_data_sha256=market_data_sha256,
        k0_calibration_receipt=k0_calibration_receipt,
        transpiler=transpiler,
    )
    if _canonical_json(rebuilt_pool) != _canonical_json(mapping_pool):
        raise Qas30MappingError(
            "QAS30_MAPPING_SOURCE_CHAIN_INVALID",
            "mapping pool does not rebuild from the frozen parent and K0 artifacts",
        )
    rebuilt_selection = select_common_order_mapping(
        rebuilt_pool, mapping_selection.get("candidateIds", [])
    )
    if _canonical_json(rebuilt_selection) != _canonical_json(mapping_selection):
        raise Qas30MappingError(
            "QAS30_MAPPING_SOURCE_CHAIN_INVALID",
            "mapping selection does not rebuild from the frozen mapping pool",
        )
    rebuilt_verification = finalize_common_order_mapping(
        mapping_pool=rebuilt_pool,
        mapping_selection=rebuilt_selection,
        k0_calibration_receipt=k0_calibration_receipt,
        regularity_observations=regularity_observations,
    )
    if _canonical_json(rebuilt_verification) != _canonical_json(mapping_verification):
        raise Qas30MappingError(
            "QAS30_MAPPING_SOURCE_CHAIN_INVALID",
            "mapping verification does not rebuild from official observations",
        )

    verification = rebuilt_verification
    verification_sha = verification.get("batchMappingVerificationSha256")
    verification_unhashed = {
        key: value for key, value in verification.items() if key != "batchMappingVerificationSha256"
    }
    selection = rebuilt_selection
    selection_sha = selection.get("mappingSnapshotSha256")
    selection_unhashed = {
        key: value for key, value in selection.items() if key != "mappingSnapshotSha256"
    }
    if (
        verification.get("schemaVersion") != MAPPING_VERIFICATION_SCHEMA_VERSION
        or verification.get("state") != "BATCH_MAPPING_REGULARITY_VERIFIED"
        or verification_sha != _entity_sha256(verification_unhashed)
        or selection.get("schemaVersion") != MAPPING_SELECTION_SCHEMA_VERSION
        or selection_sha != _entity_sha256(selection_unhashed)
        or verification.get("mappingSnapshotSha256") != selection_sha
        or verification.get("candidateIds") != selection.get("candidateIds")
    ):
        raise Qas30MappingError(
            "QAS30_MAPPING_VERIFICATION_INVALID",
            "mapping verification does not bind the selection",
        )
    regularity_by_id = {
        row.get("candidateId"): dict(row)
        for row in verification.get("regularityReceipts", [])
        if isinstance(row, Mapping)
    }
    feature_by_id = {
        row.get("candidateId"): dict(row)
        for row in verification.get("mappingFeatureRows", [])
        if isinstance(row, Mapping)
    }
    if set(regularity_by_id) != set(selection["candidateIds"]) or set(feature_by_id) != set(
        selection["candidateIds"]
    ):
        raise Qas30MappingError(
            "QAS30_MAPPING_VERIFICATION_INVALID",
            "mapping verification candidate coverage changed",
        )
    rows: list[dict[str, Any]] = []
    for mapping in selection["mappings"]:
        candidate_id = mapping["candidateId"]
        regularity_row = regularity_by_id[candidate_id]
        regularity_sha = regularity_row.pop("regularityReceiptSha256", None)
        feature_row = feature_by_id[candidate_id]
        feature_sha = feature_row.get("mappingFeatureRowSha256")
        feature_unhashed = {
            key: value for key, value in feature_row.items() if key != "mappingFeatureRowSha256"
        }
        if (
            regularity_sha != _entity_sha256(regularity_row)
            or feature_sha != _entity_sha256(feature_unhashed)
            or regularity_row.get("qcisSha256") != mapping.get("mappedQcisSha256")
            or feature_row.get("mappedQcisSha256") != mapping.get("mappedQcisSha256")
        ):
            raise Qas30MappingError(
                "QAS30_MAPPING_VERIFICATION_INVALID",
                "verified mapping row hash changed",
            )
        row: dict[str, Any] = {
            "schemaVersion": "qf.qas30.mapping-circuit-row.v1",
            "candidateId": candidate_id,
            "qcis": mapping["mappedQcis"],
            "qcisSha256": mapping["mappedQcisSha256"],
            "measurementPhysicalOrder": mapping["measurementPhysicalOrder"],
            "regularityReceipt": regularity_row,
            "regularityReceiptSha256": regularity_sha,
            "mappingFeatureRow": feature_row,
            "mappingFeatureRowSha256": feature_sha,
            # These are the only mapping values that may enter S_HW.  They
            # describe the five QCISs actually compiled under their common
            # physical order, not the 30-candidate selection surrogate.
            "executionMappingFeatures": dict(feature_row["mappingDerivedFeatures"]),
            "executionMappingFeatureSha256": _entity_sha256(
                {
                    "candidateId": candidate_id,
                    "compilationCalibrationSha256": selection[
                        "activeCalibrationSha256"
                    ],
                    "mappingSnapshotSha256": selection_sha,
                    "features": feature_row["mappingDerivedFeatures"],
                }
            ),
        }
        row["mappingCircuitRowSha256"] = _entity_sha256(row)
        rows.append(row)
    return rows


def project_mapping_circuit_rows(
    mapping_rows: Sequence[Mapping[str, Any]],
    *,
    predictor_feature_freeze: Mapping[str, Any],
    block_batch_index: int,
    compilation_calibration_sha256: str,
    selection_feature_snapshot_sha256: str,
) -> list[dict[str, Any]]:
    """Project mapping intermediates into the strict Tianyan circuit contract.

    ``mapping_circuit_rows`` deliberately carries audit-only envelope fields
    (including ``mappingFeatureRow``).  This is the sole boundary at which
    those entities are reduced to the ten fields accepted by
    :func:`qas30_tianyan.validate_live_batch`.  Predictor receipts are rebuilt
    from the supplied, hash-verified K0/K1 freeze rather than copied from an
    untrusted row, and execution-feature hashes are rebound to the explicit
    compilation calibration and mapping snapshot used by the eventual batch.
    """

    if (
        not isinstance(mapping_rows, Sequence)
        or isinstance(mapping_rows, str | bytes)
        or len(mapping_rows) != qas30_protocol.BATCH_SIZE
    ):
        raise Qas30MappingError(
            "QAS30_MAPPING_PROJECTOR_INPUT_INVALID",
            "mapping projector requires exactly five mapping rows",
        )
    if block_batch_index not in range(1, 7) or isinstance(block_batch_index, bool):
        raise Qas30MappingError(
            "QAS30_MAPPING_PROJECTOR_INPUT_INVALID",
            "mapping projector batch index must be 1-6",
        )
    expected_predictor_label = "K0" if block_batch_index in {1, 2} else "K1"
    for value, name in (
        (compilation_calibration_sha256, "compilationCalibrationSha256"),
        (selection_feature_snapshot_sha256, "selectionFeatureSnapshotSha256"),
    ):
        _require_sha256(value, name)

    # Rebuild every predictor receipt through the canonical protocol helper.
    # The helper validates the freeze entity hash and candidate row shape;
    # these additional bindings prevent a valid freeze for another run/K
    # snapshot from being silently mixed into this mapped batch.
    if not isinstance(predictor_feature_freeze, Mapping):
        raise Qas30MappingError(
            "QAS30_MAPPING_PROJECTOR_PREDICTOR_INVALID",
            "predictor feature freeze is not an object",
        )
    predictor_stored = predictor_feature_freeze.get("predictorFeatureFreezeSha256")
    predictor_unhashed = {
        key: value
        for key, value in predictor_feature_freeze.items()
        if key != "predictorFeatureFreezeSha256"
    }
    if (
        predictor_stored != _entity_sha256(predictor_unhashed)
        or predictor_feature_freeze.get("predictorCalibrationLabel")
        != expected_predictor_label
        or predictor_feature_freeze.get("selectionFeatureSnapshotSha256")
        != selection_feature_snapshot_sha256
        or not _valid_sha256(predictor_feature_freeze.get("k0ConfigSha256"))
        or not _valid_sha256(predictor_feature_freeze.get("predictorCalibrationSha256"))
    ):
        raise Qas30MappingError(
            "QAS30_MAPPING_PROJECTOR_PREDICTOR_INVALID",
            "predictor freeze label or selection snapshot is not bound",
        )

    candidate_ids: list[str] = []
    projected: list[dict[str, Any]] = []
    mapping_snapshot_sha256: str | None = None
    source_bindings: tuple[Any, ...] | None = None
    for source in mapping_rows:
        if not isinstance(source, Mapping) or set(source) != MAPPING_CIRCUIT_ROW_FIELDS:
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_INPUT_INVALID",
                "mapping row contains fields outside the audited envelope contract",
            )
        row = dict(source)
        candidate_id = row.get("candidateId")
        if (
            not isinstance(candidate_id, str)
            or re.fullmatch(r"C(?:0[1-9]|[12][0-9]|30)", candidate_id) is None
            or candidate_id in candidate_ids
            or row.get("schemaVersion") != "qf.qas30.mapping-circuit-row.v1"
        ):
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_INPUT_INVALID",
                "mapping row candidate identity is invalid",
            )
        row_without_hash = {
            key: value for key, value in row.items() if key != "mappingCircuitRowSha256"
        }
        if row.get("mappingCircuitRowSha256") != _entity_sha256(row_without_hash):
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_INPUT_INVALID",
                "mapping row hash changed",
            )
        qcis = row.get("qcis")
        qcis_sha256 = row.get("qcisSha256")
        order = row.get("measurementPhysicalOrder")
        if (
            not isinstance(qcis, str)
            or not qcis.strip()
            or not _valid_sha256(qcis_sha256)
            or qcis_sha256 != _text_sha256(qcis)
            or not isinstance(order, list)
            or len(order) != 6
            or len(set(order)) != 6
            or any(
                not isinstance(item, str) or PHYSICAL_QUBIT_PATTERN.fullmatch(item) is None
                for item in order
            )
            or [item.upper() for item in MEASUREMENT_PATTERN.findall(qcis)] != order
        ):
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_INPUT_INVALID",
                "mapping row QCIS or measurement order is invalid",
            )

        regularity = row.get("regularityReceipt")
        feature_row = row.get("mappingFeatureRow")
        feature_snapshot = (
            feature_row.get("mappingSnapshotSha256")
            if isinstance(feature_row, Mapping)
            else None
        )
        regularity_sha = row.get("regularityReceiptSha256")
        if (
            not isinstance(regularity, Mapping)
            or set(regularity) != REGULARITY_RECEIPT_FIELDS
            or not _valid_sha256(regularity_sha)
            or regularity_sha != _entity_sha256(regularity)
            or regularity.get("candidateId") != candidate_id
            or regularity.get("qcisSha256") != qcis_sha256
            or regularity.get("measurementPhysicalOrder") != order
            or regularity.get("activeCalibrationSha256") != compilation_calibration_sha256
            or regularity.get("mappingSnapshotSha256")
            != feature_snapshot
        ):
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_REGULARITY_INVALID",
                "regularity receipt is not bound to the mapped QCIS and compilation K",
            )

        feature_sha = row.get("mappingFeatureRowSha256")
        feature_unhashed = (
            {
                key: value
                for key, value in feature_row.items()
                if key != "mappingFeatureRowSha256"
            }
            if isinstance(feature_row, Mapping)
            else None
        )
        if (
            not isinstance(feature_row, Mapping)
            or set(feature_row) != MAPPING_FEATURE_ROW_FIELDS
            or not _valid_sha256(feature_sha)
            or not isinstance(feature_unhashed, Mapping)
            or feature_sha != _entity_sha256(feature_unhashed)
            or feature_row.get("candidateId") != candidate_id
            or feature_row.get("mappedQcisSha256") != qcis_sha256
            or feature_row.get("regularityReceiptSha256") != regularity_sha
            or feature_row.get("mappingSnapshotSha256")
            != regularity.get("mappingSnapshotSha256")
        ):
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_FEATURE_INVALID",
                "mapping feature row is not bound to its regularity receipt",
            )
        execution = row.get("executionMappingFeatures")
        if (
            not isinstance(execution, Mapping)
            or execution != feature_row.get("mappingDerivedFeatures")
            or set(execution) != qas30_tianyan.MAPPING_DERIVED_FEATURES
        ):
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_FEATURE_INVALID",
                "execution mapping features do not match the verified feature row",
            )
        mapping_sha = regularity.get("mappingSnapshotSha256")
        if not _valid_sha256(mapping_sha):
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_INPUT_INVALID",
                "mapping snapshot binding is invalid",
            )
        if mapping_snapshot_sha256 is None:
            mapping_snapshot_sha256 = str(mapping_sha)
            source_bindings = (
                regularity.get("sourceCommitSha"),
                regularity.get("dataEpoch"),
                regularity.get("freezeManifestSha256"),
                regularity.get("k0ConfigSha256"),
            )
        elif mapping_sha != mapping_snapshot_sha256:
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_INPUT_INVALID",
                "mapping rows do not share one mapping snapshot",
            )

        try:
            predictor_receipt = qas30_protocol.predictor_feature_receipt(
                predictor_feature_freeze, candidate_id=candidate_id
            )
        except qas30_protocol.Qas30ProtocolError as error:
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_PREDICTOR_INVALID",
                "predictor feature receipt cannot be rebuilt from its freeze",
            ) from error
        if (
            predictor_receipt.get("sourceCommitSha") != regularity.get("sourceCommitSha")
            or predictor_receipt.get("dataEpoch") != regularity.get("dataEpoch")
            or predictor_receipt.get("freezeManifestSha256")
            != regularity.get("freezeManifestSha256")
            or predictor_receipt.get("k0ConfigSha256") != regularity.get("k0ConfigSha256")
        ):
            raise Qas30MappingError(
                "QAS30_MAPPING_PROJECTOR_PREDICTOR_INVALID",
                "predictor receipt does not bind the mapped run and K0 config",
            )
        execution_binding = {
            "candidateId": candidate_id,
            "compilationCalibrationSha256": compilation_calibration_sha256,
            "mappingSnapshotSha256": mapping_snapshot_sha256,
            "features": dict(execution),
        }
        projected.append(
            {
                "candidateId": candidate_id,
                "qcis": qcis,
                "qcisSha256": qcis_sha256,
                "regularityReceipt": dict(regularity),
                "regularityReceiptSha256": regularity_sha,
                "predictorFeatureReceipt": predictor_receipt,
                "predictorFeatureReceiptSha256": _entity_sha256(predictor_receipt),
                "executionMappingFeatures": dict(execution),
                "executionMappingFeatureSha256": _entity_sha256(execution_binding),
                "measurementPhysicalOrder": list(order),
            }
        )
        candidate_ids.append(candidate_id)

    # Binding the per-row source identity also makes it impossible to combine
    # independently generated mapping rows with one predictor freeze.
    if source_bindings is None or len(set(candidate_ids)) != qas30_protocol.BATCH_SIZE:
        raise Qas30MappingError(
            "QAS30_MAPPING_PROJECTOR_INPUT_INVALID",
            "mapping projector candidate coverage is incomplete",
        )
    return projected
