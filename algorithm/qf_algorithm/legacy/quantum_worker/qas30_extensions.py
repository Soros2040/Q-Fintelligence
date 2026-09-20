"""Frozen, local-only design records for the QAS30 extension experiments.

This module creates canonical five-circuit batch specifications and budget
manifests.  It performs no authentication, network access, submission, query,
or filesystem writes.  Hardware-facing entities are represented only by their
already-frozen SHA-256 identities.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

EXTENSION_PLAN_SCHEMA_VERSION = "qf.qas30.extension-plan.v1"
EXTENSION_BATCH_SCHEMA_VERSION = "qf.qas30.extension-batch-spec.v1"
DESIGN_ENTITY_SCHEMA_VERSION = "qf.qas30.extension-design-entity.v1"
EXTENSION_BUDGET_SCHEMA_VERSION = "qf.qas30.extension-budget.v1"
TOTAL_BUDGET_SCHEMA_VERSION = "qf.qas30.total-budget-table.v1"

SCIENTIFIC_STATUS = "DESIGN_READY"
READY_STATE = "READY_FOR_LIVE_GATE"
UNSUPPORTED_STATE = "UNSUPPORTED"
BATCH_SIZE = 5
DEFAULT_SHOTS = 1_000
CANDIDATE_IDS = tuple(f"C{index:02d}" for index in range(1, 31))
REPLICATION_BLOCK_COUNTS = (10, 20, 30, 40)
REPLICATION_STAGES = (
    "R1_B1",
    "R1_B2",
    "R2_RIDGE",
    "R2_FIXED",
    "R2_RANDOM",
    "R2_LLM",
)
SHOT_LEVELS = (100, 250, 500, 1_000, 2_000, 5_000, 10_000)
END_TO_END_WINDOW_END_MONTHS = (
    "2006-07",
    "2011-07",
    "2016-07",
    "2021-07",
    "2026-07",
)
END_TO_END_ARMS = (
    "FIXED_MAPPING",
    "TOPOLOGY_AWARE_QAS",
    "LLM_TOPOLOGY_AND_CALIBRATION_FEEDBACK_QAS",
)
EXTENSION_EXPECTED_BUDGETS = {
    "SHOT_SCALING": {"calls": 21, "circuits": 105, "shots": 282_750},
    "CALIBRATION_DRIFT": {"calls": 12, "circuits": 60, "shots": 60_000},
    "MAPPING_SENSITIVITY": {"calls": 5, "circuits": 25, "shots": 25_000},
    "END_TO_END_FEEDBACK": {"calls": 15, "circuits": 75, "shots": 75_000},
}
EXTENSION_EXPECTED_TOTAL = {"calls": 53, "circuits": 265, "shots": 442_750}

_SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
_COMMIT_PATTERN = re.compile(r"[a-f0-9]{40}")
_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


class Qas30ExtensionError(ValueError):
    """A frozen extension-design invariant was violated."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise Qas30ExtensionError("value is not canonical-JSON compatible") from error


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 of the canonical JSON representation."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _require_identity(value: Any, name: str, *, maximum: int = 160) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _IDENTITY_PATTERN.fullmatch(value) is None
    ):
        raise Qas30ExtensionError(f"{name} is invalid")
    return value


def _require_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise Qas30ExtensionError(f"{name} is not a SHA-256 digest")
    return value


def _require_commit(value: Any) -> str:
    if not isinstance(value, str) or _COMMIT_PATTERN.fullmatch(value) is None:
        raise Qas30ExtensionError("sourceCommitSha is not a 40-character commit")
    return value


def _require_positive_integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise Qas30ExtensionError(f"{name} must be a positive integer")
    return value


def _candidate_order(value: Sequence[str]) -> list[str]:
    if (
        isinstance(value, str | bytes)
        or len(value) != BATCH_SIZE
        or len(set(value)) != BATCH_SIZE
        or any(item not in CANDIDATE_IDS for item in value)
    ):
        raise Qas30ExtensionError("candidateOrder must contain five unique C01-C30 IDs")
    return list(value)


def _measurement_order(value: Sequence[str]) -> list[str]:
    if (
        isinstance(value, str | bytes)
        or len(value) != 6
        or len(set(value)) != 6
        or any(
            not isinstance(item, str) or re.fullmatch(r"Q\d{1,4}", item) is None
            for item in value
        )
    ):
        raise Qas30ExtensionError(
            "common measurement order must contain six unique physical qubits"
        )
    return list(value)


def _seal(value: Mapping[str, Any], field: str = "canonicalSha256") -> dict[str, Any]:
    sealed = _json_copy(dict(value))
    sealed[field] = canonical_sha256(sealed)
    return sealed


def validate_canonical_record(
    value: Mapping[str, Any], *, field: str = "canonicalSha256"
) -> dict[str, Any]:
    """Validate and return a canonical record without mutating the caller."""

    if not isinstance(value, Mapping):
        raise Qas30ExtensionError("canonical record must be an object")
    stored = value.get(field)
    _require_sha256(stored, field)
    unsigned = {key: item for key, item in value.items() if key != field}
    if canonical_sha256(unsigned) != stored:
        raise Qas30ExtensionError("canonical record SHA-256 changed")
    return _json_copy(dict(value))


def build_design_entity(entity_kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create one immutable, positive-scope design entity."""

    kind = _require_identity(entity_kind, "entityKind")
    if not isinstance(payload, Mapping):
        raise Qas30ExtensionError("design entity payload must be an object")
    return _seal(
        {
            "schemaVersion": DESIGN_ENTITY_SCHEMA_VERSION,
            "entityKind": kind,
            "payload": _json_copy(dict(payload)),
            "scientificStatus": SCIENTIFIC_STATUS,
        },
        field="entitySha256",
    )


def _validate_design_entity(value: Mapping[str, Any]) -> dict[str, Any]:
    entity = validate_canonical_record(value, field="entitySha256")
    if (
        entity.get("schemaVersion") != DESIGN_ENTITY_SCHEMA_VERSION
        or entity.get("scientificStatus") != SCIENTIFIC_STATUS
    ):
        raise Qas30ExtensionError("design entity identity changed")
    _require_identity(entity.get("entityKind"), "entityKind")
    if not isinstance(entity.get("payload"), dict):
        raise Qas30ExtensionError("design entity payload is missing")
    return entity


def build_batch_spec(
    *,
    experiment: str,
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
    batch_id: str,
    batch_role: str,
    ordinal: int,
    candidate_order: Sequence[str],
    selection_entity_sha256: str,
    window_entity_sha256: str,
    mapping_entity_sha256: str,
    snapshot_entity_sha256: str,
    shots: int,
    execution_state: str = READY_STATE,
    coordinates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one canonical five-circuit extension batch specification."""

    experiment_id = _require_identity(experiment, "experiment")
    run_identity = _require_identity(run_id, "runId")
    epoch = _require_identity(data_epoch, "dataEpoch")
    _require_commit(source_commit_sha)
    _require_sha256(freeze_manifest_sha256, "freezeManifestSha256")
    _require_identity(batch_id, "batchId")
    role = _require_identity(batch_role, "batchRole")
    position = _require_positive_integer(ordinal, "ordinal")
    shot_count = _require_positive_integer(shots, "shots")
    if shot_count > 10_000_000:
        raise Qas30ExtensionError("shots exceeds the finite batch bound")
    if execution_state not in {READY_STATE, UNSUPPORTED_STATE}:
        raise Qas30ExtensionError("executionState is invalid")
    coordinate_value = {} if coordinates is None else _json_copy(dict(coordinates))
    batch = {
        "schemaVersion": EXTENSION_BATCH_SCHEMA_VERSION,
        "experiment": experiment_id,
        "runId": run_identity,
        "dataEpoch": epoch,
        "sourceCommitSha": source_commit_sha,
        "freezeManifestSha256": freeze_manifest_sha256,
        "batchId": batch_id,
        "batchRole": role,
        "ordinal": position,
        "candidateOrder": _candidate_order(candidate_order),
        "selectionEntitySha256": _require_sha256(
            selection_entity_sha256, "selectionEntitySha256"
        ),
        "windowEntitySha256": _require_sha256(
            window_entity_sha256, "windowEntitySha256"
        ),
        "mappingEntitySha256": _require_sha256(
            mapping_entity_sha256, "mappingEntitySha256"
        ),
        "snapshotEntitySha256": _require_sha256(
            snapshot_entity_sha256, "snapshotEntitySha256"
        ),
        "shots": shot_count,
        "circuitCount": BATCH_SIZE,
        "executionState": execution_state,
        "substitutionAllowed": False,
        "coordinates": coordinate_value,
        "scientificStatus": SCIENTIFIC_STATUS,
    }
    return _seal(batch)


def validate_batch_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate every frozen identity of an extension batch specification."""

    batch = validate_canonical_record(value)
    if (
        batch.get("schemaVersion") != EXTENSION_BATCH_SCHEMA_VERSION
        or batch.get("scientificStatus") != SCIENTIFIC_STATUS
        or batch.get("circuitCount") != BATCH_SIZE
        or batch.get("substitutionAllowed") is not False
    ):
        raise Qas30ExtensionError("extension batch identity changed")
    _require_identity(batch.get("experiment"), "experiment")
    _require_identity(batch.get("runId"), "runId")
    _require_identity(batch.get("dataEpoch"), "dataEpoch")
    _require_commit(batch.get("sourceCommitSha"))
    _require_sha256(batch.get("freezeManifestSha256"), "freezeManifestSha256")
    _require_identity(batch.get("batchId"), "batchId")
    _require_identity(batch.get("batchRole"), "batchRole")
    _require_positive_integer(batch.get("ordinal"), "ordinal")
    _candidate_order(batch.get("candidateOrder", []))
    for name in (
        "selectionEntitySha256",
        "windowEntitySha256",
        "mappingEntitySha256",
        "snapshotEntitySha256",
    ):
        _require_sha256(batch.get(name), name)
    _require_positive_integer(batch.get("shots"), "shots")
    if batch.get("executionState") not in {READY_STATE, UNSUPPORTED_STATE}:
        raise Qas30ExtensionError("executionState changed")
    if not isinstance(batch.get("coordinates"), dict):
        raise Qas30ExtensionError("batch coordinates are missing")
    return batch


def _budget(records: Sequence[Mapping[str, Any]], *, executable_only: bool) -> dict[str, int]:
    selected = []
    for record in records:
        validated = validate_batch_spec(record)
        if not executable_only or validated["executionState"] == READY_STATE:
            selected.append(validated)
    return {
        "calls": len(selected),
        "circuits": sum(int(record["circuitCount"]) for record in selected),
        "shots": sum(
            int(record["circuitCount"]) * int(record["shots"]) for record in selected
        ),
    }


def _build_plan(
    *,
    experiment: str,
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
    records: Sequence[Mapping[str, Any]],
    entities: Sequence[Mapping[str, Any]],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    validated_records = [validate_batch_spec(record) for record in records]
    if len({record["canonicalSha256"] for record in validated_records}) != len(
        validated_records
    ):
        raise Qas30ExtensionError("extension plan contains duplicate batch specifications")
    ordinals = [record["ordinal"] for record in validated_records]
    if ordinals != list(range(1, len(validated_records) + 1)):
        raise Qas30ExtensionError("extension batch ordinals must be contiguous")
    validated_entities = [_validate_design_entity(entity) for entity in entities]
    if len({entity["entitySha256"] for entity in validated_entities}) != len(
        validated_entities
    ):
        raise Qas30ExtensionError("extension plan contains duplicate design entities")
    return _seal(
        {
            "schemaVersion": EXTENSION_PLAN_SCHEMA_VERSION,
            "experiment": _require_identity(experiment, "experiment"),
            "runId": _require_identity(run_id, "runId"),
            "dataEpoch": _require_identity(data_epoch, "dataEpoch"),
            "sourceCommitSha": _require_commit(source_commit_sha),
            "freezeManifestSha256": _require_sha256(
                freeze_manifest_sha256, "freezeManifestSha256"
            ),
            "records": validated_records,
            "entities": validated_entities,
            "registeredBudget": _budget(validated_records, executable_only=False),
            "executionBudget": _budget(validated_records, executable_only=True),
            "protocol": _json_copy(dict(protocol)),
            "scientificStatus": SCIENTIFIC_STATUS,
        }
    )


def validate_extension_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a complete extension plan and recompute both budgets."""

    plan = validate_canonical_record(value)
    if (
        plan.get("schemaVersion") != EXTENSION_PLAN_SCHEMA_VERSION
        or plan.get("scientificStatus") != SCIENTIFIC_STATUS
    ):
        raise Qas30ExtensionError("extension plan identity changed")
    experiment = _require_identity(plan.get("experiment"), "experiment")
    run_id = _require_identity(plan.get("runId"), "runId")
    data_epoch = _require_identity(plan.get("dataEpoch"), "dataEpoch")
    source_commit_sha = _require_commit(plan.get("sourceCommitSha"))
    freeze_manifest_sha256 = _require_sha256(
        plan.get("freezeManifestSha256"), "freezeManifestSha256"
    )
    records = plan.get("records")
    entities = plan.get("entities")
    if (
        not isinstance(records, list)
        or not isinstance(entities, list)
        or not isinstance(plan.get("protocol"), dict)
    ):
        raise Qas30ExtensionError("extension plan records are missing")
    validated_records = [validate_batch_spec(record) for record in records]
    if [record["ordinal"] for record in validated_records] != list(
        range(1, len(validated_records) + 1)
    ):
        raise Qas30ExtensionError("extension batch ordinals changed")
    for record in validated_records:
        if (
            record["experiment"] != experiment
            or record["dataEpoch"] != data_epoch
            or record["sourceCommitSha"] != source_commit_sha
            or record["freezeManifestSha256"] != freeze_manifest_sha256
            or record["runId"] != run_id
        ):
            raise Qas30ExtensionError("extension batch plan identity changed")
    for entity in entities:
        _validate_design_entity(entity)
    if plan.get("registeredBudget") != _budget(validated_records, executable_only=False):
        raise Qas30ExtensionError("registered extension budget changed")
    if plan.get("executionBudget") != _budget(validated_records, executable_only=True):
        raise Qas30ExtensionError("executable extension budget changed")
    return plan


def _identity(
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
) -> tuple[str, str, str, str]:
    return (
        _require_identity(run_id, "runId"),
        _require_identity(data_epoch, "dataEpoch"),
        _require_commit(source_commit_sha),
        _require_sha256(freeze_manifest_sha256, "freezeManifestSha256"),
    )


def build_independent_replication_plan(
    *,
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
    block_count: int,
    candidate_orders_by_block: Sequence[Mapping[str, Sequence[str]]],
) -> dict[str, Any]:
    """Freeze 10, 20, 30, or 40 independent six-batch replication blocks."""

    identity = _identity(run_id, data_epoch, source_commit_sha, freeze_manifest_sha256)
    if block_count not in REPLICATION_BLOCK_COUNTS:
        raise Qas30ExtensionError("replication block count must be 10, 20, 30, or 40")
    orders_by_block = list(candidate_orders_by_block)
    if len(orders_by_block) != block_count:
        raise Qas30ExtensionError("replication candidate-order block count changed")

    records: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    ordinal = 0
    for block_index, block_orders in enumerate(orders_by_block, start=1):
        if set(block_orders) != set(REPLICATION_STAGES):
            raise Qas30ExtensionError("replication block stages are incomplete")
        candidate_orders = {
            stage: _candidate_order(block_orders[stage]) for stage in REPLICATION_STAGES
        }
        cover = candidate_orders["R1_B1"] + candidate_orders["R1_B2"]
        if len(set(cover)) != 10:
            raise Qas30ExtensionError("replication R1 cover10 contains duplicate candidates")
        unseen = set(CANDIDATE_IDS) - set(cover)
        if any(
            not set(candidate_orders[stage]) <= unseen
            for stage in REPLICATION_STAGES[2:]
        ):
            raise Qas30ExtensionError("replication R2 candidates must come from the unseen20")

        block_id = f"BLOCK-{block_index:03d}"
        window_entity = build_design_entity(
            "REPLICATION_BLOCK_SCOPE",
            {"blockId": block_id, "blockOrdinal": block_index, "runId": identity[0]},
        )
        k_entities = {
            label: build_design_entity(
                "REPLICATION_CALIBRATION_SLOT",
                {
                    "blockId": block_id,
                    "blockOrdinal": block_index,
                    "label": label,
                    "runId": identity[0],
                },
            )
            for label in ("K0", "K1", "K2")
        }
        entities.extend([window_entity, *k_entities.values()])
        for stage_index, stage in enumerate(REPLICATION_STAGES, start=1):
            ordinal += 1
            active_k = "K0" if stage_index <= 2 else "K1"
            selection_payload: dict[str, Any] = {
                "blockOrdinal": block_index,
                "stage": stage,
                "candidateOrder": candidate_orders[stage],
            }
            if stage == "R2_RANDOM":
                selection_payload["randomSeedSha256"] = hashlib.sha256(
                    identity[0].encode("utf-8")
                ).hexdigest()
            selection_entity = build_design_entity(
                "REPLICATION_CANDIDATE_SELECTION", selection_payload
            )
            mapping_entity = build_design_entity(
                "REPLICATION_COMMON_MAPPING_SLOT",
                {"blockOrdinal": block_index, "stage": stage, "activeCalibration": active_k},
            )
            entities.extend([selection_entity, mapping_entity])
            records.append(
                build_batch_spec(
                    experiment="INDEPENDENT_REPLICATION",
                    run_id=identity[0],
                    data_epoch=identity[1],
                    source_commit_sha=identity[2],
                    freeze_manifest_sha256=identity[3],
                    batch_id=f"{identity[0]}-{block_id}-B{stage_index}",
                    batch_role=stage,
                    ordinal=ordinal,
                    candidate_order=candidate_orders[stage],
                    selection_entity_sha256=selection_entity["entitySha256"],
                    window_entity_sha256=window_entity["entitySha256"],
                    mapping_entity_sha256=mapping_entity["entitySha256"],
                    snapshot_entity_sha256=k_entities[active_k]["entitySha256"],
                    shots=DEFAULT_SHOTS,
                    coordinates={
                        "blockId": block_id,
                        "blockOrdinal": block_index,
                        "blockBatchOrdinal": stage_index,
                        "activeCalibration": active_k,
                    },
                )
            )
    plan = _build_plan(
        experiment="INDEPENDENT_REPLICATION",
        run_id=identity[0],
        data_epoch=identity[1],
        source_commit_sha=identity[2],
        freeze_manifest_sha256=identity[3],
        records=records,
        entities=entities,
        protocol={
            "blockCount": block_count,
            "callsPerBlock": 6,
            "circuitsPerBlock": 30,
            "shotsPerBlock": 30_000,
            "checkpoints": list(REPLICATION_BLOCK_COUNTS),
            "candidateOverlapPolicy": "INDEPENDENT_RERUN",
        },
    )
    expected = {
        "calls": block_count * 6,
        "circuits": block_count * 30,
        "shots": block_count * 30_000,
    }
    if plan["registeredBudget"] != expected or plan["executionBudget"] != expected:
        raise Qas30ExtensionError("replication budget does not match the frozen block design")
    return plan


def build_shot_scaling_plan(
    *,
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
    candidate_order: Sequence[str],
    common_measurement_order: Sequence[str],
    capability_snapshot_entity_sha256: str,
    supported_shot_levels: Sequence[int] = SHOT_LEVELS,
) -> dict[str, Any]:
    """Freeze all shot-scaling slots and preserve each capability decision."""

    identity = _identity(run_id, data_epoch, source_commit_sha, freeze_manifest_sha256)
    candidates = _candidate_order(candidate_order)
    measurement_order = _measurement_order(common_measurement_order)
    snapshot_sha = _require_sha256(
        capability_snapshot_entity_sha256, "capabilitySnapshotEntitySha256"
    )
    if isinstance(supported_shot_levels, str | bytes):
        raise Qas30ExtensionError("supported shot levels must be a sequence")
    supported = set(supported_shot_levels)
    if any(
        not isinstance(level, int) or isinstance(level, bool) or level not in SHOT_LEVELS
        for level in supported
    ):
        raise Qas30ExtensionError("supported shot levels must come from the frozen grid")
    selection_entity = build_design_entity(
        "SHOT_SCALING_CANDIDATE_SELECTION", {"candidateOrder": candidates}
    )
    window_entity = build_design_entity(
        "SHOT_SCALING_COMPARISON_SCOPE", {"shotLevels": list(SHOT_LEVELS), "repetitions": 3}
    )
    mapping_entity = build_design_entity(
        "SHOT_SCALING_COMMON_MAPPING",
        {"commonMeasurementOrder": measurement_order},
    )
    entities = [selection_entity, window_entity, mapping_entity]
    records: list[dict[str, Any]] = []
    ordinal = 0
    for shot_level in SHOT_LEVELS:
        for repetition in range(1, 4):
            ordinal += 1
            records.append(
                build_batch_spec(
                    experiment="SHOT_SCALING",
                    run_id=identity[0],
                    data_epoch=identity[1],
                    source_commit_sha=identity[2],
                    freeze_manifest_sha256=identity[3],
                    batch_id=f"{identity[0]}-SHOT-{shot_level}-R{repetition}",
                    batch_role=f"SHOT_{shot_level}_R{repetition}",
                    ordinal=ordinal,
                    candidate_order=candidates,
                    selection_entity_sha256=selection_entity["entitySha256"],
                    window_entity_sha256=window_entity["entitySha256"],
                    mapping_entity_sha256=mapping_entity["entitySha256"],
                    snapshot_entity_sha256=snapshot_sha,
                    shots=shot_level,
                    execution_state=(
                        READY_STATE if shot_level in supported else UNSUPPORTED_STATE
                    ),
                    coordinates={"shotLevel": shot_level, "repetition": repetition},
                )
            )
    plan = _build_plan(
        experiment="SHOT_SCALING",
        run_id=identity[0],
        data_epoch=identity[1],
        source_commit_sha=identity[2],
        freeze_manifest_sha256=identity[3],
        records=records,
        entities=entities,
        protocol={
            "shotLevels": list(SHOT_LEVELS),
            "repetitions": 3,
            "capabilitySnapshotEntitySha256": snapshot_sha,
            "stoppingRule": "SUPPORTED_LEVELS_ONLY",
        },
    )
    if plan["registeredBudget"] != EXTENSION_EXPECTED_BUDGETS["SHOT_SCALING"]:
        raise Qas30ExtensionError("shot-scaling registered budget changed")
    return plan


def _parse_snapshot_time(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise Qas30ExtensionError("snapshot times must use UTC Z timestamps")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise Qas30ExtensionError("snapshot time is not valid ISO-8601") from error
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise Qas30ExtensionError("snapshot time spelling is not canonical")
    return parsed


def build_calibration_drift_plan(
    *,
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
    sentinel_candidate_order: Sequence[str],
    common_measurement_order: Sequence[str],
    snapshot_times: Sequence[str],
) -> dict[str, Any]:
    """Freeze twelve ordered sentinel snapshots at intervals of at least 30 minutes."""

    identity = _identity(run_id, data_epoch, source_commit_sha, freeze_manifest_sha256)
    candidates = _candidate_order(sentinel_candidate_order)
    measurement_order = _measurement_order(common_measurement_order)
    if isinstance(snapshot_times, str | bytes) or len(snapshot_times) != 12:
        raise Qas30ExtensionError("calibration drift requires twelve snapshot times")
    parsed = [_parse_snapshot_time(value) for value in snapshot_times]
    if any(
        (current - previous).total_seconds() < 1_800
        for previous, current in zip(parsed[:-1], parsed[1:], strict=True)
    ):
        raise Qas30ExtensionError("calibration snapshot interval is below 30 minutes")
    selection_entity = build_design_entity(
        "CALIBRATION_SENTINEL_SELECTION", {"candidateOrder": candidates}
    )
    window_entity = build_design_entity(
        "CALIBRATION_DRIFT_SERIES", {"validSnapshotTarget": 12, "minimumIntervalSeconds": 1800}
    )
    mapping_entity = build_design_entity(
        "CALIBRATION_SENTINEL_COMMON_MAPPING",
        {"commonMeasurementOrder": measurement_order},
    )
    entities = [selection_entity, window_entity, mapping_entity]
    records: list[dict[str, Any]] = []
    for ordinal, scheduled_at in enumerate(snapshot_times, start=1):
        snapshot_entity = build_design_entity(
            "CALIBRATION_SNAPSHOT_SLOT",
            {"snapshotOrdinal": ordinal, "scheduledAt": scheduled_at},
        )
        entities.append(snapshot_entity)
        records.append(
            build_batch_spec(
                experiment="CALIBRATION_DRIFT",
                run_id=identity[0],
                data_epoch=identity[1],
                source_commit_sha=identity[2],
                freeze_manifest_sha256=identity[3],
                batch_id=f"{identity[0]}-CAL-{ordinal:02d}",
                batch_role=f"SNAPSHOT_{ordinal:02d}",
                ordinal=ordinal,
                candidate_order=candidates,
                selection_entity_sha256=selection_entity["entitySha256"],
                window_entity_sha256=window_entity["entitySha256"],
                mapping_entity_sha256=mapping_entity["entitySha256"],
                snapshot_entity_sha256=snapshot_entity["entitySha256"],
                shots=DEFAULT_SHOTS,
                coordinates={"snapshotOrdinal": ordinal, "scheduledAt": scheduled_at},
            )
        )
    plan = _build_plan(
        experiment="CALIBRATION_DRIFT",
        run_id=identity[0],
        data_epoch=identity[1],
        source_commit_sha=identity[2],
        freeze_manifest_sha256=identity[3],
        records=records,
        entities=entities,
        protocol={
            "validSnapshotTarget": 12,
            "minimumIntervalSeconds": 1_800,
            "stoppingRule": "TWELVE_VALID_SNAPSHOTS_OR_PLATFORM_STATE_CHANGE",
        },
    )
    if plan["registeredBudget"] != EXTENSION_EXPECTED_BUDGETS["CALIBRATION_DRIFT"]:
        raise Qas30ExtensionError("calibration-drift budget changed")
    return plan


def build_mapping_sensitivity_plan(
    *,
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
    candidate_order: Sequence[str],
    common_measurement_orders: Sequence[Sequence[str]],
    active_snapshot_entity_sha256: str,
) -> dict[str, Any]:
    """Freeze five distinct common-order mappings for the same five candidates."""

    identity = _identity(run_id, data_epoch, source_commit_sha, freeze_manifest_sha256)
    candidates = _candidate_order(candidate_order)
    snapshot_sha = _require_sha256(
        active_snapshot_entity_sha256, "activeSnapshotEntitySha256"
    )
    if isinstance(common_measurement_orders, str | bytes) or len(common_measurement_orders) != 5:
        raise Qas30ExtensionError("mapping sensitivity requires five common orders")
    orders = [_measurement_order(value) for value in common_measurement_orders]
    if len({tuple(value) for value in orders}) != 5:
        raise Qas30ExtensionError("mapping sensitivity common orders must be distinct")
    selection_entity = build_design_entity(
        "MAPPING_SENSITIVITY_CANDIDATE_SELECTION", {"candidateOrder": candidates}
    )
    window_entity = build_design_entity(
        "MAPPING_SENSITIVITY_COMPARISON_SCOPE", {"commonOrderMappingCount": 5}
    )
    entities = [selection_entity, window_entity]
    records: list[dict[str, Any]] = []
    for ordinal, order in enumerate(orders, start=1):
        mapping_entity = build_design_entity(
            "COMMON_ORDER_MAPPING",
            {"mappingOrdinal": ordinal, "commonMeasurementOrder": order},
        )
        entities.append(mapping_entity)
        records.append(
            build_batch_spec(
                experiment="MAPPING_SENSITIVITY",
                run_id=identity[0],
                data_epoch=identity[1],
                source_commit_sha=identity[2],
                freeze_manifest_sha256=identity[3],
                batch_id=f"{identity[0]}-MAP-{ordinal:02d}",
                batch_role=f"MAPPING_{ordinal:02d}",
                ordinal=ordinal,
                candidate_order=candidates,
                selection_entity_sha256=selection_entity["entitySha256"],
                window_entity_sha256=window_entity["entitySha256"],
                mapping_entity_sha256=mapping_entity["entitySha256"],
                snapshot_entity_sha256=snapshot_sha,
                shots=DEFAULT_SHOTS,
                coordinates={"mappingOrdinal": ordinal},
            )
        )
    plan = _build_plan(
        experiment="MAPPING_SENSITIVITY",
        run_id=identity[0],
        data_epoch=identity[1],
        source_commit_sha=identity[2],
        freeze_manifest_sha256=identity[3],
        records=records,
        entities=entities,
        protocol={
            "commonOrderMappingCount": 5,
            "stoppingRule": "FIVE_VALID_COMMON_ORDER_MAPPINGS",
        },
    )
    if plan["registeredBudget"] != EXTENSION_EXPECTED_BUDGETS["MAPPING_SENSITIVITY"]:
        raise Qas30ExtensionError("mapping-sensitivity budget changed")
    return plan


def end_to_end_key(window_end_month: str, arm: str) -> str:
    """Return the canonical lookup key for one end-to-end window and arm."""

    if window_end_month not in END_TO_END_WINDOW_END_MONTHS or arm not in END_TO_END_ARMS:
        raise Qas30ExtensionError("end-to-end window or arm is outside the frozen design")
    return f"{window_end_month}:{arm}"


def _month_index(value: str) -> int:
    year_text, month_text = value.split("-")
    return int(year_text) * 12 + int(month_text) - 1


def _month_label(value: int) -> str:
    year, month_index = divmod(value, 12)
    return f"{year:04d}-{month_index + 1:02d}"


def build_end_to_end_feedback_plan(
    *,
    run_id: str,
    data_epoch: str,
    source_commit_sha: str,
    freeze_manifest_sha256: str,
    candidate_orders_by_window_arm: Mapping[str, Sequence[str]],
    mapping_entity_sha256_by_window_arm: Mapping[str, str],
    snapshot_entity_sha256s: Sequence[str],
) -> dict[str, Any]:
    """Freeze five 360-month windows, three arms, and fifteen five-circuit batches."""

    identity = _identity(run_id, data_epoch, source_commit_sha, freeze_manifest_sha256)
    expected_keys = {
        end_to_end_key(window, arm)
        for window in END_TO_END_WINDOW_END_MONTHS
        for arm in END_TO_END_ARMS
    }
    if set(candidate_orders_by_window_arm) != expected_keys:
        raise Qas30ExtensionError("end-to-end candidate selections are incomplete")
    if set(mapping_entity_sha256_by_window_arm) != expected_keys:
        raise Qas30ExtensionError("end-to-end mapping bindings are incomplete")
    if isinstance(snapshot_entity_sha256s, str | bytes) or len(snapshot_entity_sha256s) != 5:
        raise Qas30ExtensionError("end-to-end feedback requires five snapshot identities")
    snapshot_hashes = [
        _require_sha256(value, f"snapshotEntitySha256[{index}]")
        for index, value in enumerate(snapshot_entity_sha256s)
    ]

    records: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    ordinal = 0
    for window_ordinal, window_end in enumerate(END_TO_END_WINDOW_END_MONTHS, start=1):
        end_index = _month_index(window_end)
        window_entity = build_design_entity(
            "END_TO_END_MARKET_WINDOW",
            {
                "windowOrdinal": window_ordinal,
                "windowStartMonth": _month_label(end_index - 359),
                "trainingStartMonth": _month_label(end_index - 359),
                "trainingEndMonth": _month_label(end_index - 60),
                "validationStartMonth": _month_label(end_index - 59),
                "validationEndMonth": window_end,
                "windowMonths": 360,
                "trainingMonths": 300,
                "validationMonths": 60,
            },
        )
        entities.append(window_entity)
        for arm_ordinal, arm in enumerate(END_TO_END_ARMS, start=1):
            ordinal += 1
            key = end_to_end_key(window_end, arm)
            candidates = _candidate_order(candidate_orders_by_window_arm[key])
            selection_entity = build_design_entity(
                "END_TO_END_ARM_SELECTION",
                {"windowEndMonth": window_end, "arm": arm, "candidateOrder": candidates},
            )
            entities.append(selection_entity)
            records.append(
                build_batch_spec(
                    experiment="END_TO_END_FEEDBACK",
                    run_id=identity[0],
                    data_epoch=identity[1],
                    source_commit_sha=identity[2],
                    freeze_manifest_sha256=identity[3],
                    batch_id=f"{identity[0]}-W{window_ordinal}-A{arm_ordinal}",
                    batch_role=f"WINDOW_{window_ordinal}_ARM_{arm_ordinal}",
                    ordinal=ordinal,
                    candidate_order=candidates,
                    selection_entity_sha256=selection_entity["entitySha256"],
                    window_entity_sha256=window_entity["entitySha256"],
                    mapping_entity_sha256=_require_sha256(
                        mapping_entity_sha256_by_window_arm[key],
                        f"mappingEntitySha256[{key}]",
                    ),
                    snapshot_entity_sha256=snapshot_hashes[window_ordinal - 1],
                    shots=DEFAULT_SHOTS,
                    coordinates={
                        "windowOrdinal": window_ordinal,
                        "windowEndMonth": window_end,
                        "armOrdinal": arm_ordinal,
                        "arm": arm,
                    },
                )
            )
    plan = _build_plan(
        experiment="END_TO_END_FEEDBACK",
        run_id=identity[0],
        data_epoch=identity[1],
        source_commit_sha=identity[2],
        freeze_manifest_sha256=identity[3],
        records=records,
        entities=entities,
        protocol={
            "windowEndMonths": list(END_TO_END_WINDOW_END_MONTHS),
            "windowMonths": 360,
            "trainingMonths": 300,
            "validationMonths": 60,
            "arms": list(END_TO_END_ARMS),
            "snapshotCount": 5,
            "stoppingRule": "THREE_ARMS_ACROSS_FIVE_COMPLETE_SNAPSHOTS",
        },
    )
    if plan["registeredBudget"] != EXTENSION_EXPECTED_BUDGETS["END_TO_END_FEEDBACK"]:
        raise Qas30ExtensionError("end-to-end feedback budget changed")
    return plan


def build_extension_budget_manifest(
    *,
    shot_scaling: Mapping[str, Any],
    calibration_drift: Mapping[str, Any],
    mapping_sensitivity: Mapping[str, Any],
    end_to_end_feedback: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the four extension plans and seal their combined resource budget."""

    plans = {
        "SHOT_SCALING": validate_extension_plan(shot_scaling),
        "CALIBRATION_DRIFT": validate_extension_plan(calibration_drift),
        "MAPPING_SENSITIVITY": validate_extension_plan(mapping_sensitivity),
        "END_TO_END_FEEDBACK": validate_extension_plan(end_to_end_feedback),
    }
    for name, plan in plans.items():
        if plan.get("experiment") != name:
            raise Qas30ExtensionError(f"{name} plan identity changed")
        if plan.get("registeredBudget") != EXTENSION_EXPECTED_BUDGETS[name]:
            raise Qas30ExtensionError(f"{name} registered budget changed")
    registered = {
        key: sum(plan["registeredBudget"][key] for plan in plans.values())
        for key in ("calls", "circuits", "shots")
    }
    execution = {
        key: sum(plan["executionBudget"][key] for plan in plans.values())
        for key in ("calls", "circuits", "shots")
    }
    if registered != EXTENSION_EXPECTED_TOTAL:
        raise Qas30ExtensionError("combined extension budget changed")
    return _seal(
        {
            "schemaVersion": EXTENSION_BUDGET_SCHEMA_VERSION,
            "componentPlanSha256s": {
                name: plan["canonicalSha256"] for name, plan in plans.items()
            },
            "registeredBudget": registered,
            "executionBudget": execution,
            "scientificStatus": SCIENTIFIC_STATUS,
        }
    )


def build_total_budget_table() -> dict[str, Any]:
    """Seal the primary + replication + extension totals at all stop checks."""

    rows = []
    for completed_blocks in REPLICATION_BLOCK_COUNTS:
        rows.append(
            {
                "completedReplicationBlocks": completed_blocks,
                "calls": 6 + completed_blocks * 6 + EXTENSION_EXPECTED_TOTAL["calls"],
                "circuits": 30
                + completed_blocks * 30
                + EXTENSION_EXPECTED_TOTAL["circuits"],
                "shots": 30_000
                + completed_blocks * 30_000
                + EXTENSION_EXPECTED_TOTAL["shots"],
            }
        )
    return _seal(
        {
            "schemaVersion": TOTAL_BUDGET_SCHEMA_VERSION,
            "primaryBudget": {"calls": 6, "circuits": 30, "shots": 30_000},
            "extensionBudget": dict(EXTENSION_EXPECTED_TOTAL),
            "replicationBlockBudget": {"calls": 6, "circuits": 30, "shots": 30_000},
            "rows": rows,
            "scientificStatus": SCIENTIFIC_STATUS,
        }
    )


def validate_total_budget_table(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the exact 10/20/30/40-block total resource table."""

    table = validate_canonical_record(value)
    expected = build_total_budget_table()
    if table != expected:
        raise Qas30ExtensionError("total resource budget table changed")
    return table
