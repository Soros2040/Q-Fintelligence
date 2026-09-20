"""Official single-request Tianyan observations for the governed QAS30 run.

The functions in this module never discover credentials, retry, redirect,
poll, or submit circuits.  Callers inject an in-memory access token and the
strict transport from :mod:`qas30_tianyan` for exactly one official request.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cqlib import TianYanPlatform

from . import qas30_tianyan

TARGET = "tianyan176"
MACHINE_STATE_SCHEMA_VERSION = "qf.qas30.machine-state-observation.v1"
CALIBRATION_ATTEMPT_SCHEMA_VERSION = "qf.qas30.calibration-config-attempt.v1"
CALIBRATION_RECEIPT_SCHEMA_VERSION = "qf.qas30.calibration-receipt.v1"
CALIBRATION_SNAPSHOT_SCHEMA_VERSION = "qf.qas30.calibration-snapshot.v1"
NORMALIZED_TOPOLOGY_SCHEMA_VERSION = "qf.qas30.normalized-topology.v1"
REGULARITY_OBSERVATION_SCHEMA_VERSION = "qf.qas30.qcis-regularity-observation.v1"
REGULARITY_METHOD = "TIANYAN_QCIS_CHECK_REGULAR_SINGLE_REQUEST"
MACHINE_STATUS_NAMES = {
    0: "RUNNING",
    1: "CALIBRATING",
    2: "UNDER_MAINTENANCE",
    3: "OFFLINE",
}
IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
COMMIT_PATTERN = re.compile(r"[a-f0-9]{40}")
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
QUBIT_PATTERN = re.compile(r"Q(?:0|[1-9][0-9]{0,3})")
COUPLER_PATTERN = re.compile(r"G(?:0|[1-9][0-9]{0,3})")
CANDIDATE_PATTERN = re.compile(r"C(?:0[1-9]|[12][0-9]|30)")
READ_TIME_PATTERN = re.compile(
    r"\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]) "
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
)


class Qas30ObservationError(RuntimeError):
    """One official observation could not establish its required fact."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        observation: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.observation = dict(observation) if observation is not None else None
        self.retry_allowed = False


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


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value:
        raise Qas30ObservationError(
            "QAS30_OBSERVED_AT_INVALID", "observation timestamp is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Qas30ObservationError(
            "QAS30_OBSERVED_AT_INVALID", "observation timestamp is invalid"
        ) from error
    if parsed.tzinfo is None:
        raise Qas30ObservationError(
            "QAS30_OBSERVED_AT_INVALID", "observation timestamp needs a timezone"
        )
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _governance(
    *,
    run_id: str,
    data_epoch: str,
    freeze_manifest_sha256: str,
    source_commit_sha: str,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
) -> dict[str, str]:
    if not IDENTITY_PATTERN.fullmatch(run_id) or not IDENTITY_PATTERN.fullmatch(
        data_epoch
    ):
        raise Qas30ObservationError(
            "QAS30_OBSERVATION_IDENTITY_INVALID", "run identity is invalid"
        )
    if not COMMIT_PATTERN.fullmatch(source_commit_sha):
        raise Qas30ObservationError(
            "QAS30_OBSERVATION_COMMIT_INVALID", "source commit is invalid"
        )
    for value in (
        freeze_manifest_sha256,
        authorization_envelope_sha256,
        resource_sample_sha256,
    ):
        if not SHA256_PATTERN.fullmatch(value):
            raise Qas30ObservationError(
                "QAS30_OBSERVATION_BINDING_INVALID", "governance hash is invalid"
            )
    return {
        "runId": run_id,
        "dataEpoch": data_epoch,
        "freezeManifestSha256": freeze_manifest_sha256,
        "sourceCommitSha": source_commit_sha,
        "authorizationEnvelopeSha256": authorization_envelope_sha256,
        "resourceSampleSha256": resource_sample_sha256,
    }


def _provider_request_sha256(
    *,
    method: str,
    url: str,
    query_params: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
    governance: Mapping[str, str] | None = None,
) -> str:
    return _entity_sha256(
        {
            "method": method,
            "url": url,
            "queryParams": dict(query_params) if query_params is not None else None,
            "jsonBody": dict(json_body) if json_body is not None else None,
            "governance": dict(governance) if governance is not None else None,
        }
    )


def _failure_observation(
    *,
    action: str,
    outcome: str,
    governance: Mapping[str, str],
    request_sha256: str,
    response: qas30_tianyan.SingleAttemptHttpResponse,
    scientific_status: str,
    observed_at: str,
) -> dict[str, Any]:
    observation = {
        "schemaVersion": CALIBRATION_ATTEMPT_SCHEMA_VERSION,
        "action": action,
        **governance,
        "target": TARGET,
        "outcome": outcome,
        "providerRequestSha256": request_sha256,
        "providerResponseSha256": _text_sha256(_canonical_json(response.payload)),
        "providerHttpStatus": response.status_code,
        "transportRequestCount": 1,
        "scientificStatus": scientific_status,
        "observedAt": observed_at,
    }
    observation["attemptObservationSha256"] = _entity_sha256(observation)
    return observation


def _single_response(
    *,
    transport: qas30_tianyan.SingleAttemptTransport,
    action: str,
    method: str,
    url: str,
    access_token: str,
    timeout_seconds: int,
    query_params: Mapping[str, str | int | float] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[qas30_tianyan.SingleAttemptHttpResponse, bool]:
    try:
        response = transport.request_once(
            method=method,
            url=url,
            headers=qas30_tianyan._headers(access_token, action=action),  # noqa: SLF001
            timeout_seconds=timeout_seconds,
            query_params=query_params,
            json_body=json_body,
        )
        return response, False
    except Exception as error:  # the attempt outcome must remain explicit
        return (
            qas30_tianyan.SingleAttemptHttpResponse(
                status_code=0,
                payload={"transportErrorType": type(error).__name__},
                headers={},
            ),
            True,
        )


def _claim_observation(
    *, receipt_path: Path | None, action: str, request_sha256: str
) -> None:
    """Optionally create the caller-selected durable intent before one request."""

    if receipt_path is None:
        return
    try:
        qas30_tianyan._claim_once(  # noqa: SLF001
            receipt_path, request_sha256=request_sha256, action=action
        )
    except qas30_tianyan.Qas30TianyanError as error:
        raise Qas30ObservationError(error.code, str(error)) from error


def _validate_observation_token(*, access_token: str, action: str) -> None:
    """Reject forbidden credentials before creating a durable request intent."""

    try:
        qas30_tianyan._headers(access_token, action=action)  # noqa: SLF001
    except qas30_tianyan.Qas30TianyanError as error:
        raise Qas30ObservationError(error.code, str(error)) from error


def _persist_observation(
    *,
    receipt_path: Path | None,
    action: str,
    request_sha256: str,
    response: qas30_tianyan.SingleAttemptHttpResponse,
    observation: Mapping[str, Any],
) -> None:
    """Write the response artifact then one immutable caller-selected receipt."""

    if receipt_path is None:
        return
    try:
        artifact_sha256, artifact_name = qas30_tianyan._persist_provider_response_artifact(  # noqa: SLF001
            response=response,
            request_sha256=request_sha256,
            artifact_path=receipt_path.with_name(
                f"{receipt_path.stem}.provider-response.json"
            ),
        )
        durable_receipt = {
            "schemaVersion": "qf.qas30.observation-receipt.v1",
            "action": action,
            "state": "OBSERVATION_PERSISTED",
            "requestSha256": request_sha256,
            "providerResponseArtifact": artifact_name,
            "providerResponseArtifactSha256": artifact_sha256,
            "observation": dict(observation),
        }
        durable_receipt["observationReceiptSha256"] = _entity_sha256(
            durable_receipt
        )
        qas30_tianyan._persist_json(receipt_path, durable_receipt)  # noqa: SLF001
    except qas30_tianyan.ReceiptPersistenceError as error:
        raise Qas30ObservationError(
            "QAS30_OBSERVATION_PERSIST_FAILED",
            "the sent observation is unknown because durable persistence failed",
            observation=error.receipt,
        ) from error


def machine_state_once(
    *,
    run_id: str,
    data_epoch: str,
    freeze_manifest_sha256: str,
    source_commit_sha: str,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
    access_token: str,
    transport: qas30_tianyan.SingleAttemptTransport,
    observed_at: str | None = None,
    timeout_seconds: int = 60,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Observe the official machine list once and select exactly Tianyan176."""

    governance = _governance(
        run_id=run_id,
        data_epoch=data_epoch,
        freeze_manifest_sha256=freeze_manifest_sha256,
        source_commit_sha=source_commit_sha,
        authorization_envelope_sha256=authorization_envelope_sha256,
        resource_sample_sha256=resource_sample_sha256,
    )
    timestamp = _timestamp(observed_at)
    url = f"{qas30_tianyan.BASE_URL}{TianYanPlatform.MACHINE_LIST_PATH}"
    request_sha256 = _provider_request_sha256(
        method="GET", url=url, governance=governance
    )
    _validate_observation_token(access_token=access_token, action="MACHINE_STATE")
    _claim_observation(
        receipt_path=receipt_path, action="MACHINE_STATE", request_sha256=request_sha256
    )
    response, transport_unknown = _single_response(
        transport=transport,
        action="MACHINE_STATE",
        method="GET",
        url=url,
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
    payload = response.payload
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    matches = (
        [
            dict(row)
            for row in rows
            if isinstance(row, Mapping)
            and str(row.get("code", row.get("machineName", ""))) == TARGET
        ]
        if isinstance(rows, list)
        else []
    )
    provider_status_code: Any = None
    status = "UNAVAILABLE"
    selected: dict[str, Any] | None = None
    if transport_unknown:
        outcome = "TRANSPORT_UNKNOWN"
    elif response.status_code != 200:
        outcome = "HTTP_REJECTED"
    elif not isinstance(payload, Mapping) or payload.get("code") != 0:
        outcome = "PROVIDER_REJECTED"
    elif len(matches) != 1:
        outcome = "IDENTITY_MISMATCH"
    else:
        selected = matches[0]
        provider_status_code = selected.get("status")
        status = MACHINE_STATUS_NAMES.get(
            provider_status_code, f"UNKNOWN_STATUS_{provider_status_code!s}"
        )
        outcome = "MACHINE_STATE_OBSERVED"
    observation = {
        "schemaVersion": MACHINE_STATE_SCHEMA_VERSION,
        **governance,
        "target": TARGET,
        "outcome": outcome,
        "providerMachineRow": selected,
        "providerStatusCode": provider_status_code,
        "machineStatus": status,
        "online": outcome == "MACHINE_STATE_OBSERVED" and status == "RUNNING",
        "providerRequestSha256": request_sha256,
        "providerResponseSha256": _text_sha256(_canonical_json(payload)),
        "providerHttpStatus": response.status_code,
        "transportRequestCount": 1,
        "scientificStatus": qas30_tianyan.transport_scientific_status(transport),
        "observedAt": timestamp,
    }
    observation["machineStateObservationSha256"] = _entity_sha256(observation)
    _persist_observation(
        receipt_path=receipt_path,
        action="MACHINE_STATE",
        request_sha256=request_sha256,
        response=response,
        observation=observation,
    )
    return observation


def _csv_identities(value: Any, *, pattern: re.Pattern[str], field: str) -> set[str]:
    if not isinstance(value, str):
        raise Qas30ObservationError(
            "QAS30_CONFIG_DISABLED_INVALID", f"{field} is invalid"
        )
    result = {item.strip().upper() for item in value.split(",") if item.strip()}
    if any(pattern.fullmatch(item) is None for item in result):
        raise Qas30ObservationError(
            "QAS30_CONFIG_DISABLED_INVALID", f"{field} is invalid"
        )
    return result


def _percentage_series(
    value: Any,
    *,
    pattern: re.Pattern[str],
    field: str,
) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise Qas30ObservationError(
            "QAS30_CONFIG_ERROR_SERIES_MISSING", f"{field} is missing"
        )
    identities = value.get("qubit_used")
    parameters = value.get("param_list")
    if (
        value.get("unit") != "%"
        or not isinstance(identities, list)
        or not isinstance(parameters, list)
        or len(identities) != len(parameters)
        or len(set(identities)) != len(identities)
    ):
        raise Qas30ObservationError(
            "QAS30_CONFIG_ERROR_SERIES_INVALID", f"{field} is invalid"
        )
    result: dict[str, float] = {}
    for raw_identity, raw_parameter in zip(identities, parameters, strict=True):
        identity = str(raw_identity).upper()
        if pattern.fullmatch(identity) is None or isinstance(raw_parameter, bool):
            raise Qas30ObservationError(
                "QAS30_CONFIG_ERROR_SERIES_INVALID", f"{field} is invalid"
            )
        try:
            percentage = float(raw_parameter)
        except (TypeError, ValueError) as error:
            raise Qas30ObservationError(
                "QAS30_CONFIG_ERROR_SERIES_INVALID", f"{field} is invalid"
            ) from error
        if not math.isfinite(percentage) or not 0.0 <= percentage <= 100.0:
            raise Qas30ObservationError(
                "QAS30_CONFIG_ERROR_SERIES_INVALID", f"{field} is invalid"
            )
        result[identity] = percentage / 100.0
    return result


def normalize_machine_config(machine_config: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the cqlib 1.3.11 config into mapping's exact topology contract."""

    if not isinstance(machine_config, Mapping) or machine_config.get("computerId") != TARGET:
        raise Qas30ObservationError(
            "QAS30_CONFIG_TARGET_INVALID", "machine configuration target changed"
        )
    overview = machine_config.get("overview")
    coupler_map = overview.get("coupler_map") if isinstance(overview, Mapping) else None
    if not isinstance(coupler_map, Mapping) or not coupler_map:
        raise Qas30ObservationError(
            "QAS30_CONFIG_COUPLER_MAP_INVALID", "coupler map is missing"
        )
    pairs: dict[str, tuple[str, str]] = {}
    qubits: set[str] = set()
    for raw_coupler, raw_pair in coupler_map.items():
        coupler = str(raw_coupler).upper()
        if (
            COUPLER_PATTERN.fullmatch(coupler) is None
            or not isinstance(raw_pair, Sequence)
            or isinstance(raw_pair, str | bytes)
            or len(raw_pair) != 2
        ):
            raise Qas30ObservationError(
                "QAS30_CONFIG_COUPLER_MAP_INVALID", "coupler map is invalid"
            )
        pair = tuple(str(item).upper() for item in raw_pair)
        if (
            pair[0] == pair[1]
            or QUBIT_PATTERN.fullmatch(pair[0]) is None
            or QUBIT_PATTERN.fullmatch(pair[1]) is None
        ):
            raise Qas30ObservationError(
                "QAS30_CONFIG_COUPLER_MAP_INVALID", "coupler map is invalid"
            )
        pairs[coupler] = pair
        qubits.update(pair)
    disabled_qubits = _csv_identities(
        machine_config.get("disabledQubits"),
        pattern=QUBIT_PATTERN,
        field="disabledQubits",
    )
    disabled_couplers = _csv_identities(
        machine_config.get("disabledCouplers"),
        pattern=COUPLER_PATTERN,
        field="disabledCouplers",
    )
    if not disabled_qubits <= qubits or not disabled_couplers <= set(pairs):
        raise Qas30ObservationError(
            "QAS30_CONFIG_DISABLED_INVALID", "disabled identities are outside topology"
        )
    try:
        readout_source = machine_config["readout"]["readoutArray"]["Readout Error"]
        single_source = machine_config["qubit"]["singleQubit"]["gate error"]
        cz_source = machine_config["twoQubitGate"]["czGate"]["gate error"]
    except (KeyError, TypeError) as error:
        raise Qas30ObservationError(
            "QAS30_CONFIG_ERROR_SERIES_MISSING", "required calibration error is missing"
        ) from error
    readout = _percentage_series(
        readout_source, pattern=QUBIT_PATTERN, field="Readout Error"
    )
    single = _percentage_series(
        single_source, pattern=QUBIT_PATTERN, field="singleQubit gate error"
    )
    cz = _percentage_series(cz_source, pattern=COUPLER_PATTERN, field="CZ gate error")
    if not set(readout) <= qubits or not set(single) <= qubits or not set(cz) <= set(
        pairs
    ):
        raise Qas30ObservationError(
            "QAS30_CONFIG_ERROR_SERIES_INVALID", "error series contains unknown identities"
        )
    active_qubits = qubits - disabled_qubits
    if not active_qubits <= set(readout) or not active_qubits <= set(single):
        raise Qas30ObservationError(
            "QAS30_CONFIG_ACTIVE_ERROR_MISSING",
            "an active qubit has no readout or single-qubit error",
        )
    active_couplers = {
        coupler
        for coupler, pair in pairs.items()
        if coupler not in disabled_couplers
        and pair[0] not in disabled_qubits
        and pair[1] not in disabled_qubits
    }
    if not active_couplers <= set(cz):
        raise Qas30ObservationError(
            "QAS30_CONFIG_ACTIVE_ERROR_MISSING",
            "an active coupler has no CZ error",
        )
    machine_config_sha256 = _entity_sha256(machine_config)
    topology = {
        "schemaVersion": NORMALIZED_TOPOLOGY_SCHEMA_VERSION,
        "target": TARGET,
        "sourceConfigSha256": machine_config_sha256,
        "qubits": {
            qubit: {
                "active": qubit in active_qubits,
                "readoutError": readout.get(qubit),
                "singleGateError": single.get(qubit),
            }
            for qubit in sorted(qubits, key=lambda item: int(item[1:]))
        },
        "couplers": {
            coupler: {
                "source": pairs[coupler][0],
                "target": pairs[coupler][1],
                "active": coupler in active_couplers,
                "twoQubitError": cz.get(coupler),
            }
            for coupler in sorted(pairs, key=lambda item: int(item[1:]))
        },
    }
    topology["normalizedTopologySha256"] = _entity_sha256(topology)
    return topology


def _structural_capabilities(
    machine_config: Mapping[str, Any], topology: Mapping[str, Any]
) -> dict[str, Any]:
    qubits = topology["qubits"]
    couplers = topology["couplers"]
    single = machine_config.get("qubit", {}).get("singleQubit")
    two = machine_config.get("twoQubitGate", {}).get("czGate")
    if not isinstance(single, Mapping) or not isinstance(two, Mapping):
        raise Qas30ObservationError(
            "QAS30_CONFIG_GATE_FAMILY_MISSING", "gate-family structure is missing"
        )
    return {
        "activeQubitIds": [name for name, row in qubits.items() if row["active"]],
        "activeQubitCount": sum(row["active"] for row in qubits.values()),
        "activeCouplerIds": [
            name for name, row in couplers.items() if row["active"]
        ],
        "activeCouplerCount": sum(row["active"] for row in couplers.values()),
        "gateFamilies": {"singleQubit": ["X/2"], "twoQubit": ["CZ"]},
    }


def calibration_config_once(
    *,
    calibration_label: str,
    read_time: str,
    run_id: str,
    data_epoch: str,
    freeze_manifest_sha256: str,
    source_commit_sha: str,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
    access_token: str,
    transport: qas30_tianyan.SingleAttemptTransport,
    captured_at: str | None = None,
    timeout_seconds: int = 60,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Download and normalize one official configuration without SDK retries."""

    if calibration_label not in {"K0", "K1", "K2"}:
        raise Qas30ObservationError(
            "QAS30_CALIBRATION_LABEL_INVALID", "calibration label is invalid"
        )
    if not isinstance(read_time, str) or READ_TIME_PATTERN.fullmatch(read_time) is None:
        raise Qas30ObservationError(
            "QAS30_CALIBRATION_READ_TIME_INVALID", "readTime is invalid"
        )
    governance = _governance(
        run_id=run_id,
        data_epoch=data_epoch,
        freeze_manifest_sha256=freeze_manifest_sha256,
        source_commit_sha=source_commit_sha,
        authorization_envelope_sha256=authorization_envelope_sha256,
        resource_sample_sha256=resource_sample_sha256,
    )
    timestamp = _timestamp(captured_at)
    url = (
        f"{qas30_tianyan.BASE_URL}{TianYanPlatform.DOWNLOAD_CONFIG_PATH}/{TARGET}"
    )
    query_params = {"readTime": read_time}
    request_sha256 = _provider_request_sha256(
        method="GET", url=url, query_params=query_params, governance=governance
    )
    _validate_observation_token(
        access_token=access_token, action="CALIBRATION_CONFIG"
    )
    _claim_observation(
        receipt_path=receipt_path,
        action="CALIBRATION_CONFIG",
        request_sha256=request_sha256,
    )
    response, transport_unknown = _single_response(
        transport=transport,
        action="CALIBRATION_CONFIG",
        method="GET",
        url=url,
        access_token=access_token,
        timeout_seconds=timeout_seconds,
        query_params=query_params,
    )
    scientific_status = qas30_tianyan.transport_scientific_status(transport)
    if transport_unknown or response.status_code != 200:
        outcome = "TRANSPORT_UNKNOWN" if transport_unknown else "HTTP_REJECTED"
        observation = _failure_observation(
            action="CALIBRATION_CONFIG",
            outcome=outcome,
            governance=governance,
            request_sha256=request_sha256,
            response=response,
            scientific_status=scientific_status,
            observed_at=timestamp,
        )
        _persist_observation(
            receipt_path=receipt_path,
            action="CALIBRATION_CONFIG",
            request_sha256=request_sha256,
            response=response,
            observation=observation,
        )
        raise Qas30ObservationError(
            f"QAS30_CALIBRATION_{outcome}",
            "the one configuration request did not establish a calibration",
            observation=observation,
        )
    payload = response.payload
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(payload, Mapping) or payload.get("code") != 0:
        observation = _failure_observation(
            action="CALIBRATION_CONFIG",
            outcome="PROVIDER_REJECTED",
            governance=governance,
            request_sha256=request_sha256,
            response=response,
            scientific_status=scientific_status,
            observed_at=timestamp,
        )
        _persist_observation(
            receipt_path=receipt_path,
            action="CALIBRATION_CONFIG",
            request_sha256=request_sha256,
            response=response,
            observation=observation,
        )
        raise Qas30ObservationError(
            "QAS30_CALIBRATION_PROVIDER_REJECTED",
            "the provider rejected the configuration request",
            observation=observation,
        )
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as error:
            raise Qas30ObservationError(
                "QAS30_CALIBRATION_CONFIG_INVALID", "string configuration is not JSON"
            ) from error
    if not isinstance(data, Mapping):
        raise Qas30ObservationError(
            "QAS30_CALIBRATION_CONFIG_INVALID", "configuration data is not an object"
        )
    machine_config = dict(data)
    topology = normalize_machine_config(machine_config)
    machine_config_sha256 = _entity_sha256(machine_config)
    provider_response_sha256 = _text_sha256(_canonical_json(payload))
    structural = _structural_capabilities(machine_config, topology)
    capabilities = {
        "machineConfigSha256": machine_config_sha256,
        "structuralCapabilities": structural,
        "evidenceHashes": {
            "providerRequestSha256": request_sha256,
            "providerResponseSha256": provider_response_sha256,
            "machineConfigSha256": machine_config_sha256,
            "normalizedTopologySha256": topology["normalizedTopologySha256"],
        },
        "configContract": "CQLIB_1_3_11_DOWNLOAD_CONFIG",
    }
    snapshot = {
        "schemaVersion": CALIBRATION_SNAPSHOT_SCHEMA_VERSION,
        "calibrationLabel": calibration_label,
        "target": TARGET,
        "providerMachineId": TARGET,
        "capturedAt": timestamp,
        "capabilities": capabilities,
        "topology": topology,
        "machineConfig": machine_config,
        "machineConfigSha256": machine_config_sha256,
        "scientificStatus": scientific_status,
    }
    receipt = {
        "schemaVersion": CALIBRATION_RECEIPT_SCHEMA_VERSION,
        "runId": run_id,
        "dataEpoch": data_epoch,
        "calibrationLabel": calibration_label,
        "target": TARGET,
        "sourceCommitSha": source_commit_sha,
        "freezeManifestSha256": freeze_manifest_sha256,
        "authorizationEnvelopeSha256": authorization_envelope_sha256,
        "resourceSampleSha256": resource_sample_sha256,
        "snapshot": snapshot,
        "activeCalibrationSha256": _entity_sha256(snapshot),
        "transportRequestCount": 1,
        "capturedAt": timestamp,
        "scientificStatus": scientific_status,
    }
    _persist_observation(
        receipt_path=receipt_path,
        action="CALIBRATION_CONFIG",
        request_sha256=request_sha256,
        response=response,
        observation=receipt,
    )
    return receipt


def qcis_regularity_once(
    *,
    candidate_id: str,
    mapped_qcis: str,
    mapping_candidate_sha256: str,
    k0_config_sha256: str,
    active_calibration_sha256: str,
    run_id: str,
    data_epoch: str,
    freeze_manifest_sha256: str,
    source_commit_sha: str,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
    access_token: str,
    transport: qas30_tianyan.SingleAttemptTransport,
    observed_at: str | None = None,
    timeout_seconds: int = 60,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Classify one mapped QCIS through the official regularity endpoint once."""

    governance = _governance(
        run_id=run_id,
        data_epoch=data_epoch,
        freeze_manifest_sha256=freeze_manifest_sha256,
        source_commit_sha=source_commit_sha,
        authorization_envelope_sha256=authorization_envelope_sha256,
        resource_sample_sha256=resource_sample_sha256,
    )
    if CANDIDATE_PATTERN.fullmatch(candidate_id) is None:
        raise Qas30ObservationError(
            "QAS30_REGULARITY_CANDIDATE_INVALID", "candidateId is invalid"
        )
    if (
        not isinstance(mapped_qcis, str)
        or not mapped_qcis.strip()
        or len(mapped_qcis.encode("utf-8")) > 1_048_576
        or any(
            SHA256_PATTERN.fullmatch(value) is None
            for value in (
                mapping_candidate_sha256,
                k0_config_sha256,
                active_calibration_sha256,
            )
        )
    ):
        raise Qas30ObservationError(
            "QAS30_REGULARITY_BINDING_INVALID", "mapped QCIS binding is invalid"
        )
    timestamp = _timestamp(observed_at)
    url = f"{qas30_tianyan.BASE_URL}{TianYanPlatform.QCIS_CHECK_REGULAR_PATH}"
    body = {"computerCode": TARGET, "qcis": mapped_qcis}
    request_sha256 = _provider_request_sha256(
        method="POST",
        url=url,
        json_body=body,
        governance={
            **governance,
            "candidateId": candidate_id,
            "mappingCandidateSha256": mapping_candidate_sha256,
            "k0ConfigSha256": k0_config_sha256,
            "activeCalibrationSha256": active_calibration_sha256,
        },
    )
    _validate_observation_token(access_token=access_token, action="QCIS_REGULARITY")
    _claim_observation(
        receipt_path=receipt_path,
        action="QCIS_REGULARITY",
        request_sha256=request_sha256,
    )
    response, _transport_unknown = _single_response(
        transport=transport,
        action="QCIS_REGULARITY",
        method="POST",
        url=url,
        access_token=access_token,
        timeout_seconds=timeout_seconds,
        json_body=body,
    )
    payload = response.payload
    observation = {
        "schemaVersion": REGULARITY_OBSERVATION_SCHEMA_VERSION,
        "candidateId": candidate_id,
        "target": TARGET,
        "sourceCommitSha": source_commit_sha,
        "dataEpoch": data_epoch,
        "freezeManifestSha256": freeze_manifest_sha256,
        "mappedQcisSha256": _text_sha256(mapped_qcis),
        "mappingCandidateSha256": mapping_candidate_sha256,
        "k0ConfigSha256": k0_config_sha256,
        "activeCalibrationSha256": active_calibration_sha256,
        "validationMethod": REGULARITY_METHOD,
        "providerRequestSha256": request_sha256,
        "providerResponseSha256": _text_sha256(_canonical_json(payload)),
        "providerHttpStatus": response.status_code,
        "transportRequestCount": 1,
        "regular": (
            response.status_code == 200
            and isinstance(payload, Mapping)
            and payload.get("code") == 0
        ),
        "scientificStatus": qas30_tianyan.transport_scientific_status(transport),
        "observedAt": timestamp,
    }
    observation["regularityObservationSha256"] = _entity_sha256(observation)
    _persist_observation(
        receipt_path=receipt_path,
        action="QCIS_REGULARITY",
        request_sha256=request_sha256,
        response=response,
        observation=observation,
    )
    return observation
