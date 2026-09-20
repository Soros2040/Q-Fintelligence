"""Frozen local protocol primitives for the QAS30 hardware experiment."""

from __future__ import annotations

import hashlib
import json
import math
import random
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

PROTOCOL_SCHEMA_VERSION = "qf.qas30.protocol.v2"
TARGET = "tianyan176"
LOCAL_READY_STATE = "READY_FOR_K0"
BATCH_SIZE = 5
SHOTS_PER_CIRCUIT = 1_000
CANDIDATE_IDS = tuple(f"C{index:02d}" for index in range(1, 31))
HARDWARE_SCORE_COMPONENTS = (
    "localValidationLoss",
    "quboObjectiveDegradation",
    "infeasibleRate",
    "compiledDepth",
    "twoQubitGates",
    "swapCount",
    "calibrationNoiseProxy",
)
PREDICTOR_FEATURE_WHITELIST = (
    "localValidationLoss",
    "quboObjectiveDegradation",
    "compiledDepth",
    "twoQubitGates",
    "swapCount",
    "calibrationNoiseProxy",
)
RIDGE_ALPHA_GRID = (1e-6, 1e-4, 1e-2, 1.0, 100.0)
PRODUCTION_PCG64_SEED = 2026080902
READOUT_CONTRACT = "SHARED_LINEAR_Z_SCALE_BIAS"
MEASUREMENT_IMPLEMENTATION = "COMPUTATIONAL_BASIS_M_Q0_Q5"
ANALYTICAL_EXTENSION = "SPARSE_ZZ_QUBO_QAOA_ONLY"
LOCAL_FEATURE_DEFINITION = "qf.qas30.local-predictor-features.v1"
EXPECTED_LOCAL_FEATURE_EVIDENCE_SHA256 = (
    "d56e65a94ab683a8748141e31f599135d05eeff4cd65db6e59495c52775838ed"
)
ANALYTICAL_ZZ_SPARSE_EDGES = (
    ("Q0", "Q4"),
    ("Q0", "Q5"),
    ("Q1", "Q2"),
    ("Q1", "Q4"),
    ("Q2", "Q3"),
    ("Q2", "Q4"),
    ("Q3", "Q4"),
    ("Q4", "Q5"),
)
# Compatibility name for callers that only need the frozen sparse graph.  The
# graph is an analytical QUBO/QAOA extension, never a claim about the trained
# parent readout.
READOUT_SPARSE_EDGES = ANALYTICAL_ZZ_SPARSE_EDGES
PARENT_FEATURE_TEMPLATE = (
    ("RY", "ret5"),
    ("RZ", "ret20"),
    ("RX", "downside5"),
    ("RY", "downside20"),
    ("RZ", "amount_change20"),
)
PARENT_TASK_MANIFEST_SHA256 = (
    "135fbf54c0306e60773ab9af9808c39372c21f9dc4f0c33fe9f34861a121a07f"
)
PARENT_CANONICAL_IR_TEMPLATE_SHA256 = (
    "0a1c0ecb64a36ef202e7a95fe051ff5e672a2666934f8a196fcb522688e93592"
)
PARENT_CANONICAL_IR_ARTIFACT_SHA256 = (
    "081a73297d0f6ed2503377803db13085c781653bdd6725e15117cce45efd2df0"
)
PARENT_CANONICAL_IR_SHA256 = (
    "1f9ed401f053529ced062fab54cbda46a7c4d670ad25e5910aa630b3ac08ca06"
)
PARENT_DETERMINISTIC_INPUTS_SHA256 = (
    "ebd0313000514d54fcc267e32e08fd08b4f7b1ae539e49a5076f7692036719d8"
)
MARKET_799_CSV_SHA256 = (
    "6dd12ecd688a82ca1a603303d47881559ce314e7a8bf2c6b29613ce6829740db"
)
CONFIRMATORY_COMPARISONS = (
    "RIDGE_MINUS_FIXED",
    "RIDGE_MINUS_RANDOM",
    "LLM_MINUS_FIXED",
    "LLM_MINUS_RANDOM",
)
REPLICATION_BUDGETS = (10, 20, 30, 40)
BLIND_LABELS = ("A", "B", "C", "D")
STATE_TRANSITIONS = {
    "DESIGN_FROZEN": ("LOCAL_INPUTS_READY",),
    "LOCAL_INPUTS_READY": ("READY_FOR_K0",),
    "READY_FOR_K0": ("K0_CAPTURED",),
    "K0_CAPTURED": ("READY_FOR_FIRST_SUBMIT", "QAS30_BATCH_MAPPING_INFEASIBLE"),
    "READY_FOR_FIRST_SUBMIT": (
        "QUERY_IDS_PERSISTED",
        "PARTIAL_UNKNOWN",
        "UNKNOWN_PROVIDER_OUTCOME",
        "IDENTITY_MISMATCH",
    ),
    "QUERY_IDS_PERSISTED": ("RESULTS_OBSERVED",),
    "RESULTS_OBSERVED": ("READY_FOR_NEXT_CALIBRATION", "EXPERIMENT_COMPLETE"),
    "READY_FOR_NEXT_CALIBRATION": ("CALIBRATION_CAPTURED",),
    "CALIBRATION_CAPTURED": ("READY_FOR_FIRST_SUBMIT", "EXPERIMENT_COMPLETE"),
    "PARTIAL_UNKNOWN": (),
    "UNKNOWN_PROVIDER_OUTCOME": (),
    "IDENTITY_MISMATCH": (),
    "QAS30_BATCH_MAPPING_INFEASIBLE": (),
    "EXPERIMENT_COMPLETE": (),
}


class Qas30ProtocolError(ValueError):
    """One frozen protocol invariant was violated."""


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _valid_identity(value: Any, *, maximum: int = 256) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and value[0].isalnum()
        and all(character.isalnum() or character in "._:-" for character in value)
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_parent_manifest(parent_manifest_bytes: bytes) -> dict[str, Any]:
    """Validate the audited E02 input artifact before deriving QAS30 inputs."""

    if hashlib.sha256(parent_manifest_bytes).hexdigest() != PARENT_TASK_MANIFEST_SHA256:
        raise Qas30ProtocolError("E02 parent task manifest hash changed")
    try:
        value = json.loads(parent_manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Qas30ProtocolError("E02 parent task manifest is not valid JSON") from error
    if not isinstance(value, dict):
        raise Qas30ProtocolError("E02 parent task manifest must be an object")
    if value.get("schemaVersion") != "qf.v42-m5-formal-task-manifest.v1":
        raise Qas30ProtocolError("E02 parent task manifest schema changed")
    if (
        value.get("status") != "FORMAL_INPUTS_FROZEN"
        or value.get("formalTestState") != "UNSEALED_FOR_FORMAL_RUN"
        or value.get("candidateId") != "DIR_L2_R0"
        or value.get("taskCount") != 5
        or value.get("seed") != PRODUCTION_PCG64_SEED
        or value.get("shotsPerCircuit") != SHOTS_PER_CIRCUIT
    ):
        raise Qas30ProtocolError("E02 parent task identity changed")
    if value.get("canonicalIrTemplateSha256") != PARENT_CANONICAL_IR_TEMPLATE_SHA256:
        raise Qas30ProtocolError("E02 canonical IR template binding changed")
    if value.get("deterministicInputsArtifactSha256") != PARENT_DETERMINISTIC_INPUTS_SHA256:
        raise Qas30ProtocolError("E02 deterministic input binding changed")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 5:
        raise Qas30ProtocolError("E02 parent task set is incomplete")
    task_ids: set[str] = set()
    formal_dates: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise Qas30ProtocolError("E02 parent task row is invalid")
        task_id = task.get("taskId")
        formal_date = task.get("formalDate")
        if (
            task.get("candidateId") != "DIR_L2_R0"
            or not isinstance(task_id, str)
            or not task_id
            or task_id in task_ids
            or not isinstance(formal_date, str)
            or not re_full_date(formal_date)
            or formal_date in formal_dates
            or task.get("seed") != PRODUCTION_PCG64_SEED
        ):
            raise Qas30ProtocolError("E02 parent task row identity changed")
        task_ids.add(task_id)
        formal_dates.add(formal_date)
        for name in (
            "canonicalIrArtifactSha256",
            "canonicalIrSha256",
            "genericQcisArtifactSha256",
            "genericQcisSha256",
        ):
            if not _valid_sha256(task.get(name)):
                raise Qas30ProtocolError(f"E02 parent task {name} is invalid")
    if "2026-06-30" not in formal_dates:
        raise Qas30ProtocolError("E02 latest formal parent task is missing")
    return value


def re_full_date(value: str) -> bool:
    """Validate the frozen ISO calendar spelling without accepting timestamps."""

    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return False
    try:
        year, month, day = (int(item) for item in value.split("-"))
    except ValueError:
        return False
    return 2020 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


def _validate_parent_canonical_ir(
    parent_canonical_ir_bytes: bytes,
    parent_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and extract the frozen 2026-06-30 six-qubit parent circuit."""

    tasks = parent_manifest["tasks"]
    latest = next(
        (
            task
            for task in tasks
            if task.get("formalDate") == "2026-06-30"
            and task.get("candidateId", "DIR_L2_R0") == "DIR_L2_R0"
        ),
        None,
    )
    if latest is None:
        raise Qas30ProtocolError("E02 latest formal parent task is missing")
    artifact_sha256 = hashlib.sha256(parent_canonical_ir_bytes).hexdigest()
    if (
        artifact_sha256 != PARENT_CANONICAL_IR_ARTIFACT_SHA256
        or latest.get("canonicalIrArtifactSha256") != artifact_sha256
    ):
        raise Qas30ProtocolError("E02 parent Canonical IR artifact hash changed")
    try:
        value = json.loads(parent_canonical_ir_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Qas30ProtocolError("E02 parent Canonical IR is not valid JSON") from error
    if not isinstance(value, dict):
        raise Qas30ProtocolError("E02 parent Canonical IR must be an object")
    canonical_sha256 = value.get("canonicalIrSha256")
    without_hash = {key: item for key, item in value.items() if key != "canonicalIrSha256"}
    if (
        canonical_sha256 != PARENT_CANONICAL_IR_SHA256
        or latest.get("canonicalIrSha256") != canonical_sha256
        or _canonical_sha256(without_hash) != canonical_sha256
    ):
        raise Qas30ProtocolError("E02 parent Canonical IR logical hash changed")
    if (
        value.get("schemaVersion") != "qf.canonical-circuit-ir.v1"
        or value.get("candidateId") != "DIR_L2_R0"
        or value.get("status") != "FORMAL_PARAMETERS_FROZEN"
        or value.get("formalDate") != "2026-06-30"
        or value.get("width") != 6
        or value.get("classicalBits") != 6
        or value.get("logicalQubitOrder") != list(range(6))
        or value.get("measurementOrder") != list(range(6))
        or value.get("formalTestState") != "SEALED"
        or value.get("messageLayers") != 2
        or value.get("reuploadCount") != 0
        or value.get("sharedUpdateAxis") not in {"RX", "RY", "RZ"}
    ):
        raise Qas30ProtocolError("E02 parent Canonical IR identity changed")
    asset_order = value.get("assetOrder")
    if not isinstance(asset_order, list) or len(asset_order) != 6 or len(set(asset_order)) != 6:
        raise Qas30ProtocolError("E02 parent asset order is invalid")
    operations = value.get("operations")
    if not isinstance(operations, list) or len(operations) != 67:
        raise Qas30ProtocolError("E02 parent operations are missing")
    measurement = operations[-1]
    if (
        not isinstance(measurement, dict)
        or measurement.get("op") != "MEASURE_ALL"
        or measurement.get("controls") != []
        or measurement.get("targets") != list(range(6))
        or measurement.get("measurementOrder") != list(range(6))
        or any(
            isinstance(operation, dict) and operation.get("op") == "MEASURE_ALL"
            for operation in operations[:-1]
        )
    ):
        raise Qas30ProtocolError("E02 parent measurement order changed")

    def validate_feature_stage(
        rows: Sequence[Any],
        template: Sequence[tuple[str, str]],
        *,
        layer: int,
    ) -> list[dict[str, Any]]:
        if len(rows) != 6 * len(template):
            raise Qas30ProtocolError("E02 frozen feature stage length changed")
        validated: list[dict[str, Any]] = []
        cursor = 0
        for qubit, asset_id in enumerate(asset_order):
            for gate, feature in template:
                operation = rows[cursor]
                cursor += 1
                if (
                    not isinstance(operation, dict)
                    or operation.get("op") != gate
                    or operation.get("controls") != []
                    or operation.get("targets") != [qubit]
                    or operation.get("parameterSource") != f"feature:{feature}"
                    or operation.get("assetId") != asset_id
                    or operation.get("layer") != layer
                    or not math.isfinite(float(operation.get("angle", math.nan)))
                ):
                    raise Qas30ProtocolError("E02 frozen feature stage changed")
                validated.append(dict(operation))
        return validated

    first_features = validate_feature_stage(
        operations[:18], PARENT_FEATURE_TEMPLATE[:3], layer=0
    )
    shared_zero = operations[18:24]
    second_features = validate_feature_stage(
        operations[24:36], PARENT_FEATURE_TEMPLATE[3:], layer=1
    )
    cry_operations = [dict(operation) for operation in operations[36:60]]
    shared_one = operations[60:66]

    def validate_shared_stage(rows: Sequence[Any], *, omega: int) -> float:
        axis = value["sharedUpdateAxis"]
        angles: list[float] = []
        for qubit, (operation, asset_id) in enumerate(zip(rows, asset_order, strict=True)):
            if (
                not isinstance(operation, dict)
                or operation.get("op") != axis
                or operation.get("controls") != []
                or operation.get("targets") != [qubit]
                or operation.get("parameterSource") != f"shared:omega_{omega}"
                or operation.get("assetId") != asset_id
                or operation.get("layer") != omega
                or not math.isfinite(float(operation.get("angle", math.nan)))
            ):
                raise Qas30ProtocolError("E02 shared-angle broadcast stage changed")
            angles.append(float(operation["angle"]))
        if len(set(angles)) != 1:
            raise Qas30ProtocolError("E02 shared angle is not broadcast across six qubits")
        return angles[0]

    shared_angles = (
        validate_shared_stage(shared_zero, omega=0),
        validate_shared_stage(shared_one, omega=1),
    )
    trained_omega = value.get("trainedOmega")
    if (
        not isinstance(trained_omega, list)
        or len(trained_omega) != 2
        or any(not math.isfinite(float(item)) for item in trained_omega)
        or any(
            not math.isclose(
                float(trained_omega[index]),
                shared_angles[index],
                rel_tol=0.0,
                abs_tol=5e-10,
            )
            for index in range(2)
        )
    ):
        raise Qas30ProtocolError("E02 trained omega binding changed")
    if len(cry_operations) != 24 or cry_operations[:12] != cry_operations[12:]:
        raise Qas30ProtocolError("E02 parent must repeat one frozen 12-edge graph twice")
    directed_edges = cry_operations[:12]
    incoming = {target: 0 for target in range(6)}
    edge_identities: set[tuple[int, int]] = set()
    for edge in directed_edges:
        controls = edge.get("controls")
        targets = edge.get("targets")
        if (
            edge.get("directionSemantics") != "source_control_to_affected_target"
            or not isinstance(controls, list)
            or not isinstance(targets, list)
            or len(controls) != 1
            or len(targets) != 1
        ):
            raise Qas30ProtocolError("E02 directed edge semantics changed")
        control, target = controls[0], targets[0]
        if (
            not isinstance(control, int)
            or not isinstance(target, int)
            or control == target
            or control not in incoming
            or target not in incoming
            or (control, target) in edge_identities
            or not math.isfinite(float(edge.get("angle", math.nan)))
        ):
            raise Qas30ProtocolError("E02 directed edge is invalid")
        if (
            edge.get("sourceAssetId") != asset_order[control]
            or edge.get("affectedAssetId") != asset_order[target]
        ):
            raise Qas30ProtocolError("E02 directed edge asset binding changed")
        incoming[target] += 1
        edge_identities.add((control, target))
    if set(incoming.values()) != {2}:
        raise Qas30ProtocolError("E02 parent must have two incoming edges per target")
    sparse_edges = sorted(
        {
            tuple(sorted((f"Q{control}", f"Q{target}")))
            for control, target in edge_identities
        }
    )
    if sparse_edges != list(ANALYTICAL_ZZ_SPARSE_EDGES):
        raise Qas30ProtocolError("E02 frozen analytical sparse ZZ support changed")
    shared_readout = value.get("sharedReadout")
    if not isinstance(shared_readout, dict) or set(shared_readout) != {"bias", "scale"}:
        raise Qas30ProtocolError("E02 shared readout metadata is invalid")
    if not all(math.isfinite(float(item)) for item in shared_readout.values()):
        raise Qas30ProtocolError("E02 shared readout parameters are invalid")
    full_reupload: list[dict[str, Any]] = []
    first_by_qubit = [first_features[index * 3 : (index + 1) * 3] for index in range(6)]
    second_by_qubit = [second_features[index * 2 : (index + 1) * 2] for index in range(6)]
    for qubit in range(6):
        for operation in [*first_by_qubit[qubit], *second_by_qubit[qubit]]:
            full_reupload.append({**operation, "layer": 3})
    return {
        "value": value,
        "assetOrder": list(asset_order),
        "featureFirstThreeOperations": first_features,
        "featureLastTwoOperations": second_features,
        "fullFeatureReuploadOperations": full_reupload,
        "directedEdges": directed_edges,
        "sharedReadout": dict(shared_readout),
        "parentSharedAngles": list(shared_angles),
        "sparseEdges": [list(edge) for edge in sparse_edges],
    }


def _production_factor_rows(initialization_seeds: Sequence[int]) -> list[dict[str, Any]]:
    """Return five six-architecture blocks with exact frozen marginal balance."""

    if len(initialization_seeds) != 5 or len(set(initialization_seeds)) != 5:
        raise Qas30ProtocolError("production requires five unique initialization seeds")
    rows: list[dict[str, Any]] = []
    for seed_index, initialization_seed in enumerate(initialization_seeds):
        for architecture_index in range(6):
            rows.append(
                {
                    "messageLayers": 1 + architecture_index % 2,
                    "reuploadCount": (architecture_index // 3 + seed_index) % 2,
                    "rotationAxis": ("RX", "RY", "RZ")[architecture_index % 3],
                    "messageGate": "CRY_DIRECTED_DECOMPOSABLE",
                    "initialization": f"PCG64_INIT_{initialization_seed:016x}",
                    "readout": READOUT_CONTRACT,
                    "measurementImplementation": MEASUREMENT_IMPLEMENTATION,
                    "analyticalExtension": ANALYTICAL_EXTENSION,
                    "analyticalExtensionSparseEdges": [
                        list(edge) for edge in ANALYTICAL_ZZ_SPARSE_EDGES
                    ],
                }
            )
    return rows


def _qcis_from_ir(ir: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for operation in ir["operations"]:
        gate = operation["op"]
        if gate in {"RX", "RY", "RZ"}:
            target = operation["targets"][0]
            lines.append(f"{gate} Q{target} {float(operation['angle']):.10f}")
        elif gate == "CRY":
            control = operation["controls"][0]
            target = operation["targets"][0]
            half_angle = float(operation["angle"]) / 2.0
            lines.extend(
                (
                    f"RY Q{target} {half_angle:.10f}",
                    f"H Q{target}",
                    f"CZ Q{control} Q{target}",
                    f"H Q{target}",
                    f"RY Q{target} {-half_angle:.10f}",
                    f"H Q{target}",
                    f"CZ Q{control} Q{target}",
                    f"H Q{target}",
                )
            )
        elif gate == "MEASURE_ALL":
            lines.extend(f"M Q{qubit}" for qubit in operation["measurementOrder"])
        else:
            raise Qas30ProtocolError(f"unsupported QAS30 Canonical IR operation {gate}")
    return "\n".join(lines)


def production_candidate_bundle(
    *,
    parent_manifest_bytes: bytes,
    parent_canonical_ir_bytes: bytes,
    market_data_sha256: str,
) -> dict[str, Any]:
    """Generate the balanced C01-C30 production pool from audited parent inputs.

    The generator does not claim that a circuit is regular on current hardware;
    regularity and physical mapping remain K0-dependent live gates.
    """

    parent = _validate_parent_manifest(parent_manifest_bytes)
    parent_ir = _validate_parent_canonical_ir(parent_canonical_ir_bytes, parent)
    if market_data_sha256 != MARKET_799_CSV_SHA256:
        raise Qas30ProtocolError("799-period market data hash changed")
    if parent.get("seed") != PRODUCTION_PCG64_SEED:
        raise Qas30ProtocolError("E02 deterministic PCG64 seed changed")
    generator = np.random.Generator(np.random.PCG64(PRODUCTION_PCG64_SEED))
    initialization_seeds = [
        int(value)
        for value in generator.integers(0, 2**63, size=5, dtype=np.uint64)
    ]
    candidates: list[dict[str, Any]] = []
    logical_rows: list[dict[str, Any]] = []
    canonical_ir_artifacts: list[dict[str, Any]] = []
    for candidate_id, factors in zip(
        CANDIDATE_IDS,
        _production_factor_rows(initialization_seeds),
        strict=True,
    ):
        candidate = {
            "schemaVersion": "qf.qas30.candidate-spec.v1",
            "candidateId": candidate_id,
            **factors,
            "scientificStatus": "FROZEN_LOCAL_INPUT",
        }
        candidate["candidateSpecSha256"] = _canonical_sha256(candidate)
        initialization_seed = int(str(factors["initialization"]).removeprefix("PCG64_INIT_"), 16)
        candidate_generator = np.random.Generator(np.random.PCG64(initialization_seed))
        parameters = [
            float(f"{value:.10f}")
            for value in candidate_generator.uniform(
                -math.pi, math.pi, size=2
            )
        ]
        operations: list[dict[str, Any]] = [
            dict(operation) for operation in parent_ir["featureFirstThreeOperations"]
        ]
        for qubit, asset_id in enumerate(parent_ir["assetOrder"]):
            operations.append(
                {
                    "op": factors["rotationAxis"],
                    "controls": [],
                    "targets": [qubit],
                    "angle": parameters[0],
                    "parameterSource": "shared:omega_0",
                    "assetId": asset_id,
                    "layer": 0,
                }
            )
        operations.extend(
            dict(operation) for operation in parent_ir["featureLastTwoOperations"]
        )
        for layer in range(int(factors["messageLayers"])):
            for edge in parent_ir["directedEdges"]:
                operations.append({**dict(edge), "messageLayerIndex": layer})
        for qubit, asset_id in enumerate(parent_ir["assetOrder"]):
            operations.append(
                {
                    "op": factors["rotationAxis"],
                    "controls": [],
                    "targets": [qubit],
                    "angle": parameters[1],
                    "parameterSource": "shared:omega_1",
                    "assetId": asset_id,
                    "layer": 1,
                }
            )
        if int(factors["reuploadCount"]) == 1:
            operations.extend(
                dict(operation)
                for operation in parent_ir["fullFeatureReuploadOperations"]
            )
        operations.append(
            {
                "op": "MEASURE_ALL",
                "controls": [],
                "targets": list(range(6)),
                "measurementOrder": list(range(6)),
            }
        )
        ir = {
            "schemaVersion": "qf.qas30.canonical-ir.v1",
            "candidateId": candidate_id,
            "candidateSpecSha256": candidate["candidateSpecSha256"],
            "logicalQubits": [f"Q{index}" for index in range(6)],
            "factors": factors,
            "parameters": parameters,
            "operations": operations,
            "stageSequence": [
                {"stage": "FEATURE_FIRST_THREE", "operationCount": 18},
                {"stage": "SHARED_OMEGA_0_BROADCAST", "operationCount": 6},
                {"stage": "FEATURE_LAST_TWO", "operationCount": 12},
                {
                    "stage": "DIRECTED_CRY_MESSAGE",
                    "operationCount": 12 * int(factors["messageLayers"]),
                },
                {"stage": "SHARED_OMEGA_1_BROADCAST", "operationCount": 6},
                {
                    "stage": "FULL_FEATURE_REUPLOAD_ONCE",
                    "operationCount": 30 * int(factors["reuploadCount"]),
                },
                {"stage": "MEASURE_ALL", "operationCount": 1},
            ],
            "parentFrozenInputOperations": [
                *parent_ir["featureFirstThreeOperations"],
                *parent_ir["featureLastTwoOperations"],
            ],
            "parentDirectedMessageEdges": parent_ir["directedEdges"],
            "parameterPrecisionDecimalPlaces": 10,
            "parameterGenerator": {
                "algorithm": "numpy.random.PCG64",
                "catalogSeed": PRODUCTION_PCG64_SEED,
                "initializationSeed": initialization_seed,
                "stream": f"initialization-seed:{initialization_seed}",
            },
            "parentBindings": {
                "formalTaskManifestSha256": PARENT_TASK_MANIFEST_SHA256,
                "canonicalIrTemplateSha256": PARENT_CANONICAL_IR_TEMPLATE_SHA256,
                "canonicalIrArtifactSha256": PARENT_CANONICAL_IR_ARTIFACT_SHA256,
                "canonicalIrSha256": PARENT_CANONICAL_IR_SHA256,
                "deterministicInputsArtifactSha256": PARENT_DETERMINISTIC_INPUTS_SHA256,
                "market799CsvSha256": MARKET_799_CSV_SHA256,
            },
            "readoutContract": {
                "function": "max(0,scale*Z_i+bias)",
                "quantumInputs": ["Z_i"],
                "sharedAcrossAssets": True,
                "frozenParentParameters": parent_ir["sharedReadout"],
                "measurementImplementation": MEASUREMENT_IMPLEMENTATION,
                "zDerivedFromComputationalBasisCounts": True,
                "validatedParentImplementation": "v42_m3_training.fit_shared_readout",
            },
            "analyticalExtensions": {
                "name": ANALYTICAL_EXTENSION,
                "quantumInputs": ["ZZ_frozen_sparse_edges"],
                "sparseEdges": parent_ir["sparseEdges"],
                "sparseEdgeSource": "E02_2026_06_30_FROZEN_DIRECTED_SUPPORT_UNDIRECTED_UNION",
                "scope": "QUBO_QAOA_ANALYSIS_ONLY",
                "parentTrainingReadoutStatus": "NOT_USED_BY_VALIDATED_PARENT_TRAINING",
                "derivedFromComputationalBasisCounts": True,
            },
        }
        ir_json = _canonical_json(ir)
        ir_sha256 = hashlib.sha256(ir_json.encode("utf-8")).hexdigest()
        qcis = _qcis_from_ir(ir)
        canonical_ir_artifacts.append(
            {
                "candidateId": candidate_id,
                "canonicalIr": ir,
                "canonicalIrJson": ir_json,
                "canonicalIrSha256": ir_sha256,
                "parameterSha256": _canonical_sha256(parameters),
            }
        )
        logical_rows.append(
            {
                "schemaVersion": "qf.qas30.logical-circuit-input.v1",
                "candidateId": candidate_id,
                "candidateSpecSha256": candidate["candidateSpecSha256"],
                "canonicalIrSha256": ir_sha256,
                "canonicalIrArtifactSha256": hashlib.sha256(
                    ir_json.encode("utf-8")
                ).hexdigest(),
                "parentModelSha256": PARENT_TASK_MANIFEST_SHA256,
                "qcis": qcis,
                "qcisSha256": hashlib.sha256(qcis.encode("utf-8")).hexdigest(),
                "logicalMeasurementOrder": [f"Q{index}" for index in range(6)],
                "readoutContract": ir["readoutContract"],
                "scientificStatus": "FROZEN_LOCAL_INPUT",
            }
        )
        candidates.append(candidate)
    catalog = candidate_catalog(candidates)
    circuits = logical_circuit_manifest(catalog, logical_rows)
    bundle = {
        "schemaVersion": "qf.qas30.production-candidate-bundle.v1",
        "scientificStatus": "FROZEN_LOCAL_INPUT",
        "parentBindings": {
            "formalTaskManifestSha256": PARENT_TASK_MANIFEST_SHA256,
            "canonicalIrTemplateSha256": PARENT_CANONICAL_IR_TEMPLATE_SHA256,
            "canonicalIrArtifactSha256": PARENT_CANONICAL_IR_ARTIFACT_SHA256,
            "canonicalIrSha256": PARENT_CANONICAL_IR_SHA256,
            "deterministicInputsArtifactSha256": PARENT_DETERMINISTIC_INPUTS_SHA256,
            "market799CsvSha256": MARKET_799_CSV_SHA256,
        },
        "parameterGenerator": {
            "algorithm": "numpy.random.PCG64",
            "seed": PRODUCTION_PCG64_SEED,
            "precisionDecimalPlaces": 10,
            "initializationSeeds": initialization_seeds,
        },
        "candidateCatalog": catalog,
        "canonicalIrArtifacts": canonical_ir_artifacts,
        "logicalCircuits": circuits,
    }
    bundle["bundleSha256"] = _canonical_sha256(bundle)
    return bundle


def validate_production_candidate_bundle(
    bundle: Mapping[str, Any],
    *,
    parent_manifest_bytes: bytes,
    parent_canonical_ir_bytes: bytes,
    market_data_sha256: str,
) -> dict[str, Any]:
    """Rebuild the complete production bundle from the frozen parent entities.

    No catalog status, embedded hash, Canonical IR text, or QCIS supplied by the
    bundle is treated as authority.  The function deterministically regenerates
    all of them from the two parent artifacts and compares the complete value.
    """

    if not isinstance(bundle, Mapping):
        raise Qas30ProtocolError("production candidate bundle must be an object")
    required = {
        "schemaVersion",
        "scientificStatus",
        "parentBindings",
        "parameterGenerator",
        "candidateCatalog",
        "canonicalIrArtifacts",
        "logicalCircuits",
        "bundleSha256",
    }
    if set(bundle) != required:
        raise Qas30ProtocolError("production candidate bundle fields changed")
    supplied = dict(bundle)
    stored_sha = supplied.get("bundleSha256")
    unhashed = {key: value for key, value in supplied.items() if key != "bundleSha256"}
    if stored_sha != _canonical_sha256(unhashed):
        raise Qas30ProtocolError("production candidate bundle hash changed")
    expected = production_candidate_bundle(
        parent_manifest_bytes=parent_manifest_bytes,
        parent_canonical_ir_bytes=parent_canonical_ir_bytes,
        market_data_sha256=market_data_sha256,
    )
    if _canonical_json(supplied) != _canonical_json(expected):
        raise Qas30ProtocolError(
            "production candidate bundle does not derive from the frozen parent entities"
        )
    artifact_by_id = {
        row["candidateId"]: row for row in expected["canonicalIrArtifacts"]
    }
    for circuit in expected["logicalCircuits"]:
        artifact = artifact_by_id[circuit["candidateId"]]
        ir = artifact["canonicalIr"]
        if (
            artifact["canonicalIrJson"] != _canonical_json(ir)
            or artifact["canonicalIrSha256"]
            != hashlib.sha256(artifact["canonicalIrJson"].encode("utf-8")).hexdigest()
            or artifact["parameterSha256"] != _canonical_sha256(ir["parameters"])
            or circuit["canonicalIrSha256"] != artifact["canonicalIrSha256"]
            or circuit["canonicalIrArtifactSha256"]
            != hashlib.sha256(artifact["canonicalIrJson"].encode("utf-8")).hexdigest()
            or circuit["qcis"] != _qcis_from_ir(ir)
            or circuit["qcisSha256"]
            != hashlib.sha256(circuit["qcis"].encode("utf-8")).hexdigest()
            or ir["parentBindings"] != expected["parentBindings"]
        ):
            raise Qas30ProtocolError("production Canonical IR/QCIS binding changed")
    return expected


def candidate_catalog(
    frozen_candidate_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate the 30-row L/R/A/M/I/readout design without inventing parameters."""

    if (
        isinstance(frozen_candidate_rows, str | bytes)
        or len(frozen_candidate_rows) != len(CANDIDATE_IDS)
    ):
        raise Qas30ProtocolError("candidate catalog requires exactly 30 rows")
    required = {
        "schemaVersion",
        "candidateId",
        "messageLayers",
        "reuploadCount",
        "rotationAxis",
        "messageGate",
        "initialization",
        "readout",
        "measurementImplementation",
        "analyticalExtension",
        "analyticalExtensionSparseEdges",
        "scientificStatus",
    }
    rows: list[dict[str, Any]] = []
    identities: set[str] = set()
    factor_tuples: set[tuple[Any, ...]] = set()
    for row in frozen_candidate_rows:
        allowed = (required, required | {"candidateSpecSha256"})
        if not isinstance(row, Mapping) or set(row) not in allowed:
            raise Qas30ProtocolError("candidate row fields do not match the contract")
        candidate_id = row.get("candidateId")
        if candidate_id not in CANDIDATE_IDS or candidate_id in identities:
            raise Qas30ProtocolError("candidate identity is invalid")
        if row.get("schemaVersion") != "qf.qas30.candidate-spec.v1":
            raise Qas30ProtocolError("candidate schema version is invalid")
        if row.get("scientificStatus") not in {
            "FROZEN_LOCAL_INPUT",
            "PROTOCOL_IMPLEMENTATION_FIXTURE",
        }:
            raise Qas30ProtocolError("candidate scientific status is invalid")
        message_layers = row.get("messageLayers")
        reupload_count = row.get("reuploadCount")
        if (
            not isinstance(message_layers, int)
            or isinstance(message_layers, bool)
            or message_layers < 1
            or not isinstance(reupload_count, int)
            or isinstance(reupload_count, bool)
            or reupload_count < 0
        ):
            raise Qas30ProtocolError("candidate numeric factor is invalid")
        rotation_axis = row.get("rotationAxis")
        if rotation_axis not in {"RX", "RY", "RZ"}:
            raise Qas30ProtocolError("candidate rotation axis is invalid")
        text_factors = [
            row.get(name)
            for name in (
                "messageGate",
                "initialization",
                "readout",
                "measurementImplementation",
                "analyticalExtension",
            )
        ]
        if any(not isinstance(value, str) or not value for value in text_factors):
            raise Qas30ProtocolError("candidate categorical factor is invalid")
        if row.get("readout") != READOUT_CONTRACT:
            raise Qas30ProtocolError("candidate shared readout contract is invalid")
        if row.get("measurementImplementation") != MEASUREMENT_IMPLEMENTATION:
            raise Qas30ProtocolError("candidate measurement implementation is invalid")
        if row.get("analyticalExtension") != ANALYTICAL_EXTENSION:
            raise Qas30ProtocolError("candidate analytical extension is invalid")
        if row.get("analyticalExtensionSparseEdges") != [
            list(edge) for edge in ANALYTICAL_ZZ_SPARSE_EDGES
        ]:
            raise Qas30ProtocolError("candidate analytical sparse ZZ edges are invalid")
        factor_tuple = (
            message_layers,
            reupload_count,
            rotation_axis,
            *text_factors,
        )
        if factor_tuple in factor_tuples:
            raise Qas30ProtocolError("candidate factor rows must be unique")
        identities.add(str(candidate_id))
        factor_tuples.add(factor_tuple)
        value = {name: row[name] for name in required}
        candidate_spec_sha256 = _canonical_sha256(value)
        if "candidateSpecSha256" in row and row.get(
            "candidateSpecSha256"
        ) != candidate_spec_sha256:
            raise Qas30ProtocolError("candidate specification hash changed")
        value["candidateSpecSha256"] = candidate_spec_sha256
        rows.append(value)
    if identities != set(CANDIDATE_IDS):
        raise Qas30ProtocolError("candidate catalog does not cover C01-C30")
    return sorted(rows, key=lambda row: CANDIDATE_IDS.index(row["candidateId"]))


def logical_circuit_manifest(
    catalog: Sequence[Mapping[str, Any]],
    frozen_ir_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate evidence-bound logical circuits before declaring K0 readiness."""

    if isinstance(frozen_ir_rows, str | bytes) or len(frozen_ir_rows) != 30:
        raise Qas30ProtocolError("READY_FOR_K0 requires exactly 30 frozen IR rows")
    catalog_by_id = {str(row["candidateId"]): row for row in catalog}
    logical_order = [f"Q{qubit}" for qubit in range(6)]
    required = {
        "schemaVersion",
        "candidateId",
        "candidateSpecSha256",
        "canonicalIrSha256",
        "canonicalIrArtifactSha256",
        "parentModelSha256",
        "qcis",
        "qcisSha256",
        "logicalMeasurementOrder",
        "readoutContract",
        "scientificStatus",
    }
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in frozen_ir_rows:
        if not isinstance(row, Mapping) or set(row) != required:
            raise Qas30ProtocolError("frozen IR row fields do not match the contract")
        candidate_id = row.get("candidateId")
        if candidate_id not in catalog_by_id or candidate_id in seen:
            raise Qas30ProtocolError("frozen IR candidate identity is invalid")
        if row.get("schemaVersion") != "qf.qas30.logical-circuit-input.v1":
            raise Qas30ProtocolError("frozen IR schema version is invalid")
        if row.get("scientificStatus") not in {
            "FROZEN_LOCAL_INPUT",
            "PROTOCOL_IMPLEMENTATION_FIXTURE",
        }:
            raise Qas30ProtocolError("frozen IR scientific status is invalid")
        if row.get("candidateSpecSha256") != catalog_by_id[str(candidate_id)].get(
            "candidateSpecSha256"
        ):
            raise Qas30ProtocolError("frozen IR candidate specification hash changed")
        for name in (
            "canonicalIrSha256",
            "canonicalIrArtifactSha256",
            "parentModelSha256",
            "qcisSha256",
        ):
            if not _valid_sha256(row.get(name)):
                raise Qas30ProtocolError(f"frozen IR {name} is invalid")
        qcis = row.get("qcis")
        if not isinstance(qcis, str) or not qcis.strip():
            raise Qas30ProtocolError("frozen IR QCIS is empty")
        if hashlib.sha256(qcis.encode("utf-8")).hexdigest() != row.get("qcisSha256"):
            raise Qas30ProtocolError("frozen IR QCIS hash changed")
        if row.get("logicalMeasurementOrder") != logical_order:
            raise Qas30ProtocolError("frozen IR logical measurement order changed")
        readout = row["readoutContract"]
        if (
            not isinstance(readout, Mapping)
            or readout.get("function") != "max(0,scale*Z_i+bias)"
            or readout.get("quantumInputs") != ["Z_i"]
            or readout.get("measurementImplementation")
            != MEASUREMENT_IMPLEMENTATION
            or readout.get("zDerivedFromComputationalBasisCounts") is not True
            or "ZZ" in _canonical_json(dict(readout))
        ):
            raise Qas30ProtocolError("frozen IR shared readout contract changed")
        measured = [
            parts[1]
            for line in qcis.splitlines()
            if (parts := line.strip().split()) and parts[0] == "M" and len(parts) == 2
        ]
        if measured != logical_order:
            raise Qas30ProtocolError("frozen IR QCIS measurement order changed")
        seen.add(str(candidate_id))
        validated.append(dict(row))
    if seen != set(CANDIDATE_IDS):
        raise Qas30ProtocolError("frozen IR rows do not cover the candidate pool")
    return sorted(validated, key=lambda row: CANDIDATE_IDS.index(row["candidateId"]))


def mapping_plan_manifest() -> dict[str, Any]:
    """Return the frozen K0-dependent common-order mapping plan."""

    return {
        "schemaVersion": "qf.qas30.mapping-plan.v1",
        "strategy": "BATCH_COMMON_MEASUREMENT_ORDER_DEPTH_V1",
        "mappingSeeds": list(range(10)),
        "batchSize": BATCH_SIZE,
        "logicalWidth": 6,
        "requirements": {
            "regularCircuitCount": 5,
            "commonPhysicalMeasurementSet": True,
            "commonPhysicalMeasurementOrder": True,
            "bindQcisSha256": True,
            "bindK0ConfigSha256": True,
            "bindMappingSnapshotSha256": True,
            "bindSourceCommit": True,
        },
        "infeasibleState": "QAS30_BATCH_MAPPING_INFEASIBLE",
    }


def statistical_plan_manifest() -> dict[str, Any]:
    """Return the frozen primary endpoint and multiplicity plan."""

    return {
        "schemaVersion": "qf.qas30.statistics-plan.v1",
        "primaryEndpoint": "MEDIAN_S_HW_ARM_DIFFERENCE",
        "primaryComparisons": ["P01", "P02", "P03", "P04"],
        "hardwareScoreComponents": list(HARDWARE_SCORE_COMPONENTS),
        "fallbackWeights": {
            name: 1.0 / len(HARDWARE_SCORE_COMPONENTS)
            for name in HARDWARE_SCORE_COMPONENTS
        },
        "inference": {
            "pairingUnit": "SAME_REPLICATION_BLOCK",
            "permutation": "EXACT_COMPLETE_PAIRED_SIGN_FLIP",
            "bootstrap": (
                "PAIRED_BLOCK_INDEPENDENT_ARM_CANDIDATE_AND_SHOT_RESAMPLE"
            ),
            "bootstrapReplicates": 10_000,
            "multiplicity": "HOLM_FOUR_CONFIRMATORY_COMPARISONS",
        },
        "weightsFrozenBeforeLiveResults": True,
    }


def exact_median_permutation_test(
    first: Sequence[float], second: Sequence[float]
) -> dict[str, Any]:
    """Compute the complete paired sign-flip test and retain extreme/total."""

    first_values = np.asarray(first, dtype=float)
    second_values = np.asarray(second, dtype=float)
    if (
        len(first_values) == 0
        or len(first_values) != len(second_values)
        or not np.isfinite(first_values).all()
        or not np.isfinite(second_values).all()
    ):
        raise Qas30ProtocolError(
            "paired sign-flip inputs must be finite, non-empty, and equally sized"
        )
    paired_differences = first_values - second_values
    observed_signed = float(np.median(paired_differences))
    observed = abs(observed_signed)
    magnitudes = np.sort(np.abs(paired_differences))
    count = len(magnitudes)
    epsilon = np.finfo(float).eps * max(1.0, observed)
    extreme = 0
    total = 0
    if count % 2 == 1:
        half = count // 2
        for positive_excess in range(1, half + 2):
            for index, magnitude in enumerate(magnitudes):
                weight = (
                    2
                    * math.comb(index, positive_excess - 1)
                    * math.comb(count - index - 1, half)
                )
                total += weight
                if magnitude >= observed - epsilon:
                    extreme += weight
    else:
        half = count // 2
        for positive_excess in range(1, half + 1):
            for first_index in range(count):
                for second_index in range(first_index + 1, count):
                    weight = (
                        2
                        * math.comb(first_index, positive_excess - 1)
                        * math.comb(count - second_index - 1, half - 1)
                    )
                    total += weight
                    statistic = (
                        magnitudes[first_index] + magnitudes[second_index]
                    ) / 2.0
                    if statistic >= observed - epsilon:
                        extreme += weight
        for second_index in range(1, count):
            weight = 2 * math.comb(count - second_index - 1, half - 1)
            total += weight
            statistic = (magnitudes[second_index] - magnitudes[0]) / 2.0
            if statistic >= observed - epsilon:
                extreme += weight
    if total != 2**count:
        raise Qas30ProtocolError("paired sign-flip combinatorial count is inconsistent")
    return {
        "schemaVersion": "qf.qas30.exact-paired-sign-flip.v1",
        "method": "COMPLETE_ENUMERATION_PAIRED_SIGN_FLIP",
        "pairCount": len(paired_differences),
        "statistic": "ABSOLUTE_MEDIAN_PAIRED_DIFFERENCE",
        "observedSigned": observed_signed,
        "observed": observed,
        "extreme": extreme,
        "total": total,
        "pValue": extreme / total,
    }


def exact_median_permutation_pvalue(first: Sequence[float], second: Sequence[float]) -> float:
    """Compatibility wrapper for the frozen exact permutation p-value."""

    return float(exact_median_permutation_test(first, second)["pValue"])


def holm_adjust(p_values: Mapping[str, float]) -> dict[str, float]:
    """Apply the frozen Holm family correction with monotone adjusted values."""

    if not p_values:
        raise Qas30ProtocolError("Holm correction requires at least one p-value")
    values = {name: float(value) for name, value in p_values.items()}
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in values.values()):
        raise Qas30ProtocolError("Holm p-values must be finite probabilities")
    ordered = sorted(values, key=lambda name: (values[name], name))
    adjusted: dict[str, float] = {}
    running = 0.0
    count = len(ordered)
    for rank, name in enumerate(ordered):
        running = max(running, min(1.0, (count - rank) * values[name]))
        adjusted[name] = running
    return adjusted


def nested_bootstrap_median_difference(
    first: Sequence[Sequence[Sequence[float]]],
    second: Sequence[Sequence[Sequence[float]]],
    *,
    seed_material: str,
    replicates: int = 10_000,
) -> dict[str, Any]:
    """Resample paired blocks with independent nested arm-median draws.

    Each outer row is one block, containing that arm's five candidates, each of
    which contains shot-level score draws.  Only block indices are paired across
    arms.  Candidate and shot resampling is independent within each arm.
    """

    if not 100 <= replicates <= 100_000:
        raise Qas30ProtocolError("bootstrap replicate count is outside the frozen bounds")

    def matrix(
        rows: Sequence[Sequence[Sequence[float]]], label: str
    ) -> list[list[np.ndarray]]:
        result = [
            [np.asarray(candidate, dtype=float) for candidate in block]
            for block in rows
        ]
        if (
            not result
            or any(len(block) != BATCH_SIZE for block in result)
            or any(
                len(candidate) == 0 or not np.isfinite(candidate).all()
                for block in result
                for candidate in block
            )
        ):
            raise Qas30ProtocolError(
                f"bootstrap {label} requires five finite candidate shot rows per block"
            )
        return result

    first_rows = matrix(first, "first")
    second_rows = matrix(second, "second")
    if len(first_rows) != len(second_rows):
        raise Qas30ProtocolError(
            "paired bootstrap arms must cover the same number of blocks"
        )
    seed_sha256 = hashlib.sha256(seed_material.encode("utf-8")).hexdigest()
    generator = np.random.default_rng(int(seed_sha256[:16], 16))

    def resampled_arm_median(block: list[np.ndarray]) -> float:
        selected_candidates = generator.integers(
            0, len(block), size=len(block)
        )
        candidate_scores: list[float] = []
        for candidate_index in selected_candidates:
            candidate = block[int(candidate_index)]
            shot_indices = generator.integers(
                0, len(candidate), size=len(candidate)
            )
            candidate_scores.append(float(np.mean(candidate[shot_indices])))
        return float(np.median(candidate_scores))

    def observed_arm_median(block: list[np.ndarray]) -> float:
        return float(np.median([np.mean(candidate) for candidate in block]))

    shot_lengths = {
        len(candidate)
        for rows in (first_rows, second_rows)
        for block in rows
        for candidate in block
    }
    draws: list[float] = []
    dense_shots = len(shot_lengths) == 1 and all(
        candidate.ndim == 1
        for rows in (first_rows, second_rows)
        for block in rows
        for candidate in block
    )
    if dense_shots:
        # Keep the large temporary arrays bounded.  A chunk contains only a
        # handful of outer replicates; its shape is (chunk, block, candidate,
        # shot), rather than all 10,000 replicates at once.
        shot_count = shot_lengths.pop()
        first_matrix = np.stack(first_rows, axis=0)
        second_matrix = np.stack(second_rows, axis=0)
        first_flat = first_matrix.reshape(-1, shot_count)
        second_flat = second_matrix.reshape(-1, shot_count)
        chunk_size = 4
        block_count = len(first_rows)

        def resampled_chunk_medians(
            matrix: np.ndarray, selected_blocks: np.ndarray
        ) -> np.ndarray:
            chunk_count = len(selected_blocks)
            selected_candidates = generator.integers(
                0,
                BATCH_SIZE,
                size=(chunk_count, block_count, BATCH_SIZE),
            )
            shot_indices = generator.integers(
                0,
                shot_count,
                size=(chunk_count, block_count, BATCH_SIZE, shot_count),
            )
            linear_candidates = (
                selected_blocks[:, :, None] * BATCH_SIZE + selected_candidates
            )
            sampled = matrix[linear_candidates[..., None], shot_indices]
            candidate_means = sampled.mean(axis=-1)
            return np.median(candidate_means, axis=-1)

        for start in range(0, replicates, chunk_size):
            chunk_count = min(chunk_size, replicates - start)
            selected = generator.integers(
                0, block_count, size=(chunk_count, block_count)
            )
            pair_differences = (
                resampled_chunk_medians(first_flat, selected)
                - resampled_chunk_medians(second_flat, selected)
            )
            draws.extend(np.median(pair_differences, axis=1).tolist())
    else:
        # Variable shot lengths cannot be represented by one dense chunk
        # without padding (which would alter the resampling distribution).
        # Keep the original ragged implementation for this safe fallback.
        for _ in range(replicates):
            selected = generator.integers(0, len(first_rows), size=len(first_rows))
            pair_differences: list[float] = []
            for index in selected:
                first_samples = first_rows[int(index)]
                second_samples = second_rows[int(index)]
                pair_differences.append(
                    resampled_arm_median(first_samples)
                    - resampled_arm_median(second_samples)
                )
            draws.append(float(np.median(pair_differences)))
    draw_array = np.asarray(draws, dtype=float)
    lower, upper = np.quantile(draw_array, [0.025, 0.975])
    point = float(
        np.median(
            [
                observed_arm_median(first_row) - observed_arm_median(second_row)
                for first_row, second_row in zip(
                    first_rows, second_rows, strict=True
                )
            ]
        )
    )
    return {
        "schemaVersion": "qf.qas30.paired-bootstrap.v1",
        "method": "PAIRED_BLOCK_INDEPENDENT_ARM_CANDIDATE_AND_SHOT_RESAMPLE",
        "pairCount": len(first_rows),
        "replicates": replicates,
        "seedSha256": seed_sha256,
        "medianDifference": point,
        "confidenceInterval95": [float(lower), float(upper)],
        "confidenceIntervalHalfWidth": float((upper - lower) / 2.0),
    }


def freeze_normalization_bounds(
    pre_result_rows: Sequence[Mapping[str, float]],
) -> dict[str, Any]:
    """Freeze seven-component min/max boundaries before live results are visible."""

    if isinstance(pre_result_rows, str | bytes) or not pre_result_rows:
        raise Qas30ProtocolError("normalization requires pre-result rows")
    bounds: dict[str, dict[str, float]] = {}
    for name in HARDWARE_SCORE_COMPONENTS:
        values = np.asarray([float(row[name]) for row in pre_result_rows], dtype=float)
        if not np.isfinite(values).all():
            raise Qas30ProtocolError(f"normalization component {name} is non-finite")
        lower = float(values.min())
        upper = float(values.max())
        if math.isclose(lower, upper, abs_tol=0.0):
            raise Qas30ProtocolError(f"normalization component {name} has zero span")
        bounds[name] = {"lower": lower, "upper": upper}
    manifest = {
        "schemaVersion": "qf.qas30.normalization-freeze.v1",
        "components": bounds,
        "clipRange": [0.0, 1.0],
        "frozenBeforeLiveResults": True,
    }
    manifest["normalizationSha256"] = _canonical_sha256(manifest)
    return manifest


def normalize_hardware_components(
    components: Mapping[str, float],
    normalization_manifest: Mapping[str, Any],
) -> dict[str, float]:
    """Apply frozen boundaries and the preregistered [0,1] clipping rule."""

    if set(components) != set(HARDWARE_SCORE_COMPONENTS):
        raise Qas30ProtocolError("normalization requires the seven frozen components")
    if normalization_manifest.get("schemaVersion") != "qf.qas30.normalization-freeze.v1":
        raise Qas30ProtocolError("normalization manifest schema is invalid")
    stored_sha = normalization_manifest.get("normalizationSha256")
    unhashed = {
        key: value
        for key, value in normalization_manifest.items()
        if key != "normalizationSha256"
    }
    if stored_sha != _canonical_sha256(unhashed):
        raise Qas30ProtocolError("normalization manifest hash changed")
    bounds = normalization_manifest.get("components")
    if not isinstance(bounds, Mapping) or set(bounds) != set(HARDWARE_SCORE_COMPONENTS):
        raise Qas30ProtocolError("normalization bounds are incomplete")
    normalized: dict[str, float] = {}
    for name in HARDWARE_SCORE_COMPONENTS:
        row = bounds[name]
        if not isinstance(row, Mapping) or set(row) != {"lower", "upper"}:
            raise Qas30ProtocolError(f"normalization bound {name} is invalid")
        lower, upper = float(row["lower"]), float(row["upper"])
        value = float(components[name])
        if not all(math.isfinite(item) for item in (lower, upper, value)) or upper <= lower:
            raise Qas30ProtocolError(f"normalization bound {name} is invalid")
        normalized[name] = min(1.0, max(0.0, (value - lower) / (upper - lower)))
    return normalized


def validate_local_feature_evidence(
    evidence: Mapping[str, Any],
    *,
    production_bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Revalidate the complete deterministic local-feature evidence entity."""

    required = {
        "schemaVersion",
        "scientificStatus",
        "definitions",
        "sourceBindings",
        "featureRows",
        "candidateDetails",
        "evidenceSha256",
    }
    stored = evidence.get("evidenceSha256")
    unhashed = {key: value for key, value in evidence.items() if key != "evidenceSha256"}
    status = evidence.get("scientificStatus")
    if (
        set(evidence) != required
        or evidence.get("schemaVersion") != LOCAL_FEATURE_DEFINITION
        or status
        not in {
            "LOCAL_DERIVED_PRE_HARDWARE_FEATURES",
            "PROTOCOL_IMPLEMENTATION_FIXTURE",
        }
        or stored != _canonical_sha256(unhashed)
        or (
            status == "LOCAL_DERIVED_PRE_HARDWARE_FEATURES"
            and stored != EXPECTED_LOCAL_FEATURE_EVIDENCE_SHA256
        )
    ):
        raise Qas30ProtocolError("local feature evidence identity or hash changed")
    bundle_sha = production_bundle.get("bundleSha256")
    bundle_unhashed = {
        key: value
        for key, value in production_bundle.items()
        if key != "bundleSha256"
    }
    bindings = evidence.get("sourceBindings")
    if (
        not isinstance(bindings, Mapping)
        or set(bindings)
        != {
            "productionBundleSha256",
            "parentTaskManifestSha256",
            "deterministicInputsArtifactSha256",
            "parentCanonicalIrArtifactSha256s",
            "market799ParentLineageSha256",
            "market799Role",
            "directComputationInputs",
        }
        or bundle_sha != _canonical_sha256(bundle_unhashed)
        or bindings.get("productionBundleSha256") != bundle_sha
        or bindings.get("parentTaskManifestSha256") != PARENT_TASK_MANIFEST_SHA256
        or bindings.get("deterministicInputsArtifactSha256")
        != PARENT_DETERMINISTIC_INPUTS_SHA256
        or bindings.get("market799ParentLineageSha256") != MARKET_799_CSV_SHA256
        or bindings.get("market799Role") != "PARENT_LINEAGE_ONLY"
        or bindings.get("directComputationInputs")
        != ["E02_PARENT_CANONICAL_IR_X5", "E02_DETERMINISTIC_LABELS_X5"]
        or not isinstance(bindings.get("parentCanonicalIrArtifactSha256s"), list)
        or len(bindings["parentCanonicalIrArtifactSha256s"]) != 5
        or len(set(bindings["parentCanonicalIrArtifactSha256s"])) != 5
        or not all(
            _valid_sha256(value)
            for value in bindings["parentCanonicalIrArtifactSha256s"]
        )
        or PARENT_CANONICAL_IR_ARTIFACT_SHA256
        not in bindings["parentCanonicalIrArtifactSha256s"]
    ):
        raise Qas30ProtocolError("local feature evidence source binding changed")
    definitions = evidence.get("definitions")
    degradation = (
        definitions.get("quboObjectiveDegradation")
        if isinstance(definitions, Mapping)
        else None
    )
    if (
        not isinstance(definitions, Mapping)
        or set(definitions) != {"localValidationLoss", "quboObjectiveDegradation"}
        or not isinstance(degradation, Mapping)
        or degradation.get("scope")
        != "LOCAL_E02_NODE_SPILLOVER_ARCHITECTURE_PRESELECTION_SURROGATE"
        or degradation.get("downstreamUse") != "K0_PRE_HARDWARE_QAS_FEATURE"
    ):
        raise Qas30ProtocolError("local feature definitions changed")
    feature_rows = evidence.get("featureRows")
    details = evidence.get("candidateDetails")
    catalog = {
        row.get("candidateId"): row
        for row in production_bundle.get("candidateCatalog", [])
        if isinstance(row, Mapping)
    }
    artifacts = {
        row.get("candidateId"): row
        for row in production_bundle.get("canonicalIrArtifacts", [])
        if isinstance(row, Mapping)
    }
    if (
        not isinstance(feature_rows, list)
        or not isinstance(details, list)
        or len(feature_rows) != 30
        or len(details) != 30
        or set(catalog) != set(CANDIDATE_IDS)
        or set(artifacts) != set(CANDIDATE_IDS)
    ):
        raise Qas30ProtocolError("local feature evidence candidate rows are incomplete")
    validated_rows: list[dict[str, Any]] = []
    for candidate_id, row, detail in zip(
        CANDIDATE_IDS, feature_rows, details, strict=True
    ):
        if (
            not isinstance(row, Mapping)
            or set(row)
            != {"candidateId", "localValidationLoss", "quboObjectiveDegradation"}
            or row.get("candidateId") != candidate_id
            or not isinstance(detail, Mapping)
            or detail.get("candidateId") != candidate_id
            or detail.get("candidateSpecSha256")
            != catalog[candidate_id].get("candidateSpecSha256")
            or detail.get("parameterSha256")
            != artifacts[candidate_id].get("parameterSha256")
            or detail.get("localValidationLoss") != row.get("localValidationLoss")
            or detail.get("quboObjectiveDegradation")
            != row.get("quboObjectiveDegradation")
            or detail.get("validationUnitCount") != 30
            or detail.get("quboAnchorCount") != 5
            or not isinstance(detail.get("anchors"), list)
            or len(detail["anchors"]) != 5
        ):
            raise Qas30ProtocolError("local feature evidence candidate binding changed")
        try:
            values = {
                "localValidationLoss": float(row["localValidationLoss"]),
                "quboObjectiveDegradation": float(row["quboObjectiveDegradation"]),
            }
        except (TypeError, ValueError) as error:
            raise Qas30ProtocolError("local feature evidence value is invalid") from error
        if not all(math.isfinite(value) and value >= 0.0 for value in values.values()):
            raise Qas30ProtocolError("local feature evidence value is invalid")
        validated_rows.append({"candidateId": candidate_id, "features": values})
    result = dict(evidence)
    result["validatedPreHardwareRows"] = validated_rows
    return result


def freeze_pre_hardware_predictor_features(
    *,
    source_commit: str,
    local_feature_evidence: Mapping[str, Any],
    production_bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze two predictor inputs only after complete evidence revalidation."""

    if not isinstance(source_commit, str) or len(source_commit) != 40 or any(
        character not in "0123456789abcdef" for character in source_commit
    ):
        raise Qas30ProtocolError("pre-hardware feature source commit is invalid")
    verified = validate_local_feature_evidence(
        local_feature_evidence,
        production_bundle=production_bundle,
    )
    rows = verified.pop("validatedPreHardwareRows")
    manifest = {
        "schemaVersion": "qf.qas30.pre-hardware-feature-freeze.v1",
        "state": "FROZEN_BEFORE_ANY_HARDWARE_RESULT",
        "sourceCommitSha": source_commit,
        "candidateBundleSha256": production_bundle["bundleSha256"],
        "localFeatureEvidenceSha256": verified["evidenceSha256"],
        "localFeatureDefinition": verified["schemaVersion"],
        "localFeatureScientificStatus": verified["scientificStatus"],
        "hardwareResultsVisibleAtFreeze": False,
        "rows": rows,
    }
    manifest["preHardwareFeatureFreezeSha256"] = _canonical_sha256(manifest)
    return manifest


def validate_pre_hardware_predictor_feature_freeze(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the complete immutable two-feature entity and return a copy."""

    required = {
        "schemaVersion",
        "state",
        "sourceCommitSha",
        "candidateBundleSha256",
        "localFeatureEvidenceSha256",
        "localFeatureDefinition",
        "localFeatureScientificStatus",
        "hardwareResultsVisibleAtFreeze",
        "rows",
        "preHardwareFeatureFreezeSha256",
    }
    stored = manifest.get("preHardwareFeatureFreezeSha256")
    unhashed = {
        key: value
        for key, value in manifest.items()
        if key != "preHardwareFeatureFreezeSha256"
    }
    source_commit = manifest.get("sourceCommitSha")
    rows = manifest.get("rows")
    if (
        set(manifest) != required
        or manifest.get("schemaVersion")
        != "qf.qas30.pre-hardware-feature-freeze.v1"
        or manifest.get("state") != "FROZEN_BEFORE_ANY_HARDWARE_RESULT"
        or manifest.get("hardwareResultsVisibleAtFreeze") is not False
        or not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
        or not _valid_sha256(manifest.get("localFeatureEvidenceSha256"))
        or manifest.get("localFeatureDefinition") != LOCAL_FEATURE_DEFINITION
        or manifest.get("localFeatureScientificStatus")
        not in {
            "LOCAL_DERIVED_PRE_HARDWARE_FEATURES",
            "PROTOCOL_IMPLEMENTATION_FIXTURE",
        }
        or not _valid_sha256(manifest.get("candidateBundleSha256"))
        or stored != _canonical_sha256(unhashed)
        or not isinstance(rows, list)
        or len(rows) != len(CANDIDATE_IDS)
    ):
        raise Qas30ProtocolError("pre-hardware feature freeze is invalid")
    required_features = {"localValidationLoss", "quboObjectiveDegradation"}
    seen: set[str] = set()
    for expected_id, row in zip(CANDIDATE_IDS, rows, strict=True):
        if (
            not isinstance(row, Mapping)
            or set(row) != {"candidateId", "features"}
            or row.get("candidateId") != expected_id
            or expected_id in seen
            or not isinstance(row.get("features"), Mapping)
            or set(row["features"]) != required_features
            or any(isinstance(value, bool) for value in row["features"].values())
        ):
            raise Qas30ProtocolError("pre-hardware feature rows are invalid")
        try:
            finite_non_negative = all(
                math.isfinite(float(value)) and float(value) >= 0.0
                for value in row["features"].values()
            )
        except (TypeError, ValueError):
            finite_non_negative = False
        if not finite_non_negative:
            raise Qas30ProtocolError("pre-hardware feature rows are invalid")
        seen.add(expected_id)
    return dict(manifest)


def freeze_mapping_predictor_features(
    *,
    pre_hardware_freeze: Mapping[str, Any],
    data_epoch: str,
    freeze_manifest_sha256: str,
    mapping_feature_rows: Mapping[str, Mapping[str, float]],
    k0_config_sha256: str,
    active_calibration_sha256: str,
    mapping_snapshot_sha256: str,
    normalization_manifest: Mapping[str, Any],
    predictor_calibration_label: str = "K0",
) -> dict[str, Any]:
    """Freeze candidate-level *selection* features, never execution features.

    ``mapping_feature_rows`` is retained as the argument name for the moment so
    callers can be migrated without an ambiguous positional API.  Its contents
    are deliberately projected into ``selectionMappingFeatures`` and are not a
    claim about the five QCISs ultimately compiled for a live batch.
    """

    pre_hardware_freeze = validate_pre_hardware_predictor_feature_freeze(
        pre_hardware_freeze
    )
    pre_stored = pre_hardware_freeze["preHardwareFeatureFreezeSha256"]
    pre_rows = pre_hardware_freeze.get("rows")
    if not isinstance(pre_rows, list) or len(pre_rows) != 30:
        raise Qas30ProtocolError("pre-hardware feature rows are incomplete")
    pre_by_id = {
        row.get("candidateId"): row.get("features")
        for row in pre_rows
        if isinstance(row, Mapping)
    }
    if set(pre_by_id) != set(CANDIDATE_IDS) or set(mapping_feature_rows) != set(
        CANDIDATE_IDS
    ):
        raise Qas30ProtocolError("predictor feature rows must cover C01-C30")
    if not _valid_identity(data_epoch):
        raise Qas30ProtocolError("predictor feature dataEpoch is invalid")
    if predictor_calibration_label not in {"K0", "K1"}:
        raise Qas30ProtocolError("predictor feature calibration label is invalid")
    for sha in (
        freeze_manifest_sha256,
        k0_config_sha256,
        active_calibration_sha256,
        mapping_snapshot_sha256,
    ):
        if not _valid_sha256(sha):
            raise Qas30ProtocolError("predictor feature K/mapping hash is invalid")
    normalization_stored = normalization_manifest.get("normalizationSha256")
    normalization_unhashed = {
        key: value
        for key, value in normalization_manifest.items()
        if key != "normalizationSha256"
    }
    if (
        normalization_manifest.get("schemaVersion")
        != "qf.qas30.normalization-freeze.v1"
        or normalization_stored != _canonical_sha256(normalization_unhashed)
    ):
        raise Qas30ProtocolError("predictor feature normalization freeze is invalid")
    mapping_required = {
        "compiledDepth",
        "twoQubitGates",
        "swapCount",
        "calibrationNoiseProxy",
    }
    rows: list[dict[str, Any]] = []
    for candidate_id in CANDIDATE_IDS:
        pre = pre_by_id[candidate_id]
        mapping = mapping_feature_rows[candidate_id]
        if (
            not isinstance(pre, Mapping)
            or set(pre) != {"localValidationLoss", "quboObjectiveDegradation"}
            or not isinstance(mapping, Mapping)
            or set(mapping) != mapping_required
        ):
            raise Qas30ProtocolError("predictor feature row is incomplete")
        features = {**{key: float(value) for key, value in pre.items()}, **{
            key: float(value) for key, value in mapping.items()
        }}
        if not all(math.isfinite(value) for value in features.values()):
            raise Qas30ProtocolError("predictor feature row is non-finite")
        rows.append(
            {
                "candidateId": candidate_id,
                "preHardwareFeatures": {
                    key: float(value) for key, value in pre.items()
                },
                "selectionMappingFeatures": {
                    key: float(value) for key, value in mapping.items()
                },
            }
        )
    manifest = {
        "schemaVersion": "qf.qas30.predictor-feature-freeze.v2",
        "state": "FROZEN_FOR_SELECTION",
        "sourceCommitSha": pre_hardware_freeze["sourceCommitSha"],
        "dataEpoch": data_epoch,
        "freezeManifestSha256": freeze_manifest_sha256,
        "k0ConfigSha256": k0_config_sha256,
        "predictorCalibrationSha256": active_calibration_sha256,
        "predictorCalibrationLabel": predictor_calibration_label,
        "selectionFeatureSnapshotSha256": mapping_snapshot_sha256,
        "normalizationSha256": normalization_stored,
        "preHardwareFeatureFreezeSha256": pre_stored,
        "rows": rows,
    }
    manifest["predictorFeatureFreezeSha256"] = _canonical_sha256(manifest)
    return manifest


def predictor_feature_receipt(
    predictor_freeze: Mapping[str, Any], *, candidate_id: str
) -> dict[str, Any]:
    """Project one frozen selection row without binding it to actual QCIS data."""

    stored = predictor_freeze.get("predictorFeatureFreezeSha256")
    unhashed = {
        key: value
        for key, value in predictor_freeze.items()
        if key != "predictorFeatureFreezeSha256"
    }
    if (
        predictor_freeze.get("schemaVersion")
        != "qf.qas30.predictor-feature-freeze.v2"
        or predictor_freeze.get("state") != "FROZEN_FOR_SELECTION"
        or stored != _canonical_sha256(unhashed)
        or candidate_id not in CANDIDATE_IDS
    ):
        raise Qas30ProtocolError("predictor feature freeze is invalid")
    rows = predictor_freeze.get("rows")
    matching = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("candidateId") == candidate_id
    ] if isinstance(rows, list) else []
    if (
        len(matching) != 1
        or not isinstance(matching[0].get("preHardwareFeatures"), Mapping)
        or not isinstance(matching[0].get("selectionMappingFeatures"), Mapping)
    ):
        raise Qas30ProtocolError("predictor feature candidate row is missing")
    pre = dict(matching[0]["preHardwareFeatures"])
    selection_mapping = dict(matching[0]["selectionMappingFeatures"])
    pre_binding = {
        "candidateId": candidate_id,
        "sourceCommitSha": predictor_freeze["sourceCommitSha"],
        "dataEpoch": predictor_freeze["dataEpoch"],
        "freezeManifestSha256": predictor_freeze["freezeManifestSha256"],
        "features": pre,
        "frozenBeforeHardwareResults": True,
    }
    selection_binding = {
        "candidateId": candidate_id,
        "sourceCommitSha": predictor_freeze["sourceCommitSha"],
        "dataEpoch": predictor_freeze["dataEpoch"],
        "freezeManifestSha256": predictor_freeze["freezeManifestSha256"],
        "k0ConfigSha256": predictor_freeze["k0ConfigSha256"],
        "predictorCalibrationSha256": predictor_freeze["predictorCalibrationSha256"],
        "predictorCalibrationLabel": predictor_freeze["predictorCalibrationLabel"],
        "predictorFeatureFreezeSha256": stored,
        "selectionFeatureSnapshotSha256": predictor_freeze[
            "selectionFeatureSnapshotSha256"
        ],
        "features": selection_mapping,
    }
    return {
        "schemaVersion": "qf.qas30.predictor-feature-receipt.v2",
        "state": "FROZEN_FOR_SELECTION",
        "candidateId": candidate_id,
        "sourceCommitSha": predictor_freeze["sourceCommitSha"],
        "dataEpoch": predictor_freeze["dataEpoch"],
        "freezeManifestSha256": predictor_freeze["freezeManifestSha256"],
        "predictorFeatureFreezeSha256": stored,
        "preHardwareFeatureFreezeSha256": predictor_freeze[
            "preHardwareFeatureFreezeSha256"
        ],
        "k0ConfigSha256": predictor_freeze["k0ConfigSha256"],
        "predictorCalibrationSha256": predictor_freeze["predictorCalibrationSha256"],
        "predictorCalibrationLabel": predictor_freeze["predictorCalibrationLabel"],
        "selectionFeatureSnapshotSha256": predictor_freeze[
            "selectionFeatureSnapshotSha256"
        ],
        "normalizationSha256": predictor_freeze["normalizationSha256"],
        "preHardwareFeatures": pre,
        "selectionMappingFeatures": selection_mapping,
        "preHardwareFeatureSha256": _canonical_sha256(pre_binding),
        "selectionMappingFeatureSha256": _canonical_sha256(selection_binding),
        "preHardwareResultsVisibleAtFreeze": False,
        "selectionFeaturesFrozenBeforeBatchSubmit": True,
    }


def confirmatory_analysis_freeze(*, run_id: str) -> dict[str, Any]:
    """Freeze four comparisons, bootstrap, permutation and Holm before unblinding."""

    if not isinstance(run_id, str) or not run_id:
        raise Qas30ProtocolError("runId is invalid")
    manifest = {
        "schemaVersion": "qf.qas30.confirmatory-analysis-freeze.v1",
        "runId": run_id,
        "comparisons": ["P01", "P02", "P03", "P04"],
        "replicationBudgets": list(REPLICATION_BUDGETS),
        "confidenceIntervalHalfWidthThreshold": 0.05,
        "bootstrap": {
            "method": "PAIRED_BLOCK_INDEPENDENT_ARM_CANDIDATE_AND_SHOT_RESAMPLE",
            "replicates": 10_000,
            "seedDerivation": "SHA256(runId:comparison)",
        },
        "permutation": "COMPLETE_ENUMERATION_EXTREME_OVER_TOTAL",
        "multiplicity": "HOLM_FOUR_CONFIRMATORY_COMPARISONS",
        "blindLabelManifestRequired": True,
        "frozenBeforeLiveResults": True,
    }
    manifest["analysisFreezeSha256"] = _canonical_sha256(manifest)
    return manifest


def blind_label_artifacts(
    *, run_id: str, entropy: bytes | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create random opaque public codes and a separate controlled mapping."""

    if not isinstance(run_id, str) or not run_id:
        raise Qas30ProtocolError("runId is invalid")
    secret = secrets.token_bytes(32) if entropy is None else bytes(entropy)
    if len(secret) < 32:
        raise Qas30ProtocolError("blind-code entropy must contain at least 256 bits")
    true_arms = ("RIDGE", "FIXED", "RANDOM", "LLM")

    def opaque(prefix: str, domain: str) -> str:
        return f"{prefix}_{hashlib.sha256(secret + domain.encode('utf-8')).hexdigest()}"

    shuffled = [opaque("H", f"arm:{index}") for index in range(4)]
    generator = np.random.Generator(
        np.random.PCG64(int.from_bytes(secret[:16], "big"))
    )
    generator.shuffle(shuffled)
    true_to_blind = dict(zip(true_arms, shuffled, strict=True))
    blind_run_code = opaque("R", "run")
    true_comparisons = (
        ("P01", "RIDGE", "FIXED"),
        ("P02", "RIDGE", "RANDOM"),
        ("P03", "LLM", "FIXED"),
        ("P04", "LLM", "RANDOM"),
    )
    comparison_codes: list[str] = []
    comparison_mapping: dict[str, dict[str, str]] = {}
    for index, (comparison_id, first, second) in enumerate(true_comparisons):
        comparison_code = opaque("X", f"comparison:{index}")
        comparison_codes.append(comparison_code)
        comparison_mapping[comparison_code] = {
            "comparisonId": comparison_id,
            "firstTrueArm": first,
            "secondTrueArm": second,
        }
    assignment = {
        "schemaVersion": "qf.qas30.blind-comparison-assignment.v1",
        "blindRunCode": blind_run_code,
        "comparisons": sorted(
            [
                {
                    "comparisonCode": code,
                    "firstBlindCode": true_to_blind[row["firstTrueArm"]],
                    "secondBlindCode": true_to_blind[row["secondTrueArm"]],
                }
                for code, row in comparison_mapping.items()
            ],
            key=lambda row: row["comparisonCode"],
        ),
    }
    assignment["comparisonAssignmentSha256"] = _canonical_sha256(assignment)
    sealed = {
        "schemaVersion": "qf.qas30.sealed-arm-mapping.v2",
        "runId": run_id,
        "blindRunCode": blind_run_code,
        "entropySha256": hashlib.sha256(secret).hexdigest(),
        "trueToBlind": true_to_blind,
        "comparisonCodeToTrueComparison": comparison_mapping,
        "blindComparisonAssignment": assignment,
        "releaseState": "SEALED_PENDING_INDEPENDENT_REVIEW",
    }
    sealed["sealedMappingSha256"] = _canonical_sha256(sealed)
    public = {
        "schemaVersion": "qf.qas30.blind-analysis-manifest.v2",
        "blindRunCode": blind_run_code,
        "blindCodes": sorted(shuffled),
        "comparisonCodes": sorted(comparison_codes),
        "comparisonAssignmentSha256": assignment["comparisonAssignmentSha256"],
        "sealedMappingSha256": sealed["sealedMappingSha256"],
        "unblindState": "SEALED",
    }
    public["blindManifestSha256"] = _canonical_sha256(public)
    return public, sealed


def validate_sealed_blind_mapping(mapping: Mapping[str, Any]) -> None:
    """Reject missing, duplicate, or hash-modified true/blind mappings."""

    if mapping.get("schemaVersion") != "qf.qas30.sealed-arm-mapping.v2":
        raise Qas30ProtocolError("sealed arm mapping schema is invalid")
    stored_sha = mapping.get("sealedMappingSha256")
    unhashed = {key: value for key, value in mapping.items() if key != "sealedMappingSha256"}
    if stored_sha != _canonical_sha256(unhashed):
        raise Qas30ProtocolError("sealed arm mapping hash changed")
    true_to_blind = mapping.get("trueToBlind")
    if not isinstance(true_to_blind, Mapping) or set(true_to_blind) != {
        "RIDGE",
        "FIXED",
        "RANDOM",
        "LLM",
    }:
        raise Qas30ProtocolError("sealed arm mapping is missing a true arm")
    values = list(true_to_blind.values())
    if (
        len(set(values)) != 4
        or any(
            not isinstance(value, str) or not re_opaque_code(value, "H")
            for value in values
        )
    ):
        raise Qas30ProtocolError("sealed arm mapping has duplicate or missing blind labels")
    comparison_mapping = mapping.get("comparisonCodeToTrueComparison")
    if not isinstance(comparison_mapping, Mapping) or len(comparison_mapping) != 4:
        raise Qas30ProtocolError("sealed comparison mapping is incomplete")
    seen_ids: set[str] = set()
    for code, row in comparison_mapping.items():
        if (
            not isinstance(code, str)
            or not re_opaque_code(code, "X")
            or not isinstance(row, Mapping)
            or set(row) != {"comparisonId", "firstTrueArm", "secondTrueArm"}
            or row.get("comparisonId") not in {"P01", "P02", "P03", "P04"}
            or row.get("comparisonId") in seen_ids
            or row.get("firstTrueArm") not in true_to_blind
            or row.get("secondTrueArm") not in true_to_blind
            or row.get("firstTrueArm") == row.get("secondTrueArm")
        ):
            raise Qas30ProtocolError("sealed comparison mapping is invalid")
        seen_ids.add(str(row["comparisonId"]))
    if seen_ids != {"P01", "P02", "P03", "P04"}:
        raise Qas30ProtocolError("sealed comparison mapping is incomplete")
    assignment = mapping.get("blindComparisonAssignment")
    rows = assignment.get("comparisons") if isinstance(assignment, Mapping) else None
    assignment_stored = (
        assignment.get("comparisonAssignmentSha256")
        if isinstance(assignment, Mapping)
        else None
    )
    assignment_unhashed = (
        {
            key: value
            for key, value in assignment.items()
            if key != "comparisonAssignmentSha256"
        }
        if isinstance(assignment, Mapping)
        else {}
    )
    if (
        not isinstance(assignment, Mapping)
        or assignment.get("schemaVersion")
        != "qf.qas30.blind-comparison-assignment.v1"
        or assignment.get("blindRunCode") != mapping.get("blindRunCode")
        or assignment_stored != _canonical_sha256(assignment_unhashed)
        or not isinstance(rows, list)
        or len(rows) != 4
        or {row.get("comparisonCode") for row in rows if isinstance(row, Mapping)}
        != set(comparison_mapping)
        or any(
            not isinstance(row, Mapping)
            or set(row)
            != {"comparisonCode", "firstBlindCode", "secondBlindCode"}
            or row.get("firstBlindCode") not in values
            or row.get("secondBlindCode") not in values
            or row.get("firstBlindCode") == row.get("secondBlindCode")
            for row in rows
        )
    ):
        raise Qas30ProtocolError("sealed blind comparison assignment is invalid")
    reverse = {blind: true for true, blind in true_to_blind.items()}
    for row in rows:
        true_row = comparison_mapping[row["comparisonCode"]]
        if (
            reverse.get(row["firstBlindCode"]) != true_row["firstTrueArm"]
            or reverse.get(row["secondBlindCode"]) != true_row["secondTrueArm"]
        ):
            raise Qas30ProtocolError("sealed blind comparison assignment changed")
    if not re_opaque_code(mapping.get("blindRunCode"), "R"):
        raise Qas30ProtocolError("sealed blind run code is invalid")


def re_opaque_code(value: Any, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(f"{prefix}_")
        and len(value) == 66
        and _valid_sha256(value[2:])
    )


def replication_stopping_decision(
    *,
    completed_blocks: int,
    confidence_intervals: Mapping[str, Sequence[float]],
) -> dict[str, Any]:
    """Evaluate the preregistered B=10/20/30/40 four-CI stopping rule."""

    if completed_blocks not in REPLICATION_BUDGETS:
        raise Qas30ProtocolError("stopping may only be evaluated at B=10/20/30/40")
    if set(confidence_intervals) != set(CONFIRMATORY_COMPARISONS):
        raise Qas30ProtocolError("all four confirmatory intervals are required")
    half_widths: dict[str, float] = {}
    for name, interval in confidence_intervals.items():
        if isinstance(interval, str | bytes) or len(interval) != 2:
            raise Qas30ProtocolError(f"comparison interval {name} is invalid")
        lower, upper = (float(value) for value in interval)
        if not math.isfinite(lower) or not math.isfinite(upper) or upper < lower:
            raise Qas30ProtocolError(f"comparison interval {name} is invalid")
        half_widths[name] = (upper - lower) / 2.0
    precision_met = all(value <= 0.05 for value in half_widths.values())
    stop = precision_met or completed_blocks == REPLICATION_BUDGETS[-1]
    next_budget = None
    if not stop:
        next_budget = REPLICATION_BUDGETS[
            REPLICATION_BUDGETS.index(completed_blocks) + 1
        ]
    return {
        "schemaVersion": "qf.qas30.replication-stop-decision.v1",
        "completedBlocks": completed_blocks,
        "halfWidths": half_widths,
        "threshold": 0.05,
        "precisionCriterionMet": precision_met,
        "stop": stop,
        "reason": (
            "FOUR_CI_HALF_WIDTHS_MET"
            if precision_met
            else "MAXIMUM_BUDGET_REACHED"
            if completed_blocks == REPLICATION_BUDGETS[-1]
            else "CONTINUE_TO_NEXT_BUDGET"
        ),
        "nextBudget": next_budget,
    }


def six_batch_block_manifest(
    *,
    block_id: str,
    cover_candidate_ids: Sequence[str],
    r2_arms: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Freeze one 2xR1 + four-arm R2 block without inventing post-R1 choices."""

    if not isinstance(block_id, str) or not block_id or len(block_id) > 128:
        raise Qas30ProtocolError("blockId is invalid")
    if (
        isinstance(cover_candidate_ids, str | bytes)
        or len(cover_candidate_ids) != 10
        or len(set(cover_candidate_ids)) != 10
        or not set(cover_candidate_ids) <= set(CANDIDATE_IDS)
    ):
        raise Qas30ProtocolError("six-batch block requires the frozen cover10")
    batches: list[dict[str, Any]] = [
        {
            "blockId": block_id,
            "batchIndex": index,
            "batchId": f"{block_id}-B{index}",
            "stage": "R1",
            "candidateIds": list(cover_candidate_ids[(index - 1) * 5 : index * 5]),
            "shots": SHOTS_PER_CIRCUIT,
            "requiredActiveCalibration": "K0",
            "selectionState": "FROZEN_PRE_RESULT",
        }
        for index in (1, 2)
    ]
    arm_names = ("RIDGE", "FIXED", "RANDOM", "LLM")
    if r2_arms is not None and set(r2_arms) != set(arm_names):
        raise Qas30ProtocolError("R2 block must contain Ridge/fixed/random/LLM")
    unseen = set(CANDIDATE_IDS) - set(cover_candidate_ids)
    for index, arm in enumerate(arm_names, start=3):
        selected: list[str] | None = None
        if r2_arms is not None:
            values = r2_arms[arm]
            if (
                isinstance(values, str | bytes)
                or len(values) != 5
                or len(set(values)) != 5
                or not set(values) <= unseen
            ):
                raise Qas30ProtocolError(f"R2 {arm} arm is invalid")
            selected = list(values)
        batches.append(
            {
                "blockId": block_id,
                "batchIndex": index,
                "batchId": f"{block_id}-B{index}",
                "stage": f"R2_{arm}",
                "candidateIds": selected,
                "shots": SHOTS_PER_CIRCUIT,
                "requiredActiveCalibration": "K1",
                "selectionState": (
                    "FROZEN_POST_R1" if selected is not None else "AWAITING_R1_RESULTS"
                ),
            }
        )
    return {
        "schemaVersion": "qf.qas30.six-batch-block.v1",
        "blockId": block_id,
        "batches": batches,
        "calibrationSequence": [
            {"label": "K0", "position": "BEFORE_BATCH_1"},
            {"label": "K1", "position": "BEFORE_BATCH_3"},
            {"label": "K2", "position": "AFTER_BATCH_6"},
        ],
        "budget": {"calls": 6, "circuits": 30, "shots": 30_000},
    }


def state_machine_manifest() -> dict[str, Any]:
    """Return the batch lifecycle, including sticky identity states."""

    return {
        "schemaVersion": "qf.qas30.state-machine.v1",
        "initialState": "DESIGN_FROZEN",
        "localTerminalState": LOCAL_READY_STATE,
        "allowedTransitions": {
            state: list(targets) for state, targets in STATE_TRANSITIONS.items()
        },
        "stickyStates": [
            "PARTIAL_UNKNOWN",
            "UNKNOWN_PROVIDER_OUTCOME",
            "IDENTITY_MISMATCH",
        ],
        "blockContract": {
            "batchCount": 6,
            "maximumConcurrentNonterminalBatches": 1,
            "orderedStages": [
                "R1",
                "R1",
                "R2_RIDGE",
                "R2_FIXED",
                "R2_RANDOM",
                "R2_LLM",
            ],
            "calibrationSchedule": {
                "K0": "BEFORE_BATCH_1",
                "K1": "BEFORE_BATCH_3",
                "K2": "AFTER_BATCH_6",
            },
            "queryScheduleSeconds": [60, 120, 300],
            "recoverableQueryAfterHours": 24,
            "writeStopAfterDays": 7,
        },
    }


def validate_state_transition(current: str, requested: str) -> None:
    """Reject transitions outside the frozen QAS30 lifecycle."""

    if requested not in STATE_TRANSITIONS.get(current, ()):
        raise Qas30ProtocolError(f"state transition {current} -> {requested} is invalid")


def select_cover10(catalog: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Select the deterministic maximin-Gower cover10 from a frozen catalog."""

    if len(catalog) != 30 or {row.get("candidateId") for row in catalog} != set(
        CANDIDATE_IDS
    ):
        raise Qas30ProtocolError("cover10 requires a complete candidate catalog")
    ordered = sorted(catalog, key=lambda row: CANDIDATE_IDS.index(str(row["candidateId"])))
    numeric_names = ("messageLayers", "reuploadCount")
    categorical_names = ("rotationAxis", "messageGate", "initialization", "readout")
    numeric_ranges = {}
    for name in numeric_names:
        values = [float(row[name]) for row in ordered]
        numeric_ranges[name] = max(values) - min(values)

    def distance(first: Mapping[str, Any], second: Mapping[str, Any]) -> float:
        components = []
        for name in numeric_names:
            span = numeric_ranges[name]
            difference = abs(float(first[name]) - float(second[name]))
            components.append(0.0 if span == 0.0 else difference / span)
        components.extend(
            0.0 if first[name] == second[name] else 1.0 for name in categorical_names
        )
        return float(sum(components) / len(components))

    pairwise = {
        (str(first["candidateId"]), str(second["candidateId"])): distance(first, second)
        for first in ordered
        for second in ordered
    }
    first_id = min(
        CANDIDATE_IDS,
        key=lambda candidate_id: (
            -sum(pairwise[(candidate_id, other)] for other in CANDIDATE_IDS) / 29.0,
            candidate_id,
        ),
    )
    selected = [first_id]
    while len(selected) < 10:
        remaining = [item for item in CANDIDATE_IDS if item not in selected]
        selected.append(
            min(
                remaining,
                key=lambda candidate_id: (
                    -min(pairwise[(candidate_id, chosen)] for chosen in selected),
                    candidate_id,
                ),
            )
        )
    return {
        "schemaVersion": "qf.qas30.cover10.v1",
        "method": "MAXIMIN_GOWER_SIX_FACTORS",
        "initialPoint": "MAXIMUM_MEAN_GOWER_DISTANCE",
        "tieBreak": "CANDIDATE_ID_ASCENDING",
        "candidateIds": selected,
    }


def r1_batches(cover_candidate_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Return the two batches for one frozen cover10 selection."""

    if (
        isinstance(cover_candidate_ids, str | bytes)
        or len(cover_candidate_ids) != 10
        or len(set(cover_candidate_ids)) != 10
        or not set(cover_candidate_ids) <= set(CANDIDATE_IDS)
    ):
        raise Qas30ProtocolError("R1 requires ten unique candidate IDs")

    return [
        {
            "batchId": "QAS30-R1-B1",
            "stage": "R1",
            "candidateIds": list(cover_candidate_ids[:BATCH_SIZE]),
            "shots": SHOTS_PER_CIRCUIT,
        },
        {
            "batchId": "QAS30-R1-B2",
            "stage": "R1",
            "candidateIds": list(cover_candidate_ids[BATCH_SIZE:]),
            "shots": SHOTS_PER_CIRCUIT,
        },
    ]


def equal_weight_hardware_score(
    normalized_components: Mapping[str, float],
    weights: Mapping[str, float] | None = None,
) -> float:
    """Compute S_HW, using the frozen equal-weight rule when no weights exist."""

    if set(normalized_components) != set(HARDWARE_SCORE_COMPONENTS):
        raise Qas30ProtocolError("S_HW requires the seven frozen components")
    selected_weights = (
        {name: 1.0 / len(HARDWARE_SCORE_COMPONENTS) for name in HARDWARE_SCORE_COMPONENTS}
        if weights is None
        else dict(weights)
    )
    if set(selected_weights) != set(HARDWARE_SCORE_COMPONENTS):
        raise Qas30ProtocolError("S_HW weights must cover the seven frozen components")
    if not math.isclose(sum(selected_weights.values()), 1.0, abs_tol=1e-12):
        raise Qas30ProtocolError("S_HW weights must sum to one")
    score = 0.0
    for name in HARDWARE_SCORE_COMPONENTS:
        value = float(normalized_components[name])
        weight = float(selected_weights[name])
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise Qas30ProtocolError(f"S_HW component {name} must be normalized")
        if not math.isfinite(weight) or weight < 0.0:
            raise Qas30ProtocolError(f"S_HW weight {name} is invalid")
        score += weight * value
    return score


def _feature_matrix(
    candidate_ids: Sequence[str],
    feature_rows: Mapping[str, Mapping[str, float]],
) -> np.ndarray:
    rows: list[list[float]] = []
    for candidate_id in candidate_ids:
        row = feature_rows.get(candidate_id)
        if row is None or set(row) != set(PREDICTOR_FEATURE_WHITELIST):
            raise Qas30ProtocolError(f"candidate {candidate_id} has no complete feature row")
        values = [float(row[name]) for name in PREDICTOR_FEATURE_WHITELIST]
        if not all(math.isfinite(value) for value in values):
            raise Qas30ProtocolError(f"candidate {candidate_id} has a non-finite feature")
        rows.append(values)
    return np.asarray(rows, dtype=float)


def _ridge_fit(x: np.ndarray, y: np.ndarray, alpha: float) -> tuple[np.ndarray, float]:
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale == 0.0] = 1.0
    standardized = (x - mean) / scale
    y_mean = float(y.mean())
    gram = standardized.T @ standardized + alpha * np.eye(standardized.shape[1])
    beta = np.linalg.solve(gram, standardized.T @ (y - y_mean))
    return beta, y_mean


def _ridge_predict(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    alpha: float,
) -> np.ndarray:
    mean = x_train.mean(axis=0)
    scale = x_train.std(axis=0)
    scale[scale == 0.0] = 1.0
    beta, y_mean = _ridge_fit(x_train, y_train, alpha)
    return y_mean + ((x_test - mean) / scale) @ beta


def _select_ridge_alpha(x: np.ndarray, y: np.ndarray) -> tuple[float, dict[str, float]]:
    losses: dict[str, float] = {}
    for alpha in RIDGE_ALPHA_GRID:
        errors: list[float] = []
        for held_out in range(len(y)):
            train = np.arange(len(y)) != held_out
            prediction = _ridge_predict(x[train], y[train], x[~train], alpha)[0]
            errors.append(float((prediction - y[held_out]) ** 2))
        losses[f"{alpha:g}"] = float(np.mean(errors))
    selected = min(RIDGE_ALPHA_GRID, key=lambda value: (losses[f"{value:g}"], value))
    return selected, losses


def _prediction_rows(
    train_x: np.ndarray,
    train_y: np.ndarray,
    candidate_x: np.ndarray,
    candidate_ids: Sequence[str],
    train_candidate_ids: Sequence[str],
    alpha: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    mean = train_x.mean(axis=0)
    scale = train_x.std(axis=0)
    scale[scale == 0.0] = 1.0
    standardized_train = (train_x - mean) / scale
    standardized_candidate = (candidate_x - mean) / scale
    beta, y_mean = _ridge_fit(train_x, train_y, alpha)
    fitted = y_mean + standardized_train @ beta
    predictions = y_mean + standardized_candidate @ beta
    residuals = train_y - fitted
    degrees_of_freedom = max(1, len(train_y) - standardized_train.shape[1] - 1)
    residual_variance = float(residuals @ residuals) / degrees_of_freedom
    residual_scale = math.sqrt(residual_variance)
    inverse = np.linalg.inv(
        standardized_train.T @ standardized_train
        + alpha * np.eye(standardized_train.shape[1])
    )
    training_leverage = np.einsum(
        "ij,jk,ik->i",
        standardized_train,
        inverse,
        standardized_train,
    )
    parameter_count = standardized_train.shape[1]
    cooks_distance = (
        residuals**2
        / max(np.finfo(float).eps, parameter_count * residual_variance)
        * training_leverage
        / np.maximum(np.finfo(float).eps, (1.0 - training_leverage) ** 2)
    )
    training_extent = np.abs(standardized_train).max(axis=0)
    rows = []
    for index, candidate_id in enumerate(candidate_ids):
        vector = standardized_candidate[index]
        leverage = float(vector @ inverse @ vector)
        rows.append(
            {
                "candidateId": candidate_id,
                "predictedSHw": float(predictions[index]),
                "uncertainty": residual_scale * math.sqrt(1.0 + max(0.0, leverage)),
                "leverage": leverage,
                "extrapolationFlag": bool(
                    np.any(np.abs(vector) > training_extent + np.finfo(float).eps)
                ),
            }
        )
    report = {
        "intercept": y_mean,
        "standardizedCoefficients": {
            name: float(beta[index])
            for index, name in enumerate(PREDICTOR_FEATURE_WHITELIST)
        },
        "featureMeans": {
            name: float(mean[index])
            for index, name in enumerate(PREDICTOR_FEATURE_WHITELIST)
        },
        "featureScales": {
            name: float(scale[index])
            for index, name in enumerate(PREDICTOR_FEATURE_WHITELIST)
        },
        "trainingDiagnostics": [
            {
                "candidateId": candidate_id,
                "fittedSHw": float(fitted[index]),
                "residual": float(residuals[index]),
                "leverage": float(training_leverage[index]),
                "cooksDistance": float(cooks_distance[index]),
            }
            for index, candidate_id in enumerate(train_candidate_ids)
        ],
    }
    return rows, report


def _minmax(values: np.ndarray) -> np.ndarray:
    low = values.min(axis=0)
    span = values.max(axis=0) - low
    span[span == 0.0] = 1.0
    return (values - low) / span


def validate_r1_score_receipt_set(
    receipt_set: Mapping[str, Any],
    *,
    run_id: str,
    data_epoch: str,
    freeze_manifest_sha256: str,
    block_id: str,
    source_commit_sha: str,
    normalization_sha256: str,
    predictor_feature_freeze_sha256: str,
    cover_candidate_ids: Sequence[str],
) -> dict[str, float]:
    """Validate the ordered R1 score evidence set and return its bound scores."""

    required = {
        "schemaVersion",
        "runId",
        "dataEpoch",
        "freezeManifestSha256",
        "blockId",
        "sourceCommitSha",
        "normalizationSha256",
        "predictorFeatureFreezeSha256",
        "scientificStatus",
        "candidateIds",
        "receipts",
        "scoreView",
        "feasibilityContract",
        "scoreReceiptSetSha256",
    }
    stored = receipt_set.get("scoreReceiptSetSha256")
    unhashed = {
        key: value
        for key, value in receipt_set.items()
        if key != "scoreReceiptSetSha256"
    }
    expected_cover = list(cover_candidate_ids)
    receipts = receipt_set.get("receipts")
    if (
        set(receipt_set) != required
        or receipt_set.get("schemaVersion") != "qf.qas30.r1-score-receipt-set.v1"
        or receipt_set.get("runId") != run_id
        or receipt_set.get("dataEpoch") != data_epoch
        or receipt_set.get("freezeManifestSha256") != freeze_manifest_sha256
        or receipt_set.get("blockId") != block_id
        or receipt_set.get("sourceCommitSha") != source_commit_sha
        or receipt_set.get("normalizationSha256") != normalization_sha256
        or receipt_set.get("predictorFeatureFreezeSha256")
        != predictor_feature_freeze_sha256
        or receipt_set.get("scientificStatus")
        not in {"OBSERVED_NOT_RELEASED", "PROTOCOL_IMPLEMENTATION_FIXTURE"}
        or receipt_set.get("candidateIds") != expected_cover
        or receipt_set.get("scoreView") != "RAW_PRIMARY"
        or receipt_set.get("feasibilityContract")
        != "EXACT_HAMMING_WEIGHT_3_Q0_Q5"
        or stored != _canonical_sha256(unhashed)
        or not isinstance(receipts, list)
        or len(receipts) != len(expected_cover)
        or len(expected_cover) != 10
        or len(set(expected_cover)) != 10
        or not _valid_identity(run_id)
        or not _valid_identity(data_epoch)
        or not _valid_identity(block_id, maximum=128)
        or not isinstance(source_commit_sha, str)
        or len(source_commit_sha) != 40
        or any(character not in "0123456789abcdef" for character in source_commit_sha)
        or not all(
            _valid_sha256(value)
            for value in (
                freeze_manifest_sha256,
                normalization_sha256,
                predictor_feature_freeze_sha256,
            )
        )
    ):
        raise Qas30ProtocolError("R1 score receipt set is incomplete or modified")
    scores: dict[str, float] = {}
    seen_queries: set[str] = set()
    seen_receipts: set[str] = set()
    for index, (candidate_id, receipt) in enumerate(
        zip(expected_cover, receipts, strict=True)
    ):
        expected_batch_id = (
            f"{block_id}-B1" if index < BATCH_SIZE else f"{block_id}-B2"
        )
        if (
            not isinstance(receipt, Mapping)
            or set(receipt)
            != {
                "candidateId",
                "batchId",
                "queryId",
                "dataEpoch",
                "freezeManifestSha256",
                "queryObservationSha256",
                "scoreReceiptSha256",
                "sHw",
            }
            or receipt.get("candidateId") != candidate_id
            or receipt.get("batchId") != expected_batch_id
            or receipt.get("dataEpoch") != data_epoch
            or receipt.get("freezeManifestSha256") != freeze_manifest_sha256
            or not _valid_identity(receipt.get("queryId"), maximum=512)
            or receipt.get("queryId") in seen_queries
            or not _valid_sha256(receipt.get("queryObservationSha256"))
            or not _valid_sha256(receipt.get("scoreReceiptSha256"))
            or receipt.get("scoreReceiptSha256") in seen_receipts
            or isinstance(receipt.get("sHw"), bool)
        ):
            raise Qas30ProtocolError("R1 score receipt summary is invalid")
        try:
            score = float(receipt["sHw"])
        except (TypeError, ValueError) as error:
            raise Qas30ProtocolError("R1 score receipt summary is invalid") from error
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise Qas30ProtocolError("R1 scores must be normalized and finite")
        scores[candidate_id] = score
        seen_queries.add(str(receipt["queryId"]))
        seen_receipts.add(str(receipt["scoreReceiptSha256"]))
    return scores


def select_r2_arms(
    *,
    run_id: str,
    data_epoch: str,
    freeze_manifest_sha256: str,
    block_id: str,
    candidate_catalog_rows: Sequence[Mapping[str, Any]],
    cover_candidate_ids: Sequence[str],
    predictor_feature_freeze: Mapping[str, Any] | None,
    normalization_manifest: Mapping[str, Any],
    r1_score_receipt_set: Mapping[str, Any],
    llm_candidate_ids: Sequence[str] | None = None,
    llm_selection_receipt_sha256: str | None = None,
    training_predictor_feature_freeze: Mapping[str, Any] | None = None,
    prediction_predictor_feature_freeze: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Select local arms and bind, but never invent, the governed LLM Top5."""

    if not _valid_identity(run_id):
        raise Qas30ProtocolError("runId is invalid")
    # R1 scores are training facts from K0; unseen R2 estimates require a new
    # K1 freeze.  A single freeze is intentionally not a compatibility path:
    # accepting it would reintroduce the temporal leakage this receipt avoids.
    if predictor_feature_freeze is not None:
        raise Qas30ProtocolError("R2 does not accept a single predictor feature freeze")
    training_freeze = training_predictor_feature_freeze
    prediction_freeze = prediction_predictor_feature_freeze
    if not isinstance(training_freeze, Mapping) or not isinstance(prediction_freeze, Mapping):
        raise Qas30ProtocolError("R2 requires K0 training and K1 prediction freezes")
    training_feature_freeze_sha = training_freeze.get("predictorFeatureFreezeSha256")
    prediction_feature_freeze_sha = prediction_freeze.get("predictorFeatureFreezeSha256")
    feature_freeze_sha = prediction_feature_freeze_sha
    feature_freeze_unhashed = {
        key: value
        for key, value in prediction_freeze.items()
        if key != "predictorFeatureFreezeSha256"
    }
    normalization_sha = normalization_manifest.get("normalizationSha256")
    normalization_unhashed = {
        key: value
        for key, value in normalization_manifest.items()
        if key != "normalizationSha256"
    }
    feature_freeze_rows = prediction_freeze.get("rows")
    if (
        prediction_freeze.get("schemaVersion")
        != "qf.qas30.predictor-feature-freeze.v2"
        or prediction_freeze.get("state") != "FROZEN_FOR_SELECTION"
        or feature_freeze_sha != _canonical_sha256(feature_freeze_unhashed)
        or prediction_freeze.get("normalizationSha256") != normalization_sha
        or normalization_manifest.get("schemaVersion")
        != "qf.qas30.normalization-freeze.v1"
        or normalization_sha != _canonical_sha256(normalization_unhashed)
        or not isinstance(feature_freeze_rows, list)
        or len(feature_freeze_rows) != 30
    ):
        raise Qas30ProtocolError(
            "R2 requires frozen six-feature and normalization entities"
        )
    feature_rows = {
        str(row.get("candidateId")): {
            **dict(row.get("preHardwareFeatures", {})),
            **dict(row.get("selectionMappingFeatures", {})),
        }
        for row in feature_freeze_rows
        if isinstance(row, Mapping)
    }
    if set(feature_rows) != set(CANDIDATE_IDS) or any(
        not isinstance(row, Mapping)
        or set(row) != set(PREDICTOR_FEATURE_WHITELIST)
        for row in feature_rows.values()
    ):
        raise Qas30ProtocolError("R2 six-feature freeze rows are incomplete")
    catalog = candidate_catalog(candidate_catalog_rows)
    expected_cover = select_cover10(catalog)["candidateIds"]
    if list(cover_candidate_ids) != expected_cover:
        raise Qas30ProtocolError("R1 candidate order does not match the frozen cover10")
    source_commit_sha = prediction_freeze.get("sourceCommitSha")
    if (
        prediction_freeze.get("dataEpoch") != data_epoch
        or prediction_freeze.get("freezeManifestSha256")
        != freeze_manifest_sha256
        or training_freeze.get("dataEpoch") != data_epoch
        or training_freeze.get("freezeManifestSha256") != freeze_manifest_sha256
        or training_freeze.get("sourceCommitSha") != source_commit_sha
        or training_freeze.get("schemaVersion")
        != "qf.qas30.predictor-feature-freeze.v2"
        or training_freeze.get("state") != "FROZEN_FOR_SELECTION"
        or training_feature_freeze_sha
        != _canonical_sha256(
            {
                key: value
                for key, value in training_freeze.items()
                if key != "predictorFeatureFreezeSha256"
            }
        )
        or training_freeze.get("predictorCalibrationLabel") != "K0"
        or prediction_freeze.get("k0ConfigSha256")
        != training_freeze.get("k0ConfigSha256")
        or prediction_freeze.get("predictorCalibrationLabel") != "K1"
    ):
        raise Qas30ProtocolError("R2 predictor features do not bind the frozen run")
    cover_scores = validate_r1_score_receipt_set(
        r1_score_receipt_set,
        run_id=run_id,
        data_epoch=data_epoch,
        freeze_manifest_sha256=freeze_manifest_sha256,
        block_id=block_id,
        source_commit_sha=str(source_commit_sha),
        normalization_sha256=str(normalization_sha),
        predictor_feature_freeze_sha256=str(training_feature_freeze_sha),
        cover_candidate_ids=cover_candidate_ids,
    )
    train_y = np.asarray(
        [float(cover_scores[item]) for item in cover_candidate_ids],
        dtype=float,
    )
    if not np.isfinite(train_y).all() or np.any((train_y < 0.0) | (train_y > 1.0)):
        raise Qas30ProtocolError("R1 scores must be normalized and finite")
    unseen = tuple(item for item in CANDIDATE_IDS if item not in cover_candidate_ids)
    train_x = _feature_matrix(cover_candidate_ids, feature_rows)
    candidate_x = _feature_matrix(unseen, feature_rows)
    alpha, loocv = _select_ridge_alpha(train_x, train_y)
    predictions, model_report = _prediction_rows(
        train_x,
        train_y,
        candidate_x,
        unseen,
        cover_candidate_ids,
        alpha,
    )
    feature_index = {
        name: index for index, name in enumerate(PREDICTOR_FEATURE_WHITELIST)
    }
    compiled_depth = feature_index["compiledDepth"]
    predictions.sort(
        key=lambda row: (
            row["predictedSHw"],
            row["uncertainty"],
            candidate_x[unseen.index(row["candidateId"]), compiled_depth],
            row["candidateId"],
        )
    )
    fixed_columns = [
        feature_index["compiledDepth"],
        feature_index["twoQubitGates"],
        feature_index["swapCount"],
    ]
    catalog_by_id = {row["candidateId"]: row for row in catalog}
    architecture = np.asarray(
        [
            [
                catalog_by_id[candidate_id]["messageLayers"],
                catalog_by_id[candidate_id]["reuploadCount"],
            ]
            for candidate_id in unseen
        ],
        dtype=float,
    )
    fixed_matrix = np.column_stack((candidate_x[:, fixed_columns], architecture))
    fixed_scores = _minmax(fixed_matrix).mean(axis=1)
    fixed_order = sorted(
        range(len(unseen)),
        key=lambda index: (
            fixed_scores[index],
            candidate_x[index, compiled_depth],
            unseen[index],
        ),
    )
    seed_sha256 = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    random_order = list(unseen)
    random.Random(int(seed_sha256[:16], 16)).shuffle(random_order)
    if llm_candidate_ids is not None and (
        isinstance(llm_candidate_ids, str | bytes)
        or len(llm_candidate_ids) != BATCH_SIZE
        or len(set(llm_candidate_ids)) != BATCH_SIZE
        or any(not isinstance(item, str) for item in llm_candidate_ids)
        or not set(llm_candidate_ids) <= set(unseen)
    ):
        raise Qas30ProtocolError("R2 governed LLM arm is invalid")
    if (llm_candidate_ids is None) != (llm_selection_receipt_sha256 is None) or (
        llm_selection_receipt_sha256 is not None
        and not _valid_sha256(llm_selection_receipt_sha256)
    ):
        raise Qas30ProtocolError(
            "R2 governed LLM arm must bind one selection receipt"
        )
    selection = {
        "schemaVersion": "qf.qas30.r2-selection.v3",
        "runId": run_id,
        "dataEpoch": data_epoch,
        "freezeManifestSha256": freeze_manifest_sha256,
        "blockId": block_id,
        "sourceCommitSha": source_commit_sha,
        "trainingPredictorFeatureFreezeSha256": training_feature_freeze_sha,
        "predictionPredictorFeatureFreezeSha256": prediction_feature_freeze_sha,
        "selectionFeatureSnapshotSha256": prediction_freeze[
            "selectionFeatureSnapshotSha256"
        ],
        "normalizationSha256": normalization_sha,
        "coverCandidateIds": list(cover_candidate_ids),
        "r1ScoreReceiptSetSha256": r1_score_receipt_set[
            "scoreReceiptSetSha256"
        ],
        "llmSelectionReceiptSha256": llm_selection_receipt_sha256,
        "candidatePool": list(unseen),
        "ridge": {
            "featureWhitelist": list(PREDICTOR_FEATURE_WHITELIST),
            "alphaGrid": list(RIDGE_ALPHA_GRID),
            "selectedAlpha": alpha,
            "loocvMeanSquaredError": loocv,
            "model": model_report,
            "predictions": predictions,
        },
        "arms": {
            "RIDGE": [row["candidateId"] for row in predictions[:BATCH_SIZE]],
            "FIXED": [unseen[index] for index in fixed_order[:BATCH_SIZE]],
            "RANDOM": random_order[:BATCH_SIZE],
            "LLM": list(llm_candidate_ids) if llm_candidate_ids is not None else None,
        },
        "llmSelectionState": (
            "GOVERNED_INPUT_BOUND"
            if llm_candidate_ids is not None
            else "PENDING_GOVERNED_SELECTION"
        ),
        "fixedRule": {
            "components": [
                "compiledDepth",
                "twoQubitGates",
                "swapCount",
                "messageLayers",
                "reuploadCount",
            ],
            "normalization": "UNSEEN20_MINMAX",
            "aggregation": "EQUAL_MEAN",
            "scores": {
                candidate_id: float(fixed_scores[index])
                for index, candidate_id in enumerate(unseen)
            },
        },
        "overlapPolicy": "INDEPENDENT_RERUN",
        "randomSeedSha256": seed_sha256,
    }
    selection["r2SelectionSha256"] = _canonical_sha256(selection)
    return selection


def r2_batches(selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Materialize four independent v2 R2 batches without cross-arm deduplication."""

    stored = selection.get("r2SelectionSha256")
    unhashed = {
        key: value for key, value in selection.items() if key != "r2SelectionSha256"
    }
    if (
        selection.get("schemaVersion") != "qf.qas30.r2-selection.v3"
        or not _valid_identity(selection.get("runId"))
        or not _valid_identity(selection.get("dataEpoch"))
        or not _valid_identity(selection.get("blockId"), maximum=128)
        or not isinstance(selection.get("sourceCommitSha"), str)
        or len(selection["sourceCommitSha"]) != 40
        or any(
            character not in "0123456789abcdef"
            for character in selection["sourceCommitSha"]
        )
        or not _valid_sha256(selection.get("freezeManifestSha256"))
        or not _valid_sha256(selection.get("r1ScoreReceiptSetSha256"))
        or not _valid_sha256(selection.get("trainingPredictorFeatureFreezeSha256"))
        or not _valid_sha256(selection.get("predictionPredictorFeatureFreezeSha256"))
        or not _valid_sha256(selection.get("selectionFeatureSnapshotSha256"))
        or stored != _canonical_sha256(unhashed)
        or selection.get("llmSelectionState") != "GOVERNED_INPUT_BOUND"
        or not _valid_sha256(selection.get("llmSelectionReceiptSha256"))
    ):
        raise Qas30ProtocolError("R2 selection entity is incomplete or modified")
    arms = selection.get("arms")
    if not isinstance(arms, Mapping) or set(arms) != {
        "RIDGE",
        "FIXED",
        "RANDOM",
        "LLM",
    }:
        raise Qas30ProtocolError("R2 selection must contain the four frozen arms")
    candidate_pool = selection.get("candidatePool")
    if (
        not isinstance(candidate_pool, Sequence)
        or isinstance(candidate_pool, str | bytes)
        or len(candidate_pool) != 20
        or len(set(candidate_pool)) != 20
        or not set(candidate_pool) <= set(CANDIDATE_IDS)
    ):
        raise Qas30ProtocolError("R2 selection candidate pool is invalid")
    unseen = set(candidate_pool)
    rows = []
    block_id = str(selection["blockId"])
    for batch_index, arm in enumerate(("RIDGE", "FIXED", "RANDOM", "LLM"), start=3):
        candidate_ids = arms[arm]
        if (
            not isinstance(candidate_ids, Sequence)
            or isinstance(candidate_ids, str | bytes)
            or len(candidate_ids) != BATCH_SIZE
            or any(not isinstance(item, str) for item in candidate_ids)
            or len(set(candidate_ids)) != BATCH_SIZE
            or not set(candidate_ids) <= unseen
        ):
            raise Qas30ProtocolError(f"R2 {arm} arm is invalid")
        rows.append(
            {
                "blockId": block_id,
                "batchIndex": batch_index,
                "batchId": f"{block_id}-B{batch_index}",
                "stage": f"R2_{arm}",
                "candidateIds": list(candidate_ids),
                "shots": SHOTS_PER_CIRCUIT,
                "requiredActiveCalibration": "K1",
                "selectionState": "FROZEN_POST_R1",
                "overlapPolicy": "INDEPENDENT_RERUN",
            }
        )
    return rows


def extension_budget_manifest() -> dict[str, Any]:
    """Return the frozen finite resource and stopping-rule manifest."""

    rows = [
        {
            "experiment": "QAS30_PRIMARY",
            "calls": 6,
            "circuits": 30,
            "shots": 30_000,
            "stoppingRule": "FIXED_COMPLETE",
        },
        {
            "experiment": "INDEPENDENT_REPLICATION",
            "calls": 60,
            "circuits": 300,
            "shots": 300_000,
            "stoppingRule": "FOUR_CI_HALF_WIDTH_0.05_AT_B10_20_30_MAX_B40",
        },
        {
            "experiment": "SHOT_SCALING",
            "calls": 21,
            "circuits": 105,
            "shots": 282_750,
            "stoppingRule": "SUPPORTED_LEVELS_ONLY",
        },
        {
            "experiment": "CALIBRATION_DRIFT",
            "calls": 12,
            "circuits": 60,
            "shots": 60_000,
            "stoppingRule": "TWELVE_VALID_SNAPSHOTS_OR_PLATFORM_STATE_CHANGE",
        },
        {
            "experiment": "MAPPING_SENSITIVITY",
            "calls": 5,
            "circuits": 25,
            "shots": 25_000,
            "stoppingRule": "FIVE_VALID_COMMON_ORDER_MAPPINGS",
        },
        {
            "experiment": "END_TO_END_FEEDBACK",
            "calls": 15,
            "circuits": 75,
            "shots": 75_000,
            "stoppingRule": "THREE_ARMS_ACROSS_FIVE_COMPLETE_SNAPSHOTS",
        },
    ]
    return {
        "schemaVersion": "qf.qas30.extension-budget.v1",
        "defaultRows": rows,
        "replicationBudgetTotals": {
            "10": {"calls": 119, "circuits": 595, "shots": 772_750},
            "20": {"calls": 179, "circuits": 895, "shots": 1_072_750},
            "30": {"calls": 239, "circuits": 1_195, "shots": 1_372_750},
            "40": {"calls": 299, "circuits": 1_495, "shots": 1_672_750},
        },
        "defaultTotal": {"calls": 119, "circuits": 595, "shots": 772_750},
        "maximumTotal": {"calls": 299, "circuits": 1_495, "shots": 1_672_750},
    }


def ready_for_k0_manifest(
    *,
    run_id: str,
    data_epoch: str,
    source_commit: str,
    production_bundle: Mapping[str, Any],
    parent_manifest_bytes: bytes,
    parent_canonical_ir_bytes: bytes,
    market_data_sha256: str,
    pre_hardware_feature_freeze: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the local terminal state immediately before live K0 discovery."""

    if not _valid_identity(run_id):
        raise Qas30ProtocolError("runId is invalid")
    if not _valid_identity(data_epoch):
        raise Qas30ProtocolError("dataEpoch is invalid")
    if len(source_commit) != 40 or any(
        character not in "0123456789abcdef" for character in source_commit
    ):
        raise Qas30ProtocolError("source commit must be one lowercase Git SHA-1")
    verified_bundle = validate_production_candidate_bundle(
        production_bundle,
        parent_manifest_bytes=parent_manifest_bytes,
        parent_canonical_ir_bytes=parent_canonical_ir_bytes,
        market_data_sha256=market_data_sha256,
    )
    verified_pre_hardware = validate_pre_hardware_predictor_feature_freeze(
        pre_hardware_feature_freeze
    )
    if verified_pre_hardware.get("sourceCommitSha") != source_commit:
        raise Qas30ProtocolError(
            "pre-hardware predictor features do not bind the source commit"
        )
    if (
        verified_pre_hardware.get("candidateBundleSha256")
        != verified_bundle.get("bundleSha256")
        or verified_pre_hardware.get("localFeatureScientificStatus")
        != "LOCAL_DERIVED_PRE_HARDWARE_FEATURES"
    ):
        raise Qas30ProtocolError(
            "READY_FOR_K0 requires production local-feature evidence"
        )
    catalog = candidate_catalog(verified_bundle["candidateCatalog"])
    logical_circuits = logical_circuit_manifest(
        catalog, verified_bundle["logicalCircuits"]
    )
    if any(
        row["scientificStatus"] != "FROZEN_LOCAL_INPUT"
        for row in [*catalog, *logical_circuits]
    ):
        raise Qas30ProtocolError("protocol fixtures cannot establish READY_FOR_K0")
    cover = select_cover10(catalog)
    cover_ids = cover["candidateIds"]
    return {
        "schemaVersion": PROTOCOL_SCHEMA_VERSION,
        "runId": run_id,
        "dataEpoch": data_epoch,
        "sourceCommit": source_commit,
        "target": TARGET,
        "state": LOCAL_READY_STATE,
        "scientificStatus": "DESIGN_ONLY",
        "candidateBundleSha256": verified_bundle["bundleSha256"],
        "parentBindings": verified_bundle["parentBindings"],
        "preHardwareFeatureFreezeSha256": verified_pre_hardware[
            "preHardwareFeatureFreezeSha256"
        ],
        "localFeatureEvidenceSha256": verified_pre_hardware[
            "localFeatureEvidenceSha256"
        ],
        "candidateCatalog": catalog,
        "candidateCatalogSha256": _canonical_sha256(catalog),
        "logicalCircuits": logical_circuits,
        "logicalCircuitSetSha256": _canonical_sha256(logical_circuits),
        "cover10": cover,
        "r1Batches": r1_batches(cover_ids),
        "r2": {
            "candidatePool": [item for item in CANDIDATE_IDS if item not in cover_ids],
            "arms": ["RIDGE", "FIXED", "RANDOM", "LLM"],
            "batchSize": BATCH_SIZE,
            "overlapPolicy": "INDEPENDENT_RERUN",
        },
        "hardwareScore": {
            "components": list(HARDWARE_SCORE_COMPONENTS),
            "hardwareScoreWeights": {
                name: 1.0 / len(HARDWARE_SCORE_COMPONENTS)
                for name in HARDWARE_SCORE_COMPONENTS
            },
        },
        "calibrationSequence": ["K0", "K1", "K2"],
        "mappingPlan": mapping_plan_manifest(),
        "statisticsPlan": statistical_plan_manifest(),
        "stateMachine": state_machine_manifest(),
        "primaryBudget": {"calls": 6, "circuits": 30, "shots": 30_000},
        "firstBlock": six_batch_block_manifest(
            block_id=f"{run_id}-BLOCK-01", cover_candidate_ids=cover_ids
        ),
        "extensionBudget": extension_budget_manifest(),
        "localReadinessChecks": {
            "candidatePoolComplete": True,
            "canonicalIrBindingsVerified": True,
            "parentEntitiesRevalidated": True,
            "completeBundleRegenerated": True,
            "logicalQcisHashesVerified": True,
            "logicalMeasurementOrderVerified": True,
            "cover10AndR1Frozen": True,
            "r2SelectionContractFrozen": True,
            "statisticsImplementationFrozen": True,
            "stateMachineFrozen": True,
            "preHardwarePredictorFeaturesFrozen": True,
            "k0Captured": False,
        },
    }
