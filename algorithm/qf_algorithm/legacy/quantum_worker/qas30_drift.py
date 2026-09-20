"""Pure, fail-closed drift gates for the frozen QAS30 experiment.

The module is deliberately local-only: it does not read files or environment
variables, persist artifacts, or contact a provider.  Callers supply complete
self-hashed evidence entities and persist the returned receipt separately.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from . import qas30_tianyan

TARGET = "tianyan176"
HISTORICAL_SCOPE = "P15_G1_G7"
MINIMUM_THRESHOLD = 0.02
QUANTILE_PROBABILITY = 0.95
METRIC = "MAX_ABSOLUTE_ERROR_VECTOR_DELTA"
VECTOR_DEFINITION = (
    "READOUT_ERROR_FOR_SIX_MAPPED_QUBITS",
    "TWO_QUBIT_ERROR_FOR_USED_COUPLERS",
)

HISTORICAL_VECTOR_SCHEMA_VERSION = "qf.qas30.historical-calibration-vector.v1"
THRESHOLD_SCHEMA_VERSION = "qf.qas30.drift-threshold.v1"
MAPPING_BINDING_SCHEMA_VERSION = "qf.qas30.drift-mapping-binding.v1"
DRIFT_RECEIPT_SCHEMA_VERSION = "qf.qas30.drift-receipt.v1"
NORMALIZED_TOPOLOGY_SCHEMA_VERSION = "qf.qas30.normalized-topology.v1"
CALIBRATION_CONFIG_CONTRACT = "CQLIB_1_3_11_DOWNLOAD_CONFIG"

OBSERVED_STATUS = qas30_tianyan.OBSERVED_SCIENTIFIC_STATUS
FIXTURE_STATUS = qas30_tianyan.FIXTURE_SCIENTIFIC_STATUS
SCIENTIFIC_STATUSES = {OBSERVED_STATUS, FIXTURE_STATUS}

SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
COMMIT_PATTERN = re.compile(r"[a-f0-9]{40}")
IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
QUBIT_PATTERN = re.compile(r"Q\d{1,4}")
COUPLER_PATTERN = re.compile(r"G\d{1,4}")


class Qas30DriftError(ValueError):
    """A threshold, calibration, mapping, or drift invariant failed."""

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


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _require_sha256(value: Any, name: str) -> str:
    if not _valid_sha256(value):
        raise Qas30DriftError("QAS30_DRIFT_BINDING_INVALID", f"{name} is invalid")
    return str(value)


def _require_identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or IDENTITY_PATTERN.fullmatch(value) is None:
        raise Qas30DriftError("QAS30_DRIFT_IDENTITY_INVALID", f"{name} is invalid")
    return value


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise Qas30DriftError("QAS30_DRIFT_NUMERIC_INVALID", f"{name} is invalid")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise Qas30DriftError(
            "QAS30_DRIFT_NUMERIC_INVALID", f"{name} is invalid"
        ) from error
    if not math.isfinite(numeric) or numeric < 0.0:
        raise Qas30DriftError("QAS30_DRIFT_NUMERIC_INVALID", f"{name} is invalid")
    return numeric


def _require_status(value: Any) -> str:
    if value not in SCIENTIFIC_STATUSES:
        raise Qas30DriftError(
            "QAS30_DRIFT_SCIENTIFIC_STATUS_INVALID",
            "scientific status is invalid",
        )
    return str(value)


def _require_commit(value: Any) -> str:
    if not isinstance(value, str) or COMMIT_PATTERN.fullmatch(value) is None:
        raise Qas30DriftError(
            "QAS30_DRIFT_BINDING_INVALID", "sourceCommitSha is invalid"
        )
    return value


def _require_timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise Qas30DriftError("QAS30_DRIFT_TIMESTAMP_INVALID", f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Qas30DriftError(
            "QAS30_DRIFT_TIMESTAMP_INVALID", f"{name} is invalid"
        ) from error
    if parsed.tzinfo is None:
        raise Qas30DriftError("QAS30_DRIFT_TIMESTAMP_INVALID", f"{name} is invalid")
    return value


def type7_quantile(values: Sequence[float], probability: float) -> float:
    """Return the Hyndman-Fan Type-7 sample quantile used by R and NumPy."""

    if isinstance(values, str | bytes) or not isinstance(values, Sequence) or not values:
        raise Qas30DriftError(
            "QAS30_DRIFT_HISTORY_INCOMPLETE", "quantile values are incomplete"
        )
    if isinstance(probability, bool) or not isinstance(probability, int | float):
        raise Qas30DriftError(
            "QAS30_DRIFT_QUANTILE_INVALID", "quantile probability is invalid"
        )
    probability_value = float(probability)
    if not math.isfinite(probability_value) or not 0.0 <= probability_value <= 1.0:
        raise Qas30DriftError(
            "QAS30_DRIFT_QUANTILE_INVALID", "quantile probability is invalid"
        )
    ordered = sorted(_finite_nonnegative(item, "quantile value") for item in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability_value
    lower = math.floor(position)
    fraction = position - lower
    if lower == len(ordered) - 1:
        return ordered[-1]
    return ordered[lower] + fraction * (ordered[lower + 1] - ordered[lower])


def _validate_historical_vector(value: Mapping[str, Any], expected_index: int) -> dict[str, Any]:
    required = {
        "schemaVersion",
        "historicalScope",
        "snapshotId",
        "sequenceIndex",
        "machine",
        "sourcePath",
        "errorVector",
        "scientificStatus",
        "sourceArtifactSha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise Qas30DriftError(
            "QAS30_DRIFT_HISTORY_INVALID", "historical vector fields changed"
        )
    stored_sha = value.get("sourceArtifactSha256")
    unhashed = {key: item for key, item in value.items() if key != "sourceArtifactSha256"}
    source_path = value.get("sourcePath")
    vector = value.get("errorVector")
    if (
        value.get("schemaVersion") != HISTORICAL_VECTOR_SCHEMA_VERSION
        or value.get("historicalScope") != HISTORICAL_SCOPE
        or value.get("snapshotId") != f"G{expected_index}"
        or value.get("sequenceIndex") != expected_index
        or value.get("machine") != TARGET
        or not isinstance(source_path, str)
        or not source_path
        or source_path.startswith(("/", "~"))
        or ".." in source_path.split("/")
        or not isinstance(vector, Mapping)
        or not vector
        or stored_sha != _sha256(unhashed)
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_HISTORY_INVALID", "historical vector binding changed"
        )
    status = _require_status(value.get("scientificStatus"))
    normalized: dict[str, float] = {}
    for raw_name, raw_value in vector.items():
        if not isinstance(raw_name, str) or not raw_name:
            raise Qas30DriftError(
                "QAS30_DRIFT_HISTORY_INVALID", "historical vector key is invalid"
            )
        normalized[raw_name] = _finite_nonnegative(
            raw_value, f"historical error vector {raw_name}"
        )
    result = dict(value)
    result["scientificStatus"] = status
    result["errorVector"] = normalized
    return result


def build_frozen_threshold_entity(
    *,
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
    historical_snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a frozen Type-7 threshold from seven complete P15 vectors.

    The function intentionally rejects an empty or partial history.  The
    current protocol's documented 0.02 floor remains a distinct preregistered
    parameter until seven complete, source-bound vectors are available.
    """

    _require_identity(run_id, "runId")
    _require_identity(data_epoch, "dataEpoch")
    _require_commit(source_commit_sha)
    _require_sha256(freeze_manifest_sha256, "freezeManifestSha256")
    if (
        isinstance(historical_snapshots, str | bytes)
        or not isinstance(historical_snapshots, Sequence)
        or len(historical_snapshots) != 7
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_HISTORY_INCOMPLETE",
            "P15 G1-G7 requires seven complete historical vectors",
        )
    snapshots = [
        _validate_historical_vector(item, index)
        for index, item in enumerate(historical_snapshots, start=1)
    ]
    statuses = {item["scientificStatus"] for item in snapshots}
    vector_keys = {tuple(sorted(item["errorVector"])) for item in snapshots}
    source_hashes = {item["sourceArtifactSha256"] for item in snapshots}
    source_paths = {item["sourcePath"] for item in snapshots}
    if len(statuses) != 1:
        raise Qas30DriftError(
            "QAS30_DRIFT_SCIENTIFIC_STATUS_MIXED",
            "historical observations mix scientific statuses",
        )
    if len(vector_keys) != 1 or len(source_hashes) != 7 or len(source_paths) != 7:
        raise Qas30DriftError(
            "QAS30_DRIFT_HISTORY_INVALID",
            "historical vectors or source bindings are not one-to-one",
        )

    adjacent_rows: list[dict[str, Any]] = []
    adjacent_maxima: list[float] = []
    for left, right in zip(snapshots[:-1], snapshots[1:], strict=True):
        deltas = {
            name: abs(right["errorVector"][name] - left["errorVector"][name])
            for name in sorted(left["errorVector"])
        }
        maximum = max(deltas.values())
        adjacent_maxima.append(maximum)
        adjacent_rows.append(
            {
                "leftSnapshotId": left["snapshotId"],
                "rightSnapshotId": right["snapshotId"],
                "leftSourceArtifactSha256": left["sourceArtifactSha256"],
                "rightSourceArtifactSha256": right["sourceArtifactSha256"],
                "componentAbsoluteDeltaSha256": _sha256(deltas),
                "maxAbsoluteErrorDelta": maximum,
            }
        )

    quantile = type7_quantile(adjacent_maxima, QUANTILE_PROBABILITY)
    threshold = max(MINIMUM_THRESHOLD, quantile)
    source_bindings = [
        {
            "snapshotId": item["snapshotId"],
            "sourcePath": item["sourcePath"],
            "sourceArtifactSha256": item["sourceArtifactSha256"],
            "errorVectorSha256": _sha256(item["errorVector"]),
        }
        for item in snapshots
    ]
    entity: dict[str, Any] = {
        "schemaVersion": THRESHOLD_SCHEMA_VERSION,
        "runId": run_id,
        "dataEpoch": data_epoch,
        "sourceCommitSha": source_commit_sha,
        "freezeManifestSha256": freeze_manifest_sha256,
        "target": TARGET,
        "metric": METRIC,
        "historicalScope": HISTORICAL_SCOPE,
        "historicalUniqueSnapshotIds": 7,
        "historicalCompleteNumericSnapshots": 7,
        "historicalAdjacentDeltaCount": 6,
        "historicalAdjacentMaxDeltas": adjacent_rows,
        "historicalType7QuantileProbability": QUANTILE_PROBABILITY,
        "historicalType7Quantile95": quantile,
        "minimumThreshold": MINIMUM_THRESHOLD,
        "frozenThreshold": threshold,
        "selectionRule": "MAXIMUM_OF_TYPE7_95_AND_PROTOCOL_MINIMUM",
        "requiredLiveVector": list(VECTOR_DEFINITION),
        "sourceBindings": source_bindings,
        "sourceBindingsSha256": _sha256(source_bindings),
        "thresholdPlanSha256": None,
        "thresholdBasisSha256": _sha256(
            {
                "sourceBindings": source_bindings,
                "historicalAdjacentMaxDeltas": adjacent_rows,
            }
        ),
        "parameterStatus": "FROZEN_PROTOCOL_PARAMETER",
        "scientificStatus": statuses.pop(),
    }
    entity["driftThresholdSha256"] = _sha256(entity)
    return entity


def build_protocol_floor_threshold_entity(
    *,
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
    threshold_plan: Mapping[str, Any],
    source_artifact_bytes: Mapping[str, bytes],
    scientific_status: str,
) -> dict[str, Any]:
    """Freeze the preregistered 0.02 floor when P15 deltas are not estimable.

    This branch preserves ``q95=null`` and an empty derived-delta list.  It
    verifies the raw source bytes named by the plan, so missing history is
    represented as an audited boundary rather than as a fabricated zero delta.
    """

    _require_identity(run_id, "runId")
    _require_identity(data_epoch, "dataEpoch")
    _require_commit(source_commit_sha)
    _require_sha256(freeze_manifest_sha256, "freezeManifestSha256")
    status = _require_status(scientific_status)
    plan_required = {
        "schemaVersion",
        "metric",
        "historicalScope",
        "historicalUniqueSnapshotIds",
        "historicalCompleteNumericSnapshots",
        "historicalType7Quantile95",
        "minimumThreshold",
        "frozenThreshold",
        "selectionRule",
        "requiredLiveVector",
        "sourceBindings",
        "liveRules",
        "scientificStatus",
    }
    bindings = threshold_plan.get("sourceBindings") if isinstance(threshold_plan, Mapping) else None
    if (
        not isinstance(threshold_plan, Mapping)
        or set(threshold_plan) != plan_required
        or threshold_plan.get("schemaVersion") != "qf.qas30.drift-threshold-plan.v1"
        or threshold_plan.get("metric") != METRIC
        or threshold_plan.get("historicalScope") != HISTORICAL_SCOPE
        or threshold_plan.get("historicalUniqueSnapshotIds") != 2
        or threshold_plan.get("historicalCompleteNumericSnapshots") != 1
        or threshold_plan.get("historicalType7Quantile95") is not None
        or threshold_plan.get("minimumThreshold") != MINIMUM_THRESHOLD
        or threshold_plan.get("frozenThreshold") != MINIMUM_THRESHOLD
        or threshold_plan.get("selectionRule")
        != "MINIMUM_THRESHOLD_WHEN_HISTORICAL_NUMERIC_DELTA_IS_NOT_ESTIMABLE"
        or threshold_plan.get("requiredLiveVector") != list(VECTOR_DEFINITION)
        or threshold_plan.get("scientificStatus") != "FROZEN_PROTOCOL_PARAMETER"
        or not isinstance(bindings, list)
        or len(bindings) != 2
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_THRESHOLD_PLAN_INVALID", "protocol floor plan changed"
        )
    live_rules = threshold_plan.get("liveRules")
    if live_rules != {
        "k0ToK1AboveThreshold": "STOP_BEFORE_R2",
        "k1ToK2AboveThreshold": "RETAIN_AND_STRATIFY_COMPLETED_DATA",
        "missingRequiredVectorCoverage": "STOP_BEFORE_NEXT_SUBMIT",
    }:
        raise Qas30DriftError(
            "QAS30_DRIFT_THRESHOLD_PLAN_INVALID", "protocol floor live rules changed"
        )
    if not isinstance(source_artifact_bytes, Mapping):
        raise Qas30DriftError(
            "QAS30_DRIFT_SOURCE_INVALID", "protocol floor source bytes are missing"
        )
    expected_paths: set[str] = set()
    parsed_by_name: dict[str, Any] = {}
    frozen_bindings: list[dict[str, str]] = []
    for binding in bindings:
        if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256"}:
            raise Qas30DriftError(
                "QAS30_DRIFT_SOURCE_INVALID", "protocol floor source binding is invalid"
            )
        path = binding.get("path")
        expected_sha = binding.get("sha256")
        if not isinstance(path, str) or not path or path in expected_paths:
            raise Qas30DriftError(
                "QAS30_DRIFT_SOURCE_INVALID", "protocol floor source path is invalid"
            )
        expected_paths.add(path)
        _require_sha256(expected_sha, "source sha256")
        raw = source_artifact_bytes.get(path)
        if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != expected_sha:
            raise Qas30DriftError(
                "QAS30_DRIFT_SOURCE_INVALID", "protocol floor source hash changed"
            )
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Qas30DriftError(
                "QAS30_DRIFT_SOURCE_INVALID", "protocol floor source is not JSON"
            ) from error
        parsed_by_name[path.rsplit("/", 1)[-1]] = parsed
        frozen_bindings.append({"path": path, "sha256": str(expected_sha)})
    if set(source_artifact_bytes) != expected_paths:
        raise Qas30DriftError(
            "QAS30_DRIFT_SOURCE_INVALID", "protocol floor source set changed"
        )
    diff = parsed_by_name.get("calibration_diff.json")
    manifest = parsed_by_name.get("calibration_manifest.json")
    if (
        not isinstance(diff, Mapping)
        or diff.get("schema_version") != "qf.machine-calibration-diff.v1"
        or diff.get("previous_snapshot") is not None
        or not isinstance(diff.get("selected_qubits_drift"), list)
        or diff.get("selected_qubits_drift")
        or not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != "qf.machine-calibration-manifest.v1"
        or manifest.get("machine_name") != TARGET
        or manifest.get("data_completeness") != "COMPLETE"
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_SOURCE_INVALID",
            "protocol floor sources do not prove a non-estimable adjacent sequence",
        )
    plan_sha = _sha256(threshold_plan)
    entity: dict[str, Any] = {
        "schemaVersion": THRESHOLD_SCHEMA_VERSION,
        "runId": run_id,
        "dataEpoch": data_epoch,
        "sourceCommitSha": source_commit_sha,
        "freezeManifestSha256": freeze_manifest_sha256,
        "target": TARGET,
        "metric": METRIC,
        "historicalScope": HISTORICAL_SCOPE,
        "historicalUniqueSnapshotIds": 2,
        "historicalCompleteNumericSnapshots": 1,
        "historicalAdjacentDeltaCount": 0,
        "historicalAdjacentMaxDeltas": [],
        "historicalType7QuantileProbability": QUANTILE_PROBABILITY,
        "historicalType7Quantile95": None,
        "minimumThreshold": MINIMUM_THRESHOLD,
        "frozenThreshold": MINIMUM_THRESHOLD,
        "selectionRule": "MINIMUM_THRESHOLD_WHEN_HISTORICAL_NUMERIC_DELTA_IS_NOT_ESTIMABLE",
        "requiredLiveVector": list(VECTOR_DEFINITION),
        "sourceBindings": frozen_bindings,
        "sourceBindingsSha256": _sha256(frozen_bindings),
        "thresholdPlanSha256": plan_sha,
        "thresholdBasisSha256": _sha256(
            {"thresholdPlanSha256": plan_sha, "sourceBindings": frozen_bindings}
        ),
        "parameterStatus": "FROZEN_PROTOCOL_PARAMETER",
        "scientificStatus": status,
    }
    entity["driftThresholdSha256"] = _sha256(entity)
    return entity


def validate_frozen_threshold_entity(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a complete empirical threshold entity and its source bindings."""

    required = {
        "schemaVersion",
        "runId",
        "dataEpoch",
        "sourceCommitSha",
        "freezeManifestSha256",
        "target",
        "metric",
        "historicalScope",
        "historicalUniqueSnapshotIds",
        "historicalCompleteNumericSnapshots",
        "historicalAdjacentDeltaCount",
        "historicalAdjacentMaxDeltas",
        "historicalType7QuantileProbability",
        "historicalType7Quantile95",
        "minimumThreshold",
        "frozenThreshold",
        "selectionRule",
        "requiredLiveVector",
        "sourceBindings",
        "sourceBindingsSha256",
        "thresholdPlanSha256",
        "thresholdBasisSha256",
        "parameterStatus",
        "scientificStatus",
        "driftThresholdSha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise Qas30DriftError(
            "QAS30_DRIFT_THRESHOLD_INVALID", "drift threshold fields changed"
        )
    stored_sha = value.get("driftThresholdSha256")
    unhashed = {key: item for key, item in value.items() if key != "driftThresholdSha256"}
    rows = value.get("historicalAdjacentMaxDeltas")
    bindings = value.get("sourceBindings")
    if (
        value.get("schemaVersion") != THRESHOLD_SCHEMA_VERSION
        or value.get("target") != TARGET
        or value.get("metric") != METRIC
        or value.get("historicalScope") != HISTORICAL_SCOPE
        or not isinstance(rows, list)
        or not isinstance(bindings, list)
        or value.get("historicalType7QuantileProbability") != QUANTILE_PROBABILITY
        or value.get("minimumThreshold") != MINIMUM_THRESHOLD
        or value.get("selectionRule")
        not in {
            "MAXIMUM_OF_TYPE7_95_AND_PROTOCOL_MINIMUM",
            "MINIMUM_THRESHOLD_WHEN_HISTORICAL_NUMERIC_DELTA_IS_NOT_ESTIMABLE",
        }
        or value.get("requiredLiveVector") != list(VECTOR_DEFINITION)
        or value.get("sourceBindingsSha256") != _sha256(bindings)
        or value.get("parameterStatus") != "FROZEN_PROTOCOL_PARAMETER"
        or not _valid_sha256(value.get("thresholdBasisSha256"))
        or stored_sha != _sha256(unhashed)
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_THRESHOLD_INVALID", "drift threshold binding changed"
        )
    _require_identity(value.get("runId"), "runId")
    _require_identity(value.get("dataEpoch"), "dataEpoch")
    _require_commit(value.get("sourceCommitSha"))
    _require_sha256(value.get("freezeManifestSha256"), "freezeManifestSha256")
    _require_status(value.get("scientificStatus"))
    empirical = value.get("selectionRule") == "MAXIMUM_OF_TYPE7_95_AND_PROTOCOL_MINIMUM"
    floor_only = (
        value.get("selectionRule")
        == "MINIMUM_THRESHOLD_WHEN_HISTORICAL_NUMERIC_DELTA_IS_NOT_ESTIMABLE"
    )
    if empirical:
        if (
            value.get("historicalUniqueSnapshotIds") != 7
            or value.get("historicalCompleteNumericSnapshots") != 7
            or value.get("historicalAdjacentDeltaCount") != 6
            or len(rows) != 6
            or len(bindings) != 7
            or value.get("thresholdPlanSha256") is not None
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_THRESHOLD_INVALID", "empirical threshold counts changed"
            )
    elif floor_only:
        if (
            value.get("historicalUniqueSnapshotIds") != 2
            or value.get("historicalCompleteNumericSnapshots") != 1
            or value.get("historicalAdjacentDeltaCount") != 0
            or rows
            or len(bindings) != 2
            or value.get("historicalType7Quantile95") is not None
            or value.get("frozenThreshold") != MINIMUM_THRESHOLD
            or not _valid_sha256(value.get("thresholdPlanSha256"))
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_THRESHOLD_INVALID", "protocol floor threshold changed"
            )
    else:
        raise Qas30DriftError(
            "QAS30_DRIFT_THRESHOLD_INVALID", "threshold selection rule is invalid"
        )
    maxima: list[float] = []
    expected_pairs = [(f"G{index}", f"G{index + 1}") for index in range(1, 7)]
    for row, pair in zip(rows, expected_pairs, strict=empirical):
        if not isinstance(row, Mapping) or set(row) != {
            "leftSnapshotId",
            "rightSnapshotId",
            "leftSourceArtifactSha256",
            "rightSourceArtifactSha256",
            "componentAbsoluteDeltaSha256",
            "maxAbsoluteErrorDelta",
        }:
            raise Qas30DriftError(
                "QAS30_DRIFT_THRESHOLD_INVALID", "adjacent drift row is invalid"
            )
        if (row.get("leftSnapshotId"), row.get("rightSnapshotId")) != pair:
            raise Qas30DriftError(
                "QAS30_DRIFT_THRESHOLD_INVALID", "adjacent drift sequence changed"
            )
        for name in (
            "leftSourceArtifactSha256",
            "rightSourceArtifactSha256",
            "componentAbsoluteDeltaSha256",
        ):
            _require_sha256(row.get(name), name)
        maxima.append(
            _finite_nonnegative(row.get("maxAbsoluteErrorDelta"), "historical delta")
        )
    if empirical:
        quantile = type7_quantile(maxima, QUANTILE_PROBABILITY)
        frozen = max(MINIMUM_THRESHOLD, quantile)
        if (
            not math.isclose(
                _finite_nonnegative(
                    value.get("historicalType7Quantile95"), "historical quantile"
                ),
                quantile,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
            or not math.isclose(
                _finite_nonnegative(value.get("frozenThreshold"), "frozen threshold"),
                frozen,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_THRESHOLD_INVALID", "drift threshold arithmetic changed"
            )
    binding_by_id: dict[str, Mapping[str, Any]] = {}
    for index, binding in enumerate(bindings, start=1):
        empirical_fields = {
            "snapshotId",
            "sourcePath",
            "sourceArtifactSha256",
            "errorVectorSha256",
        }
        floor_fields = {"path", "sha256"}
        if not isinstance(binding, Mapping) or set(binding) != (
            empirical_fields if empirical else floor_fields
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_THRESHOLD_INVALID", "source binding is invalid"
            )
        if floor_only:
            if not isinstance(binding.get("path"), str) or not binding.get("path"):
                raise Qas30DriftError(
                    "QAS30_DRIFT_THRESHOLD_INVALID", "source path is invalid"
                )
            _require_sha256(binding.get("sha256"), "source sha256")
            continue
        snapshot_id = f"G{index}"
        if binding.get("snapshotId") != snapshot_id:
            raise Qas30DriftError(
                "QAS30_DRIFT_THRESHOLD_INVALID", "source binding order changed"
            )
        _require_sha256(binding.get("sourceArtifactSha256"), "sourceArtifactSha256")
        _require_sha256(binding.get("errorVectorSha256"), "errorVectorSha256")
        binding_by_id[snapshot_id] = binding
    for row in rows:
        if (
            row["leftSourceArtifactSha256"]
            != binding_by_id[row["leftSnapshotId"]]["sourceArtifactSha256"]
            or row["rightSourceArtifactSha256"]
            != binding_by_id[row["rightSnapshotId"]]["sourceArtifactSha256"]
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_THRESHOLD_INVALID", "adjacent source binding changed"
            )
    return dict(value)


def _validate_topology(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    machine_config = snapshot.get("machineConfig")
    topology = snapshot.get("topology")
    if not isinstance(machine_config, Mapping) or not isinstance(topology, Mapping):
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "machine config or topology is missing"
        )
    machine_config_sha = _sha256(machine_config)
    topology_unhashed = {
        key: item for key, item in topology.items() if key != "normalizedTopologySha256"
    }
    if (
        machine_config.get("computerId") != TARGET
        or snapshot.get("machineConfigSha256") != machine_config_sha
        or topology.get("schemaVersion") != NORMALIZED_TOPOLOGY_SCHEMA_VERSION
        or topology.get("target") != TARGET
        or topology.get("sourceConfigSha256") != machine_config_sha
        or topology.get("normalizedTopologySha256") != _sha256(topology_unhashed)
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "topology hash or machine binding changed"
        )
    overview = machine_config.get("overview")
    config_couplers = overview.get("coupler_map") if isinstance(overview, Mapping) else None
    qubits = topology.get("qubits")
    couplers = topology.get("couplers")
    if (
        not isinstance(config_couplers, Mapping)
        or not config_couplers
        or not isinstance(qubits, Mapping)
        or not isinstance(couplers, Mapping)
        or set(couplers) != set(config_couplers)
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "topology coverage is invalid"
        )
    expected_qubits: set[str] = set()
    structural_couplers: dict[str, Any] = {}
    for coupler_id, endpoints in config_couplers.items():
        if (
            not isinstance(coupler_id, str)
            or COUPLER_PATTERN.fullmatch(coupler_id) is None
            or isinstance(endpoints, str | bytes)
            or not isinstance(endpoints, Sequence)
            or len(endpoints) != 2
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_CALIBRATION_INVALID", "machine coupler map is invalid"
            )
        endpoint_pair = [str(item) for item in endpoints]
        if endpoint_pair[0] == endpoint_pair[1] or any(
            QUBIT_PATTERN.fullmatch(item) is None for item in endpoint_pair
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_CALIBRATION_INVALID", "machine coupler endpoint is invalid"
            )
        expected_qubits.update(endpoint_pair)
        row = couplers.get(coupler_id)
        if (
            not isinstance(row, Mapping)
            or set(row) != {"source", "target", "active", "twoQubitError"}
            or [row.get("source"), row.get("target")] != endpoint_pair
            or not isinstance(row.get("active"), bool)
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_CALIBRATION_INVALID", "coupler calibration is invalid"
            )
        if row["active"] or row.get("twoQubitError") is not None:
            _finite_nonnegative(
                row.get("twoQubitError"), f"{coupler_id}.twoQubitError"
            )
        structural_couplers[coupler_id] = {
            "source": endpoint_pair[0],
            "target": endpoint_pair[1],
            "active": row["active"],
        }
    if set(qubits) != expected_qubits:
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "qubit topology coverage is invalid"
        )
    structural_qubits: dict[str, Any] = {}
    for qubit_id in sorted(expected_qubits, key=lambda item: int(item[1:])):
        row = qubits.get(qubit_id)
        if (
            not isinstance(row, Mapping)
            or set(row) != {"active", "readoutError", "singleGateError"}
            or not isinstance(row.get("active"), bool)
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_CALIBRATION_INVALID", "qubit calibration is invalid"
            )
        if row["active"] or row.get("readoutError") is not None:
            _finite_nonnegative(row.get("readoutError"), f"{qubit_id}.readoutError")
        if row["active"] or row.get("singleGateError") is not None:
            _finite_nonnegative(
                row.get("singleGateError"), f"{qubit_id}.singleGateError"
            )
        structural_qubits[qubit_id] = {"active": row["active"]}
    if sum(row["active"] for row in structural_qubits.values()) < 6:
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "fewer than six active qubits"
        )
    for row in structural_couplers.values():
        if row["active"] and not (
            structural_qubits[row["source"]]["active"]
            and structural_qubits[row["target"]]["active"]
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_CALIBRATION_INVALID",
                "an active coupler has an inactive endpoint",
            )
    return (
        {"qubits": structural_qubits, "couplers": structural_couplers},
        {"qubits": dict(qubits), "couplers": dict(couplers)},
    )


def _normalize_capabilities(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    capabilities = snapshot.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "capabilities are missing"
        )
    machine_config_sha = snapshot.get("machineConfigSha256")
    topology = snapshot.get("topology")
    structural = capabilities.get("structuralCapabilities")
    evidence_hashes = capabilities.get("evidenceHashes")
    if (
        set(capabilities)
        != {
            "machineConfigSha256",
            "structuralCapabilities",
            "evidenceHashes",
            "configContract",
        }
        or capabilities.get("machineConfigSha256") != machine_config_sha
        or capabilities.get("configContract") != CALIBRATION_CONFIG_CONTRACT
        or not isinstance(topology, Mapping)
        or not isinstance(structural, Mapping)
        or set(structural)
        != {
            "activeQubitIds",
            "activeQubitCount",
            "activeCouplerIds",
            "activeCouplerCount",
            "gateFamilies",
        }
        or not isinstance(evidence_hashes, Mapping)
        or set(evidence_hashes)
        != {
            "providerRequestSha256",
            "providerResponseSha256",
            "machineConfigSha256",
            "normalizedTopologySha256",
        }
        or evidence_hashes.get("machineConfigSha256") != machine_config_sha
        or evidence_hashes.get("normalizedTopologySha256")
        != topology.get("normalizedTopologySha256")
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "capability or gate-family coverage is invalid"
        )
    for name in (
        "providerRequestSha256",
        "providerResponseSha256",
        "machineConfigSha256",
        "normalizedTopologySha256",
    ):
        _require_sha256(evidence_hashes.get(name), f"capabilities.{name}")

    active_qubits = structural.get("activeQubitIds")
    active_couplers = structural.get("activeCouplerIds")
    gate_families = structural.get("gateFamilies")
    topology_qubits = topology.get("qubits")
    topology_couplers = topology.get("couplers")
    if (
        isinstance(active_qubits, str | bytes)
        or not isinstance(active_qubits, Sequence)
        or not active_qubits
        or any(
            not isinstance(item, str) or QUBIT_PATTERN.fullmatch(item) is None
            for item in active_qubits
        )
        or len(set(active_qubits)) != len(active_qubits)
        or isinstance(active_couplers, str | bytes)
        or not isinstance(active_couplers, Sequence)
        or not active_couplers
        or any(
            not isinstance(item, str) or COUPLER_PATTERN.fullmatch(item) is None
            for item in active_couplers
        )
        or len(set(active_couplers)) != len(active_couplers)
        or isinstance(structural.get("activeQubitCount"), bool)
        or structural.get("activeQubitCount") != len(active_qubits)
        or isinstance(structural.get("activeCouplerCount"), bool)
        or structural.get("activeCouplerCount") != len(active_couplers)
        or not isinstance(topology_qubits, Mapping)
        or not isinstance(topology_couplers, Mapping)
        or list(active_qubits)
        != [name for name, row in topology_qubits.items() if row.get("active") is True]
        or list(active_couplers)
        != [name for name, row in topology_couplers.items() if row.get("active") is True]
        or not isinstance(gate_families, Mapping)
        or set(gate_families) != {"singleQubit", "twoQubit"}
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "structural capabilities are invalid"
        )
    normalized_gates: dict[str, list[str]] = {}
    flattened_gates: list[str] = []
    for family in ("singleQubit", "twoQubit"):
        values = gate_families.get(family)
        if (
            isinstance(values, str | bytes)
            or not isinstance(values, Sequence)
            or not values
            or any(not isinstance(item, str) or not item for item in values)
            or len(set(values)) != len(values)
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_CALIBRATION_INVALID", "gate-family coverage is invalid"
            )
        normalized_gates[family] = sorted(values)
        flattened_gates.extend(
            f"{family}:{gate}" for gate in normalized_gates[family]
        )

    # Request/config digests bind the evidence entity but do not describe a
    # structural capability. Numeric K refreshes therefore do not create a
    # structural stop by themselves.
    normalized = {
        "activeQubitIds": list(active_qubits),
        "activeQubitCount": len(active_qubits),
        "activeCouplerIds": list(active_couplers),
        "activeCouplerCount": len(active_couplers),
        "gateFamilies": normalized_gates,
    }
    return normalized, flattened_gates


def validate_calibration_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one rich K0/K1/K2 calibration receipt and all content hashes."""

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
    if not isinstance(value, Mapping) or set(value) != required:
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "calibration receipt fields changed"
        )
    snapshot = value.get("snapshot")
    label = value.get("calibrationLabel")
    status = _require_status(value.get("scientificStatus"))
    if (
        value.get("schemaVersion") != "qf.qas30.calibration-receipt.v1"
        or label not in {"K0", "K1", "K2"}
        or value.get("target") != TARGET
        or value.get("transportRequestCount") != 1
        or not isinstance(snapshot, Mapping)
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "calibration receipt identity changed"
        )
    _require_identity(value.get("runId"), "runId")
    _require_identity(value.get("dataEpoch"), "dataEpoch")
    _require_commit(value.get("sourceCommitSha"))
    for name in (
        "freezeManifestSha256",
        "authorizationEnvelopeSha256",
        "resourceSampleSha256",
        "activeCalibrationSha256",
    ):
        _require_sha256(value.get(name), name)
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
    provider_machine = snapshot.get("providerMachineId")
    if (
        set(snapshot) != snapshot_required
        or snapshot.get("schemaVersion") != "qf.qas30.calibration-snapshot.v1"
        or snapshot.get("calibrationLabel") != label
        or snapshot.get("target") != TARGET
        or not isinstance(provider_machine, str)
        or not provider_machine
        or snapshot.get("scientificStatus") != status
        or value.get("capturedAt") != snapshot.get("capturedAt")
        or value.get("activeCalibrationSha256") != _sha256(snapshot)
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_CALIBRATION_INVALID", "calibration snapshot binding changed"
        )
    if status == OBSERVED_STATUS and provider_machine != TARGET:
        raise Qas30DriftError(
            "QAS30_DRIFT_MACHINE_INVALID", "observed calibration is not tianyan176"
        )
    _require_timestamp(value.get("capturedAt"), "capturedAt")
    _validate_topology(snapshot)
    _normalize_capabilities(snapshot)
    return dict(value)


def build_mapping_binding(
    *,
    calibration_receipt: Mapping[str, Any],
    mapping_snapshot_sha256: str,
    common_measurement_physical_order: Sequence[str],
    used_couplers: Sequence[str],
) -> dict[str, Any]:
    """Bind one K snapshot to the common order and actually used couplers."""

    receipt = validate_calibration_receipt(calibration_receipt)
    _require_sha256(mapping_snapshot_sha256, "mappingSnapshotSha256")
    if (
        isinstance(common_measurement_physical_order, str | bytes)
        or not isinstance(common_measurement_physical_order, Sequence)
        or len(common_measurement_physical_order) != 6
        or any(
            not isinstance(item, str) or QUBIT_PATTERN.fullmatch(item) is None
            for item in common_measurement_physical_order
        )
        or len(set(common_measurement_physical_order)) != 6
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_MAPPING_INVALID", "common measurement order is invalid"
        )
    if (
        isinstance(used_couplers, str | bytes)
        or not isinstance(used_couplers, Sequence)
        or not used_couplers
        or any(
            not isinstance(item, str) or COUPLER_PATTERN.fullmatch(item) is None
            for item in used_couplers
        )
        or len(set(used_couplers)) != len(used_couplers)
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_MAPPING_INVALID", "used coupler identities are invalid"
        )
    binding: dict[str, Any] = {
        "schemaVersion": MAPPING_BINDING_SCHEMA_VERSION,
        "runId": receipt["runId"],
        "dataEpoch": receipt["dataEpoch"],
        "sourceCommitSha": receipt["sourceCommitSha"],
        "freezeManifestSha256": receipt["freezeManifestSha256"],
        "target": receipt["target"],
        "providerMachineId": receipt["snapshot"]["providerMachineId"],
        "calibrationLabel": receipt["calibrationLabel"],
        "activeCalibrationSha256": receipt["activeCalibrationSha256"],
        "calibrationReceiptSha256": _sha256(receipt),
        "mappingSnapshotSha256": mapping_snapshot_sha256,
        "commonMeasurementPhysicalOrder": list(common_measurement_physical_order),
        "usedCouplers": list(used_couplers),
        "scientificStatus": receipt["scientificStatus"],
    }
    binding["driftMappingBindingSha256"] = _sha256(binding)
    return binding


def validate_mapping_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schemaVersion",
        "runId",
        "dataEpoch",
        "sourceCommitSha",
        "freezeManifestSha256",
        "target",
        "providerMachineId",
        "calibrationLabel",
        "activeCalibrationSha256",
        "calibrationReceiptSha256",
        "mappingSnapshotSha256",
        "commonMeasurementPhysicalOrder",
        "usedCouplers",
        "scientificStatus",
        "driftMappingBindingSha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise Qas30DriftError(
            "QAS30_DRIFT_MAPPING_INVALID", "mapping binding fields changed"
        )
    stored_sha = value.get("driftMappingBindingSha256")
    unhashed = {
        key: item for key, item in value.items() if key != "driftMappingBindingSha256"
    }
    order = value.get("commonMeasurementPhysicalOrder")
    used_couplers = value.get("usedCouplers")
    if (
        value.get("schemaVersion") != MAPPING_BINDING_SCHEMA_VERSION
        or value.get("target") != TARGET
        or value.get("calibrationLabel") not in {"K0", "K1", "K2"}
        or not isinstance(value.get("providerMachineId"), str)
        or not value.get("providerMachineId")
        or not isinstance(order, list)
        or len(order) != 6
        or any(not isinstance(item, str) or QUBIT_PATTERN.fullmatch(item) is None for item in order)
        or len(set(order)) != 6
        or not isinstance(used_couplers, list)
        or not used_couplers
        or any(
            not isinstance(item, str) or COUPLER_PATTERN.fullmatch(item) is None
            for item in used_couplers
        )
        or len(set(used_couplers)) != len(used_couplers)
        or stored_sha != _sha256(unhashed)
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_MAPPING_INVALID", "mapping binding changed"
        )
    _require_identity(value.get("runId"), "runId")
    _require_identity(value.get("dataEpoch"), "dataEpoch")
    _require_commit(value.get("sourceCommitSha"))
    for name in (
        "freezeManifestSha256",
        "activeCalibrationSha256",
        "calibrationReceiptSha256",
        "mappingSnapshotSha256",
    ):
        _require_sha256(value.get(name), name)
    _require_status(value.get("scientificStatus"))
    return dict(value)


def _identity_tuple(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        value.get("runId"),
        value.get("dataEpoch"),
        value.get("sourceCommitSha"),
        value.get("freezeManifestSha256"),
        value.get("target"),
        value.get("scientificStatus"),
    )


def _extract_live_error_vector(
    receipt: Mapping[str, Any], mapping: Mapping[str, Any]
) -> dict[str, float]:
    _, calibrated_topology = _validate_topology(receipt["snapshot"])
    qubits = calibrated_topology["qubits"]
    couplers = calibrated_topology["couplers"]
    vector: dict[str, float] = {}
    for qubit in mapping["commonMeasurementPhysicalOrder"]:
        row = qubits.get(qubit)
        if not isinstance(row, Mapping) or row.get("active") is not True:
            raise Qas30DriftError(
                "QAS30_DRIFT_VECTOR_INCOMPLETE", f"mapped qubit {qubit} is unavailable"
            )
        vector[f"READOUT:{qubit}"] = _finite_nonnegative(
            row.get("readoutError"), f"{qubit}.readoutError"
        )
    for coupler in mapping["usedCouplers"]:
        row = couplers.get(coupler)
        if not isinstance(row, Mapping) or row.get("active") is not True:
            raise Qas30DriftError(
                "QAS30_DRIFT_VECTOR_INCOMPLETE", f"used coupler {coupler} is unavailable"
            )
        vector[f"TWO_QUBIT:{coupler}"] = _finite_nonnegative(
            row.get("twoQubitError"), f"{coupler}.twoQubitError"
        )
    return vector


def evaluate_drift(
    *,
    threshold_entity: Mapping[str, Any],
    previous_calibration_receipt: Mapping[str, Any],
    current_calibration_receipt: Mapping[str, Any],
    previous_mapping_binding: Mapping[str, Any],
    current_mapping_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate K0->K1 or K1->K2 and return a self-hashed write gate."""

    threshold = validate_frozen_threshold_entity(threshold_entity)
    previous = validate_calibration_receipt(previous_calibration_receipt)
    current = validate_calibration_receipt(current_calibration_receipt)
    previous_mapping = validate_mapping_binding(previous_mapping_binding)
    current_mapping = validate_mapping_binding(current_mapping_binding)
    label_pair = (previous["calibrationLabel"], current["calibrationLabel"])
    transition_by_pair = {("K0", "K1"): "K0_TO_K1", ("K1", "K2"): "K1_TO_K2"}
    transition = transition_by_pair.get(label_pair)
    if transition is None:
        raise Qas30DriftError(
            "QAS30_DRIFT_TRANSITION_INVALID", "calibration transition is invalid"
        )
    common_identity = _identity_tuple(threshold)
    if (
        _identity_tuple(previous) != common_identity
        or _identity_tuple(current) != common_identity
        or _identity_tuple(previous_mapping) != common_identity
        or _identity_tuple(current_mapping) != common_identity
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_IDENTITY_MISMATCH", "run, epoch, freeze, source, or status changed"
        )
    if (
        previous["snapshot"]["providerMachineId"]
        != current["snapshot"]["providerMachineId"]
        or previous_mapping["providerMachineId"]
        != previous["snapshot"]["providerMachineId"]
        or current_mapping["providerMachineId"]
        != current["snapshot"]["providerMachineId"]
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_MACHINE_MISMATCH", "provider machine identity changed"
        )
    for receipt, mapping in ((previous, previous_mapping), (current, current_mapping)):
        if (
            mapping["calibrationLabel"] != receipt["calibrationLabel"]
            or mapping["activeCalibrationSha256"] != receipt["activeCalibrationSha256"]
            or mapping["calibrationReceiptSha256"] != _sha256(receipt)
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_MAPPING_MISMATCH", "mapping does not bind its K receipt"
            )

    previous_capabilities, previous_gates = _normalize_capabilities(previous["snapshot"])
    current_capabilities, current_gates = _normalize_capabilities(current["snapshot"])
    previous_structure, _ = _validate_topology(previous["snapshot"])
    current_structure, _ = _validate_topology(current["snapshot"])
    structural_comparison = {
        "previousCapabilitiesSha256": _sha256(previous_capabilities),
        "currentCapabilitiesSha256": _sha256(current_capabilities),
        "capabilitiesChanged": previous_capabilities != current_capabilities,
        "previousGateFamilies": previous_gates,
        "currentGateFamilies": current_gates,
        "gateFamiliesChanged": previous_gates != current_gates,
        "previousStructuralTopologySha256": _sha256(previous_structure),
        "currentStructuralTopologySha256": _sha256(current_structure),
        "normalizedTopologyChanged": previous_structure != current_structure,
        "previousCommonMeasurementPhysicalOrder": previous_mapping[
            "commonMeasurementPhysicalOrder"
        ],
        "currentCommonMeasurementPhysicalOrder": current_mapping[
            "commonMeasurementPhysicalOrder"
        ],
        "commonMeasurementOrderChanged": previous_mapping[
            "commonMeasurementPhysicalOrder"
        ]
        != current_mapping["commonMeasurementPhysicalOrder"],
        "previousUsedCouplers": previous_mapping["usedCouplers"],
        "currentUsedCouplers": current_mapping["usedCouplers"],
        "usedCouplersChanged": previous_mapping["usedCouplers"]
        != current_mapping["usedCouplers"],
    }
    structural_drift = any(
        structural_comparison[name]
        for name in (
            "capabilitiesChanged",
            "gateFamiliesChanged",
            "normalizedTopologyChanged",
            "commonMeasurementOrderChanged",
            "usedCouplersChanged",
        )
    )
    structural_comparison["structuralDrift"] = structural_drift

    previous_vector = _extract_live_error_vector(previous, previous_mapping)
    current_vector = _extract_live_error_vector(current, current_mapping)
    comparable = set(previous_vector) == set(current_vector)
    component_deltas: dict[str, float] | None
    maximum_delta: float | None
    exceeds_threshold: bool | None
    if comparable:
        component_deltas = {
            name: abs(current_vector[name] - previous_vector[name])
            for name in sorted(previous_vector)
        }
        maximum_delta = max(component_deltas.values())
        exceeds_threshold = maximum_delta > threshold["frozenThreshold"] and not math.isclose(
            maximum_delta,
            threshold["frozenThreshold"],
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    else:
        component_deltas = None
        maximum_delta = None
        exceeds_threshold = None
    numeric_comparison = {
        "metric": METRIC,
        "requiredLiveVector": list(VECTOR_DEFINITION),
        "previousVectorSha256": _sha256(previous_vector),
        "currentVectorSha256": _sha256(current_vector),
        "vectorKeysComparable": comparable,
        "componentAbsoluteDeltas": component_deltas,
        "maxAbsoluteErrorDelta": maximum_delta,
        "frozenThreshold": threshold["frozenThreshold"],
        "exceedsThreshold": exceeds_threshold,
    }
    drift_detected = structural_drift or exceeds_threshold is True or not comparable
    if transition == "K0_TO_K1":
        decision = "WRITE_STOP" if drift_detected else "WRITE_ALLOWED"
        new_submit_allowed = not drift_detected
    else:
        decision = "DRIFT_STRATIFIED" if drift_detected else "COMPLETED_STABLE"
        new_submit_allowed = False
    receipt: dict[str, Any] = {
        "schemaVersion": DRIFT_RECEIPT_SCHEMA_VERSION,
        "runId": threshold["runId"],
        "dataEpoch": threshold["dataEpoch"],
        "sourceCommitSha": threshold["sourceCommitSha"],
        "freezeManifestSha256": threshold["freezeManifestSha256"],
        "target": TARGET,
        "providerMachineId": previous["snapshot"]["providerMachineId"],
        "transition": transition,
        "previousCalibrationLabel": previous["calibrationLabel"],
        "currentCalibrationLabel": current["calibrationLabel"],
        "driftThresholdSha256": threshold["driftThresholdSha256"],
        "previousCalibrationReceiptSha256": _sha256(previous),
        "currentCalibrationReceiptSha256": _sha256(current),
        "previousMappingBindingSha256": previous_mapping[
            "driftMappingBindingSha256"
        ],
        "currentMappingBindingSha256": current_mapping["driftMappingBindingSha256"],
        "structuralComparison": structural_comparison,
        "numericComparison": numeric_comparison,
        "decision": decision,
        "newSubmitAllowed": new_submit_allowed,
        "retainCompletedData": True,
        "scientificStatus": threshold["scientificStatus"],
    }
    receipt["driftReceiptSha256"] = _sha256(receipt)
    return receipt


def validate_drift_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the persisted shape and self-hash consumed by orchestration."""

    required = {
        "schemaVersion",
        "runId",
        "dataEpoch",
        "sourceCommitSha",
        "freezeManifestSha256",
        "target",
        "providerMachineId",
        "transition",
        "previousCalibrationLabel",
        "currentCalibrationLabel",
        "driftThresholdSha256",
        "previousCalibrationReceiptSha256",
        "currentCalibrationReceiptSha256",
        "previousMappingBindingSha256",
        "currentMappingBindingSha256",
        "structuralComparison",
        "numericComparison",
        "decision",
        "newSubmitAllowed",
        "retainCompletedData",
        "scientificStatus",
        "driftReceiptSha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise Qas30DriftError(
            "QAS30_DRIFT_RECEIPT_INVALID", "drift receipt fields changed"
        )
    stored_sha = value.get("driftReceiptSha256")
    unhashed = {key: item for key, item in value.items() if key != "driftReceiptSha256"}
    transition = value.get("transition")
    expected_labels = {
        "K0_TO_K1": ("K0", "K1"),
        "K1_TO_K2": ("K1", "K2"),
    }.get(transition)
    allowed_decisions = {
        "K0_TO_K1": {"WRITE_ALLOWED", "WRITE_STOP"},
        "K1_TO_K2": {"COMPLETED_STABLE", "DRIFT_STRATIFIED"},
    }.get(transition, set())
    structural = value.get("structuralComparison")
    numeric = value.get("numericComparison")
    structural_fields = {
        "previousCapabilitiesSha256",
        "currentCapabilitiesSha256",
        "capabilitiesChanged",
        "previousGateFamilies",
        "currentGateFamilies",
        "gateFamiliesChanged",
        "previousStructuralTopologySha256",
        "currentStructuralTopologySha256",
        "normalizedTopologyChanged",
        "previousCommonMeasurementPhysicalOrder",
        "currentCommonMeasurementPhysicalOrder",
        "commonMeasurementOrderChanged",
        "previousUsedCouplers",
        "currentUsedCouplers",
        "usedCouplersChanged",
        "structuralDrift",
    }
    numeric_fields = {
        "metric",
        "requiredLiveVector",
        "previousVectorSha256",
        "currentVectorSha256",
        "vectorKeysComparable",
        "componentAbsoluteDeltas",
        "maxAbsoluteErrorDelta",
        "frozenThreshold",
        "exceedsThreshold",
    }
    if (
        value.get("schemaVersion") != DRIFT_RECEIPT_SCHEMA_VERSION
        or value.get("target") != TARGET
        or not isinstance(value.get("providerMachineId"), str)
        or not value.get("providerMachineId")
        or expected_labels
        != (value.get("previousCalibrationLabel"), value.get("currentCalibrationLabel"))
        or value.get("decision") not in allowed_decisions
        or not isinstance(value.get("newSubmitAllowed"), bool)
        or value.get("retainCompletedData") is not True
        or not isinstance(structural, Mapping)
        or set(structural) != structural_fields
        or not isinstance(numeric, Mapping)
        or set(numeric) != numeric_fields
        or numeric.get("metric") != METRIC
        or numeric.get("requiredLiveVector") != list(VECTOR_DEFINITION)
        or stored_sha != _sha256(unhashed)
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_RECEIPT_INVALID", "drift receipt binding changed"
        )
    if value.get("newSubmitAllowed") is not (
        transition == "K0_TO_K1" and value.get("decision") == "WRITE_ALLOWED"
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_RECEIPT_INVALID", "write-gate semantics changed"
        )
    change_flags = (
        "capabilitiesChanged",
        "gateFamiliesChanged",
        "normalizedTopologyChanged",
        "commonMeasurementOrderChanged",
        "usedCouplersChanged",
    )
    if any(not isinstance(structural.get(name), bool) for name in change_flags):
        raise Qas30DriftError(
            "QAS30_DRIFT_RECEIPT_INVALID", "structural drift flags are invalid"
        )
    expected_structural_drift = any(structural[name] for name in change_flags)
    if structural.get("structuralDrift") is not expected_structural_drift:
        raise Qas30DriftError(
            "QAS30_DRIFT_RECEIPT_INVALID", "structural drift aggregation changed"
        )
    for name in (
        "previousCapabilitiesSha256",
        "currentCapabilitiesSha256",
        "previousStructuralTopologySha256",
        "currentStructuralTopologySha256",
    ):
        _require_sha256(structural.get(name), name)
    previous_gates = structural.get("previousGateFamilies")
    current_gates = structural.get("currentGateFamilies")
    previous_order = structural.get("previousCommonMeasurementPhysicalOrder")
    current_order = structural.get("currentCommonMeasurementPhysicalOrder")
    previous_couplers = structural.get("previousUsedCouplers")
    current_couplers = structural.get("currentUsedCouplers")
    if (
        not isinstance(previous_gates, list)
        or not isinstance(current_gates, list)
        or not previous_gates
        or not current_gates
        or any(not isinstance(item, str) or not item for item in previous_gates + current_gates)
        or not isinstance(previous_order, list)
        or not isinstance(current_order, list)
        or len(previous_order) != 6
        or len(current_order) != 6
        or any(
            not isinstance(item, str) or QUBIT_PATTERN.fullmatch(item) is None
            for item in previous_order + current_order
        )
        or not isinstance(previous_couplers, list)
        or not isinstance(current_couplers, list)
        or not previous_couplers
        or not current_couplers
        or any(
            not isinstance(item, str) or COUPLER_PATTERN.fullmatch(item) is None
            for item in previous_couplers + current_couplers
        )
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_RECEIPT_INVALID", "structural comparison values are invalid"
        )
    expected_flags = {
        "capabilitiesChanged": structural["previousCapabilitiesSha256"]
        != structural["currentCapabilitiesSha256"],
        "gateFamiliesChanged": previous_gates != current_gates,
        "normalizedTopologyChanged": structural["previousStructuralTopologySha256"]
        != structural["currentStructuralTopologySha256"],
        "commonMeasurementOrderChanged": previous_order != current_order,
        "usedCouplersChanged": previous_couplers != current_couplers,
    }
    if any(structural[name] is not expected for name, expected in expected_flags.items()):
        raise Qas30DriftError(
            "QAS30_DRIFT_RECEIPT_INVALID", "structural comparison arithmetic changed"
        )
    for name in ("previousVectorSha256", "currentVectorSha256"):
        _require_sha256(numeric.get(name), name)
    comparable = numeric.get("vectorKeysComparable")
    frozen_threshold = _finite_nonnegative(
        numeric.get("frozenThreshold"), "frozen threshold"
    )
    if not isinstance(comparable, bool):
        raise Qas30DriftError(
            "QAS30_DRIFT_RECEIPT_INVALID", "numeric comparability is invalid"
        )
    if comparable:
        component_deltas = numeric.get("componentAbsoluteDeltas")
        if not isinstance(component_deltas, Mapping) or not component_deltas:
            raise Qas30DriftError(
                "QAS30_DRIFT_RECEIPT_INVALID", "component drift values are invalid"
            )
        normalized_deltas = [
            _finite_nonnegative(item, f"component delta {name}")
            for name, item in component_deltas.items()
            if isinstance(name, str) and name
        ]
        if len(normalized_deltas) != len(component_deltas):
            raise Qas30DriftError(
                "QAS30_DRIFT_RECEIPT_INVALID", "component drift key is invalid"
            )
        expected_maximum = max(normalized_deltas)
        maximum = _finite_nonnegative(
            numeric.get("maxAbsoluteErrorDelta"), "maximum absolute delta"
        )
        expected_exceeds = maximum > frozen_threshold and not math.isclose(
            maximum,
            frozen_threshold,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        if (
            not math.isclose(maximum, expected_maximum, rel_tol=0.0, abs_tol=1e-15)
            or numeric.get("exceedsThreshold") is not expected_exceeds
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_RECEIPT_INVALID", "numeric drift aggregation changed"
            )
    else:
        if any(
            numeric.get(name) is not None
            for name in (
                "componentAbsoluteDeltas",
                "maxAbsoluteErrorDelta",
                "exceedsThreshold",
            )
        ):
            raise Qas30DriftError(
                "QAS30_DRIFT_RECEIPT_INVALID", "noncomparable vectors gained a delta"
            )
        expected_exceeds = False
    drift_detected = expected_structural_drift or not comparable or expected_exceeds
    expected_decision = (
        ("WRITE_STOP" if drift_detected else "WRITE_ALLOWED")
        if transition == "K0_TO_K1"
        else ("DRIFT_STRATIFIED" if drift_detected else "COMPLETED_STABLE")
    )
    if value.get("decision") != expected_decision:
        raise Qas30DriftError(
            "QAS30_DRIFT_RECEIPT_INVALID", "drift decision changed"
        )
    _require_identity(value.get("runId"), "runId")
    _require_identity(value.get("dataEpoch"), "dataEpoch")
    _require_commit(value.get("sourceCommitSha"))
    for name in (
        "freezeManifestSha256",
        "driftThresholdSha256",
        "previousCalibrationReceiptSha256",
        "currentCalibrationReceiptSha256",
        "previousMappingBindingSha256",
        "currentMappingBindingSha256",
        "driftReceiptSha256",
    ):
        _require_sha256(value.get(name), name)
    _require_status(value.get("scientificStatus"))
    return dict(value)


def require_r2_write_allowed(
    value: Mapping[str, Any],
    *,
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
) -> dict[str, Any]:
    """Fail closed unless a valid K0->K1 receipt permits the first R2 write."""

    receipt = validate_drift_receipt(value)
    if (
        receipt.get("transition") != "K0_TO_K1"
        or receipt.get("decision") != "WRITE_ALLOWED"
        or receipt.get("newSubmitAllowed") is not True
        or receipt.get("runId") != run_id
        or receipt.get("dataEpoch") != data_epoch
        or receipt.get("sourceCommitSha") != source_commit_sha
        or receipt.get("freezeManifestSha256") != freeze_manifest_sha256
    ):
        raise Qas30DriftError(
            "QAS30_DRIFT_WRITE_STOP", "R2 requires a matching WRITE_ALLOWED receipt"
        )
    return receipt
