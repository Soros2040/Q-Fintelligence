"""Auditable local predictor features for the QAS30 production candidates.

The two pre-hardware features are derived only from frozen E02 entities:

* ``localValidationLoss`` is the 30-unit MAE across five formal anchors and
  six assets, using the frozen shared readout and the candidate statevector.
* ``quboObjectiveDegradation`` is the mean exact cardinality-three regret for
  the frozen local E02 node/spillover architecture-preselection surrogate.

No hardware observation, provider response, fixture value, or current
calibration field participates in this module.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from cqlib.circuits import Circuit
from cqlib.simulator import StatevectorSimulator

from qf_algorithm.legacy.quantum_worker import qgnn_m2, v42_m3_training
from qf_algorithm.legacy.quantum_worker.qas30_protocol import (
    MARKET_799_CSV_SHA256,
    PARENT_CANONICAL_IR_ARTIFACT_SHA256,
    Qas30ProtocolError,
    validate_production_candidate_bundle,
)

LOCAL_FEATURE_SCHEMA_VERSION = "qf.qas30.local-predictor-features.v1"
E02_DETERMINISTIC_INPUTS_SHA256 = (
    "ebd0313000514d54fcc267e32e08fd08b4f7b1ae539e49a5076f7692036719d8"
)
E02_RUN_ID = "qf_v42_m1_m6_20260809_181946"
E02_FORMAL_RELEASE_SHA256 = (
    "9edb9a7dda2218133529faa84d290a60170f88242d2fd73ac2b318288998566e"
)
QUBO_CARDINALITY = 3
QUBO_NODE_RISK_WEIGHT = 0.5
QUBO_SPILLOVER_WEIGHT = 0.2


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _logical_sha256(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "canonicalIrSha256"}
    return _sha256(_canonical_json(payload).encode("utf-8"))


def _validate_deterministic_inputs(value_bytes: bytes) -> dict[str, Any]:
    if _sha256(value_bytes) != E02_DETERMINISTIC_INPUTS_SHA256:
        raise Qas30ProtocolError("E02 deterministic input artifact hash changed")
    try:
        value = json.loads(value_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Qas30ProtocolError("E02 deterministic input artifact is invalid") from error
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != "qf.v42-m5-formal-deterministic-inputs.v1"
        or value.get("runId") != E02_RUN_ID
        or value.get("formalReleaseSha256") != E02_FORMAL_RELEASE_SHA256
    ):
        raise Qas30ProtocolError("E02 deterministic input identity changed")
    rows = value.get("rows")
    if not isinstance(rows, list) or len(rows) != 5:
        raise Qas30ProtocolError("E02 deterministic input requires five anchors")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "taskId",
            "formalDate",
            "assetOrder",
            "labels",
            "classicalStatevectorPredictions",
            "zeroBaselinePredictions",
        }:
            raise Qas30ProtocolError("E02 deterministic input row changed")
        task_id = row.get("taskId")
        assets = row.get("assetOrder")
        labels = row.get("labels")
        if (
            not isinstance(task_id, str)
            or task_id in seen
            or not isinstance(assets, list)
            or len(assets) != 6
            or len(set(assets)) != 6
            or not isinstance(labels, list)
            or len(labels) != 6
            or not all(math.isfinite(float(item)) for item in labels)
        ):
            raise Qas30ProtocolError("E02 deterministic input row is incomplete")
        seen.add(task_id)
    return value


def _validate_parent_ir(
    value_bytes: bytes,
    task: Mapping[str, Any],
) -> dict[str, Any]:
    artifact_sha256 = str(task.get("canonicalIrArtifactSha256", ""))
    if _sha256(value_bytes) != artifact_sha256:
        raise Qas30ProtocolError("E02 parent Canonical IR artifact hash changed")
    try:
        value = json.loads(value_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Qas30ProtocolError("E02 parent Canonical IR is invalid") from error
    if not isinstance(value, dict):
        raise Qas30ProtocolError("E02 parent Canonical IR must be an object")
    if (
        value.get("canonicalIrSha256") != task.get("canonicalIrSha256")
        or _logical_sha256(value) != value.get("canonicalIrSha256")
        or value.get("schemaVersion") != "qf.canonical-circuit-ir.v1"
        or value.get("candidateId") != "DIR_L2_R0"
        or value.get("status") != "FORMAL_PARAMETERS_FROZEN"
        or value.get("formalDate") != task.get("formalDate")
        or value.get("width") != 6
        or value.get("classicalBits") != 6
        or value.get("logicalQubitOrder") != list(range(6))
        or value.get("measurementOrder") != list(range(6))
        or value.get("messageLayers") != 2
        or value.get("reuploadCount") != 0
    ):
        raise Qas30ProtocolError("E02 parent Canonical IR identity changed")
    operations = value.get("operations")
    if not isinstance(operations, list) or len(operations) != 67:
        raise Qas30ProtocolError("E02 parent Canonical IR operation count changed")
    if operations[-1].get("op") != "MEASURE_ALL":
        raise Qas30ProtocolError("E02 parent measurement operation changed")
    if [operation.get("op") for operation in operations[:18]] != [
        gate for _ in range(6) for gate in ("RY", "RZ", "RX")
    ]:
        raise Qas30ProtocolError("E02 first feature stage changed")
    if [operation.get("op") for operation in operations[24:36]] != [
        gate for _ in range(6) for gate in ("RY", "RZ")
    ]:
        raise Qas30ProtocolError("E02 second feature stage changed")
    messages = operations[36:60]
    if messages[:12] != messages[12:] or any(
        operation.get("op") != "CRY" for operation in messages
    ):
        raise Qas30ProtocolError("E02 directed message stage changed")
    if value.get("assetOrder") != task.get("assetOrder"):
        raise Qas30ProtocolError("E02 parent asset order changed")
    return value


def _candidate_operations(
    parent_ir: Mapping[str, Any],
    *,
    rotation_axis: str,
    parameters: Sequence[float],
    message_layers: int,
    reupload_count: int,
) -> list[dict[str, Any]]:
    if (
        rotation_axis not in {"RX", "RY", "RZ"}
        or len(parameters) != 2
        or not all(math.isfinite(float(item)) for item in parameters)
        or message_layers not in {1, 2}
        or reupload_count not in {0, 1}
    ):
        raise Qas30ProtocolError("QAS30 candidate factor is invalid")
    source = parent_ir["operations"]
    assets = parent_ir["assetOrder"]
    first = [dict(operation) for operation in source[:18]]
    second = [dict(operation) for operation in source[24:36]]
    edges = [dict(operation) for operation in source[36:48]]
    operations = list(first)
    for qubit, asset_id in enumerate(assets):
        operations.append(
            {
                "op": rotation_axis,
                "controls": [],
                "targets": [qubit],
                "angle": float(parameters[0]),
                "parameterSource": "shared:omega_0",
                "assetId": asset_id,
                "layer": 0,
            }
        )
    operations.extend(second)
    for layer in range(message_layers):
        operations.extend({**edge, "messageLayerIndex": layer} for edge in edges)
    for qubit, asset_id in enumerate(assets):
        operations.append(
            {
                "op": rotation_axis,
                "controls": [],
                "targets": [qubit],
                "angle": float(parameters[1]),
                "parameterSource": "shared:omega_1",
                "assetId": asset_id,
                "layer": 1,
            }
        )
    if reupload_count == 1:
        operations.extend({**operation, "layer": 3} for operation in [*first, *second])
    operations.append(dict(source[-1]))
    return operations


def _statevector_predictions(
    operations: Sequence[Mapping[str, Any]],
    shared_readout: Mapping[str, Any],
) -> tuple[list[float], dict[str, float]]:
    circuit = Circuit(6)
    for operation in operations:
        qgnn_m2.add_operation(circuit, dict(operation))
    probabilities = {
        str(key): float(value)
        for key, value in StatevectorSimulator(circuit, omp_threads=1).probs().items()
    }
    probability_sum = float(sum(probabilities.values()))
    if not math.isclose(probability_sum, 1.0, rel_tol=0.0, abs_tol=1e-10):
        raise Qas30ProtocolError("QAS30 local statevector probabilities do not normalize")
    z_values = np.asarray(
        [qgnn_m2.z_expectation(probabilities, index) for index in range(6)],
        dtype=float,
    )
    readout = np.asarray(
        [float(shared_readout["scale"]), float(shared_readout["bias"])],
        dtype=float,
    )
    predictions = v42_m3_training.predict_shared_readout(z_values, readout)
    if not np.isfinite(predictions).all():
        raise Qas30ProtocolError("QAS30 local statevector predictions are not finite")
    return [float(value) for value in predictions], probabilities


def _symmetric_spillover(parent_ir: Mapping[str, Any]) -> np.ndarray:
    directed = np.zeros((6, 6), dtype=float)
    for operation in parent_ir["operations"][36:48]:
        source = int(operation["controls"][0])
        affected = int(operation["targets"][0])
        weight = float(operation["weight"])
        if not math.isfinite(weight) or weight < 0:
            raise Qas30ProtocolError("E02 FEVD message weight is invalid")
        directed[affected, source] = weight
    symmetric = (directed + directed.T) / 2.0
    np.fill_diagonal(symmetric, 0.0)
    return symmetric


def _qubo_regret(
    predictions: Sequence[float],
    labels: Sequence[float],
    symmetric_spillover: np.ndarray,
) -> dict[str, Any]:
    predicted_risk = np.asarray(predictions, dtype=float)
    oracle_risk = np.asarray(labels, dtype=float)
    candidates: list[dict[str, Any]] = []
    for selected in itertools.combinations(range(6), QUBO_CARDINALITY):
        x = np.zeros(6, dtype=float)
        x[list(selected)] = 1.0
        bitstring = "".join("1" if index in selected else "0" for index in range(6))
        spillover = QUBO_SPILLOVER_WEIGHT * float(x @ symmetric_spillover @ x)
        candidates.append(
            {
                "bitstring": bitstring,
                "predictedObjective": spillover
                + QUBO_NODE_RISK_WEIGHT * float(predicted_risk @ x),
                "oracleObjective": spillover
                + QUBO_NODE_RISK_WEIGHT * float(oracle_risk @ x),
            }
        )
    predicted_choice = min(
        candidates,
        key=lambda row: (float(row["predictedObjective"]), str(row["bitstring"])),
    )
    oracle_choice = min(
        candidates,
        key=lambda row: (float(row["oracleObjective"]), str(row["bitstring"])),
    )
    degradation = float(predicted_choice["oracleObjective"]) - float(
        oracle_choice["oracleObjective"]
    )
    if degradation < -1e-12:
        raise Qas30ProtocolError("QAS30 exact QUBO degradation became negative")
    return {
        "candidateSelectedBitstring": predicted_choice["bitstring"],
        "oracleSelectedBitstring": oracle_choice["bitstring"],
        "candidateSelectionOracleObjective": float(predicted_choice["oracleObjective"]),
        "oracleObjective": float(oracle_choice["oracleObjective"]),
        "degradation": max(0.0, degradation),
        "enumeratedFeasibleStates": len(candidates),
    }


def derive_local_predictor_features(
    *,
    production_bundle: Mapping[str, Any],
    parent_manifest_bytes: bytes,
    parent_ir_bytes_by_artifact_sha256: Mapping[str, bytes],
    deterministic_inputs_bytes: bytes,
) -> dict[str, Any]:
    """Derive the two frozen pre-hardware features for all 30 candidates."""

    try:
        parent_manifest = json.loads(parent_manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Qas30ProtocolError("E02 parent task manifest is invalid") from error
    latest_bytes = parent_ir_bytes_by_artifact_sha256.get(
        PARENT_CANONICAL_IR_ARTIFACT_SHA256
    )
    if latest_bytes is None:
        raise Qas30ProtocolError("E02 latest parent Canonical IR is missing")
    validated_bundle = validate_production_candidate_bundle(
        production_bundle,
        parent_manifest_bytes=parent_manifest_bytes,
        parent_canonical_ir_bytes=latest_bytes,
        market_data_sha256=MARKET_799_CSV_SHA256,
    )
    deterministic = _validate_deterministic_inputs(deterministic_inputs_bytes)
    if not isinstance(parent_manifest, dict):
        raise Qas30ProtocolError("E02 parent task manifest must be an object")
    task_by_id = {
        str(task["taskId"]): task for task in parent_manifest.get("tasks", [])
    }
    deterministic_by_id = {
        str(row["taskId"]): row for row in deterministic["rows"]
    }
    if set(task_by_id) != set(deterministic_by_id) or len(task_by_id) != 5:
        raise Qas30ProtocolError("E02 task and deterministic input identities differ")
    validated_parent_ir: dict[str, dict[str, Any]] = {}
    for task_id, task in task_by_id.items():
        artifact_sha = str(task["canonicalIrArtifactSha256"])
        value_bytes = parent_ir_bytes_by_artifact_sha256.get(artifact_sha)
        if value_bytes is None:
            raise Qas30ProtocolError(f"E02 parent Canonical IR missing for {task_id}")
        parent_ir = _validate_parent_ir(value_bytes, task)
        if parent_ir["assetOrder"] != deterministic_by_id[task_id]["assetOrder"]:
            raise Qas30ProtocolError("E02 deterministic asset order changed")
        validated_parent_ir[task_id] = parent_ir

    catalog_by_id = {
        str(row["candidateId"]): row for row in validated_bundle["candidateCatalog"]
    }
    artifact_by_id = {
        str(row["candidateId"]): row
        for row in validated_bundle["canonicalIrArtifacts"]
    }
    feature_rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    for candidate_id in sorted(catalog_by_id):
        factors = catalog_by_id[candidate_id]
        parameters = artifact_by_id[candidate_id]["canonicalIr"]["parameters"]
        absolute_errors: list[float] = []
        degradations: list[float] = []
        anchors: list[dict[str, Any]] = []
        for task_id in sorted(task_by_id):
            parent_ir = validated_parent_ir[task_id]
            deterministic_row = deterministic_by_id[task_id]
            operations = _candidate_operations(
                parent_ir,
                rotation_axis=str(factors["rotationAxis"]),
                parameters=parameters,
                message_layers=int(factors["messageLayers"]),
                reupload_count=int(factors["reuploadCount"]),
            )
            predictions, probabilities = _statevector_predictions(
                operations,
                parent_ir["sharedReadout"],
            )
            labels = [float(value) for value in deterministic_row["labels"]]
            anchor_errors = [
                abs(prediction - label)
                for prediction, label in zip(predictions, labels, strict=True)
            ]
            absolute_errors.extend(anchor_errors)
            qubo = _qubo_regret(
                predictions,
                labels,
                _symmetric_spillover(parent_ir),
            )
            degradations.append(float(qubo["degradation"]))
            anchors.append(
                {
                    "taskId": task_id,
                    "formalDate": task_by_id[task_id]["formalDate"],
                    "parentCanonicalIrArtifactSha256": task_by_id[task_id][
                        "canonicalIrArtifactSha256"
                    ],
                    "predictions": predictions,
                    "predictionSha256": _sha256(
                        _canonical_json(predictions).encode("utf-8")
                    ),
                    "probabilityDistributionSha256": _sha256(
                        _canonical_json(probabilities).encode("utf-8")
                    ),
                    "meanAbsoluteError": float(np.mean(anchor_errors)),
                    "qubo": qubo,
                }
            )
        local_validation_loss = float(np.mean(absolute_errors))
        qubo_degradation = float(np.mean(degradations))
        if len(absolute_errors) != 30 or len(degradations) != 5:
            raise Qas30ProtocolError("QAS30 local feature units are incomplete")
        feature_rows.append(
            {
                "candidateId": candidate_id,
                "localValidationLoss": local_validation_loss,
                "quboObjectiveDegradation": qubo_degradation,
            }
        )
        detail_rows.append(
            {
                "candidateId": candidate_id,
                "candidateSpecSha256": factors["candidateSpecSha256"],
                "parameterSha256": artifact_by_id[candidate_id]["parameterSha256"],
                "localValidationLoss": local_validation_loss,
                "quboObjectiveDegradation": qubo_degradation,
                "validationUnitCount": len(absolute_errors),
                "quboAnchorCount": len(degradations),
                "anchors": anchors,
            }
        )
    evidence = {
        "schemaVersion": LOCAL_FEATURE_SCHEMA_VERSION,
        "scientificStatus": "LOCAL_DERIVED_PRE_HARDWARE_FEATURES",
        "definitions": {
            "localValidationLoss": {
                "estimand": "MEAN_ABSOLUTE_ERROR",
                "unit": "E02_FORMAL_ANCHOR_ASSET",
                "anchorCount": 5,
                "assetsPerAnchor": 6,
                "readout": "FROZEN_E02_SHARED_LINEAR_Z_SCALE_BIAS",
            },
            "quboObjectiveDegradation": {
                "estimand": "MEAN_EXACT_ORACLE_OBJECTIVE_REGRET",
                "unit": "E02_FORMAL_ANCHOR",
                "scope": (
                    "LOCAL_E02_NODE_SPILLOVER_ARCHITECTURE_PRESELECTION_SURROGATE"
                ),
                "downstreamUse": "K0_PRE_HARDWARE_QAS_FEATURE",
                "feasibleCardinality": QUBO_CARDINALITY,
                "nodeRiskWeight": QUBO_NODE_RISK_WEIGHT,
                "spilloverWeight": QUBO_SPILLOVER_WEIGHT,
                "spilloverSource": "FROZEN_E02_DIRECTED_EDGE_WEIGHT",
                "selectionTieBreak": "OBJECTIVE_THEN_BITSTRING_ASC",
            },
        },
        "sourceBindings": {
            "productionBundleSha256": validated_bundle["bundleSha256"],
            "parentTaskManifestSha256": _sha256(parent_manifest_bytes),
            "deterministicInputsArtifactSha256": _sha256(
                deterministic_inputs_bytes
            ),
            "parentCanonicalIrArtifactSha256s": sorted(
                parent_ir_bytes_by_artifact_sha256
            ),
            "market799ParentLineageSha256": MARKET_799_CSV_SHA256,
            "market799Role": "PARENT_LINEAGE_ONLY",
            "directComputationInputs": [
                "E02_PARENT_CANONICAL_IR_X5",
                "E02_DETERMINISTIC_LABELS_X5",
            ],
        },
        "featureRows": feature_rows,
        "candidateDetails": detail_rows,
    }
    evidence["evidenceSha256"] = _sha256(_canonical_json(evidence).encode("utf-8"))
    return evidence
