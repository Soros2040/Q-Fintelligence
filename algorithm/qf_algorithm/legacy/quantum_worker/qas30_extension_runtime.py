"""Governed local execution bridge for frozen QAS30 extension batches.

This module deliberately does not construct QCIS, mappings, calibration facts,
authorizations, resource samples, or provider requests.  Those facts must
already be represented by a fully validated ``qas30_tianyan`` live batch.  The
bridge binds that batch to one immutable extension design record, writes a
durable local intent before invoking an injected one-shot submitter, and makes
every non-exact outcome sticky.

The current Tianyan live-batch contract is intentionally fixed at 1,000 shots.
In particular, SHOT_SCALING design slots at other levels cannot be silently
substituted or submitted through this runtime until a separately frozen,
validated generalization contract exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import qas30_extensions, qas30_tianyan

RUNTIME_SCHEMA_VERSION = "qf.qas30.extension-runtime.v1"
INTENT_SCHEMA_VERSION = "qf.qas30.extension-submit-intent.v1"
READY_STATE = "READY_FOR_GOVERNED_SUBMIT"
INTENT_STATE = "SUBMIT_INTENT_PERSISTED"
QUERY_IDS_PERSISTED = "QUERY_IDS_PERSISTED"
STICKY_OUTCOME_STATE = "UNKNOWN_OR_PARTIAL_PROVIDER_OUTCOME"
_SHA256 = re.compile(r"[a-f0-9]{64}")
_QUERY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}")


class Qas30ExtensionRuntimeError(RuntimeError):
    """A local extension execution or recovery invariant failed closed."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.receipt = dict(receipt) if receipt is not None else None
        self.resubmit_allowed = False


SubmitLiveBatchOnce = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ReconcileExistingQuery = Callable[..., Mapping[str, Any]]


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
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_JSON_INVALID", "value is not canonical JSON"
        ) from error


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 for canonical JSON, including callback receipts."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _require_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_BINDING_INVALID", f"{name} must be a SHA-256 digest"
        )
    return value


def _frozen_identity(extension_batch: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "runId": extension_batch["runId"],
        "dataEpoch": extension_batch["dataEpoch"],
        "sourceCommitSha": extension_batch["sourceCommitSha"],
        "freezeManifestSha256": extension_batch["freezeManifestSha256"],
        "batchId": extension_batch["batchId"],
        "candidateOrder": list(extension_batch["candidateOrder"]),
        "shots": extension_batch["shots"],
    }


def prepare_extension_submission(
    extension_batch: Mapping[str, Any],
    live_batch: Mapping[str, Any],
    *,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
) -> dict[str, Any]:
    """Validate the complete design/live binding before the one-shot gate.

    The returned ``READY_FOR_GOVERNED_SUBMIT`` value is only a local binding
    witness: the caller must still invoke :func:`submit_extension_batch_once`
    with its explicitly injected exactly-once submit boundary.
    """

    design = qas30_extensions.validate_batch_spec(extension_batch)
    if design["executionState"] != qas30_extensions.READY_STATE:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_DESIGN_NOT_READY",
            "the frozen extension batch is not eligible for a live gate",
        )
    # qas30_tianyan.v3 has no generalized shots contract.  Fail before any
    # live-batch substitution/mutation can make a different design look ready.
    if design["shots"] != qas30_extensions.DEFAULT_SHOTS:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_SHOT_GENERALIZATION_REQUIRED",
            "non-1000-shot extension slots require an explicit frozen live-batch generalization",
        )
    try:
        live = qas30_tianyan.validate_live_batch(live_batch)
    except qas30_tianyan.Qas30TianyanError as error:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_LIVE_BATCH_INVALID",
            "the upstream live batch did not pass strict QCIS, mapping, regularity, "
            "authorization, and resource validation",
        ) from error

    authorization_sha = _require_sha256(
        authorization_envelope_sha256, "authorizationEnvelopeSha256"
    )
    resource_sha = _require_sha256(resource_sample_sha256, "resourceSampleSha256")
    if (
        live.get("authorizationEnvelopeSha256") != authorization_sha
        or live.get("resourceSampleSha256") != resource_sha
    ):
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_RUNTIME_GATE_MISMATCH",
            "the strict live batch does not bind the supplied authorization and resource gate",
        )

    identity = _frozen_identity(design)
    for name in ("runId", "dataEpoch", "sourceCommitSha", "freezeManifestSha256", "batchId"):
        if live.get(name) != identity[name]:
            raise Qas30ExtensionRuntimeError(
                "QAS30_EXTENSION_FROZEN_IDENTITY_MISMATCH",
                f"live batch {name} does not match the frozen extension batch",
            )
    if live.get("candidateIds") != identity["candidateOrder"]:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_CANDIDATE_ORDER_MISMATCH",
            "live batch candidate order does not match the frozen extension batch",
        )
    if live.get("shots") != identity["shots"]:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_SHOTS_MISMATCH",
            "live batch shots do not match the frozen extension batch",
        )
    if live.get("selectionEntitySha256") != design["selectionEntitySha256"]:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_SELECTION_MISMATCH",
            "live batch selection entity does not match the frozen extension batch",
        )

    binding = {
        "schemaVersion": RUNTIME_SCHEMA_VERSION,
        "state": READY_STATE,
        "extensionBatchCanonicalSha256": design["canonicalSha256"],
        "liveBatchSha256": canonical_sha256(live),
        "frozenIdentity": identity,
        "authorizationEnvelopeSha256": authorization_sha,
        "resourceSampleSha256": resource_sha,
        "resubmitAllowed": False,
    }
    binding["bindingSha256"] = canonical_sha256(binding)
    return binding


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    """Create one 0600 JSON fact and fsync its file and parent directory."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = _canonical_json(dict(payload)).encode("utf-8")
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


def _intent_path(receipt_path: Path) -> Path:
    return receipt_path.with_name(f"{receipt_path.name}.intent")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_RECEIPT_UNREADABLE", "the durable runtime receipt is unreadable"
        ) from error
    if not isinstance(value, dict):
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_RECEIPT_UNREADABLE", "the durable runtime receipt is not an object"
        )
    return value


def _validate_callback_receipt(
    callback_receipt: Mapping[str, Any], binding: Mapping[str, Any], live: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    receipt = _json_copy(dict(callback_receipt))
    if receipt.get("state") != QUERY_IDS_PERSISTED:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_CALLBACK_OUTCOME_STICKY",
            "the one-shot submitter returned an unknown, partial, or non-queryable outcome",
        )
    for name in ("runId", "dataEpoch", "sourceCommitSha", "freezeManifestSha256", "batchId"):
        if receipt.get(name) != binding["frozenIdentity"][name]:
            raise Qas30ExtensionRuntimeError(
                "QAS30_EXTENSION_CALLBACK_BINDING_INVALID",
                "the callback receipt does not bind the frozen extension identity",
            )
    if (
        receipt.get("liveBatchSha256") != binding["liveBatchSha256"]
        or receipt.get("authorizationEnvelopeSha256")
        != binding["authorizationEnvelopeSha256"]
        or receipt.get("resourceSampleSha256") != binding["resourceSampleSha256"]
        or receipt.get("resubmitAllowed") is not False
    ):
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_CALLBACK_BINDING_INVALID",
            "the callback receipt does not bind this exact live submit gate",
        )
    provider_ids = receipt.get("providerQueryIds")
    bindings = receipt.get("queryBindings")
    expected_candidates = live["candidateIds"]
    if (
        not isinstance(provider_ids, list)
        or len(provider_ids) != 5
        or len(set(provider_ids)) != 5
        or any(
            not isinstance(item, str) or _QUERY_ID.fullmatch(item) is None
            for item in provider_ids
        )
        or not isinstance(bindings, list)
        or len(bindings) != 5
    ):
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_CALLBACK_OUTCOME_STICKY",
            "the callback receipt lacks five unique persisted Query IDs",
        )
    normalized_bindings: list[dict[str, Any]] = []
    for position, (row, query_id, candidate_id) in enumerate(
        zip(bindings, provider_ids, expected_candidates, strict=True)
    ):
        if (
            not isinstance(row, Mapping)
            or row.get("position") != position
            or row.get("queryId") != query_id
            or row.get("candidateId") != candidate_id
        ):
            raise Qas30ExtensionRuntimeError(
                "QAS30_EXTENSION_CALLBACK_BINDING_INVALID",
                "callback Query IDs do not preserve the submitted candidate order",
            )
        normalized_bindings.append(_json_copy(dict(row)))
    return receipt, normalized_bindings


def _runtime_receipt(
    *,
    binding: Mapping[str, Any],
    state: str,
    callback_receipt: Mapping[str, Any] | None,
    query_bindings: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schemaVersion": RUNTIME_SCHEMA_VERSION,
        "state": state,
        "extensionBatchCanonicalSha256": binding["extensionBatchCanonicalSha256"],
        "liveBatchSha256": binding["liveBatchSha256"],
        "frozenIdentity": _json_copy(binding["frozenIdentity"]),
        "authorizationEnvelopeSha256": binding["authorizationEnvelopeSha256"],
        "resourceSampleSha256": binding["resourceSampleSha256"],
        "bindingSha256": binding["bindingSha256"],
        "resubmitAllowed": False,
        "persistedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    if callback_receipt is not None:
        copied = _json_copy(dict(callback_receipt))
        receipt["callbackReceipt"] = copied
        receipt["callbackReceiptSha256"] = canonical_sha256(copied)
    if query_bindings is not None:
        receipt["queryBindings"] = _json_copy(query_bindings)
    receipt["runtimeReceiptSha256"] = canonical_sha256(receipt)
    return receipt


def _validate_runtime_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    receipt = _json_copy(dict(value))
    stored = receipt.pop("runtimeReceiptSha256", None)
    if (
        not isinstance(stored, str)
        or _SHA256.fullmatch(stored) is None
        or canonical_sha256(receipt) != stored
        or receipt.get("schemaVersion") != RUNTIME_SCHEMA_VERSION
        or receipt.get("state") not in {QUERY_IDS_PERSISTED, STICKY_OUTCOME_STATE}
        or receipt.get("resubmitAllowed") is not False
        or not isinstance(receipt.get("frozenIdentity"), dict)
    ):
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_RECEIPT_INVALID", "the runtime receipt identity changed"
        )
    receipt["runtimeReceiptSha256"] = stored
    return receipt


def submit_extension_batch_once(
    extension_batch: Mapping[str, Any],
    live_batch: Mapping[str, Any],
    *,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
    receipt_path: Path,
    submit_live_batch_once: SubmitLiveBatchOnce,
) -> dict[str, Any]:
    """Persist an intent then invoke exactly one injected live-submit action.

    A pre-existing intent means an interrupted outcome is unknown; it is never
    retried here.  A callback failure, malformed receipt, partial ID list, or
    duplicate ID list produces a durable sticky runtime receipt.
    """

    binding = prepare_extension_submission(
        extension_batch,
        live_batch,
        authorization_envelope_sha256=authorization_envelope_sha256,
        resource_sample_sha256=resource_sample_sha256,
    )
    receipt = Path(receipt_path)
    intent = _intent_path(receipt)
    if receipt.exists():
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_RECEIPT_ALREADY_EXISTS",
            "a durable extension runtime receipt already blocks resubmission",
        )
    if intent.exists():
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_INTENT_ALREADY_EXISTS",
            "a prior extension submit intent has an unknown outcome and blocks resubmission",
        )
    intent_payload = {
        "schemaVersion": INTENT_SCHEMA_VERSION,
        "state": INTENT_STATE,
        "extensionBatchCanonicalSha256": binding["extensionBatchCanonicalSha256"],
        "liveBatchSha256": binding["liveBatchSha256"],
        "frozenIdentity": binding["frozenIdentity"],
        "authorizationEnvelopeSha256": binding["authorizationEnvelopeSha256"],
        "resourceSampleSha256": binding["resourceSampleSha256"],
        "bindingSha256": binding["bindingSha256"],
        "resubmitAllowed": False,
    }
    intent_payload["intentSha256"] = canonical_sha256(intent_payload)
    try:
        _write_exclusive(intent, intent_payload)
    except FileExistsError as error:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_INTENT_ALREADY_EXISTS",
            "a prior extension submit intent has an unknown outcome and blocks resubmission",
        ) from error
    except OSError as error:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_INTENT_PERSIST_FAILED",
            "the extension submit intent could not be durably persisted",
        ) from error

    callback_copy: dict[str, Any] | None = None
    try:
        callback_receipt = submit_live_batch_once(_json_copy(dict(live_batch)))
        if not isinstance(callback_receipt, Mapping):
            raise TypeError("submit callback did not return an object")
        # Preserve a returned partial/duplicate-ID receipt in the sticky local
        # fact.  It is evidence for recovery, never authorization to replay.
        callback_copy = _json_copy(dict(callback_receipt))
        callback_copy, query_bindings = _validate_callback_receipt(
            callback_copy, binding, live_batch
        )
    except Exception as error:
        sticky = _runtime_receipt(
            binding=binding,
            state=STICKY_OUTCOME_STATE,
            callback_receipt=callback_copy,
            query_bindings=None,
        )
        try:
            _write_exclusive(receipt, sticky)
        except OSError as persist_error:
            raise Qas30ExtensionRuntimeError(
                "QAS30_EXTENSION_RECEIPT_PERSIST_FAILED",
                "the one-shot outcome could not be durably marked sticky",
            ) from persist_error
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_CALLBACK_OUTCOME_STICKY",
            "the one-shot submission outcome is sticky and cannot be resubmitted",
            receipt=sticky,
        ) from error

    persisted = _runtime_receipt(
        binding=binding,
        state=QUERY_IDS_PERSISTED,
        callback_receipt=callback_copy,
        query_bindings=query_bindings,
    )
    try:
        _write_exclusive(receipt, persisted)
    except OSError as error:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_RECEIPT_PERSIST_FAILED",
            "the submitted Query IDs could not be durably bound to the extension batch",
        ) from error
    return persisted


def reconcile_existing_queries(
    extension_batch: Mapping[str, Any],
    live_batch: Mapping[str, Any],
    *,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
    receipt_path: Path,
    reconcile_existing_query: ReconcileExistingQuery,
) -> dict[str, Any]:
    """Resume only by querying the five durably persisted Query IDs.

    This function has no submit callback and refuses every uncertain or partial
    submission receipt.  The injected callback is given one already-persisted
    ``queryId`` at a time; scheduling and query authorization remain upstream.
    """

    binding = prepare_extension_submission(
        extension_batch,
        live_batch,
        authorization_envelope_sha256=authorization_envelope_sha256,
        resource_sample_sha256=resource_sample_sha256,
    )
    receipt = _validate_runtime_receipt(_read_json(Path(receipt_path)))
    for name in (
        "extensionBatchCanonicalSha256",
        "liveBatchSha256",
        "authorizationEnvelopeSha256",
        "resourceSampleSha256",
        "bindingSha256",
    ):
        if receipt.get(name) != binding.get(name):
            raise Qas30ExtensionRuntimeError(
                "QAS30_EXTENSION_RESUME_BINDING_MISMATCH",
                "the persisted runtime receipt does not bind this exact extension live batch",
            )
    if receipt.get("frozenIdentity") != binding.get("frozenIdentity"):
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_RESUME_BINDING_MISMATCH",
            "the persisted runtime receipt frozen identity changed",
        )
    if receipt["state"] != QUERY_IDS_PERSISTED:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_RECONCILIATION_STOPPED",
            "unknown or partial submit outcomes cannot be queried or resubmitted",
            receipt=receipt,
        )
    query_bindings = receipt.get("queryBindings")
    if not isinstance(query_bindings, list) or len(query_bindings) != 5:
        raise Qas30ExtensionRuntimeError(
            "QAS30_EXTENSION_RECEIPT_INVALID",
            "the successful runtime receipt lacks its five persisted Query IDs",
        )
    observations: list[dict[str, Any]] = []
    for row in query_bindings:
        if not isinstance(row, Mapping) or not isinstance(row.get("queryId"), str):
            raise Qas30ExtensionRuntimeError(
                "QAS30_EXTENSION_RECEIPT_INVALID", "a persisted Query ID is invalid"
            )
        observation = reconcile_existing_query(
            query_id=row["queryId"],
            query_binding=_json_copy(dict(row)),
            runtime_receipt=_json_copy(receipt),
            live_batch=_json_copy(dict(live_batch)),
        )
        if not isinstance(observation, Mapping):
            raise Qas30ExtensionRuntimeError(
                "QAS30_EXTENSION_RECONCILIATION_INVALID",
                "existing-ID reconciliation did not return an observation object",
            )
        copied = _json_copy(dict(observation))
        if "queryId" in copied and copied["queryId"] != row["queryId"]:
            raise Qas30ExtensionRuntimeError(
                "QAS30_EXTENSION_RECONCILIATION_INVALID",
                "reconciliation returned an observation for a different Query ID",
            )
        observations.append(copied)
    return {
        "schemaVersion": RUNTIME_SCHEMA_VERSION,
        "state": "EXISTING_IDS_RECONCILED",
        "queryExistingIdsOnly": True,
        "resubmitAllowed": False,
        "runtimeReceiptSha256": receipt["runtimeReceiptSha256"],
        "observations": observations,
    }
