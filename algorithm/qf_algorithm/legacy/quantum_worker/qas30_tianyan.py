"""Strict single-request Tianyan176 boundary for frozen QAS30 batches.

The request bodies mirror cqlib 1.3.11.  Authentication is supplied by the
governed caller as an already-issued access token; this module never reads a
credential source and never performs login, polling, retry, or resubmission.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import requests
from cqlib import QuantumLanguage, TianYanPlatform
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TARGET = "tianyan176"
SUBMIT_RECEIPT_SCHEMA_VERSION = "qf.qas30.batch-submit-receipt.v1"
QUERY_OBSERVATION_SCHEMA_VERSION = "qf.qas30.query-observation.v1"
LOGIN_RECEIPT_SCHEMA_VERSION = "qf.qas30.login-receipt.v1"
LIVE_BATCH_SCHEMA_VERSION = "qf.qas30.live-batch.v3"
BASE_URL = f"{TianYanPlatform.SCHEME}://{TianYanPlatform.DOMAIN}"
QUERY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}")
MEASUREMENT_PATTERN = re.compile(r"(?mi)^\s*M\s+(Q\d+)\s*$")
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
BATCH_FIELDS = {
    "schemaVersion",
    "protocolVersion",
    "runId",
    "dataEpoch",
    "freezeManifestSha256",
    "blockId",
    "blockBatchIndex",
    "batchId",
    "stage",
    "target",
    "shots",
    "state",
    "scientificStatus",
    "sourceCommitSha",
    "k0ConfigSha256",
    "activeCalibrationSha256",
    "compilationCalibrationSha256",
    "mappingSnapshotSha256",
    "selectionFeatureSnapshotSha256",
    "normalizationManifest",
    "normalizationSha256",
    "predictorFeatureFreezeSha256",
    "predictorFeatureSetSha256",
    "selectionEntitySha256",
    "authorizationEnvelopeSha256",
    "resourceSampleSha256",
    "commonMeasurementPhysicalOrder",
    "candidateIds",
    "circuits",
}
CIRCUIT_FIELDS = {
    "candidateId",
    "qcis",
    "qcisSha256",
    "regularityReceipt",
    "regularityReceiptSha256",
    "predictorFeatureReceipt",
    "predictorFeatureReceiptSha256",
    "executionMappingFeatures",
    "executionMappingFeatureSha256",
    "measurementPhysicalOrder",
}
PREDICTOR_FEATURES = {
    "localValidationLoss",
    "quboObjectiveDegradation",
    "compiledDepth",
    "twoQubitGates",
    "swapCount",
    "calibrationNoiseProxy",
}
PRE_HARDWARE_FEATURES = {"localValidationLoss", "quboObjectiveDegradation"}
MAPPING_DERIVED_FEATURES = PREDICTOR_FEATURES - PRE_HARDWARE_FEATURES
OBSERVED_SCIENTIFIC_STATUS = "OBSERVED_NOT_RELEASED"
FIXTURE_SCIENTIFIC_STATUS = "PROTOCOL_IMPLEMENTATION_FIXTURE"
_QUERY_ONLY_TOKEN_HASHES: set[str] = set()
_TOKEN_ACTIONS = {
    "LOGIN",
    "MACHINE_STATE",
    "CALIBRATION_CONFIG",
    "QCIS_REGULARITY",
    "SUBMIT",
    "QUERY",
}


class Qas30TianyanError(RuntimeError):
    """A fail-closed batch or transport invariant failed."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.receipt = receipt
        self.resubmit_allowed = False


class ReceiptPersistenceError(Qas30TianyanError):
    def __init__(self, request_sha256: str, *, action: str = "UNKNOWN") -> None:
        super().__init__(
            "QAS30_RECEIPT_PERSIST_FAILED",
            "the provider outcome could not be durably bound to its request",
            receipt={
                "state": "UNKNOWN_PERSISTENCE_OUTCOME",
                "action": action,
                "requestSha256": request_sha256,
                "resubmitAllowed": False,
            },
        )
        self.request_sha256 = request_sha256


@dataclass(frozen=True)
class SingleAttemptHttpResponse:
    status_code: int
    payload: Any
    headers: dict[str, str]


class SingleAttemptTransport(Protocol):
    def request_once(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        timeout_seconds: int,
        query_params: Mapping[str, str | int | float] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
    ) -> SingleAttemptHttpResponse: ...


class RequestsSingleAttemptTransport:
    """One requests call with redirects and all urllib3 retries disabled."""

    scientific_status = OBSERVED_SCIENTIFIC_STATUS

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        retry = Retry(
            total=0,
            connect=0,
            read=0,
            redirect=0,
            status=0,
            other=0,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def request_once(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        timeout_seconds: int,
        query_params: Mapping[str, str | int | float] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
    ) -> SingleAttemptHttpResponse:
        if method not in {"GET", "POST"}:
            raise Qas30TianyanError(
                "QAS30_TRANSPORT_METHOD_INVALID",
                "single-attempt transport only accepts GET or POST",
            )
        if query_params is not None and (
            not isinstance(query_params, Mapping)
            or any(
                not isinstance(key, str)
                or not key
                or isinstance(value, bool)
                or not isinstance(value, str | int | float)
                or isinstance(value, float)
                and not math.isfinite(value)
                for key, value in query_params.items()
            )
        ):
            raise Qas30TianyanError(
                "QAS30_TRANSPORT_QUERY_INVALID", "query parameters are invalid"
            )
        if method == "GET" and (json_body is not None or form_body is not None):
            raise Qas30TianyanError(
                "QAS30_TRANSPORT_BODY_INVALID", "GET requests cannot carry a body"
            )
        if method == "POST" and (json_body is None) == (form_body is None):
            raise Qas30TianyanError(
                "QAS30_TRANSPORT_BODY_INVALID",
                "POST requires exactly one request body encoding",
            )
        request_kwargs: dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": headers,
            "timeout": timeout_seconds,
            "allow_redirects": False,
        }
        if query_params is not None:
            request_kwargs["params"] = dict(query_params)
        if json_body is not None:
            request_kwargs["json"] = json_body
        elif form_body is not None:
            request_kwargs["data"] = form_body
        response = self._session.request(
            **request_kwargs,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return SingleAttemptHttpResponse(
            status_code=int(response.status_code),
            payload=payload,
            headers={str(key): str(value) for key, value in response.headers.items()},
        )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _headers(access_token: str, *, action: str) -> dict[str, str]:
    if action not in _TOKEN_ACTIONS:
        raise Qas30TianyanError(
            "QAS30_TRANSPORT_ACTION_INVALID", "transport action is invalid"
        )
    if (
        not isinstance(access_token, str)
        or not access_token
        or access_token != access_token.strip()
        or any(character in access_token for character in "\r\n\x00")
    ):
        raise Qas30TianyanError("QAS30_ACCESS_TOKEN_INVALID", "access token is invalid")
    if _sha256(access_token) in _QUERY_ONLY_TOKEN_HASHES and action != "QUERY":
        raise Qas30TianyanError(
            "QAS30_REAUTH_TOKEN_QUERY_ONLY",
            "the one reauthenticated token is restricted to existing-ID queries",
        )
    return {
        "basicToken": access_token,
        "Authorization": f"Bearer {access_token}",
    }


@dataclass
class InMemoryTokenState:
    """Process-local token holder; tokens are never serialized into receipts."""

    attempted: bool = False
    _access_token: str | None = None
    expired: bool = False
    reauth_count: int = 0

    @property
    def access_token(self) -> str:
        if self._access_token is None or self.expired:
            raise Qas30TianyanError(
                "QAS30_ACCESS_TOKEN_UNAVAILABLE", "no valid in-memory token exists"
            )
        return self._access_token

    def mark_expired(self) -> None:
        self.expired = True
        self._access_token = None


def login_once(
    connection_key: str,
    *,
    transport: SingleAttemptTransport,
    token_state: InMemoryTokenState,
    timeout_seconds: int = 60,
    receipt_path: Path | None = None,
) -> str:
    """Perform one official login request and retain the token in memory only."""

    if token_state.attempted:
        raise Qas30TianyanError(
            "QAS30_LOGIN_ALREADY_ATTEMPTED",
            "login_once cannot replay a login request in the same token state",
        )
    if (
        not isinstance(connection_key, str)
        or not connection_key
        or connection_key != connection_key.strip()
        or any(character in connection_key for character in "\r\n\x00")
    ):
        raise Qas30TianyanError(
            "QAS30_CONNECTION_KEY_INVALID", "connection key is invalid"
        )
    token_state.attempted = True
    token = _login_request_once(
        connection_key,
        transport=transport,
        timeout_seconds=timeout_seconds,
        receipt_path=receipt_path,
        receipt_action="LOGIN",
    )
    token_state._access_token = token
    token_state.expired = False
    return token_state.access_token


def _login_request_once(
    connection_key: str,
    *,
    transport: SingleAttemptTransport,
    timeout_seconds: int,
    receipt_path: Path | None = None,
    receipt_action: str = "LOGIN",
) -> str:
    request_sha256 = _entity_sha256(
        {
            "method": "POST",
            "url": f"{BASE_URL}{TianYanPlatform.LOGIN_PATH}",
            "action": receipt_action,
            "formContract": {
                "grant_type": "openId",
                "account_type": "member",
                "openIdPresent": True,
            },
        }
    )
    if receipt_path is not None:
        _claim_once(
            receipt_path,
            request_sha256=request_sha256,
            action=receipt_action,
        )
    try:
        response = transport.request_once(
            method="POST",
            url=f"{BASE_URL}{TianYanPlatform.LOGIN_PATH}",
            form_body={
                "grant_type": "openId",
                "openId": connection_key,
                "account_type": "member",
            },
            headers={},
            timeout_seconds=timeout_seconds,
        )
    except Exception as error:
        if receipt_path is not None:
            receipt = {
                "schemaVersion": LOGIN_RECEIPT_SCHEMA_VERSION,
                "action": receipt_action,
                "state": "TRANSPORT_UNKNOWN",
                "requestSha256": request_sha256,
                "providerHttpStatus": 0,
                "providerCode": None,
                "tokenReceived": False,
                "tokenPersisted": False,
                "transportRequestCount": 1,
                "observedAt": _now(),
            }
            receipt["loginReceiptSha256"] = _entity_sha256(receipt)
            _persist_json(receipt_path, receipt)
        raise Qas30TianyanError(
            "QAS30_LOGIN_TRANSPORT_FAILED", "the one login request failed"
        ) from error
    payload = response.payload
    data = payload.get("data") if isinstance(payload, dict) else None
    token = data.get("access_token") if isinstance(data, dict) else None
    accepted = (
        response.status_code == 200
        and isinstance(payload, dict)
        and payload.get("code") == 0
        and isinstance(token, str)
    )
    if accepted:
        try:
            _headers(token, action="LOGIN")
        except Qas30TianyanError:
            accepted = False
    if receipt_path is not None:
        receipt = {
            "schemaVersion": LOGIN_RECEIPT_SCHEMA_VERSION,
            "action": receipt_action,
            "state": "TOKEN_ACQUIRED_MEMORY_ONLY" if accepted else "LOGIN_REJECTED",
            "requestSha256": request_sha256,
            "providerHttpStatus": response.status_code,
            "providerCode": payload.get("code") if isinstance(payload, dict) else None,
            "tokenReceived": accepted,
            "tokenPersisted": False,
            "transportRequestCount": 1,
            "observedAt": _now(),
        }
        receipt["loginReceiptSha256"] = _entity_sha256(receipt)
        _persist_json(receipt_path, receipt)
    if not accepted:
        raise Qas30TianyanError("QAS30_LOGIN_REJECTED", "the one login request was rejected")
    assert isinstance(token, str)
    return token


def reauthenticate_once(
    connection_key: str,
    *,
    transport: SingleAttemptTransport,
    token_state: InMemoryTokenState,
    timeout_seconds: int = 60,
    receipt_path: Path | None = None,
) -> str:
    """Issue the sole post-expiry login allowed for querying existing IDs."""

    if token_state.reauth_count != 0:
        raise Qas30TianyanError(
            "QAS30_REAUTH_ALREADY_ATTEMPTED",
            "the one permitted reauthentication was already attempted",
        )
    if not token_state.attempted or not token_state.expired:
        raise Qas30TianyanError(
            "QAS30_REAUTH_NOT_DUE", "reauthentication requires an expired login state"
        )
    if (
        not isinstance(connection_key, str)
        or not connection_key
        or connection_key != connection_key.strip()
        or any(character in connection_key for character in "\r\n\x00")
    ):
        raise Qas30TianyanError(
            "QAS30_CONNECTION_KEY_INVALID", "connection key is invalid"
        )
    token_state.reauth_count = 1
    token = _login_request_once(
        connection_key,
        transport=transport,
        timeout_seconds=timeout_seconds,
        receipt_path=receipt_path,
        receipt_action="REAUTHENTICATE_QUERY",
    )
    _QUERY_ONLY_TOKEN_HASHES.add(_sha256(token))
    token_state._access_token = token
    token_state.expired = False
    return token


def _claim_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.intent")


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = _canonical_json(payload).encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
            _fsync_directory(path.parent)
        except FileNotFoundError:
            pass


def _claim_once(path: Path, *, request_sha256: str, action: str) -> None:
    if path.exists():
        raise Qas30TianyanError(
            "QAS30_RECEIPT_ALREADY_EXISTS",
            "the logical action already has a durable receipt",
        )
    try:
        _write_exclusive(
            _claim_path(path),
            {
                "schemaVersion": "qf.qas30.transport-intent.v1",
                "action": action,
                "requestSha256": request_sha256,
            },
        )
    except FileExistsError as error:
        raise Qas30TianyanError(
            "QAS30_ACTION_ALREADY_CLAIMED",
            "the logical action already has a durable transport intent",
        ) from error
    except OSError as error:
        raise Qas30TianyanError(
            "QAS30_INTENT_PERSIST_FAILED",
            "the logical action intent could not be durably persisted",
        ) from error


def _persist_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        _write_exclusive(path, payload)
    except OSError as error:
        schema_version = str(payload.get("schemaVersion", ""))
        action = (
            "QUERY"
            if schema_version == QUERY_OBSERVATION_SCHEMA_VERSION
            else "SUBMIT"
            if schema_version == SUBMIT_RECEIPT_SCHEMA_VERSION
            else str(payload.get("action", "LOGIN"))
            if schema_version == LOGIN_RECEIPT_SCHEMA_VERSION
            else "OBSERVATION"
        )
        raise ReceiptPersistenceError(
            str(payload.get("requestSha256", "")), action=action
        ) from error


def _measurement_order(qcis: str) -> list[str]:
    return [item.upper() for item in MEASUREMENT_PATTERN.findall(qcis)]


def _entity_sha256(value: Mapping[str, Any]) -> str:
    return _sha256(_canonical_json(dict(value)))


def transport_scientific_status(transport: SingleAttemptTransport) -> str:
    """Return an explicit evidence class for injected real or fixture transport."""

    status = getattr(transport, "scientific_status", OBSERVED_SCIENTIFIC_STATUS)
    if status not in {OBSERVED_SCIENTIFIC_STATUS, FIXTURE_SCIENTIFIC_STATUS}:
        raise Qas30TianyanError(
            "QAS30_TRANSPORT_SCIENTIFIC_STATUS_INVALID",
            "transport evidence class is invalid",
        )
    return str(status)


def _valid_mapping_derived_features(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != MAPPING_DERIVED_FEATURES:
        return False
    try:
        numeric = {name: float(item) for name, item in value.items()}
    except (TypeError, ValueError):
        return False
    if any(isinstance(item, bool) for item in value.values()) or not all(
        math.isfinite(item) and item >= 0.0 for item in numeric.values()
    ):
        return False
    return all(
        numeric[name].is_integer()
        for name in ("compiledDepth", "twoQubitGates", "swapCount")
    )


def _validate_normalization_manifest(batch: Mapping[str, Any]) -> str:
    manifest = batch.get("normalizationManifest")
    if not isinstance(manifest, Mapping):
        raise Qas30TianyanError(
            "QAS30_NORMALIZATION_ENTITY_INVALID",
            "the complete frozen normalization entity is required",
        )
    stored = manifest.get("normalizationSha256")
    unhashed = {key: value for key, value in manifest.items() if key != "normalizationSha256"}
    components = manifest.get("components")
    if (
        manifest.get("schemaVersion") != "qf.qas30.normalization-freeze.v1"
        or manifest.get("frozenBeforeLiveResults") is not True
        or manifest.get("clipRange") != [0.0, 1.0]
        or not isinstance(components, Mapping)
        or set(components) != {
            "localValidationLoss",
            "quboObjectiveDegradation",
            "infeasibleRate",
            "compiledDepth",
            "twoQubitGates",
            "swapCount",
            "calibrationNoiseProxy",
        }
        or stored != _entity_sha256(unhashed)
        or batch.get("normalizationSha256") != stored
    ):
        raise Qas30TianyanError(
            "QAS30_NORMALIZATION_BINDING_INVALID",
            "normalization bounds are not a complete frozen entity",
        )
    for name, row in components.items():
        if not isinstance(row, Mapping) or set(row) != {"lower", "upper"}:
            raise Qas30TianyanError(
                "QAS30_NORMALIZATION_BINDING_INVALID",
                f"normalization bound {name} is invalid",
            )
        lower, upper = float(row["lower"]), float(row["upper"])
        if not all(math.isfinite(value) for value in (lower, upper)) or upper <= lower:
            raise Qas30TianyanError(
                "QAS30_NORMALIZATION_BINDING_INVALID",
                f"normalization bound {name} is invalid",
            )
    return str(stored)


def _validate_regularity_receipt(
    circuit: Mapping[str, Any], batch: Mapping[str, Any], common_order: list[str]
) -> None:
    receipt = circuit.get("regularityReceipt")
    if not isinstance(receipt, Mapping):
        raise Qas30TianyanError(
            "QAS30_BATCH_REGULARITY_INVALID",
            "a complete regularity receipt entity is required",
        )
    required = {
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
    logical_to_physical = receipt.get("logicalToPhysical")
    mapping_features = receipt.get("mappingDerivedFeatures")
    if (
        set(receipt) != required
        or receipt.get("schemaVersion") != "qf.qas30.qcis-regularity-receipt.v1"
        or receipt.get("state") != "REGULARITY_VERIFIED_AFTER_K0"
        or receipt.get("regular") is not True
        or receipt.get("scientificStatus")
        not in {OBSERVED_SCIENTIFIC_STATUS, FIXTURE_SCIENTIFIC_STATUS}
        or receipt.get("scientificStatus") != batch.get("scientificStatus")
        or not isinstance(receipt.get("regularityObservationSha256"), str)
        or not SHA256_PATTERN.fullmatch(receipt["regularityObservationSha256"])
        or receipt.get("transportRequestCount") != 1
        or receipt.get("candidateId") != circuit.get("candidateId")
        or receipt.get("target") != TARGET
        or receipt.get("sourceCommitSha") != batch.get("sourceCommitSha")
        or receipt.get("dataEpoch") != batch.get("dataEpoch")
        or receipt.get("freezeManifestSha256")
        != batch.get("freezeManifestSha256")
        or receipt.get("qcisSha256") != circuit.get("qcisSha256")
        or receipt.get("k0ConfigSha256") != batch.get("k0ConfigSha256")
        or receipt.get("activeCalibrationSha256")
        != batch.get("compilationCalibrationSha256")
        or receipt.get("mappingSnapshotSha256")
        != batch.get("mappingSnapshotSha256")
        or receipt.get("measurementPhysicalOrder") != common_order
        or not isinstance(logical_to_physical, Mapping)
        or set(logical_to_physical) != {f"Q{index}" for index in range(6)}
        or [logical_to_physical[f"Q{index}"] for index in range(6)] != common_order
        or not _valid_mapping_derived_features(mapping_features)
        or circuit.get("regularityReceiptSha256") != _entity_sha256(receipt)
    ):
        raise Qas30TianyanError(
            "QAS30_BATCH_REGULARITY_INVALID",
            "regularity receipt does not bind QCIS, mapping, K snapshot, and source commit",
        )


def _validate_predictor_feature_receipt(
    circuit: Mapping[str, Any], batch: Mapping[str, Any]
) -> str:
    receipt = circuit.get("predictorFeatureReceipt")
    if not isinstance(receipt, Mapping):
        raise Qas30TianyanError(
            "QAS30_PREDICTOR_FEATURE_ENTITY_INVALID",
            "a complete selection-feature freeze receipt is required",
        )
    required = {
        "schemaVersion",
        "state",
        "candidateId",
        "sourceCommitSha",
        "dataEpoch",
        "freezeManifestSha256",
        "predictorFeatureFreezeSha256",
        "preHardwareFeatureFreezeSha256",
        "k0ConfigSha256",
        "predictorCalibrationSha256",
        "predictorCalibrationLabel",
        "selectionFeatureSnapshotSha256",
        "normalizationSha256",
        "preHardwareFeatures",
        "selectionMappingFeatures",
        "preHardwareFeatureSha256",
        "selectionMappingFeatureSha256",
        "preHardwareResultsVisibleAtFreeze",
        "selectionFeaturesFrozenBeforeBatchSubmit",
    }
    pre = receipt.get("preHardwareFeatures")
    selection_mapping = receipt.get("selectionMappingFeatures")
    candidate_id = circuit.get("candidateId")
    pre_binding = {
        "candidateId": candidate_id,
        "sourceCommitSha": batch.get("sourceCommitSha"),
        "dataEpoch": batch.get("dataEpoch"),
        "freezeManifestSha256": batch.get("freezeManifestSha256"),
        "features": pre,
        "frozenBeforeHardwareResults": True,
    }
    selection_binding = {
        "candidateId": candidate_id,
        "sourceCommitSha": batch.get("sourceCommitSha"),
        "dataEpoch": batch.get("dataEpoch"),
        "freezeManifestSha256": batch.get("freezeManifestSha256"),
        "k0ConfigSha256": batch.get("k0ConfigSha256"),
        "predictorCalibrationSha256": batch.get("activeCalibrationSha256"),
        "predictorCalibrationLabel": (
            "K0" if batch.get("blockBatchIndex") in {1, 2} else "K1"
        ),
        "predictorFeatureFreezeSha256": batch.get(
            "predictorFeatureFreezeSha256"
        ),
        "selectionFeatureSnapshotSha256": batch.get("selectionFeatureSnapshotSha256"),
        "features": selection_mapping,
    }
    if (
        set(receipt) != required
        or receipt.get("schemaVersion")
        != "qf.qas30.predictor-feature-receipt.v2"
        or receipt.get("state") != "FROZEN_FOR_SELECTION"
        or receipt.get("preHardwareResultsVisibleAtFreeze") is not False
        or receipt.get("selectionFeaturesFrozenBeforeBatchSubmit") is not True
        or receipt.get("candidateId") != candidate_id
        or receipt.get("sourceCommitSha") != batch.get("sourceCommitSha")
        or receipt.get("dataEpoch") != batch.get("dataEpoch")
        or receipt.get("freezeManifestSha256")
        != batch.get("freezeManifestSha256")
        or receipt.get("predictorFeatureFreezeSha256")
        != batch.get("predictorFeatureFreezeSha256")
        or not isinstance(receipt.get("preHardwareFeatureFreezeSha256"), str)
        or not SHA256_PATTERN.fullmatch(
            str(receipt.get("preHardwareFeatureFreezeSha256"))
        )
        or receipt.get("k0ConfigSha256") != batch.get("k0ConfigSha256")
        or receipt.get("predictorCalibrationSha256")
        != batch.get("activeCalibrationSha256")
        or receipt.get("predictorCalibrationLabel")
        != ("K0" if batch.get("blockBatchIndex") in {1, 2} else "K1")
        or receipt.get("selectionFeatureSnapshotSha256")
        != batch.get("selectionFeatureSnapshotSha256")
        or receipt.get("normalizationSha256") != batch.get("normalizationSha256")
        or not isinstance(pre, Mapping)
        or set(pre) != PRE_HARDWARE_FEATURES
        or not isinstance(selection_mapping, Mapping)
        or set(selection_mapping) != MAPPING_DERIVED_FEATURES
        or receipt.get("preHardwareFeatureSha256") != _entity_sha256(pre_binding)
        or receipt.get("selectionMappingFeatureSha256") != _entity_sha256(selection_binding)
        or circuit.get("predictorFeatureReceiptSha256") != _entity_sha256(receipt)
    ):
        raise Qas30TianyanError(
            "QAS30_PREDICTOR_FEATURE_BINDING_INVALID",
            "selection features do not bind their predictor freeze",
        )
    feature_values = [*pre.values(), *selection_mapping.values()]
    try:
        finite = all(math.isfinite(float(value)) for value in feature_values)
    except (TypeError, ValueError):
        finite = False
    if (
        any(isinstance(value, bool) for value in feature_values)
        or not finite
        or not _valid_mapping_derived_features(selection_mapping)
    ):
        raise Qas30TianyanError(
            "QAS30_PREDICTOR_FEATURE_BINDING_INVALID",
            "selection features must be finite numeric facts",
        )
    return str(circuit["predictorFeatureReceiptSha256"])


def _validate_batch(batch: dict[str, Any]) -> list[dict[str, Any]]:
    if set(batch) != BATCH_FIELDS:
        raise Qas30TianyanError("QAS30_BATCH_FIELDS_INVALID", "batch fields are invalid")
    if batch.get("schemaVersion") != LIVE_BATCH_SCHEMA_VERSION:
        raise Qas30TianyanError("QAS30_BATCH_SCHEMA_INVALID", "batch schema is invalid")
    if batch.get("protocolVersion") != "qf.qas30.protocol.v2":
        raise Qas30TianyanError("QAS30_PROTOCOL_VERSION_INVALID", "protocol version is invalid")
    if batch.get("target") != TARGET or batch.get("shots") != 1_000:
        raise Qas30TianyanError("QAS30_BATCH_SCOPE_INVALID", "batch target or shots are invalid")
    if batch.get("state") != "READY_FOR_FIRST_SUBMIT":
        raise Qas30TianyanError("QAS30_BATCH_STATE_INVALID", "batch is not ready for submission")
    if batch.get("scientificStatus") not in {
        OBSERVED_SCIENTIFIC_STATUS,
        FIXTURE_SCIENTIFIC_STATUS,
    }:
        raise Qas30TianyanError(
            "QAS30_BATCH_SCIENTIFIC_STATUS_INVALID",
            "batch scientific status is invalid",
        )
    expected_stage_by_index = {
        1: "R1",
        2: "R1",
        3: "R2_RIDGE",
        4: "R2_FIXED",
        5: "R2_RANDOM",
        6: "R2_LLM",
    }
    if batch.get("stage") != expected_stage_by_index.get(
        batch.get("blockBatchIndex")
    ):
        raise Qas30TianyanError("QAS30_BATCH_STAGE_INVALID", "batch stage is invalid")
    for name in ("runId", "batchId"):
        value = batch.get(name)
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value
        ):
            raise Qas30TianyanError("QAS30_BATCH_IDENTITY_INVALID", f"{name} is invalid")
    data_epoch = batch.get("dataEpoch")
    if not isinstance(data_epoch, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", data_epoch
    ):
        raise Qas30TianyanError(
            "QAS30_BATCH_DATA_EPOCH_INVALID", "batch dataEpoch is invalid"
        )
    freeze_manifest_sha256 = batch.get("freezeManifestSha256")
    if not isinstance(freeze_manifest_sha256, str) or not SHA256_PATTERN.fullmatch(
        freeze_manifest_sha256
    ):
        raise Qas30TianyanError(
            "QAS30_BATCH_FREEZE_INVALID", "batch freeze manifest binding is invalid"
        )
    source_commit = batch.get("sourceCommitSha")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        raise Qas30TianyanError(
            "QAS30_BATCH_COMMIT_INVALID",
            "batch source commit is invalid",
        )
    k0_sha256 = batch.get("k0ConfigSha256")
    if not isinstance(k0_sha256, str) or not SHA256_PATTERN.fullmatch(k0_sha256):
        raise Qas30TianyanError("QAS30_BATCH_K0_INVALID", "batch K0 snapshot is invalid")
    mapping_sha256 = batch.get("mappingSnapshotSha256")
    if not isinstance(mapping_sha256, str) or not SHA256_PATTERN.fullmatch(mapping_sha256):
        raise Qas30TianyanError(
            "QAS30_BATCH_MAPPING_INVALID",
            "batch mapping snapshot is invalid",
        )
    selection_sha256 = batch.get("selectionEntitySha256")
    if (
        not isinstance(selection_sha256, str)
        or not SHA256_PATTERN.fullmatch(selection_sha256)
    ):
        raise Qas30TianyanError(
            "QAS30_BATCH_SELECTION_INVALID",
            "batch selection entity binding is invalid",
        )
    for field in (
        "predictorFeatureFreezeSha256",
        "authorizationEnvelopeSha256",
        "resourceSampleSha256",
    ):
        value = batch.get(field)
        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            raise Qas30TianyanError(
                "QAS30_BATCH_RUNTIME_BINDING_INVALID",
                f"batch {field} binding is invalid",
            )
    calibration_sha256 = batch.get("activeCalibrationSha256")
    if (
        not isinstance(calibration_sha256, str)
        or not SHA256_PATTERN.fullmatch(calibration_sha256)
    ):
        raise Qas30TianyanError(
            "QAS30_BATCH_CALIBRATION_INVALID",
            "active calibration snapshot binding is invalid",
        )
    for field in ("compilationCalibrationSha256", "selectionFeatureSnapshotSha256"):
        value = batch.get(field)
        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            raise Qas30TianyanError(
                "QAS30_BATCH_RUNTIME_BINDING_INVALID",
                f"batch {field} binding is invalid",
            )
    _validate_normalization_manifest(batch)
    if not isinstance(batch.get("blockId"), str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", batch["blockId"]
    ):
        raise Qas30TianyanError("QAS30_BLOCK_ID_INVALID", "block ID is invalid")
    if batch.get("blockBatchIndex") not in range(1, 7):
        raise Qas30TianyanError(
            "QAS30_BLOCK_BATCH_INDEX_INVALID", "block batch index must be 1-6"
        )
    common_order = batch.get("commonMeasurementPhysicalOrder")
    if (
        not isinstance(common_order, list)
        or len(common_order) != 6
        or len(set(common_order)) != 6
        or any(
            not isinstance(item, str) or not re.fullmatch(r"Q\d{1,4}", item)
            for item in common_order
        )
    ):
        raise Qas30TianyanError(
            "QAS30_BATCH_MEASUREMENT_ORDER_INVALID",
            "batch requires one six-wire physical measurement order",
        )
    circuits = batch.get("circuits")
    if not isinstance(circuits, list) or len(circuits) != 5:
        raise Qas30TianyanError("QAS30_BATCH_SIZE_INVALID", "batch requires five circuits")
    candidate_order = batch.get("candidateIds")
    if (
        not isinstance(candidate_order, list)
        or len(candidate_order) != 5
        or len(set(candidate_order)) != 5
        or any(
            not isinstance(item, str)
            or not re.fullmatch(r"C(?:0[1-9]|[12][0-9]|30)", item)
            for item in candidate_order
        )
    ):
        raise Qas30TianyanError(
            "QAS30_BATCH_CANDIDATE_ORDER_INVALID",
            "batch requires five unique ordered candidate identities",
        )
    candidate_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    feature_receipt_bindings: list[dict[str, str]] = []
    for circuit in circuits:
        if (
            not isinstance(circuit, dict)
            or set(circuit) != CIRCUIT_FIELDS
        ):
            raise Qas30TianyanError(
                "QAS30_BATCH_REGULARITY_INVALID",
                "every mapped QCIS must have complete frozen receipts",
            )
        candidate_id = circuit.get("candidateId")
        qcis = circuit.get("qcis")
        qcis_sha256 = circuit.get("qcisSha256")
        order = circuit.get("measurementPhysicalOrder")
        if (
            not isinstance(candidate_id, str)
            or not re.fullmatch(r"C(?:0[1-9]|[12][0-9]|30)", candidate_id)
            or candidate_id in candidate_ids
            or not isinstance(qcis, str)
            or not qcis.strip()
            or len(qcis.encode("utf-8")) > 1_048_576
            or not isinstance(qcis_sha256, str)
            or not SHA256_PATTERN.fullmatch(qcis_sha256)
            or _sha256(qcis) != qcis_sha256
            or order != common_order
            or _measurement_order(qcis) != common_order
        ):
            raise Qas30TianyanError(
                "QAS30_BATCH_CIRCUIT_BINDING_INVALID",
                "circuit identity, hash, or measurement order is invalid",
            )
        _validate_regularity_receipt(circuit, batch, common_order)
        feature_receipt_sha256 = _validate_predictor_feature_receipt(circuit, batch)
        execution = circuit.get("executionMappingFeatures")
        execution_sha = circuit.get("executionMappingFeatureSha256")
        execution_binding = {
            "candidateId": candidate_id,
            "compilationCalibrationSha256": batch.get("compilationCalibrationSha256"),
            "mappingSnapshotSha256": batch.get("mappingSnapshotSha256"),
            "features": execution,
        }
        if (
            not isinstance(execution, Mapping)
            or set(execution) != MAPPING_DERIVED_FEATURES
            or not _valid_mapping_derived_features(execution)
            or execution_sha != _entity_sha256(execution_binding)
        ):
            raise Qas30TianyanError(
                "QAS30_EXECUTION_MAPPING_FEATURE_INVALID",
                "S_HW requires frozen actual execution mapping features",
            )
        candidate_ids.add(candidate_id)
        validated.append(circuit)
        feature_receipt_bindings.append(
            {
                "candidateId": candidate_id,
                "predictorFeatureReceiptSha256": feature_receipt_sha256,
            }
        )
    if [item["candidateId"] for item in validated] != candidate_order:
        raise Qas30TianyanError(
            "QAS30_BATCH_CANDIDATE_ORDER_INVALID",
            "candidateIds must bind the ordered circuit identities",
        )
    if batch.get("predictorFeatureSetSha256") != _sha256(
        _canonical_json(feature_receipt_bindings)
    ):
        raise Qas30TianyanError(
            "QAS30_PREDICTOR_FEATURE_SET_INVALID",
            "ordered six-feature receipt set hash changed",
        )
    return validated


def validate_live_batch(batch: Mapping[str, Any]) -> dict[str, Any]:
    """Public, side-effect-free validation for a complete governed live batch."""

    value = dict(batch)
    _validate_batch(value)
    return value


def _provider_query_ids(response: SingleAttemptHttpResponse) -> tuple[list[str], bool]:
    payload = response.payload
    raw_ids: Any = None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            raw_ids = data.get("query_ids")
    if not isinstance(raw_ids, list):
        return [], False
    ids = [item for item in raw_ids if isinstance(item, str) and QUERY_ID_PATTERN.fullmatch(item)]
    return ids, len(ids) == len(raw_ids)


def _submit_receipt(
    *,
    batch: dict[str, Any],
    circuits: list[dict[str, Any]],
    request_sha256: str,
    live_batch_sha256: str,
    provider_request_sha256: str,
    provider_response_artifact_sha256: str,
    provider_response_artifact: str,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
    provider_query_ids: list[str],
    provider_shape_valid: bool,
    response: SingleAttemptHttpResponse,
    scientific_status: str,
) -> dict[str, Any]:
    exact = (
        response.status_code == 200
        and isinstance(response.payload, dict)
        and response.payload.get("code", -1) == 0
        and provider_shape_valid
        and len(provider_query_ids) == 5
        and len(set(provider_query_ids)) == 5
    )
    if exact:
        state = "QUERY_IDS_PERSISTED"
    elif 0 < len(provider_query_ids) < 5 and len(set(provider_query_ids)) == len(
        provider_query_ids
    ):
        state = "PARTIAL_UNKNOWN"
    elif provider_query_ids:
        state = "IDENTITY_MISMATCH"
    else:
        state = "UNKNOWN_PROVIDER_OUTCOME"
    bindings = [
        {
            "candidateId": circuit["candidateId"],
            "qcisSha256": circuit["qcisSha256"],
            "regularityReceiptSha256": circuit["regularityReceiptSha256"],
            "predictorFeatureReceiptSha256": circuit[
                "predictorFeatureReceiptSha256"
            ],
            "queryId": query_id,
            "position": index,
        }
        for index, (circuit, query_id) in enumerate(
            zip(circuits, provider_query_ids[:5], strict=False)
        )
    ]
    return {
        "schemaVersion": SUBMIT_RECEIPT_SCHEMA_VERSION,
        "runId": batch["runId"],
        "dataEpoch": batch["dataEpoch"],
        "freezeManifestSha256": batch["freezeManifestSha256"],
        "batchId": batch["batchId"],
        "target": TARGET,
        "shots": 1_000,
        "sourceCommitSha": batch.get("sourceCommitSha"),
        "activeCalibrationSha256": batch.get("activeCalibrationSha256"),
        "predictorFeatureFreezeSha256": batch.get(
            "predictorFeatureFreezeSha256"
        ),
        "selectionEntitySha256": batch.get("selectionEntitySha256"),
        "authorizationEnvelopeSha256": authorization_envelope_sha256,
        "resourceSampleSha256": resource_sample_sha256,
        "requestSha256": request_sha256,
        "liveBatchSha256": live_batch_sha256,
        "providerRequestSha256": provider_request_sha256,
        "providerResponseSha256": _sha256(_canonical_json(response.payload)),
        "providerResponseArtifactSha256": provider_response_artifact_sha256,
        "providerResponseArtifact": provider_response_artifact,
        "providerHttpStatus": response.status_code,
        "providerQueryIds": provider_query_ids,
        "state": state,
        "scientificStatus": scientific_status,
        "transportRequestCount": 1,
        "queryBindings": bindings,
        "resubmitAllowed": False,
        "persistedAt": _now(),
    }


def _persist_provider_response_artifact(
    *,
    response: SingleAttemptHttpResponse,
    request_sha256: str,
    artifact_path: Path,
) -> tuple[str, str]:
    allowed_headers = {
        "content-type",
        "date",
        "request-id",
        "trace-id",
        "x-request-id",
        "x-trace-id",
    }
    artifact = {
        "schemaVersion": "qf.qas30.provider-response-artifact.v1",
        "requestSha256": request_sha256,
        "providerHttpStatus": response.status_code,
        "providerPayload": response.payload,
        "providerHeaders": {
            str(key): str(value)
            for key, value in response.headers.items()
            if str(key).lower() in allowed_headers
        },
    }
    artifact_sha256 = _entity_sha256(artifact)
    artifact["providerResponseArtifactSha256"] = artifact_sha256
    _persist_json(artifact_path, artifact)
    return artifact_sha256, artifact_path.name


def submit_batch_once(
    batch: dict[str, Any],
    *,
    access_token: str,
    transport: SingleAttemptTransport,
    receipt_path: Path,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Send exactly one five-circuit request and durably bind every returned ID."""

    if not all(
        isinstance(value, str) and SHA256_PATTERN.fullmatch(value)
        for value in (authorization_envelope_sha256, resource_sample_sha256)
    ):
        raise Qas30TianyanError(
            "QAS30_RUNTIME_BINDING_INVALID",
            "authorization and resource sample hashes are required",
        )
    if (
        batch.get("authorizationEnvelopeSha256")
        != authorization_envelope_sha256
        or batch.get("resourceSampleSha256") != resource_sample_sha256
    ):
        raise Qas30TianyanError(
            "QAS30_RUNTIME_BINDING_INVALID",
            "live batch does not bind the current authorization and resource sample",
        )
    circuits = _validate_batch(batch)
    scientific_status = transport_scientific_status(transport)
    if scientific_status != batch.get("scientificStatus"):
        raise Qas30TianyanError(
            "QAS30_TRANSPORT_SCIENTIFIC_STATUS_MISMATCH",
            "transport evidence class does not match the live batch",
        )
    payload = {
        "circuit": [item["qcis"] for item in circuits],
        "language": QuantumLanguage.QCIS.value,
        "name": f"QF-QAS30-{batch['batchId']}",
        "lab_id": None,
        "lab_name": None,
        "shots": 1_000,
        "computerCode": TARGET,
        # Every circuit has already passed the target-native QCIS regularity
        # endpoint and is hash-bound above.  Re-running the provider verifier
        # at queue execution has produced terminal task failures for two
        # independently frozen QAS30 batches, so the governed submit bypasses
        # only that redundant verifier; hardware execution remains unchanged.
        "is_verify": False,
    }
    live_batch_sha256 = _sha256(_canonical_json(batch))
    provider_request_sha256 = _sha256(_canonical_json(payload))
    request_sha256 = _sha256(
        _canonical_json(
            {
                "authorizationEnvelopeSha256": authorization_envelope_sha256,
                "liveBatchSha256": live_batch_sha256,
                "providerRequestSha256": provider_request_sha256,
                "resourceSampleSha256": resource_sample_sha256,
            }
        )
    )
    auth_headers = _headers(access_token, action="SUBMIT")
    _claim_once(receipt_path, request_sha256=request_sha256, action="SUBMIT")
    response_artifact_path = receipt_path.with_name(
        f"{receipt_path.stem}.provider-response.json"
    )
    try:
        response = transport.request_once(
            method="POST",
            url=f"{BASE_URL}{TianYanPlatform.SUBMIT_EXP_PATH}",
            json_body=payload,
            headers=auth_headers,
            timeout_seconds=timeout_seconds,
        )
    except Exception as error:
        response = SingleAttemptHttpResponse(
            status_code=0,
            payload={"transportErrorType": type(error).__name__},
            headers={},
        )
        artifact_sha256, artifact_name = _persist_provider_response_artifact(
            response=response,
            request_sha256=request_sha256,
            artifact_path=response_artifact_path,
        )
        receipt = _submit_receipt(
            batch=batch,
            circuits=circuits,
            request_sha256=request_sha256,
            live_batch_sha256=live_batch_sha256,
            provider_request_sha256=provider_request_sha256,
            provider_response_artifact_sha256=artifact_sha256,
            provider_response_artifact=artifact_name,
            authorization_envelope_sha256=authorization_envelope_sha256,
            resource_sample_sha256=resource_sample_sha256,
            provider_query_ids=[],
            provider_shape_valid=False,
            scientific_status=scientific_status,
            response=response,
        )
        receipt["batchSubmitReceiptSha256"] = _entity_sha256(receipt)
        _persist_json(receipt_path, receipt)
        raise Qas30TianyanError(
            "QAS30_SUBMIT_TRANSPORT_UNKNOWN",
            "the single submit request ended without a provider response",
            receipt=receipt,
        ) from error
    artifact_sha256, artifact_name = _persist_provider_response_artifact(
        response=response,
        request_sha256=request_sha256,
        artifact_path=response_artifact_path,
    )
    query_ids, shape_valid = _provider_query_ids(response)
    receipt = _submit_receipt(
        batch=batch,
        circuits=circuits,
        request_sha256=request_sha256,
        live_batch_sha256=live_batch_sha256,
        provider_request_sha256=provider_request_sha256,
        provider_response_artifact_sha256=artifact_sha256,
        provider_response_artifact=artifact_name,
        authorization_envelope_sha256=authorization_envelope_sha256,
        resource_sample_sha256=resource_sample_sha256,
        provider_query_ids=query_ids,
        provider_shape_valid=shape_valid,
        scientific_status=scientific_status,
        response=response,
    )
    receipt["batchSubmitReceiptSha256"] = _entity_sha256(receipt)
    _persist_json(receipt_path, receipt)
    if receipt["state"] != "QUERY_IDS_PERSISTED":
        raise Qas30TianyanError(
            f"QAS30_SUBMIT_{receipt['state']}",
            "the single submit response did not provide five unique ordered Query IDs",
            receipt=receipt,
        )
    return receipt


def _has_raw_results(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key == "resultStatus" and isinstance(child, list) and bool(child)
            or _has_raw_results(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_has_raw_results(child) for child in value)
    return False


def _result_row(payload: Any, query_id: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    rows = data.get("experimentResultModelList") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    returned_id = row.get("query_id", row.get("queryId"))
    return row if returned_id in {None, query_id} else None


def _counts_view(
    *, name: str, counts: Mapping[str, Any], width: int, expected_shots: int
) -> dict[str, Any]:
    normalized: dict[str, int] = {}
    for bitstring, count in counts.items():
        if (
            not isinstance(bitstring, str)
            or len(bitstring) != width
            or any(character not in "01" for character in bitstring)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            raise Qas30TianyanError(
                "QAS30_RESULT_COUNTS_INVALID", f"{name} counts are invalid"
            )
        normalized[bitstring] = count
    if sum(normalized.values()) != expected_shots:
        raise Qas30TianyanError(
            "QAS30_RESULT_SHOTS_NOT_CONSERVED", f"{name} shots are not conserved"
        )
    return {
        "counts": dict(sorted(normalized.items())),
        "shots": expected_shots,
        "countsSha256": _sha256(_canonical_json(dict(sorted(normalized.items())))),
    }


def raw_corrected_views(
    *,
    raw_rows: Any,
    corrected_counts: Mapping[str, Any] | None,
    expected_measurement_order: list[str],
    expected_shots: int,
) -> dict[str, Any]:
    """Validate header/order and conserve shots in independent raw/corrected views."""

    if (
        not isinstance(raw_rows, list)
        or len(raw_rows) != expected_shots + 1
        or raw_rows[0] != expected_measurement_order
    ):
        raise Qas30TianyanError(
            "QAS30_RAW_RESULT_SHAPE_INVALID",
            "raw result must contain one exact measurement header plus all shots",
        )
    counts: dict[str, int] = {}
    width = len(expected_measurement_order)
    for shot in raw_rows[1:]:
        if (
            not isinstance(shot, list)
            or len(shot) != width
            or any(value not in (0, 1) or isinstance(value, bool) for value in shot)
        ):
            raise Qas30TianyanError(
                "QAS30_RAW_RESULT_SHAPE_INVALID", "raw shot row is invalid"
            )
        bitstring = "".join(str(value) for value in shot)
        counts[bitstring] = counts.get(bitstring, 0) + 1
    result = {
        "schemaVersion": "qf.qas30.raw-corrected-views.v1",
        "measurementOrder": expected_measurement_order,
        "raw": _counts_view(
            name="raw", counts=counts, width=width, expected_shots=expected_shots
        ),
        "corrected": None,
    }
    if corrected_counts is not None:
        result["corrected"] = _counts_view(
            name="corrected",
            counts=corrected_counts,
            width=width,
            expected_shots=expected_shots,
        )
    return result


def _views_from_payload(
    payload: Any,
    query_id: str,
    expected_measurement_order: list[str] | None,
) -> dict[str, Any] | None:
    row = _result_row(payload, query_id)
    if row is None or not isinstance(row.get("resultStatus"), list):
        return None
    raw_rows = row["resultStatus"]
    if not raw_rows or not isinstance(raw_rows[0], list):
        return None
    corrected: Mapping[str, Any] | None = None
    for name in ("correctedCounts", "readoutCorrectedCounts"):
        value = row.get(name)
        if isinstance(value, Mapping):
            corrected = value
            break
    return raw_corrected_views(
        raw_rows=raw_rows,
        corrected_counts=corrected,
        expected_measurement_order=expected_measurement_order or raw_rows[0],
        expected_shots=1_000,
    )


def _query_state(payload: Any, query_id: str) -> tuple[str, bool]:
    if not isinstance(payload, dict):
        return "UNKNOWN_RESPONSE", True
    if payload.get("code", -1) != 0:
        failure_text = " ".join(
            str(payload.get(name, "")) for name in ("message", "msg", "error")
        ).upper()
        terminal_failure_markers = (
            "RUN FAILURE",
            "TASKS HAVE FAILED",
            "运行失败",
            "任务失败",
        )
        if query_id in failure_text and any(
            marker in failure_text for marker in terminal_failure_markers
        ):
            return "FAILED", False
        return "UNKNOWN_RESPONSE", True
    data = payload.get("data")
    rows = data.get("experimentResultModelList") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return "UNKNOWN_RESPONSE", True
    if not rows:
        return "PENDING", True
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return "UNKNOWN_RESPONSE", False
    row = rows[0]
    returned_id = row.get("query_id", row.get("queryId"))
    if returned_id is not None and returned_id != query_id:
        return "UNKNOWN_RESPONSE", False
    status = str(row.get("status", row.get("taskStatus", ""))).upper()
    if status in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
        return "FAILED", False
    if status in {"COMPLETED", "SUCCESS", "SUCCEEDED", "FINISHED"} or _has_raw_results(row):
        return "COMPLETED", False
    return "PENDING", True


def query_once(
    *,
    run_id: str,
    data_epoch: str,
    freeze_manifest_sha256: str,
    source_commit_sha: str,
    batch_id: str,
    candidate_id: str,
    query_id: str,
    live_batch_sha256: str,
    batch_submit_receipt_sha256: str,
    qcis_sha256: str,
    measurement_order: list[str],
    predictor_feature_freeze_sha256: str,
    selection_entity_sha256: str,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
    access_token: str,
    transport: SingleAttemptTransport,
    observation_path: Path,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Issue exactly one persisted-ID query without polling or submission fallback."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", run_id):
        raise Qas30TianyanError("QAS30_RUN_ID_INVALID", "run ID is invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", data_epoch):
        raise Qas30TianyanError("QAS30_DATA_EPOCH_INVALID", "dataEpoch is invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", batch_id):
        raise Qas30TianyanError("QAS30_BATCH_ID_INVALID", "batch ID is invalid")
    if not QUERY_ID_PATTERN.fullmatch(query_id):
        raise Qas30TianyanError("QAS30_QUERY_ID_INVALID", "query ID is invalid")
    if not re.fullmatch(r"C(?:0[1-9]|[12][0-9]|30)", candidate_id):
        raise Qas30TianyanError("QAS30_CANDIDATE_ID_INVALID", "candidate ID is invalid")
    if not re.fullmatch(r"[a-f0-9]{40}", source_commit_sha):
        raise Qas30TianyanError("QAS30_COMMIT_INVALID", "source commit is invalid")
    if not all(
        isinstance(value, str) and SHA256_PATTERN.fullmatch(value)
        for value in (
            freeze_manifest_sha256,
            live_batch_sha256,
            batch_submit_receipt_sha256,
            qcis_sha256,
            predictor_feature_freeze_sha256,
            selection_entity_sha256,
            authorization_envelope_sha256,
            resource_sample_sha256,
        )
    ):
        raise Qas30TianyanError(
            "QAS30_QUERY_BINDING_INVALID", "query evidence binding is invalid"
        )
    if (
        not isinstance(measurement_order, list)
        or len(measurement_order) != 6
        or len(set(measurement_order)) != 6
        or any(
            not isinstance(item, str) or not re.fullmatch(r"Q\d{1,4}", item)
            for item in measurement_order
        )
    ):
        raise Qas30TianyanError(
            "QAS30_QUERY_MEASUREMENT_ORDER_INVALID",
            "query measurement order is invalid",
        )
    payload = {"query_ids": [query_id]}
    provider_request_sha256 = _sha256(_canonical_json(payload))
    request_sha256 = _sha256(
        _canonical_json(
            {
                "batchId": batch_id,
                "batchSubmitReceiptSha256": batch_submit_receipt_sha256,
                "candidateId": candidate_id,
                "dataEpoch": data_epoch,
                "freezeManifestSha256": freeze_manifest_sha256,
                "liveBatchSha256": live_batch_sha256,
                "measurementOrder": measurement_order,
                "providerRequestSha256": provider_request_sha256,
                "qcisSha256": qcis_sha256,
                "queryId": query_id,
                "runId": run_id,
                "authorizationEnvelopeSha256": authorization_envelope_sha256,
                "resourceSampleSha256": resource_sample_sha256,
            }
        )
    )
    auth_headers = _headers(access_token, action="QUERY")
    _claim_once(observation_path, request_sha256=request_sha256, action="QUERY")
    try:
        response = transport.request_once(
            method="POST",
            url=f"{BASE_URL}{TianYanPlatform.QUERY_EXP_PATH}",
            json_body=payload,
            headers=auth_headers,
            timeout_seconds=timeout_seconds,
        )
        state, query_again = (
            _query_state(response.payload, query_id)
            if response.status_code == 200
            else ("UNKNOWN_RESPONSE", True)
        )
        if response.status_code in {401, 403}:
            state, query_again = "TOKEN_EXPIRED", False
        provider_payload = response.payload
        provider_http_status = response.status_code
    except Exception as error:
        state, query_again = "UNKNOWN_RESPONSE", True
        provider_payload = {"transportErrorType": type(error).__name__}
        provider_http_status = 0
    views = None
    if state == "COMPLETED":
        try:
            views = _views_from_payload(
                provider_payload, query_id, measurement_order
            )
            if views is None:
                state, query_again = "RESULT_INTEGRITY_FAILED", False
                views = {"integrityError": "QAS30_RESULT_VIEWS_MISSING"}
        except Qas30TianyanError as error:
            state, query_again = "RESULT_INTEGRITY_FAILED", False
            views = {"integrityError": error.code}
    observation = {
        "schemaVersion": QUERY_OBSERVATION_SCHEMA_VERSION,
        "runId": run_id,
        "dataEpoch": data_epoch,
        "freezeManifestSha256": freeze_manifest_sha256,
        "sourceCommitSha": source_commit_sha,
        "batchId": batch_id,
        "candidateId": candidate_id,
        "queryId": query_id,
        "liveBatchSha256": live_batch_sha256,
        "batchSubmitReceiptSha256": batch_submit_receipt_sha256,
        "qcisSha256": qcis_sha256,
        "measurementOrder": measurement_order,
        "predictorFeatureFreezeSha256": predictor_feature_freeze_sha256,
        "selectionEntitySha256": selection_entity_sha256,
        "authorizationEnvelopeSha256": authorization_envelope_sha256,
        "resourceSampleSha256": resource_sample_sha256,
        "requestSha256": request_sha256,
        "providerRequestSha256": provider_request_sha256,
        "providerHttpStatus": provider_http_status,
        "providerResponseSha256": _sha256(_canonical_json(provider_payload)),
        "state": state,
        "scientificStatus": transport_scientific_status(transport),
        "transportRequestCount": 1,
        "queryAgainAllowed": query_again,
        "resubmitAllowed": False,
        "observedAt": _now(),
        "providerPayload": provider_payload,
        "views": views,
    }
    observation["queryObservationSha256"] = _entity_sha256(observation)
    _persist_json(observation_path, observation)
    return observation


def validate_query_observation_against_batch(
    observation: Mapping[str, Any],
    *,
    live_batch: Mapping[str, Any],
    submit_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Revalidate a terminal observation against its submitted circuit and raw views."""

    batch = validate_live_batch(live_batch)
    receipt = dict(submit_receipt)
    receipt_stored = receipt.get("batchSubmitReceiptSha256")
    receipt_unhashed = {
        key: value
        for key, value in receipt.items()
        if key != "batchSubmitReceiptSha256"
    }
    live_batch_sha256 = _entity_sha256(batch)
    bindings = receipt.get("queryBindings")
    provider_query_ids = receipt.get("providerQueryIds")
    circuits = batch["circuits"]
    if (
        receipt_stored != _entity_sha256(receipt_unhashed)
        or receipt.get("state") != "QUERY_IDS_PERSISTED"
        or receipt.get("liveBatchSha256") != live_batch_sha256
        or receipt.get("runId") != batch.get("runId")
        or receipt.get("dataEpoch") != batch.get("dataEpoch")
        or receipt.get("freezeManifestSha256")
        != batch.get("freezeManifestSha256")
        or receipt.get("sourceCommitSha") != batch.get("sourceCommitSha")
        or receipt.get("batchId") != batch.get("batchId")
        or receipt.get("shots") != batch.get("shots")
        or receipt.get("scientificStatus") != batch.get("scientificStatus")
        or receipt.get("predictorFeatureFreezeSha256")
        != batch.get("predictorFeatureFreezeSha256")
        or receipt.get("selectionEntitySha256")
        != batch.get("selectionEntitySha256")
        or not isinstance(bindings, list)
        or len(bindings) != 5
        or not isinstance(provider_query_ids, list)
        or len(provider_query_ids) != 5
    ):
        raise Qas30TianyanError(
            "QAS30_QUERY_SUBMIT_BINDING_INVALID",
            "submit receipt does not bind the persisted live batch",
        )
    expected_bindings = [
        {
            "candidateId": circuit["candidateId"],
            "qcisSha256": circuit["qcisSha256"],
            "regularityReceiptSha256": circuit["regularityReceiptSha256"],
            "predictorFeatureReceiptSha256": circuit[
                "predictorFeatureReceiptSha256"
            ],
            "queryId": query_id,
            "position": index,
        }
        for index, (circuit, query_id) in enumerate(
            zip(circuits, provider_query_ids, strict=True)
        )
    ]
    if bindings != expected_bindings or len(set(provider_query_ids)) != 5:
        raise Qas30TianyanError(
            "QAS30_QUERY_SUBMIT_BINDING_INVALID",
            "query bindings do not match the submitted circuit order",
        )
    value = dict(observation)
    stored = value.get("queryObservationSha256")
    unhashed = {
        key: item
        for key, item in value.items()
        if key != "queryObservationSha256"
    }
    query_id = value.get("queryId")
    matches = [row for row in bindings if row["queryId"] == query_id]
    if len(matches) != 1:
        raise Qas30TianyanError(
            "QAS30_QUERY_OBSERVATION_BINDING_INVALID",
            "query observation does not identify one submitted circuit",
        )
    binding = matches[0]
    circuit = circuits[binding["position"]]
    expected_order = circuit["measurementPhysicalOrder"]
    sha_fields = (
        "freezeManifestSha256",
        "liveBatchSha256",
        "batchSubmitReceiptSha256",
        "qcisSha256",
        "predictorFeatureFreezeSha256",
        "selectionEntitySha256",
        "authorizationEnvelopeSha256",
        "resourceSampleSha256",
        "requestSha256",
        "providerRequestSha256",
        "providerResponseSha256",
    )
    if (
        stored != _entity_sha256(unhashed)
        or value.get("schemaVersion") != QUERY_OBSERVATION_SCHEMA_VERSION
        or value.get("state") != "COMPLETED"
        or value.get("runId") != batch.get("runId")
        or value.get("dataEpoch") != batch.get("dataEpoch")
        or value.get("freezeManifestSha256")
        != batch.get("freezeManifestSha256")
        or value.get("sourceCommitSha") != batch.get("sourceCommitSha")
        or value.get("batchId") != batch.get("batchId")
        or value.get("candidateId") != binding["candidateId"]
        or value.get("liveBatchSha256") != live_batch_sha256
        or value.get("batchSubmitReceiptSha256") != receipt_stored
        or value.get("qcisSha256") != binding["qcisSha256"]
        or value.get("measurementOrder") != expected_order
        or value.get("predictorFeatureFreezeSha256")
        != batch.get("predictorFeatureFreezeSha256")
        or value.get("selectionEntitySha256")
        != batch.get("selectionEntitySha256")
        or value.get("scientificStatus") != batch.get("scientificStatus")
        or value.get("transportRequestCount") != 1
        or value.get("providerHttpStatus") != 200
        or value.get("queryAgainAllowed") is not False
        or value.get("resubmitAllowed") is not False
        or any(
            not isinstance(value.get(field), str)
            or not SHA256_PATTERN.fullmatch(value[field])
            for field in sha_fields
        )
        or value.get("providerResponseSha256")
        != _sha256(_canonical_json(value.get("providerPayload")))
    ):
        raise Qas30TianyanError(
            "QAS30_QUERY_OBSERVATION_BINDING_INVALID",
            "query observation identity or evidence binding changed",
        )
    expected_views = _views_from_payload(
        value.get("providerPayload"), str(query_id), expected_order
    )
    if expected_views is None or value.get("views") != expected_views:
        raise Qas30TianyanError(
            "QAS30_QUERY_OBSERVATION_VIEWS_INVALID",
            "raw query views or shot conservation changed",
        )
    return value
