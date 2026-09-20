"""Auditable seven-component score receipts for the QAS30 experiment.

The primary hardware score is derived from a completed, identity-bound query
observation.  Candidate-selection features remain in their predictor receipt;
the four compilation/calibration components come only from the actual five-way
execution mapping.  ``infeasibleRate`` comes only from validated raw counts.
Corrected counts remain a sensitivity view and never replace the primary raw
view in this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from qf_algorithm.legacy.quantum_worker import qas30_protocol, qas30_tianyan

HARDWARE_SCORE_RECEIPT_SCHEMA_VERSION = "qf.qas30.hardware-score-receipt.v1"
R1_SCORE_RECEIPT_SET_SCHEMA_VERSION = "qf.qas30.r1-score-receipt-set.v1"
SCORE_VIEW = "RAW_PRIMARY"
FEASIBILITY_CONTRACT = "EXACT_HAMMING_WEIGHT_3_Q0_Q5"
LOGICAL_BIT_ORDER = tuple(f"Q{index}" for index in range(6))
SCIENTIFIC_STATUSES = {
    qas30_tianyan.OBSERVED_SCIENTIFIC_STATUS,
    qas30_tianyan.FIXTURE_SCIENTIFIC_STATUS,
}
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
COMMIT_PATTERN = re.compile(r"[a-f0-9]{40}")


class Qas30ScoringError(ValueError):
    """A score source binding or frozen score invariant failed."""


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


def _validated_stored_hash(entity: Mapping[str, Any], field_name: str) -> str:
    stored = _require_sha256(entity.get(field_name), field_name)
    unsigned = dict(entity)
    del unsigned[field_name]
    if _entity_sha256(unsigned) != stored:
        raise Qas30ScoringError(f"{field_name} does not match the bound entity")
    return stored


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _require_sha256(value: Any, name: str) -> str:
    if not _valid_sha256(value):
        raise Qas30ScoringError(f"{name} is not a SHA-256 digest")
    return str(value)


def _require_identity(value: Any, name: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value) is None
    ):
        raise Qas30ScoringError(f"{name} is invalid")
    return value


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise Qas30ScoringError(f"{name} is not numeric")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise Qas30ScoringError(f"{name} is not numeric") from error
    if not math.isfinite(numeric):
        raise Qas30ScoringError(f"{name} is not finite")
    return numeric


def _candidate_circuit(
    live_batch: Mapping[str, Any], candidate_id: str
) -> Mapping[str, Any]:
    try:
        validated = qas30_tianyan.validate_live_batch(live_batch)
    except qas30_tianyan.Qas30TianyanError as error:
        raise Qas30ScoringError("live batch validation failed") from error
    circuits = validated.get("circuits")
    matching = [
        circuit
        for circuit in circuits
        if isinstance(circuit, Mapping) and circuit.get("candidateId") == candidate_id
    ] if isinstance(circuits, list) else []
    if len(matching) != 1:
        raise Qas30ScoringError("candidate is not uniquely bound in the live batch")
    return matching[0]


def _submit_binding(
    *,
    live_batch: Mapping[str, Any],
    submit_receipt: Mapping[str, Any],
    circuit: Mapping[str, Any],
) -> Mapping[str, Any]:
    live_batch_sha256 = _entity_sha256(live_batch)
    _validated_stored_hash(submit_receipt, "batchSubmitReceiptSha256")
    if (
        submit_receipt.get("schemaVersion")
        != qas30_tianyan.SUBMIT_RECEIPT_SCHEMA_VERSION
        or submit_receipt.get("state") != "QUERY_IDS_PERSISTED"
        or submit_receipt.get("runId") != live_batch.get("runId")
        or submit_receipt.get("dataEpoch") != live_batch.get("dataEpoch")
        or submit_receipt.get("batchId") != live_batch.get("batchId")
        or submit_receipt.get("sourceCommitSha")
        != live_batch.get("sourceCommitSha")
        or submit_receipt.get("freezeManifestSha256")
        != live_batch.get("freezeManifestSha256")
        or submit_receipt.get("target") != live_batch.get("target")
        or submit_receipt.get("shots") != live_batch.get("shots")
        or submit_receipt.get("liveBatchSha256") != live_batch_sha256
        or submit_receipt.get("selectionEntitySha256")
        != live_batch.get("selectionEntitySha256")
        or submit_receipt.get("predictorFeatureFreezeSha256")
        != live_batch.get("predictorFeatureFreezeSha256")
        or submit_receipt.get("authorizationEnvelopeSha256")
        != live_batch.get("authorizationEnvelopeSha256")
        or submit_receipt.get("resourceSampleSha256")
        != live_batch.get("resourceSampleSha256")
        or submit_receipt.get("scientificStatus") not in SCIENTIFIC_STATUSES
        or submit_receipt.get("transportRequestCount") != 1
        or submit_receipt.get("resubmitAllowed") is not False
    ):
        raise Qas30ScoringError("submit receipt does not bind the live batch")
    bindings = submit_receipt.get("queryBindings")
    if not isinstance(bindings, list) or len(bindings) != 5:
        raise Qas30ScoringError("submit receipt query bindings are incomplete")
    candidate_id = circuit["candidateId"]
    matching = [
        row
        for row in bindings
        if isinstance(row, Mapping) and row.get("candidateId") == candidate_id
    ]
    if len(matching) != 1:
        raise Qas30ScoringError("candidate query binding is not unique")
    binding = matching[0]
    expected_position = live_batch["candidateIds"].index(candidate_id)
    if (
        binding.get("position") != expected_position
        or binding.get("qcisSha256") != circuit.get("qcisSha256")
        or binding.get("regularityReceiptSha256")
        != circuit.get("regularityReceiptSha256")
        or binding.get("predictorFeatureReceiptSha256")
        != circuit.get("predictorFeatureReceiptSha256")
        or not isinstance(binding.get("queryId"), str)
    ):
        raise Qas30ScoringError("candidate query binding changed")
    return binding


def _validated_raw_counts(
    *,
    live_batch: Mapping[str, Any],
    submit_receipt: Mapping[str, Any],
    query_observation: Mapping[str, Any],
    circuit: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> tuple[dict[str, int], int, str, str, str]:
    live_batch_sha256 = _entity_sha256(live_batch)
    submit_receipt_sha256 = _validated_stored_hash(
        submit_receipt, "batchSubmitReceiptSha256"
    )
    query_observation_sha256 = _validated_stored_hash(
        query_observation, "queryObservationSha256"
    )
    expected_order = list(live_batch["commonMeasurementPhysicalOrder"])
    if (
        query_observation.get("schemaVersion")
        != qas30_tianyan.QUERY_OBSERVATION_SCHEMA_VERSION
        or query_observation.get("runId") != live_batch.get("runId")
        or query_observation.get("dataEpoch") != live_batch.get("dataEpoch")
        or query_observation.get("batchId") != live_batch.get("batchId")
        or query_observation.get("candidateId") != circuit.get("candidateId")
        or query_observation.get("queryId") != binding.get("queryId")
        or query_observation.get("qcisSha256") != circuit.get("qcisSha256")
        or query_observation.get("liveBatchSha256") != live_batch_sha256
        or query_observation.get("batchSubmitReceiptSha256")
        != submit_receipt_sha256
        or query_observation.get("sourceCommitSha")
        != live_batch.get("sourceCommitSha")
        or query_observation.get("freezeManifestSha256")
        != live_batch.get("freezeManifestSha256")
        or query_observation.get("predictorFeatureFreezeSha256")
        != live_batch.get("predictorFeatureFreezeSha256")
        or query_observation.get("selectionEntitySha256")
        != live_batch.get("selectionEntitySha256")
        or query_observation.get("measurementOrder") != expected_order
        or query_observation.get("state") != "COMPLETED"
        or query_observation.get("scientificStatus")
        != submit_receipt.get("scientificStatus")
        or query_observation.get("transportRequestCount") != 1
        or query_observation.get("resubmitAllowed") is not False
    ):
        raise Qas30ScoringError("query observation does not bind the submitted circuit")
    for name in ("authorizationEnvelopeSha256", "resourceSampleSha256"):
        _require_sha256(query_observation.get(name), f"query {name}")
    views = query_observation.get("views")
    if (
        not isinstance(views, Mapping)
        or views.get("schemaVersion") != "qf.qas30.raw-corrected-views.v1"
        or views.get("measurementOrder") != expected_order
    ):
        raise Qas30ScoringError("completed query has no validated raw result view")
    raw = views.get("raw")
    if not isinstance(raw, Mapping):
        raise Qas30ScoringError("completed query raw result view is missing")
    shots = raw.get("shots")
    counts = raw.get("counts")
    if (
        not isinstance(shots, int)
        or isinstance(shots, bool)
        or shots != live_batch.get("shots")
        or not isinstance(counts, Mapping)
    ):
        raise Qas30ScoringError("raw result shot identity changed")
    normalized: dict[str, int] = {}
    for bitstring, count in counts.items():
        if (
            not isinstance(bitstring, str)
            or len(bitstring) != len(LOGICAL_BIT_ORDER)
            or any(character not in "01" for character in bitstring)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            raise Qas30ScoringError("raw result counts are invalid")
        normalized[bitstring] = count
    ordered_counts = dict(sorted(normalized.items()))
    if sum(ordered_counts.values()) != shots:
        raise Qas30ScoringError("raw result shots are not conserved")
    counts_sha256 = hashlib.sha256(
        _canonical_json(ordered_counts).encode("utf-8")
    ).hexdigest()
    if raw.get("countsSha256") != counts_sha256:
        raise Qas30ScoringError("raw result counts hash changed")
    return (
        normalized,
        shots,
        live_batch_sha256,
        submit_receipt_sha256,
        query_observation_sha256,
    )


def build_hardware_score_receipt(
    *,
    live_batch: Mapping[str, Any],
    submit_receipt: Mapping[str, Any],
    query_observation: Mapping[str, Any],
    candidate_id: str,
) -> dict[str, Any]:
    """Build one primary S_HW receipt from immutable submitted/query entities."""

    if candidate_id not in qas30_protocol.CANDIDATE_IDS:
        raise Qas30ScoringError("candidateId is invalid")
    circuit = _candidate_circuit(live_batch, candidate_id)
    binding = _submit_binding(
        live_batch=live_batch,
        submit_receipt=submit_receipt,
        circuit=circuit,
    )
    (
        counts,
        shots,
        live_batch_sha256,
        submit_receipt_sha256,
        query_observation_sha256,
    ) = _validated_raw_counts(
        live_batch=live_batch,
        submit_receipt=submit_receipt,
        query_observation=query_observation,
        circuit=circuit,
        binding=binding,
    )
    feature_receipt = circuit.get("predictorFeatureReceipt")
    if not isinstance(feature_receipt, Mapping):
        raise Qas30ScoringError("predictor feature receipt is missing")
    pre = feature_receipt.get("preHardwareFeatures")
    execution = circuit.get("executionMappingFeatures")
    if not isinstance(pre, Mapping) or not isinstance(execution, Mapping):
        raise Qas30ScoringError("selection or execution feature components are missing")
    raw_components = {
        "localValidationLoss": _finite_number(
            pre.get("localValidationLoss"), "localValidationLoss"
        ),
        "quboObjectiveDegradation": _finite_number(
            pre.get("quboObjectiveDegradation"), "quboObjectiveDegradation"
        ),
        "infeasibleRate": 1.0
        - sum(
            count for bitstring, count in counts.items() if bitstring.count("1") == 3
        )
        / shots,
        "compiledDepth": _finite_number(execution.get("compiledDepth"), "compiledDepth"),
        "twoQubitGates": _finite_number(
            execution.get("twoQubitGates"), "twoQubitGates"
        ),
        "swapCount": _finite_number(execution.get("swapCount"), "swapCount"),
        "calibrationNoiseProxy": _finite_number(
            execution.get("calibrationNoiseProxy"), "calibrationNoiseProxy"
        ),
    }
    if set(raw_components) != set(qas30_protocol.HARDWARE_SCORE_COMPONENTS) or any(
        value < 0.0 for value in raw_components.values()
    ):
        raise Qas30ScoringError("raw score components are invalid")
    normalization = live_batch.get("normalizationManifest")
    if not isinstance(normalization, Mapping):
        raise Qas30ScoringError("normalization entity is missing")
    try:
        normalized_components = qas30_protocol.normalize_hardware_components(
            raw_components, normalization
        )
        score = qas30_protocol.equal_weight_hardware_score(normalized_components)
    except qas30_protocol.Qas30ProtocolError as error:
        raise Qas30ScoringError("hardware score normalization failed") from error
    feasible_shots = sum(
        count for bitstring, count in counts.items() if bitstring.count("1") == 3
    )
    weights = {
        name: 1.0 / len(qas30_protocol.HARDWARE_SCORE_COMPONENTS)
        for name in qas30_protocol.HARDWARE_SCORE_COMPONENTS
    }
    raw_view = query_observation["views"]["raw"]
    receipt = {
        "schemaVersion": HARDWARE_SCORE_RECEIPT_SCHEMA_VERSION,
        "runId": live_batch["runId"],
        "dataEpoch": live_batch["dataEpoch"],
        "blockId": live_batch["blockId"],
        "batchId": live_batch["batchId"],
        "stage": live_batch["stage"],
        "candidateId": candidate_id,
        "sourceCommitSha": live_batch["sourceCommitSha"],
        "freezeManifestSha256": live_batch["freezeManifestSha256"],
        "normalizationSha256": live_batch["normalizationSha256"],
        "predictorFeatureFreezeSha256": live_batch[
            "predictorFeatureFreezeSha256"
        ],
        "predictorFeatureReceiptSha256": circuit[
            "predictorFeatureReceiptSha256"
        ],
        "executionMappingFeatureSha256": circuit["executionMappingFeatureSha256"],
        "liveBatchSha256": live_batch_sha256,
        "batchSubmitReceiptSha256": submit_receipt_sha256,
        "queryObservationSha256": query_observation_sha256,
        "queryId": binding["queryId"],
        "qcisSha256": circuit["qcisSha256"],
        "measurementOrder": list(live_batch["commonMeasurementPhysicalOrder"]),
        "logicalBitOrder": list(LOGICAL_BIT_ORDER),
        "scoreView": SCORE_VIEW,
        "feasibilityContract": FEASIBILITY_CONTRACT,
        "shots": shots,
        "feasibleShots": feasible_shots,
        "infeasibleShots": shots - feasible_shots,
        "rawCountsSha256": raw_view["countsSha256"],
        "rawComponents": raw_components,
        "normalizedComponents": normalized_components,
        "weights": weights,
        "sHw": score,
        "submissionAuthorizationEnvelopeSha256": submit_receipt[
            "authorizationEnvelopeSha256"
        ],
        "submissionResourceSampleSha256": submit_receipt["resourceSampleSha256"],
        "queryAuthorizationEnvelopeSha256": query_observation[
            "authorizationEnvelopeSha256"
        ],
        "queryResourceSampleSha256": query_observation["resourceSampleSha256"],
        "scientificStatus": query_observation["scientificStatus"],
    }
    receipt["scoreReceiptSha256"] = _entity_sha256(receipt)
    return receipt


def validate_hardware_score_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the immutable score entity and its internal arithmetic."""

    required = {
        "schemaVersion",
        "runId",
        "dataEpoch",
        "blockId",
        "batchId",
        "stage",
        "candidateId",
        "sourceCommitSha",
        "freezeManifestSha256",
        "normalizationSha256",
        "predictorFeatureFreezeSha256",
        "predictorFeatureReceiptSha256",
        "executionMappingFeatureSha256",
        "liveBatchSha256",
        "batchSubmitReceiptSha256",
        "queryObservationSha256",
        "queryId",
        "qcisSha256",
        "measurementOrder",
        "logicalBitOrder",
        "scoreView",
        "feasibilityContract",
        "shots",
        "feasibleShots",
        "infeasibleShots",
        "rawCountsSha256",
        "rawComponents",
        "normalizedComponents",
        "weights",
        "sHw",
        "submissionAuthorizationEnvelopeSha256",
        "submissionResourceSampleSha256",
        "queryAuthorizationEnvelopeSha256",
        "queryResourceSampleSha256",
        "scientificStatus",
        "scoreReceiptSha256",
    }
    unhashed = {
        key: value for key, value in receipt.items() if key != "scoreReceiptSha256"
    }
    if (
        set(receipt) != required
        or receipt.get("schemaVersion") != HARDWARE_SCORE_RECEIPT_SCHEMA_VERSION
        or receipt.get("scoreReceiptSha256") != _entity_sha256(unhashed)
        or receipt.get("candidateId") not in qas30_protocol.CANDIDATE_IDS
        or receipt.get("scoreView") != SCORE_VIEW
        or receipt.get("feasibilityContract") != FEASIBILITY_CONTRACT
        or receipt.get("logicalBitOrder") != list(LOGICAL_BIT_ORDER)
        or receipt.get("scientificStatus") not in SCIENTIFIC_STATUSES
        or not isinstance(receipt.get("sourceCommitSha"), str)
        or COMMIT_PATTERN.fullmatch(str(receipt.get("sourceCommitSha"))) is None
    ):
        raise Qas30ScoringError("hardware score receipt identity is invalid")
    for name in (
        "freezeManifestSha256",
        "normalizationSha256",
        "predictorFeatureFreezeSha256",
        "predictorFeatureReceiptSha256",
        "executionMappingFeatureSha256",
        "liveBatchSha256",
        "batchSubmitReceiptSha256",
        "queryObservationSha256",
        "qcisSha256",
        "rawCountsSha256",
        "submissionAuthorizationEnvelopeSha256",
        "submissionResourceSampleSha256",
        "queryAuthorizationEnvelopeSha256",
        "queryResourceSampleSha256",
    ):
        _require_sha256(receipt.get(name), name)
    for name in ("runId", "dataEpoch", "blockId", "batchId", "queryId"):
        _require_identity(receipt.get(name), name)
    _require_identity(receipt.get("stage"), "stage")
    order = receipt.get("measurementOrder")
    if (
        not isinstance(order, list)
        or len(order) != 6
        or len(set(order)) != 6
        or any(
            not isinstance(item, str) or re.fullmatch(r"Q\d{1,4}", item) is None
            for item in order
        )
    ):
        raise Qas30ScoringError("score receipt measurement order is invalid")
    shots = receipt.get("shots")
    feasible = receipt.get("feasibleShots")
    infeasible = receipt.get("infeasibleShots")
    if (
        not isinstance(shots, int)
        or isinstance(shots, bool)
        or shots <= 0
        or not isinstance(feasible, int)
        or isinstance(feasible, bool)
        or not isinstance(infeasible, int)
        or isinstance(infeasible, bool)
        or feasible < 0
        or infeasible < 0
        or feasible + infeasible != shots
    ):
        raise Qas30ScoringError("score receipt shot arithmetic is invalid")
    raw = receipt.get("rawComponents")
    normalized = receipt.get("normalizedComponents")
    weights = receipt.get("weights")
    components = set(qas30_protocol.HARDWARE_SCORE_COMPONENTS)
    if (
        not isinstance(raw, Mapping)
        or set(raw) != components
        or not isinstance(normalized, Mapping)
        or set(normalized) != components
        or not isinstance(weights, Mapping)
        or set(weights) != components
    ):
        raise Qas30ScoringError("score receipt components are incomplete")
    raw_values = {name: _finite_number(raw[name], name) for name in components}
    normalized_values = {
        name: _finite_number(normalized[name], name) for name in components
    }
    weight_values = {name: _finite_number(weights[name], name) for name in components}
    if (
        any(value < 0.0 for value in raw_values.values())
        or any(not 0.0 <= value <= 1.0 for value in normalized_values.values())
        or any(value < 0.0 for value in weight_values.values())
        or not math.isclose(sum(weight_values.values()), 1.0, abs_tol=1e-12)
        or any(
            not math.isclose(
                value,
                1.0 / len(qas30_protocol.HARDWARE_SCORE_COMPONENTS),
                abs_tol=1e-12,
            )
            for value in weight_values.values()
        )
        or not math.isclose(
            raw_values["infeasibleRate"], infeasible / shots, abs_tol=1e-12
        )
    ):
        raise Qas30ScoringError("score receipt component arithmetic is invalid")
    expected_score = sum(
        normalized_values[name] * weight_values[name] for name in components
    )
    score = _finite_number(receipt.get("sHw"), "sHw")
    if not 0.0 <= score <= 1.0 or not math.isclose(
        score, expected_score, abs_tol=1e-12
    ):
        raise Qas30ScoringError("score receipt S_HW arithmetic is invalid")
    return dict(receipt)


def build_r1_score_receipt_set(
    *,
    score_receipts: Sequence[Mapping[str, Any]],
    cover_candidate_ids: Sequence[str],
) -> dict[str, Any]:
    """Bind the ten R1 candidate scores that are eligible to train Ridge-QAS."""

    if (
        isinstance(score_receipts, str | bytes)
        or isinstance(cover_candidate_ids, str | bytes)
        or len(score_receipts) != 10
        or len(cover_candidate_ids) != 10
        or len(set(cover_candidate_ids)) != 10
        or not set(cover_candidate_ids) <= set(qas30_protocol.CANDIDATE_IDS)
    ):
        raise Qas30ScoringError("R1 score set requires the frozen cover10")
    validated = [validate_hardware_score_receipt(row) for row in score_receipts]
    if [row["candidateId"] for row in validated] != list(cover_candidate_ids):
        raise Qas30ScoringError("R1 score receipt order changed")
    shared_names = (
        "runId",
        "dataEpoch",
        "blockId",
        "sourceCommitSha",
        "freezeManifestSha256",
        "normalizationSha256",
        "predictorFeatureFreezeSha256",
        "scientificStatus",
    )
    shared = {name: validated[0][name] for name in shared_names}
    if any(
        row["stage"] != "R1"
        or any(row[name] != shared[name] for name in shared_names)
        for row in validated
    ):
        raise Qas30ScoringError("R1 score receipts do not share one frozen block")
    batch_counts = Counter(row["batchId"] for row in validated)
    if sorted(batch_counts.values()) != [5, 5]:
        raise Qas30ScoringError("R1 score receipts do not contain two five-circuit batches")
    summaries = [
        {
            "candidateId": row["candidateId"],
            "dataEpoch": row["dataEpoch"],
            "freezeManifestSha256": row["freezeManifestSha256"],
            "batchId": row["batchId"],
            "queryId": row["queryId"],
            "queryObservationSha256": row["queryObservationSha256"],
            "scoreReceiptSha256": row["scoreReceiptSha256"],
            "sHw": row["sHw"],
        }
        for row in validated
    ]
    result = {
        "schemaVersion": R1_SCORE_RECEIPT_SET_SCHEMA_VERSION,
        **shared,
        "candidateIds": list(cover_candidate_ids),
        "scoreView": SCORE_VIEW,
        "feasibilityContract": FEASIBILITY_CONTRACT,
        "receipts": summaries,
    }
    result["scoreReceiptSetSha256"] = _entity_sha256(result)
    return result


def validate_r1_score_receipt_set(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the compact, hash-bound ten-score selection input."""

    required = {
        "schemaVersion",
        "runId",
        "dataEpoch",
        "blockId",
        "sourceCommitSha",
        "freezeManifestSha256",
        "normalizationSha256",
        "predictorFeatureFreezeSha256",
        "scientificStatus",
        "candidateIds",
        "scoreView",
        "feasibilityContract",
        "receipts",
        "scoreReceiptSetSha256",
    }
    unhashed = {
        key: item for key, item in value.items() if key != "scoreReceiptSetSha256"
    }
    candidates = value.get("candidateIds")
    receipts = value.get("receipts")
    if (
        set(value) != required
        or value.get("schemaVersion") != R1_SCORE_RECEIPT_SET_SCHEMA_VERSION
        or value.get("scoreReceiptSetSha256") != _entity_sha256(unhashed)
        or value.get("scoreView") != SCORE_VIEW
        or value.get("feasibilityContract") != FEASIBILITY_CONTRACT
        or value.get("scientificStatus") not in SCIENTIFIC_STATUSES
        or not isinstance(candidates, list)
        or len(candidates) != 10
        or len(set(candidates)) != 10
        or not set(candidates) <= set(qas30_protocol.CANDIDATE_IDS)
        or not isinstance(receipts, list)
        or len(receipts) != 10
    ):
        raise Qas30ScoringError("R1 score receipt set identity is invalid")
    for name in ("runId", "dataEpoch", "blockId"):
        _require_identity(value.get(name), name)
    if (
        not isinstance(value.get("sourceCommitSha"), str)
        or COMMIT_PATTERN.fullmatch(str(value.get("sourceCommitSha"))) is None
    ):
        raise Qas30ScoringError("R1 score receipt set source commit is invalid")
    for name in (
        "freezeManifestSha256",
        "normalizationSha256",
        "predictorFeatureFreezeSha256",
    ):
        _require_sha256(value.get(name), name)
    required_summary = {
        "candidateId",
        "dataEpoch",
        "freezeManifestSha256",
        "batchId",
        "queryId",
        "queryObservationSha256",
        "scoreReceiptSha256",
        "sHw",
    }
    batch_counts: Counter[str] = Counter()
    for candidate_id, row in zip(candidates, receipts, strict=True):
        if (
            not isinstance(row, Mapping)
            or set(row) != required_summary
            or row.get("candidateId") != candidate_id
            or row.get("dataEpoch") != value.get("dataEpoch")
            or row.get("freezeManifestSha256")
            != value.get("freezeManifestSha256")
        ):
            raise Qas30ScoringError("R1 score receipt summaries are invalid")
        _require_identity(row.get("batchId"), "batchId")
        _require_identity(row.get("queryId"), "queryId")
        _require_sha256(row.get("queryObservationSha256"), "queryObservationSha256")
        _require_sha256(row.get("scoreReceiptSha256"), "scoreReceiptSha256")
        score = _finite_number(row.get("sHw"), "sHw")
        if not 0.0 <= score <= 1.0:
            raise Qas30ScoringError("R1 S_HW is outside [0,1]")
        batch_counts[str(row["batchId"])] += 1
    if sorted(batch_counts.values()) != [5, 5]:
        raise Qas30ScoringError("R1 score set does not bind two five-circuit batches")
    return dict(value)
