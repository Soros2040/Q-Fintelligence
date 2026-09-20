"""Durable local orchestration helpers for the frozen QAS30 protocol.

The production command bridge reads one caller-selected, owner-only credential
file only after local gates pass.  It never serializes credentials or tokens,
scans global processes, retries a provider action, or fabricates facts.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from . import (
    qas30_drift,
    qas30_observation,
    qas30_production_transport,
    qas30_protocol,
    qas30_tianyan,
)

PROJECT_ROOT = Path(os.environ.get("QF_DATA_ROOT", Path.cwd())).resolve()
EXPECTED_NAMESPACE = "qfintelligence"
GIB = 1024**3
MIB = 1024**2
LIVE_ACTIONS = {"LOGIN", "CAPTURE_K", "SUBMIT", "QUERY", "RECONCILE_QUERY"}


class Qas30ExperimentError(RuntimeError):
    """A local orchestration, authorization, or recovery invariant failed."""


class FixtureSingleAttemptTransport:
    """One explicitly supplied offline response; it has no network capability."""

    scientific_status = qas30_tianyan.FIXTURE_SCIENTIFIC_STATUS

    def __init__(self, fixture: Mapping[str, Any]) -> None:
        if fixture.get("kind") != "SINGLE_ATTEMPT_HTTP_FIXTURE":
            raise Qas30ExperimentError("an explicit single-attempt transport fixture is required")
        response = fixture.get("response")
        if not isinstance(response, Mapping) or set(response) != {
            "statusCode",
            "payload",
            "headers",
        }:
            raise Qas30ExperimentError("transport fixture response is invalid")
        status_code = response.get("statusCode")
        headers = response.get("headers")
        if (
            not isinstance(status_code, int)
            or isinstance(status_code, bool)
            or status_code < 0
            or status_code > 599
            or not isinstance(headers, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in headers.items()
            )
        ):
            raise Qas30ExperimentError("transport fixture response is invalid")
        self._response = qas30_tianyan.SingleAttemptHttpResponse(
            status_code=status_code,
            payload=response.get("payload"),
            headers=dict(headers),
        )
        self._used = False

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
    ) -> qas30_tianyan.SingleAttemptHttpResponse:
        del method, url, headers, timeout_seconds, query_params, json_body, form_body
        if self._used:
            raise Qas30ExperimentError("transport fixture cannot be replayed")
        self._used = True
        return self._response


def _write_json_once(path: Path, payload: Mapping[str, Any]) -> None:
    """Persist one immutable local fact without replacing an existing object."""

    _atomic_publish_once(path, _canonical_json(dict(payload)).encode("utf-8"))


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_publish_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as error:
        raise Qas30ExperimentError(f"local fact already exists: {path.name}") from error
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path)
        _fsync_directory(path.parent)
    except FileExistsError as error:
        raise Qas30ExperimentError(f"local fact already exists: {path.name}") from error
    finally:
        try:
            os.unlink(temporary)
            _fsync_directory(path.parent)
        except FileNotFoundError:
            pass


def _write_bytes_once(path: Path, payload: bytes) -> None:
    """Persist exact source bytes so later freeze can revalidate artifact hashes."""

    _atomic_publish_once(path, payload)


def _protocol_state_journal_path(run_directory: Path) -> Path:
    return run_directory / "protocol-state-journal.jsonl"


def _transition_protocol_state(
    run_directory: Path, requested: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Durably validate and append one frozen-protocol lifecycle transition."""

    path = _protocol_state_journal_path(run_directory)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise Qas30ExperimentError("protocol state journal cannot be opened safely") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        file_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_status.st_mode)
            or stat.S_IMODE(file_status.st_mode) != 0o600
            or file_status.st_uid != os.getuid()
        ):
            raise Qas30ExperimentError("protocol state journal has unsafe ownership or mode")
        os.lseek(descriptor, 0, os.SEEK_SET)
        journal_bytes = b""
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            journal_bytes += chunk
        current = (
            "LOCAL_INPUTS_READY"
            if (run_directory / "prepare.json").is_file()
            else "DESIGN_FROZEN"
        )
        sequence = 0
        try:
            journal_text = journal_bytes.decode("utf-8")
            lines = journal_text.splitlines()
            if journal_text and not journal_text.endswith("\n"):
                raise ValueError
            for expected_sequence, line in enumerate(lines, start=1):
                event = json.loads(line)
                if (
                    not isinstance(event, dict)
                    or set(event)
                    != {
                        "schemaVersion",
                        "sequence",
                        "fromState",
                        "toState",
                        "recordedAt",
                    }
                    or event.get("schemaVersion")
                    != "qf.qas30.protocol-state-event.v1"
                    or event.get("sequence") != expected_sequence
                    or isinstance(event.get("sequence"), bool)
                    or event.get("fromState") != current
                    or not isinstance(event.get("toState"), str)
                    or not isinstance(event.get("recordedAt"), str)
                ):
                    raise ValueError
                _parse_timestamp(event["recordedAt"])
                qas30_protocol.validate_state_transition(current, event["toState"])
                current = event["toState"]
                sequence = expected_sequence
        except (
            ValueError,
            TypeError,
            json.JSONDecodeError,
            qas30_protocol.Qas30ProtocolError,
        ) as error:
            raise Qas30ExperimentError("protocol state journal is corrupt") from error
        try:
            qas30_protocol.validate_state_transition(current, requested)
        except qas30_protocol.Qas30ProtocolError as error:
            raise Qas30ExperimentError(
                f"protocol state transition {current} -> {requested} is invalid"
            ) from error
        event = {
            "schemaVersion": "qf.qas30.protocol-state-event.v1",
            "sequence": sequence + 1,
            "fromState": current,
            "toState": requested,
            "recordedAt": _iso(now or _now()),
        }
        payload = (_canonical_json(event) + "\n").encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    _fsync_directory(path.parent)
    return event


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Qas30ExperimentError(f"local fact is unreadable: {path.name}") from error
    if not isinstance(value, dict):
        raise Qas30ExperimentError(f"local fact is not an object: {path.name}")
    return value


def _read_frozen_manifest(run_directory: Path) -> dict[str, Any]:
    value = _read_json(run_directory / "freeze.json")
    stored = value.get("freezeManifestSha256")
    unhashed = {key: item for key, item in value.items() if key != "freezeManifestSha256"}
    if (
        stored != _sha256(unhashed)
        or value.get("state") != "READY_FOR_K0"
        or value.get("scientificStatus") != "DESIGN_ONLY"
        or not isinstance(value.get("dataEpoch"), str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value["dataEpoch"]
        )
        or not _valid_source_commit(str(value.get("sourceCommitSha", "")))
    ):
        raise Qas30ExperimentError("freeze manifest identity or hash changed")
    pre_hardware = value.get("preHardwareFeatureFreeze")
    if not isinstance(pre_hardware, Mapping):
        raise Qas30ExperimentError("pre-hardware predictor feature freeze is missing")
    try:
        verified_pre_hardware = (
            qas30_protocol.validate_pre_hardware_predictor_feature_freeze(
                pre_hardware
            )
        )
    except qas30_protocol.Qas30ProtocolError as error:
        raise Qas30ExperimentError(
            "pre-hardware predictor feature freeze is invalid"
        ) from error
    if (
        verified_pre_hardware.get("sourceCommitSha")
        != value.get("sourceCommitSha")
        or verified_pre_hardware.get("preHardwareFeatureFreezeSha256")
        != value.get("preHardwareFeatureFreezeSha256")
        or verified_pre_hardware.get("localFeatureEvidenceSha256")
        != value.get("localFeatureEvidenceSha256")
        or verified_pre_hardware.get("localFeatureDefinition")
        != value.get("localFeatureDefinition")
    ):
        raise Qas30ExperimentError(
            "pre-hardware predictor feature freeze does not bind the run"
        )
    local_feature_evidence = _read_json(run_directory / "local-feature-evidence.json")
    candidate_bundle = _read_json(run_directory / "candidate-bundle.json")
    try:
        verified_evidence = qas30_protocol.validate_local_feature_evidence(
            local_feature_evidence,
            production_bundle=candidate_bundle,
        )
    except qas30_protocol.Qas30ProtocolError as error:
        raise Qas30ExperimentError("local feature evidence revalidation failed") from error
    verified_evidence.pop("validatedPreHardwareRows")
    if (
        verified_evidence != local_feature_evidence
        or verified_evidence.get("evidenceSha256")
        != verified_pre_hardware.get("localFeatureEvidenceSha256")
    ):
        raise Qas30ExperimentError("local feature evidence changed after freeze")
    if _read_json(run_directory / "pre-hardware-feature-freeze.json") != dict(
        verified_pre_hardware
    ):
        raise Qas30ExperimentError(
            "persisted pre-hardware predictor feature entity changed"
        )
    r1_selection = _read_json(run_directory / "r1-selection.json")
    r1_stored = r1_selection.get("r1SelectionSha256")
    r1_unhashed = {
        key: item
        for key, item in r1_selection.items()
        if key != "r1SelectionSha256"
    }
    block_plan = value.get("blockPlan")
    block_batches = block_plan.get("batches") if isinstance(block_plan, Mapping) else None
    if (
        set(r1_selection)
        != {
            "schemaVersion",
            "runId",
            "dataEpoch",
            "freezeManifestSha256",
            "sourceCommitSha",
            "candidateBundleSha256",
            "blockId",
            "batches",
            "r1SelectionSha256",
        }
        or r1_selection.get("schemaVersion") != "qf.qas30.r1-selection.v1"
        or r1_stored != _sha256(r1_unhashed)
        or r1_selection.get("runId") != value.get("runId")
        or r1_selection.get("dataEpoch") != value.get("dataEpoch")
        or r1_selection.get("freezeManifestSha256") != stored
        or r1_selection.get("sourceCommitSha") != value.get("sourceCommitSha")
        or r1_selection.get("candidateBundleSha256")
        != value.get("candidateBundleSha256")
        or not isinstance(block_batches, list)
        or r1_selection.get("blockId")
        != (block_plan.get("blockId") if isinstance(block_plan, Mapping) else None)
        or r1_selection.get("batches") != block_batches[:2]
    ):
        raise Qas30ExperimentError("frozen R1 selection entity changed")
    return value


def _valid_source_commit(value: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{40}", value))


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise Qas30ExperimentError("timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Qas30ExperimentError("timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise Qas30ExperimentError("timestamp requires a timezone")
    return parsed.astimezone(UTC)


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse only numeric /proc/meminfo values into bytes."""

    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Za-z_()]+):\s+(\d+)\s+kB", line.strip())
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    required = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    if not required <= set(values):
        raise Qas30ExperimentError("meminfo lacks memory or swap capacity fields")
    return values


def parse_memory_pressure(text: str) -> dict[str, float]:
    """Parse Linux memory PSI avg10 values for some/full."""

    result: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in {"some", "full"}:
            continue
        fields = dict(item.split("=", 1) for item in parts[1:] if "=" in item)
        try:
            result[parts[0]] = float(fields["avg10"])
        except (KeyError, ValueError) as error:
            raise Qas30ExperimentError("memory PSI row is invalid") from error
    if set(result) != {"some", "full"}:
        raise Qas30ExperimentError("memory PSI requires some and full rows")
    return result


def parse_vmstat(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1])
    required = {"pswpin", "pswpout", "oom_kill"}
    if not required <= set(values):
        raise Qas30ExperimentError("vmstat lacks swap or OOM counters")
    return values


def _registry_summary(registry_text: str | None, *, project_root: Path) -> dict[str, Any]:
    if registry_text is None:
        return {"present": False, "validatedEntries": 0, "valid": True}
    try:
        payload = json.loads(registry_text)
    except json.JSONDecodeError as error:
        raise Qas30ExperimentError("process registry is not valid JSON") from error
    if isinstance(payload, dict):
        if "projectRoot" in payload or "namespace" in payload:
            if (
                payload.get("projectRoot") != str(project_root)
                or payload.get("namespace") != EXPECTED_NAMESPACE
            ):
                raise Qas30ExperimentError(
                    "process registry cwd/namespace validation failed"
                )
            groups = payload.get("childGroups", [])
            managed = payload.get("managedProcesses", [])
            if not isinstance(groups, list) or not isinstance(managed, list):
                raise Qas30ExperimentError("process registry entries are invalid")
            return {
                "present": True,
                "validatedEntries": len(groups) + len(managed),
                "valid": True,
            }
        entries = payload.get("processes", payload.get("entries", []))
    else:
        entries = payload
    if not isinstance(entries, list):
        raise Qas30ExperimentError("process registry entries are invalid")
    expected_cwd = str(project_root)
    for entry in entries:
        if not isinstance(entry, dict):
            raise Qas30ExperimentError("process registry entry is invalid")
        cwd = entry.get("cwd", entry.get("projectRoot"))
        namespace = entry.get("namespace", entry.get("processNamespace"))
        if cwd != expected_cwd or namespace != EXPECTED_NAMESPACE:
            raise Qas30ExperimentError("process registry cwd/namespace validation failed")
    return {"present": True, "validatedEntries": len(entries), "valid": True}


def evaluate_resource_gate(
    *,
    meminfo_text: str,
    pressure_text: str,
    vmstat_text: str,
    previous_vmstat_text: str,
    interval_seconds: float,
    qf_rss_bytes: int | None = None,
    prior_swapout_deltas: Sequence[int] = (),
    registry_text: str | None = None,
    project_root: Path = PROJECT_ROOT,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate one read-only pre-write resource sample from fixtureable text."""

    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise Qas30ExperimentError("vmstat interval must be positive")
    meminfo = parse_meminfo(meminfo_text)
    pressure = parse_memory_pressure(pressure_text)
    current_vmstat = parse_vmstat(vmstat_text)
    previous_vmstat = parse_vmstat(previous_vmstat_text)
    registry = _registry_summary(registry_text, project_root=project_root)
    if qf_rss_bytes is not None and (
        not isinstance(qf_rss_bytes, int)
        or isinstance(qf_rss_bytes, bool)
        or qf_rss_bytes < 0
    ):
        raise Qas30ExperimentError("QF RSS must be a non-negative integer")
    if (
        isinstance(prior_swapout_deltas, str | bytes)
        or len(prior_swapout_deltas) > 2
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in prior_swapout_deltas
        )
    ):
        raise Qas30ExperimentError("prior swapout deltas are invalid")
    page_size = os.sysconf("SC_PAGE_SIZE")
    swap_pages = sum(
        max(0, current_vmstat[name] - previous_vmstat[name])
        for name in ("pswpin", "pswpout")
    )
    swap_delta_60_bytes = int(swap_pages * page_size * 60.0 / interval_seconds)
    current_swapout_delta = max(
        0, current_vmstat["pswpout"] - previous_vmstat["pswpout"]
    )
    three_consecutive_swapout_growth = (
        len(prior_swapout_deltas) == 2
        and all(value > 0 for value in prior_swapout_deltas)
        and current_swapout_delta > 0
    )
    oom_delta = max(0, current_vmstat["oom_kill"] - previous_vmstat["oom_kill"])
    available = meminfo["MemAvailable"]
    total = meminfo["MemTotal"]
    available_ratio = available / total
    swap_used = max(0, meminfo["SwapTotal"] - meminfo["SwapFree"])
    reasons: list[str] = []
    green_failures = {
        "MEM_AVAILABLE_LT_2_5_GIB": available < int(2.5 * GIB),
        "MEM_AVAILABLE_RATIO_LT_30_PERCENT": available_ratio < 0.30,
        "SWAP_DELTA_60S_GT_128_MIB": swap_delta_60_bytes > 128 * MIB,
        "PSI_SOME_GE_2": pressure["some"] >= 2.0,
        "PSI_FULL_GE_0_5": pressure["full"] >= 0.5,
        "OOM_KILL_DELTA_NONZERO": oom_delta > 0,
        "QF_RSS_GT_1_25_GIB": qf_rss_bytes is not None
        and qf_rss_bytes > int(1.25 * GIB),
    }
    reasons.extend(name for name, failed in green_failures.items() if failed)
    critical_failures = {
        "MEM_AVAILABLE_LT_768_MIB": available < 768 * MIB,
        "OOM_KILL_DELTA_NONZERO": oom_delta > 0,
    }
    red_failures = {
        "MEM_AVAILABLE_LT_1_5_GIB": available < int(1.5 * GIB),
        "MEM_AVAILABLE_RATIO_LT_20_PERCENT": available_ratio < 0.20,
        "SWAP_USED_GT_1_GIB_AND_GROWING": swap_used > GIB
        and current_swapout_delta > 0,
        "PSI_SOME_GE_10": pressure["some"] >= 10.0,
        "PSI_FULL_GE_2": pressure["full"] >= 2.0,
        "THREE_CONSECUTIVE_SWAPOUT_GROWTH": three_consecutive_swapout_growth,
        "QF_RSS_GT_2_GIB": qf_rss_bytes is not None and qf_rss_bytes > 2 * GIB,
        "OOM_KILL_DELTA_NONZERO": oom_delta > 0,
    }
    reasons.extend(
        name
        for name, failed in {**red_failures, **critical_failures}.items()
        if failed and name not in reasons
    )
    if any(critical_failures.values()):
        level = "CRITICAL"
    elif any(red_failures.values()):
        level = "RED"
    elif any(green_failures.values()):
        level = "YELLOW"
    else:
        level = "GREEN"
    timestamp = observed_at or _now()
    return {
        "schemaVersion": "qf.qas30.pre-write-resource-snapshot.v1",
        "observedAt": _iso(timestamp),
        "level": level,
        "reasons": reasons,
        "snapshot": {
            "memAvailableBytes": available,
            "memTotalBytes": total,
            "memAvailableRatio": available_ratio,
            "swapDelta60SecondsBytes": swap_delta_60_bytes,
            "swapUsedBytes": swap_used,
            "currentSwapoutDeltaPages": current_swapout_delta,
            "priorSwapoutDeltasPages": list(prior_swapout_deltas),
            "threeConsecutiveSwapoutGrowth": three_consecutive_swapout_growth,
            "qfRssBytes": qf_rss_bytes,
            "psiMemorySomeAvg10": pressure["some"],
            "psiMemoryFullAvg10": pressure["full"],
            "oomKillDelta": oom_delta,
            "vmstatIntervalSeconds": interval_seconds,
            "processRegistry": registry,
        },
    }


def read_prewrite_resource_gate(
    *,
    previous_vmstat_text: str,
    interval_seconds: float,
    qf_rss_bytes: int | None = None,
    prior_swapout_deltas: Sequence[int] = (),
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Read only the governed /proc files and an already-validated local registry."""

    registry_path = project_root / ".runtime" / "processes.json"
    if not registry_path.is_file():
        raise Qas30ExperimentError(
            "governed resource gate requires .runtime/processes.json"
        )
    registry_text = registry_path.read_text(encoding="utf-8")
    return evaluate_resource_gate(
        meminfo_text=Path("/proc/meminfo").read_text(encoding="utf-8"),
        pressure_text=Path("/proc/pressure/memory").read_text(encoding="utf-8"),
        vmstat_text=Path("/proc/vmstat").read_text(encoding="utf-8"),
        previous_vmstat_text=previous_vmstat_text,
        interval_seconds=interval_seconds,
        qf_rss_bytes=qf_rss_bytes,
        prior_swapout_deltas=prior_swapout_deltas,
        registry_text=registry_text,
        project_root=project_root,
    )


def require_fresh_green_snapshot(
    snapshot: Mapping[str, Any], *, now: datetime | None = None
) -> None:
    """Require GREEN and an observation no older than 15 seconds."""

    if snapshot.get("level") != "GREEN":
        raise Qas30ExperimentError("pre-write resource gate is not GREEN")
    age = ((now or _now()) - _parse_timestamp(snapshot.get("observedAt"))).total_seconds()
    if age < -1.0 or age > 15.0:
        raise Qas30ExperimentError("pre-write resource snapshot is not within 15 seconds")


def require_execution_authorization(
    artifact: Mapping[str, Any],
    *,
    run_id: str,
    source_commit_sha: str,
    action: str,
    now: datetime | None = None,
) -> str:
    """Validate an explicit detached runtime authorization artifact."""

    required = {
        "schemaVersion",
        "authorizationId",
        "runId",
        "target",
        "allowedActions",
        "authorized",
        "issuedAt",
        "expiresAt",
        "sourceCommitSha",
    }
    if set(artifact) != required:
        raise Qas30ExperimentError("runtime authorization fields are invalid")
    if artifact.get("schemaVersion") != "qf.qas30.execution-authorization.v1":
        raise Qas30ExperimentError("runtime authorization schema is invalid")
    if artifact.get("authorized") is not True or artifact.get("runId") != run_id:
        raise Qas30ExperimentError("runtime authorization identity is invalid")
    if artifact.get("target") != qas30_protocol.TARGET or action not in LIVE_ACTIONS:
        raise Qas30ExperimentError("runtime authorization target/action is invalid")
    actions = artifact.get("allowedActions")
    if not isinstance(actions, list) or action not in actions:
        raise Qas30ExperimentError("runtime authorization does not permit this action")
    if (
        not _valid_source_commit(source_commit_sha)
        or artifact.get("sourceCommitSha") != source_commit_sha
    ):
        raise Qas30ExperimentError(
            "runtime authorization source commit does not match the frozen run"
        )
    current = now or _now()
    if current < _parse_timestamp(artifact["issuedAt"]) or current > _parse_timestamp(
        artifact["expiresAt"]
    ):
        raise Qas30ExperimentError("runtime authorization is outside its time window")
    return _sha256(dict(artifact))


def prepare(
    *,
    run_directory: Path,
    run_id: str,
    source_commit_sha: str,
    parent_manifest_bytes: bytes,
    parent_canonical_ir_bytes: bytes,
    market_data_sha256: str,
) -> dict[str, Any]:
    """Materialize only audited local inputs; no K snapshot or Query ID is created."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", run_id):
        raise Qas30ExperimentError("runId is invalid")
    if not _valid_source_commit(source_commit_sha):
        raise Qas30ExperimentError("source commit is invalid")
    bundle = qas30_protocol.production_candidate_bundle(
        parent_manifest_bytes=parent_manifest_bytes,
        parent_canonical_ir_bytes=parent_canonical_ir_bytes,
        market_data_sha256=market_data_sha256,
    )
    run_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_manifest_path = run_directory / "parent-task-manifest.json"
    parent_ir_path = run_directory / "parent-canonical-ir.json"
    _write_bytes_once(parent_manifest_path, parent_manifest_bytes)
    _write_bytes_once(parent_ir_path, parent_canonical_ir_bytes)
    bundle_path = run_directory / "candidate-bundle.json"
    _write_json_once(bundle_path, bundle)
    manifest = {
        "schemaVersion": "qf.qas30.prepare.v1",
        "runId": run_id,
        "sourceCommitSha": source_commit_sha,
        "state": "LOCAL_INPUTS_READY",
        "scientificStatus": "DESIGN_ONLY",
        "candidateBundleSha256": bundle["bundleSha256"],
        "candidateBundleFileSha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
        "parentTaskManifestFileSha256": hashlib.sha256(parent_manifest_bytes).hexdigest(),
        "parentCanonicalIrFileSha256": hashlib.sha256(parent_canonical_ir_bytes).hexdigest(),
        "marketDataSha256": market_data_sha256,
        "parentBindings": bundle["parentBindings"],
        "candidateCount": 30,
        "canonicalIrCount": 30,
        "qcisCount": 30,
        "externalRequestsIssued": 0,
    }
    _write_json_once(run_directory / "prepare.json", manifest)
    return manifest


def freeze(
    *,
    run_directory: Path,
    data_epoch: str,
    pre_result_normalization_rows: Sequence[Mapping[str, float]],
    local_feature_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze local QAS30 inputs and stop at the pre-live READY_FOR_K0 gate."""

    prepared = _read_json(run_directory / "prepare.json")
    if prepared.get("state") != "LOCAL_INPUTS_READY":
        raise Qas30ExperimentError("prepare state is not LOCAL_INPUTS_READY")
    if not isinstance(data_epoch, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", data_epoch
    ):
        raise Qas30ExperimentError("dataEpoch is invalid")
    bundle = _read_json(run_directory / "candidate-bundle.json")
    bundle_path = run_directory / "candidate-bundle.json"
    parent_manifest_path = run_directory / "parent-task-manifest.json"
    parent_ir_path = run_directory / "parent-canonical-ir.json"
    parent_manifest_bytes = parent_manifest_path.read_bytes()
    parent_ir_bytes = parent_ir_path.read_bytes()
    if (
        hashlib.sha256(bundle_path.read_bytes()).hexdigest()
        != prepared.get("candidateBundleFileSha256")
        or hashlib.sha256(parent_manifest_bytes).hexdigest()
        != prepared.get("parentTaskManifestFileSha256")
        or hashlib.sha256(parent_ir_bytes).hexdigest()
        != prepared.get("parentCanonicalIrFileSha256")
    ):
        raise Qas30ExperimentError("prepare source artifact hash changed")
    try:
        verified_bundle = qas30_protocol.validate_production_candidate_bundle(
            bundle,
            parent_manifest_bytes=parent_manifest_bytes,
            parent_canonical_ir_bytes=parent_ir_bytes,
            market_data_sha256=str(prepared.get("marketDataSha256")),
        )
    except qas30_protocol.Qas30ProtocolError as error:
        raise Qas30ExperimentError("candidate bundle revalidation failed") from error
    stored_bundle_sha = verified_bundle["bundleSha256"]
    if stored_bundle_sha != prepared.get("candidateBundleSha256"):
        raise Qas30ExperimentError("prepare candidate bundle binding changed")
    normalization = qas30_protocol.freeze_normalization_bounds(
        pre_result_normalization_rows
    )
    pre_hardware_feature_freeze = (
        qas30_protocol.freeze_pre_hardware_predictor_features(
            source_commit=str(prepared["sourceCommitSha"]),
            local_feature_evidence=local_feature_evidence,
            production_bundle=verified_bundle,
        )
    )
    readiness = qas30_protocol.ready_for_k0_manifest(
        run_id=str(prepared["runId"]),
        data_epoch=data_epoch,
        source_commit=str(prepared["sourceCommitSha"]),
        production_bundle=verified_bundle,
        parent_manifest_bytes=parent_manifest_bytes,
        parent_canonical_ir_bytes=parent_ir_bytes,
        market_data_sha256=str(prepared["marketDataSha256"]),
        pre_hardware_feature_freeze=pre_hardware_feature_freeze,
    )
    analysis_freeze = qas30_protocol.confirmatory_analysis_freeze(
        run_id=str(prepared["runId"])
    )
    blind_manifest, sealed_mapping = qas30_protocol.blind_label_artifacts(
        run_id=str(prepared["runId"])
    )
    _write_json_once(run_directory / "blind-analysis-manifest.json", blind_manifest)
    _write_json_once(
        run_directory / "sealed" / "blind-arm-mapping.json", sealed_mapping
    )
    _write_json_once(
        run_directory / "sealed" / "blind-comparison-assignment.json",
        sealed_mapping["blindComparisonAssignment"],
    )
    _write_json_once(
        run_directory / "pre-hardware-feature-freeze.json",
        pre_hardware_feature_freeze,
    )
    _write_json_once(
        run_directory / "local-feature-evidence.json",
        local_feature_evidence,
    )
    manifest = {
        "schemaVersion": "qf.qas30.freeze.v1",
        "runId": prepared["runId"],
        "dataEpoch": data_epoch,
        "sourceCommitSha": prepared["sourceCommitSha"],
        "state": "READY_FOR_K0",
        "scientificStatus": "DESIGN_ONLY",
        "candidateBundleSha256": stored_bundle_sha,
        "readiness": readiness,
        "normalization": normalization,
        "preHardwareFeatureFreeze": pre_hardware_feature_freeze,
        "preHardwareFeatureFreezeSha256": pre_hardware_feature_freeze[
            "preHardwareFeatureFreezeSha256"
        ],
        "localFeatureEvidenceSha256": pre_hardware_feature_freeze[
            "localFeatureEvidenceSha256"
        ],
        "localFeatureDefinition": pre_hardware_feature_freeze[
            "localFeatureDefinition"
        ],
        "analysisFreeze": analysis_freeze,
        "blindAnalysisManifest": blind_manifest,
        "sealedArmMappingSha256": sealed_mapping["sealedMappingSha256"],
        "blockPlan": readiness["firstBlock"],
        "calibrations": {"K0": None, "K1": None, "K2": None},
        "queryIds": [],
        "externalRequestsIssued": 0,
    }
    manifest["freezeManifestSha256"] = _sha256(manifest)
    _write_json_once(run_directory / "freeze.json", manifest)
    _transition_protocol_state(run_directory, "READY_FOR_K0")
    r1_selection = {
        "schemaVersion": "qf.qas30.r1-selection.v1",
        "runId": prepared["runId"],
        "dataEpoch": data_epoch,
        "freezeManifestSha256": manifest["freezeManifestSha256"],
        "sourceCommitSha": prepared["sourceCommitSha"],
        "candidateBundleSha256": stored_bundle_sha,
        "blockId": readiness["firstBlock"]["blockId"],
        "batches": readiness["firstBlock"]["batches"][:2],
    }
    r1_selection["r1SelectionSha256"] = _sha256(r1_selection)
    _write_json_once(run_directory / "r1-selection.json", r1_selection)
    return manifest


def persist_predictor_feature_freeze(
    *,
    run_directory: Path,
    predictor_feature_freeze: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist a K0/K1 selection freeze under its explicit calibration label."""

    frozen = _read_frozen_manifest(run_directory)
    k0 = _read_json(run_directory / "calibrations" / "K0.json")
    predictor_value = dict(predictor_feature_freeze)
    stored_predictor = predictor_value.get("predictorFeatureFreezeSha256")
    predictor_unhashed = {
        key: value
        for key, value in predictor_value.items()
        if key != "predictorFeatureFreezeSha256"
    }
    if (
        stored_predictor != _sha256(predictor_unhashed)
        or predictor_value.get("sourceCommitSha") != frozen.get("sourceCommitSha")
        or predictor_value.get("dataEpoch") != frozen.get("dataEpoch")
        or predictor_value.get("freezeManifestSha256")
        != frozen.get("freezeManifestSha256")
        or predictor_value.get("normalizationSha256")
        != frozen.get("normalization", {}).get("normalizationSha256")
        or predictor_value.get("preHardwareFeatureFreezeSha256")
        != frozen.get("preHardwareFeatureFreezeSha256")
        or not isinstance(k0.get("snapshot"), Mapping)
        or predictor_value.get("k0ConfigSha256")
        != k0["snapshot"].get("machineConfigSha256")
    ):
        raise Qas30ExperimentError("predictor feature freeze does not bind K0 and run")
    calibration_sha = predictor_value.get("predictorCalibrationSha256")
    label = predictor_value.get("predictorCalibrationLabel")
    if label not in {"K0", "K1"}:
        raise Qas30ExperimentError("predictor feature freeze lacks a K0/K1 label")
    if label == "K1":
        _read_valid_k1_drift_receipt(run_directory=run_directory, frozen=frozen)
        k1 = _read_json(run_directory / "calibrations" / "K1.json")
        if calibration_sha != k1.get("activeCalibrationSha256"):
            raise Qas30ExperimentError("K1 predictor freeze does not bind K1")
    _write_json_once(
        run_directory / f"predictor-feature-freeze-{label}.json", predictor_value
    )
    # Transitional K0 alias retains exactly-once compatibility for existing
    # runs; new R2 selection never reads it as the K1 prediction entity.
    if label == "K0":
        _write_json_once(run_directory / "predictor-feature-freeze.json", predictor_value)
    return predictor_value


def _read_valid_k1_drift_receipt(
    *, run_directory: Path, frozen: Mapping[str, Any]
) -> dict[str, Any]:
    from . import qas30_drift

    path = run_directory / "drift" / "K0_K1.json"
    if not path.is_file():
        raise Qas30ExperimentError("R2 requires the persisted K0-to-K1 drift receipt")
    receipt = _read_json(path)
    k0 = _read_json(run_directory / "calibrations" / "K0.json")
    k1_path = run_directory / "calibrations" / "K1.json"
    if not k1_path.is_file():
        raise Qas30ExperimentError("R2 selection requires the bound K1 capture")
    k1 = _read_json(k1_path)
    try:
        receipt = qas30_drift.require_r2_write_allowed(
            receipt,
            run_id=str(frozen["runId"]),
            data_epoch=str(frozen["dataEpoch"]),
            source_commit_sha=str(frozen["sourceCommitSha"]),
            freeze_manifest_sha256=str(frozen["freezeManifestSha256"]),
        )
    except (KeyError, qas30_drift.Qas30DriftError) as error:
        raise Qas30ExperimentError("K1 drift receipt does not permit R2") from error
    if (
        receipt.get("previousCalibrationReceiptSha256") != _sha256(k0)
        or receipt.get("currentCalibrationReceiptSha256") != _sha256(k1)
        or receipt.get("transition") != "K0_TO_K1"
        or receipt.get("decision") != "WRITE_ALLOWED"
        or receipt.get("newSubmitAllowed") is not True
    ):
        raise Qas30ExperimentError("K1 drift receipt does not permit R2")
    return receipt


def persist_r1_score_receipt_set(run_directory: Path) -> dict[str, Any]:
    """Derive the ten R1 hardware scores only from completed bound observations."""

    from . import qas30_scoring

    frozen = _read_frozen_manifest(run_directory)
    _require_terminal_batches(
        run_directory=run_directory,
        frozen=frozen,
        batch_count=2,
        gate_name="R1 scoring",
    )
    selection = _read_json(run_directory / "r1-selection.json")
    batches = selection.get("batches")
    if not isinstance(batches, list) or len(batches) != 2:
        raise Qas30ExperimentError("R1 scoring requires the frozen two-batch selection")
    cover_candidate_ids = [
        candidate_id
        for batch in batches
        if isinstance(batch, Mapping)
        for candidate_id in batch.get("candidateIds", [])
        if isinstance(candidate_id, str)
    ]
    if len(cover_candidate_ids) != 10 or len(set(cover_candidate_ids)) != 10:
        raise Qas30ExperimentError("R1 scoring candidate order is invalid")

    score_receipts: list[dict[str, Any]] = []
    for batch_index in (1, 2):
        live_batch = _read_json(
            run_directory / "live-batches" / f"B{batch_index}.json"
        )
        submit_receipt = _read_json(
            run_directory / "submissions" / f"B{batch_index}.json"
        )
        bindings = submit_receipt.get("queryBindings")
        if not isinstance(bindings, list) or len(bindings) != 5:
            raise Qas30ExperimentError("R1 scoring query bindings are incomplete")
        for binding in bindings:
            if not isinstance(binding, Mapping):
                raise Qas30ExperimentError("R1 scoring query binding is invalid")
            query_id = binding.get("queryId")
            candidate_id = binding.get("candidateId")
            completed: list[dict[str, Any]] = []
            if not isinstance(query_id, str) or not isinstance(candidate_id, str):
                raise Qas30ExperimentError("R1 scoring query identity is invalid")
            for path in sorted(
                (run_directory / "queries").glob(f"{query_id}-*.json")
            ):
                observation = _read_json(path)
                if observation.get("state") != "COMPLETED":
                    continue
                try:
                    qas30_tianyan.validate_query_observation_against_batch(
                        observation,
                        live_batch=live_batch,
                        submit_receipt=submit_receipt,
                    )
                except qas30_tianyan.Qas30TianyanError as error:
                    raise Qas30ExperimentError(
                        "R1 scoring found an invalid completed observation"
                    ) from error
                completed.append(observation)
            if len(completed) != 1:
                raise Qas30ExperimentError(
                    "R1 scoring requires exactly one completed observation per Query ID"
                )
            try:
                score = qas30_scoring.build_hardware_score_receipt(
                    live_batch=live_batch,
                    submit_receipt=submit_receipt,
                    query_observation=completed[0],
                    candidate_id=candidate_id,
                )
            except qas30_scoring.Qas30ScoringError as error:
                raise Qas30ExperimentError("R1 hardware score derivation failed") from error
            _write_json_once(
                run_directory / "scores" / "R1" / f"{candidate_id}.json",
                score,
            )
            score_receipts.append(score)
    try:
        score_set = qas30_scoring.build_r1_score_receipt_set(
            score_receipts=score_receipts,
            cover_candidate_ids=cover_candidate_ids,
        )
    except qas30_scoring.Qas30ScoringError as error:
        raise Qas30ExperimentError("R1 hardware score set derivation failed") from error
    _write_json_once(run_directory / "r1-score-receipt-set.json", score_set)
    return score_set


def persist_r2_selection(
    *,
    run_directory: Path,
    r1_score_receipt_set: Mapping[str, Any],
    llm_candidate_ids: Sequence[str],
    llm_selection_receipt_sha256: str,
) -> dict[str, Any]:
    """Derive and durably freeze all four R2 arms from governed R1 evidence."""

    frozen = _read_frozen_manifest(run_directory)
    _require_terminal_batches(
        run_directory=run_directory,
        frozen=frozen,
        batch_count=2,
        gate_name="R2 selection",
    )
    persisted_score_set = _read_json(run_directory / "r1-score-receipt-set.json")
    if persisted_score_set != dict(r1_score_receipt_set):
        raise Qas30ExperimentError(
            "R2 selection score input does not match the derived R1 score set"
        )
    k1 = _read_json(run_directory / "calibrations" / "K1.json")
    if (
        k1.get("calibrationLabel") != "K1"
        or k1.get("runId") != frozen.get("runId")
        or k1.get("dataEpoch") != frozen.get("dataEpoch")
        or k1.get("sourceCommitSha") != frozen.get("sourceCommitSha")
        or k1.get("freezeManifestSha256") != frozen.get("freezeManifestSha256")
        or not isinstance(k1.get("activeCalibrationSha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", k1["activeCalibrationSha256"])
    ):
        raise Qas30ExperimentError("R2 selection requires the bound K1 capture")
    drift_receipt = _read_valid_k1_drift_receipt(
        run_directory=run_directory, frozen=frozen
    )
    training_predictor = _read_json(
        run_directory / "predictor-feature-freeze-K0.json"
    )
    prediction_predictor = _read_json(
        run_directory / "predictor-feature-freeze-K1.json"
    )
    stored_predictor = prediction_predictor.get("predictorFeatureFreezeSha256")
    predictor_unhashed = {
        key: value
        for key, value in prediction_predictor.items()
        if key != "predictorFeatureFreezeSha256"
    }
    if (
        stored_predictor != _sha256(predictor_unhashed)
        or prediction_predictor.get("sourceCommitSha") != frozen.get("sourceCommitSha")
        or prediction_predictor.get("dataEpoch") != frozen.get("dataEpoch")
        or prediction_predictor.get("freezeManifestSha256")
        != frozen.get("freezeManifestSha256")
        or prediction_predictor.get("normalizationSha256")
        != frozen.get("normalization", {}).get("normalizationSha256")
    ):
        raise Qas30ExperimentError("predictor feature freeze does not bind the run")
    bundle = _read_json(run_directory / "candidate-bundle.json")
    block_plan = frozen.get("blockPlan")
    cover = frozen.get("readiness", {}).get("cover10")
    if not isinstance(block_plan, Mapping) or not isinstance(cover, Mapping):
        raise Qas30ExperimentError("frozen block plan is incomplete")
    try:
        selection = qas30_protocol.select_r2_arms(
            run_id=str(frozen["runId"]),
            data_epoch=str(frozen["dataEpoch"]),
            freeze_manifest_sha256=str(frozen["freezeManifestSha256"]),
            block_id=str(block_plan["blockId"]),
            candidate_catalog_rows=bundle["candidateCatalog"],
            cover_candidate_ids=cover["candidateIds"],
            predictor_feature_freeze=None,
            training_predictor_feature_freeze=training_predictor,
            prediction_predictor_feature_freeze=prediction_predictor,
            normalization_manifest=frozen["normalization"],
            r1_score_receipt_set=r1_score_receipt_set,
            llm_candidate_ids=llm_candidate_ids,
            llm_selection_receipt_sha256=llm_selection_receipt_sha256,
        )
        selection.pop("r2SelectionSha256", None)
        selection.update(
            {
                "k1CalibrationReceiptSha256": drift_receipt[
                    "currentCalibrationReceiptSha256"
                ],
                "k1DriftReceiptSha256": drift_receipt["driftReceiptSha256"],
                "mappingSnapshotSha256": training_predictor[
                    "selectionFeatureSnapshotSha256"
                ],
            }
        )
        selection["r2SelectionSha256"] = _sha256(selection)
        qas30_protocol.r2_batches(selection)
    except (KeyError, qas30_protocol.Qas30ProtocolError) as error:
        raise Qas30ExperimentError("R2 selection evidence is invalid") from error
    _write_json_once(run_directory / "r2-selection.json", selection)
    return selection


def capture_k(
    *,
    run_directory: Path,
    calibration_label: str,
    authorization: Mapping[str, Any],
    resource_snapshot: Mapping[str, Any],
    capture_once: Callable[[], Mapping[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Capture one real K snapshot through an injected one-request boundary."""

    current = now or _now()
    frozen = _read_frozen_manifest(run_directory)
    if calibration_label not in {"K0", "K1", "K2"}:
        raise Qas30ExperimentError("calibration label must be K0/K1/K2")
    expected = ("K0", "K1", "K2")
    completed = [
        label
        for label in expected
        if (run_directory / "calibrations" / f"{label}.json").is_file()
    ]
    if (
        completed != list(expected[: len(completed)])
        or len(completed) == len(expected)
        or calibration_label != expected[len(completed)]
    ):
        raise Qas30ExperimentError("calibration sequence is not K0/K1/K2")
    required_terminal_batches = {"K0": 0, "K1": 2, "K2": 6}[calibration_label]
    _require_terminal_batches(
        run_directory=run_directory,
        frozen=frozen,
        batch_count=required_terminal_batches,
        gate_name=calibration_label,
    )
    authorization_envelope_sha256 = require_execution_authorization(
        authorization,
        run_id=str(frozen["runId"]),
        source_commit_sha=str(frozen["sourceCommitSha"]),
        action="CAPTURE_K",
        now=current,
    )
    require_fresh_green_snapshot(resource_snapshot, now=current)
    resource_sample_sha256 = _sha256(dict(resource_snapshot))
    receipt_path = run_directory / "calibrations" / f"{calibration_label}.json"
    _write_json_once(
        receipt_path.with_name(f"{receipt_path.name}.intent"),
        {
            "schemaVersion": "qf.qas30.calibration-intent.v1",
            "runId": frozen["runId"],
            "dataEpoch": frozen["dataEpoch"],
            "freezeManifestSha256": frozen["freezeManifestSha256"],
            "calibrationLabel": calibration_label,
            "authorizationEnvelopeSha256": authorization_envelope_sha256,
            "resourceSampleSha256": resource_sample_sha256,
            "claimedAt": _iso(current),
        },
    )
    captured = capture_once()
    if not isinstance(captured, Mapping):
        raise Qas30ExperimentError("calibration capture returned no snapshot object")
    if captured.get("schemaVersion") == "qf.qas30.calibration-receipt.v1":
        try:
            receipt = qas30_drift.validate_calibration_receipt(captured)
        except qas30_drift.Qas30DriftError as error:
            raise Qas30ExperimentError(
                "complete calibration receipt failed drift validation"
            ) from error
        snapshot_value = receipt["snapshot"]
        if (
            receipt.get("runId") != frozen.get("runId")
            or receipt.get("dataEpoch") != frozen.get("dataEpoch")
            or receipt.get("sourceCommitSha") != frozen.get("sourceCommitSha")
            or receipt.get("freezeManifestSha256")
            != frozen.get("freezeManifestSha256")
            or receipt.get("authorizationEnvelopeSha256")
            != authorization_envelope_sha256
            or receipt.get("resourceSampleSha256") != resource_sample_sha256
            or receipt.get("calibrationLabel") != calibration_label
            or receipt.get("target") != qas30_protocol.TARGET
            or snapshot_value.get("target") != qas30_protocol.TARGET
            or snapshot_value.get("providerMachineId") != qas30_protocol.TARGET
        ):
            raise Qas30ExperimentError(
                "complete calibration receipt identity or machine changed"
            )
        try:
            captured_at = _parse_timestamp(receipt.get("capturedAt"))
        except (TypeError, ValueError) as error:
            raise Qas30ExperimentError(
                "complete calibration receipt timestamp is invalid"
            ) from error
        if captured_at > current + timedelta(seconds=1):
            raise Qas30ExperimentError("calibration capturedAt is in the future")
    else:
        # Raw snapshots are retained solely for the explicit offline protocol
        # fixture CLI path.  They are not an alternative to the complete
        # qas30_observation calibration receipt for real observations.
        snapshot = captured
        if (
            snapshot.get("schemaVersion") != "qf.qas30.calibration-snapshot.v1"
            or snapshot.get("calibrationLabel") != calibration_label
            or snapshot.get("target") != qas30_protocol.TARGET
            or not isinstance(snapshot.get("providerMachineId"), str)
            or not snapshot.get("providerMachineId")
            or not isinstance(snapshot.get("capabilities"), Mapping)
            or not isinstance(snapshot.get("topology"), Mapping)
            or snapshot.get("scientificStatus")
            != qas30_tianyan.FIXTURE_SCIENTIFIC_STATUS
        ):
            raise Qas30ExperimentError(
                "raw calibration snapshot fallback requires protocol fixture status"
            )
        try:
            captured_at = _parse_timestamp(snapshot.get("capturedAt"))
        except (TypeError, ValueError) as error:
            raise Qas30ExperimentError("calibration capturedAt is invalid") from error
        if captured_at > current + timedelta(seconds=1):
            raise Qas30ExperimentError("calibration capturedAt is in the future")
        snapshot_value = dict(snapshot)
        receipt = {
            "schemaVersion": "qf.qas30.calibration-receipt.v1",
            "runId": frozen["runId"],
            "dataEpoch": frozen["dataEpoch"],
            "calibrationLabel": calibration_label,
            "target": qas30_protocol.TARGET,
            "sourceCommitSha": frozen["sourceCommitSha"],
            "freezeManifestSha256": frozen["freezeManifestSha256"],
            "authorizationEnvelopeSha256": authorization_envelope_sha256,
            "resourceSampleSha256": resource_sample_sha256,
            "snapshot": snapshot_value,
            "activeCalibrationSha256": _sha256(snapshot_value),
            "transportRequestCount": 1,
            "capturedAt": _iso(captured_at),
            "scientificStatus": snapshot["scientificStatus"],
        }
    _write_json_once(receipt_path, receipt)
    requested_state = {
        "K0": "K0_CAPTURED",
        "K1": "CALIBRATION_CAPTURED",
        "K2": "CALIBRATION_CAPTURED",
    }[calibration_label]
    if calibration_label in {"K1", "K2"}:
        _transition_protocol_state(run_directory, "RESULTS_OBSERVED")
        _transition_protocol_state(run_directory, "READY_FOR_NEXT_CALIBRATION")
    _transition_protocol_state(run_directory, requested_state)
    if calibration_label == "K2":
        _transition_protocol_state(run_directory, "EXPERIMENT_COMPLETE")
    return receipt


def query_due_state(
    *, submitted_at: datetime, observation_times: Sequence[datetime], now: datetime
) -> dict[str, Any]:
    """Return 60s/120s/300s due state, 24h recoverability, and seven-day stop."""

    age = (now - submitted_at).total_seconds()
    if age < 0:
        raise Qas30ExperimentError("query clock precedes submission")
    if age >= 7 * 24 * 3600:
        phase = "SEVEN_DAY_WRITE_STOP"
    elif age >= 24 * 3600:
        phase = "RECOVERABLE_QUERY"
    else:
        phase = "FOREGROUND_QUERY"
    intervals = (60, 120)
    attempt_index = len(observation_times)
    delay = intervals[attempt_index] if attempt_index < len(intervals) else 300
    anchor = observation_times[-1] if observation_times else submitted_at
    due_at = anchor.timestamp() + delay
    return {
        "schemaVersion": "qf.qas30.query-due.v1",
        "phase": phase,
        "queryExistingIdsOnly": True,
        "resubmitAllowed": False,
        "nextDelaySeconds": delay,
        "due": now.timestamp() >= due_at and phase != "SEVEN_DAY_WRITE_STOP",
        "dueAt": _iso(datetime.fromtimestamp(due_at, UTC)),
    }


def _continuation_observation_times(
    observation_values: Sequence[Mapping[str, Any]],
    *,
    reauthenticated_existing_id_session: bool = False,
) -> list[datetime]:
    """Return poll timestamps only while an existing ID remains queryable."""

    times: list[datetime] = []
    token_expired_count = 0
    for observation in observation_values:
        times.append(_parse_timestamp(observation.get("observedAt")))
        state = observation.get("state")
        if state == "COMPLETED":
            raise Qas30ExperimentError("query ID already has a completed observation")
        if state == "TOKEN_EXPIRED":
            token_expired_count += 1
            continue
        if observation.get("queryAgainAllowed") is False:
            raise Qas30ExperimentError("query ID has a sticky terminal observation")
    if token_expired_count > 1:
        raise Qas30ExperimentError("query ID exhausted its one reauthentication path")
    if token_expired_count == 1 and not reauthenticated_existing_id_session:
        raise Qas30ExperimentError(
            "query ID has a token-expired observation and is sticky after recovery"
        )
    return times


def governed_login_once(
    connection_key: str,
    *,
    run_id: str,
    source_commit_sha: str,
    authorization: Mapping[str, Any],
    resource_snapshot: Mapping[str, Any],
    transport: qas30_tianyan.SingleAttemptTransport,
    token_state: qas30_tianyan.InMemoryTokenState,
    receipt_path: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Authorize and resource-gate one login; retain its token in memory only."""

    current = now or _now()
    require_execution_authorization(
        authorization,
        run_id=run_id,
        source_commit_sha=source_commit_sha,
        action="LOGIN",
        now=current,
    )
    require_fresh_green_snapshot(resource_snapshot, now=current)
    return qas30_tianyan.login_once(
        connection_key,
        transport=transport,
        token_state=token_state,
        receipt_path=receipt_path,
    )


def build_live_batch(
    *,
    run_id: str,
    data_epoch: str,
    freeze_manifest_sha256: str,
    source_commit_sha: str,
    block_batch: Mapping[str, Any],
    stage: str,
    scientific_status: str,
    calibration_sha256: str,
    k0_config_sha256: str | None = None,
    mapping_snapshot_sha256: str,
    compilation_calibration_sha256: str | None = None,
    selection_feature_snapshot_sha256: str | None = None,
    normalization_manifest: Mapping[str, Any],
    predictor_feature_freeze_sha256: str,
    selection_entity_sha256: str,
    authorization_envelope_sha256: str,
    resource_sample_sha256: str,
    common_measurement_order: list[str],
    circuits: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a live batch with an explicit active calibration binding."""

    circuit_rows = [dict(item) for item in circuits]
    feature_receipt_bindings = [
        {
            "candidateId": row.get("candidateId"),
            "predictorFeatureReceiptSha256": row.get(
                "predictorFeatureReceiptSha256"
            ),
        }
        for row in circuit_rows
    ]
    batch = {
        "schemaVersion": qas30_tianyan.LIVE_BATCH_SCHEMA_VERSION,
        "protocolVersion": qas30_protocol.PROTOCOL_SCHEMA_VERSION,
        "runId": run_id,
        "dataEpoch": data_epoch,
        "freezeManifestSha256": freeze_manifest_sha256,
        "blockId": block_batch["blockId"],
        "blockBatchIndex": block_batch["batchIndex"],
        "batchId": block_batch["batchId"],
        "stage": stage,
        "target": qas30_protocol.TARGET,
        "shots": qas30_protocol.SHOTS_PER_CIRCUIT,
        "state": "READY_FOR_FIRST_SUBMIT",
        "scientificStatus": scientific_status,
        "sourceCommitSha": source_commit_sha,
        "k0ConfigSha256": k0_config_sha256 or calibration_sha256,
        "activeCalibrationSha256": calibration_sha256,
        "compilationCalibrationSha256": compilation_calibration_sha256
        or calibration_sha256,
        "mappingSnapshotSha256": mapping_snapshot_sha256,
        "selectionFeatureSnapshotSha256": selection_feature_snapshot_sha256
        or mapping_snapshot_sha256,
        "normalizationManifest": dict(normalization_manifest),
        "normalizationSha256": normalization_manifest.get("normalizationSha256"),
        "predictorFeatureFreezeSha256": predictor_feature_freeze_sha256,
        "predictorFeatureSetSha256": _sha256(feature_receipt_bindings),
        "selectionEntitySha256": selection_entity_sha256,
        "authorizationEnvelopeSha256": authorization_envelope_sha256,
        "resourceSampleSha256": resource_sample_sha256,
        "commonMeasurementPhysicalOrder": common_measurement_order,
        "candidateIds": list(block_batch["candidateIds"]),
        "circuits": circuit_rows,
    }
    try:
        return qas30_tianyan.validate_live_batch(batch)
    except qas30_tianyan.Qas30TianyanError as error:
        raise Qas30ExperimentError("live batch receipts are incomplete or unbound") from error


def governed_submit_next(
    batch: dict[str, Any],
    *,
    authorization: Mapping[str, Any],
    resource_snapshot: Mapping[str, Any],
    access_token: str,
    transport: qas30_tianyan.SingleAttemptTransport,
    receipt_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Submit once only after current authorization and resource gates pass."""

    current = now or _now()
    authorization_envelope_sha256 = require_execution_authorization(
        authorization,
        run_id=batch["runId"],
        source_commit_sha=batch["sourceCommitSha"],
        action="SUBMIT",
        now=current,
    )
    require_fresh_green_snapshot(resource_snapshot, now=current)
    resource_sample_sha256 = _sha256(dict(resource_snapshot))
    if "activeCalibrationSha256" not in batch:
        raise Qas30ExperimentError("live batch lacks activeCalibrationSha256")
    if (
        batch.get("authorizationEnvelopeSha256")
        != authorization_envelope_sha256
        or batch.get("resourceSampleSha256") != resource_sample_sha256
    ):
        raise Qas30ExperimentError(
            "live batch does not bind the current authorization and resource sample"
        )
    return qas30_tianyan.submit_batch_once(
        batch,
        access_token=access_token,
        transport=transport,
        receipt_path=receipt_path,
        authorization_envelope_sha256=authorization_envelope_sha256,
        resource_sample_sha256=resource_sample_sha256,
    )


def governed_query_due(
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
    authorization: Mapping[str, Any],
    resource_snapshot: Mapping[str, Any],
    access_token: str,
    transport: qas30_tianyan.SingleAttemptTransport,
    observation_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    authorization_envelope_sha256 = require_execution_authorization(
        authorization,
        run_id=run_id,
        source_commit_sha=source_commit_sha,
        action="QUERY",
        now=current,
    )
    require_fresh_green_snapshot(resource_snapshot, now=current)
    resource_sample_sha256 = _sha256(dict(resource_snapshot))
    return qas30_tianyan.query_once(
        run_id=run_id,
        data_epoch=data_epoch,
        freeze_manifest_sha256=freeze_manifest_sha256,
        source_commit_sha=source_commit_sha,
        batch_id=batch_id,
        candidate_id=candidate_id,
        query_id=query_id,
        live_batch_sha256=live_batch_sha256,
        batch_submit_receipt_sha256=batch_submit_receipt_sha256,
        qcis_sha256=qcis_sha256,
        measurement_order=measurement_order,
        predictor_feature_freeze_sha256=predictor_feature_freeze_sha256,
        selection_entity_sha256=selection_entity_sha256,
        authorization_envelope_sha256=authorization_envelope_sha256,
        resource_sample_sha256=resource_sample_sha256,
        access_token=access_token,
        transport=transport,
        observation_path=observation_path,
    )


def _terminal_query_ids(run_directory: Path, receipt: Mapping[str, Any]) -> set[str]:
    matching_batches: list[dict[str, Any]] = []
    for path in (run_directory / "live-batches").glob("B*.json"):
        batch = _read_json(path)
        if (
            batch.get("batchId") == receipt.get("batchId")
            and _sha256(batch) == receipt.get("liveBatchSha256")
        ):
            matching_batches.append(batch)
    if len(matching_batches) != 1:
        return set()
    live_batch = matching_batches[0]
    observed: set[str] = set()
    for path in (run_directory / "queries").glob("*.json"):
        value = _read_json(path)
        try:
            qas30_tianyan.validate_query_observation_against_batch(
                value,
                live_batch=live_batch,
                submit_receipt=receipt,
            )
        except qas30_tianyan.Qas30TianyanError:
            continue
        if isinstance(value.get("queryId"), str):
            observed.add(value["queryId"])
    bindings = receipt.get("queryBindings")
    if not isinstance(bindings, list):
        return set()
    expected = {
        row["queryId"]
        for row in bindings
        if isinstance(row, dict) and isinstance(row.get("queryId"), str)
    }
    return expected & observed


def _require_terminal_batches(
    *,
    run_directory: Path,
    frozen: Mapping[str, Any],
    batch_count: int,
    gate_name: str,
) -> None:
    for batch_index in range(1, batch_count + 1):
        path = run_directory / "submissions" / f"B{batch_index}.json"
        if not path.is_file():
            raise Qas30ExperimentError(
                f"{gate_name} requires batch {batch_index} terminal"
            )
        receipt = _read_json(path)
        bindings = receipt.get("queryBindings")
        query_ids = (
            [row.get("queryId") for row in bindings if isinstance(row, Mapping)]
            if isinstance(bindings, list)
            else []
        )
        if (
            receipt.get("state") != "QUERY_IDS_PERSISTED"
            or receipt.get("runId") != frozen.get("runId")
            or receipt.get("dataEpoch") != frozen.get("dataEpoch")
            or receipt.get("sourceCommitSha") != frozen.get("sourceCommitSha")
            or receipt.get("freezeManifestSha256")
            != frozen.get("freezeManifestSha256")
            or not isinstance(bindings, list)
            or len(bindings) != 5
            or len(query_ids) != 5
            or any(not isinstance(query_id, str) or not query_id for query_id in query_ids)
            or len(set(query_ids)) != 5
            or _terminal_query_ids(run_directory, receipt) != set(query_ids)
        ):
            raise Qas30ExperimentError(
                f"{gate_name} requires batch {batch_index} terminal"
            )


def submit_next(
    batch: dict[str, Any],
    *,
    run_directory: Path,
    authorization: Mapping[str, Any],
    resource_snapshot: Mapping[str, Any],
    access_token: str | Callable[[], str],
    transport: qas30_tianyan.SingleAttemptTransport,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Enforce one ordered nonterminal batch, then delegate to submit-once."""

    frozen = _read_frozen_manifest(run_directory)
    if batch.get("runId") != frozen.get("runId"):
        raise Qas30ExperimentError("batch run identity does not match freeze")
    if (
        batch.get("dataEpoch") != frozen.get("dataEpoch")
        or batch.get("freezeManifestSha256")
        != frozen.get("freezeManifestSha256")
    ):
        raise Qas30ExperimentError("batch dataEpoch or freeze binding changed")
    if batch.get("sourceCommitSha") != frozen.get("sourceCommitSha"):
        raise Qas30ExperimentError("batch source commit does not match freeze")
    if (
        batch.get("normalizationManifest") != frozen.get("normalization")
        or batch.get("normalizationSha256")
        != frozen.get("normalization", {}).get("normalizationSha256")
    ):
        raise Qas30ExperimentError("batch normalization does not match freeze")
    pre_hardware = frozen["preHardwareFeatureFreeze"]
    pre_rows = pre_hardware.get("rows")
    frozen_pre_by_id = {
        row.get("candidateId"): row.get("features")
        for row in pre_rows
        if isinstance(row, Mapping)
    } if isinstance(pre_rows, list) else {}
    circuits = batch.get("circuits")
    if not isinstance(circuits, list) or len(circuits) != qas30_protocol.BATCH_SIZE:
        raise Qas30ExperimentError("batch predictor feature receipts are incomplete")
    for circuit in circuits:
        receipt = (
            circuit.get("predictorFeatureReceipt")
            if isinstance(circuit, Mapping)
            else None
        )
        candidate_id = circuit.get("candidateId") if isinstance(circuit, Mapping) else None
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("preHardwareFeatureFreezeSha256")
            != frozen.get("preHardwareFeatureFreezeSha256")
            or receipt.get("preHardwareFeatures") != frozen_pre_by_id.get(candidate_id)
        ):
            raise Qas30ExperimentError(
                "batch pre-hardware predictor features do not match the pre-K0 freeze"
            )
    receipts = sorted((run_directory / "submissions").glob("B*.json"))
    receipt_indexes = []
    for path in receipts:
        match = re.fullmatch(r"B([1-6])\.json", path.name)
        if match is None:
            raise Qas30ExperimentError("submission receipt path is not a six-block receipt")
        receipt_indexes.append(int(match.group(1)))
    if receipt_indexes != list(range(1, len(receipt_indexes) + 1)):
        raise Qas30ExperimentError("submission receipt continuity is broken")
    if receipts:
        previous = _read_json(receipts[-1])
        expected_bindings = previous.get("queryBindings")
        expected_count = len(expected_bindings) if isinstance(expected_bindings, list) else 0
        if previous.get("state") != "QUERY_IDS_PERSISTED" or len(
            _terminal_query_ids(run_directory, previous)
        ) != expected_count:
            raise Qas30ExperimentError("a previous batch is still nonterminal")
    expected_index = len(receipts) + 1
    if batch.get("blockBatchIndex") != expected_index or expected_index > 6:
        raise Qas30ExperimentError("batch does not match the next six-block position")
    required_predictor_label = "K0" if expected_index <= 2 else "K1"
    predictor_freeze = _read_json(
        run_directory / f"predictor-feature-freeze-{required_predictor_label}.json"
    )
    predictor_stored = predictor_freeze.get("predictorFeatureFreezeSha256")
    predictor_unhashed = {
        key: value
        for key, value in predictor_freeze.items()
        if key != "predictorFeatureFreezeSha256"
    }
    if (
        predictor_stored != _sha256(predictor_unhashed)
        or predictor_freeze.get("sourceCommitSha") != frozen.get("sourceCommitSha")
        or predictor_freeze.get("dataEpoch") != frozen.get("dataEpoch")
        or predictor_freeze.get("freezeManifestSha256")
        != frozen.get("freezeManifestSha256")
        or batch.get("predictorFeatureFreezeSha256") != predictor_stored
    ):
        raise Qas30ExperimentError("batch predictor feature freeze does not bind the run")
    if expected_index <= 2:
        selection = _read_json(run_directory / "r1-selection.json")
        selection_sha256 = selection.get("r1SelectionSha256")
        selection_unhashed = {
            key: value
            for key, value in selection.items()
            if key != "r1SelectionSha256"
        }
        selection_batches = selection.get("batches")
        if (
            selection_sha256 != _sha256(selection_unhashed)
            or not isinstance(selection_batches, list)
            or len(selection_batches) != 2
        ):
            raise Qas30ExperimentError("R1 selection entity is invalid")
        expected_batch = selection_batches[expected_index - 1]
    else:
        drift_receipt = _read_valid_k1_drift_receipt(
            run_directory=run_directory, frozen=frozen
        )
        selection = _read_json(run_directory / "r2-selection.json")
        try:
            selection_batches = qas30_protocol.r2_batches(selection)
        except qas30_protocol.Qas30ProtocolError as error:
            raise Qas30ExperimentError("R2 selection entity is invalid") from error
        if (
            selection.get("k1CalibrationReceiptSha256")
            != drift_receipt.get("currentCalibrationReceiptSha256")
            or selection.get("k1DriftReceiptSha256")
            != drift_receipt.get("driftReceiptSha256")
            or selection.get("mappingSnapshotSha256")
            != batch.get("mappingSnapshotSha256")
            or selection.get("predictionPredictorFeatureFreezeSha256")
            != predictor_stored
            or batch.get("selectionFeatureSnapshotSha256")
            != predictor_freeze.get("selectionFeatureSnapshotSha256")
        ):
            raise Qas30ExperimentError("R2 selection does not bind K1 drift evidence")
        selection_sha256 = selection.get("r2SelectionSha256")
        expected_batch = selection_batches[expected_index - 3]
    if (
        batch.get("selectionEntitySha256") != selection_sha256
        or any(
            batch.get(batch_field) != expected_batch.get(plan_field)
            for batch_field, plan_field in (
                ("blockId", "blockId"),
                ("blockBatchIndex", "batchIndex"),
                ("batchId", "batchId"),
                ("stage", "stage"),
                ("candidateIds", "candidateIds"),
                ("shots", "shots"),
            )
        )
    ):
        raise Qas30ExperimentError("batch identity does not match the frozen selection")
    required_label = "K0" if expected_index <= 2 else "K1"
    transport_status = qas30_tianyan.transport_scientific_status(transport)
    calibration = _read_json(
        run_directory / "calibrations" / f"{required_label}.json"
    )
    if (
        calibration.get("sourceCommitSha") != frozen.get("sourceCommitSha")
        or calibration.get("dataEpoch") != frozen.get("dataEpoch")
        or calibration.get("freezeManifestSha256")
        != frozen.get("freezeManifestSha256")
        or not isinstance(calibration.get("authorizationEnvelopeSha256"), str)
        or not re.fullmatch(
            r"[a-f0-9]{64}", calibration["authorizationEnvelopeSha256"]
        )
        or not isinstance(calibration.get("resourceSampleSha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", calibration["resourceSampleSha256"])
        or calibration.get("scientificStatus") != transport_status
        or not isinstance(calibration.get("snapshot"), Mapping)
        or calibration["snapshot"].get("scientificStatus")
        != transport_status
    ):
        raise Qas30ExperimentError("active calibration does not bind the freeze")
    if batch.get("activeCalibrationSha256") != calibration.get(
        "activeCalibrationSha256"
    ):
        raise Qas30ExperimentError("batch does not bind the active calibration")
    k0 = _read_json(run_directory / "calibrations" / "K0.json")
    if (
        k0.get("sourceCommitSha") != frozen.get("sourceCommitSha")
        or k0.get("dataEpoch") != frozen.get("dataEpoch")
        or k0.get("freezeManifestSha256") != frozen.get("freezeManifestSha256")
        or k0.get("scientificStatus") != transport_status
        or not isinstance(k0.get("snapshot"), Mapping)
        or k0["snapshot"].get("scientificStatus") != transport_status
    ):
        raise Qas30ExperimentError("K0 calibration does not bind the freeze")
    if batch.get("k0ConfigSha256") != k0["snapshot"].get(
        "machineConfigSha256"
    ):
        raise Qas30ExperimentError("batch does not retain the K0 config binding")
    live_batch_path = run_directory / "live-batches" / f"B{expected_index}.json"
    if expected_index in {1, 3}:
        _transition_protocol_state(run_directory, "READY_FOR_FIRST_SUBMIT")
    _write_json_once(live_batch_path, batch)
    receipt_path = run_directory / "submissions" / f"B{expected_index}.json"
    resolved_access_token = access_token() if callable(access_token) else access_token
    receipt = governed_submit_next(
        batch,
        authorization=authorization,
        resource_snapshot=resource_snapshot,
        access_token=resolved_access_token,
        transport=transport,
        receipt_path=receipt_path,
        now=now,
    )
    if expected_index in {1, 3}:
        _transition_protocol_state(run_directory, "QUERY_IDS_PERSISTED", now=now)
    return receipt


def query_due(
    *,
    run_directory: Path,
    batch_index: int,
    query_id: str,
    authorization: Mapping[str, Any],
    resource_snapshot: Mapping[str, Any],
    access_token: str | Callable[[], str],
    transport: qas30_tianyan.SingleAttemptTransport,
    connection_key: str | Callable[[], str] | None = None,
    token_state: qas30_tianyan.InMemoryTokenState | None = None,
    reauth_transport: qas30_tianyan.SingleAttemptTransport | None = None,
    recovery_query_transport: qas30_tianyan.SingleAttemptTransport | None = None,
    reauth_receipt_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Query one persisted ID only when its frozen schedule says it is due."""

    current = now or _now()
    frozen = _read_frozen_manifest(run_directory)
    receipt = _read_json(run_directory / "submissions" / f"B{batch_index}.json")
    receipt_stored = receipt.get("batchSubmitReceiptSha256")
    receipt_unhashed = {
        key: value
        for key, value in receipt.items()
        if key != "batchSubmitReceiptSha256"
    }
    if receipt_stored != _sha256(receipt_unhashed):
        raise Qas30ExperimentError("batch submit receipt hash changed")
    live_batch = _read_json(run_directory / "live-batches" / f"B{batch_index}.json")
    try:
        qas30_tianyan.validate_live_batch(live_batch)
    except qas30_tianyan.Qas30TianyanError as error:
        raise Qas30ExperimentError("persisted live batch is invalid") from error
    live_batch_sha256 = _sha256(live_batch)
    if (
        receipt.get("liveBatchSha256") != live_batch_sha256
        or receipt.get("runId") != frozen.get("runId")
        or receipt.get("sourceCommitSha") != frozen.get("sourceCommitSha")
        or receipt.get("dataEpoch") != frozen.get("dataEpoch")
        or receipt.get("freezeManifestSha256")
        != frozen.get("freezeManifestSha256")
        or receipt.get("runId") != live_batch.get("runId")
        or receipt.get("batchId") != live_batch.get("batchId")
        or receipt.get("dataEpoch") != live_batch.get("dataEpoch")
        or receipt.get("freezeManifestSha256")
        != live_batch.get("freezeManifestSha256")
        or receipt.get("predictorFeatureFreezeSha256")
        != live_batch.get("predictorFeatureFreezeSha256")
        or receipt.get("selectionEntitySha256")
        != live_batch.get("selectionEntitySha256")
    ):
        raise Qas30ExperimentError("submit receipt does not bind the live batch")
    if receipt.get("scientificStatus") != qas30_tianyan.transport_scientific_status(
        transport
    ):
        raise Qas30ExperimentError(
            "query transport evidence class does not match the submit receipt"
        )
    bindings = receipt.get("queryBindings")
    matches = [
        row
        for row in bindings
        if isinstance(row, Mapping) and row.get("queryId") == query_id
    ] if isinstance(bindings, list) else []
    if len(matches) != 1:
        raise Qas30ExperimentError("query ID is not bound to the batch receipt")
    binding = matches[0]
    candidate_id = binding.get("candidateId")
    circuit_matches = [
        row
        for row in live_batch.get("circuits", [])
        if isinstance(row, Mapping) and row.get("candidateId") == candidate_id
    ]
    if (
        len(circuit_matches) != 1
        or binding.get("qcisSha256") != circuit_matches[0].get("qcisSha256")
        or circuit_matches[0].get("measurementPhysicalOrder")
        != live_batch.get("commonMeasurementPhysicalOrder")
    ):
        raise Qas30ExperimentError("query binding does not match the live circuit")
    observation_values = []
    for path in sorted((run_directory / "queries").glob(f"{query_id}-*.json")):
        observation_values.append(_read_json(path))
    observations = _continuation_observation_times(
        observation_values,
        reauthenticated_existing_id_session=(
            not callable(access_token)
            and isinstance(access_token, str)
            and token_state is not None
            and token_state.reauth_count == 1
            and not token_state.expired
            and token_state._access_token == access_token  # noqa: SLF001
        ),
    )
    due = query_due_state(
        submitted_at=_parse_timestamp(receipt.get("persistedAt")),
        observation_times=observations,
        now=current,
    )
    if not due["due"]:
        raise Qas30ExperimentError("query is not due")
    resolved_access_token = access_token() if callable(access_token) else access_token
    observation_path = (
        run_directory / "queries" / f"{query_id}-{len(observations) + 1:04d}.json"
    )
    first_observation = governed_query_due(
        run_id=str(receipt["runId"]),
        data_epoch=str(receipt["dataEpoch"]),
        freeze_manifest_sha256=str(receipt["freezeManifestSha256"]),
        source_commit_sha=str(receipt["sourceCommitSha"]),
        batch_id=str(receipt["batchId"]),
        candidate_id=str(candidate_id),
        query_id=query_id,
        live_batch_sha256=live_batch_sha256,
        batch_submit_receipt_sha256=str(receipt_stored),
        qcis_sha256=str(binding["qcisSha256"]),
        measurement_order=list(live_batch["commonMeasurementPhysicalOrder"]),
        predictor_feature_freeze_sha256=str(
            live_batch["predictorFeatureFreezeSha256"]
        ),
        selection_entity_sha256=str(live_batch["selectionEntitySha256"]),
        authorization=authorization,
        resource_snapshot=resource_snapshot,
        access_token=resolved_access_token,
        transport=transport,
        observation_path=observation_path,
        now=current,
    )
    if first_observation.get("state") != "TOKEN_EXPIRED":
        return first_observation
    if (
        connection_key is None
        or token_state is None
        or reauth_transport is None
        or recovery_query_transport is None
        or token_state._access_token != resolved_access_token  # noqa: SLF001
    ):
        raise Qas30ExperimentError(
            "token expiry is sticky until the same in-memory token state performs "
            "one governed reauthentication"
        )
    require_execution_authorization(
        authorization,
        run_id=str(receipt["runId"]),
        source_commit_sha=str(receipt["sourceCommitSha"]),
        action="RECONCILE_QUERY",
        now=current,
    )
    require_fresh_green_snapshot(resource_snapshot, now=current)
    token_state.mark_expired()
    try:
        resolved_connection_key = (
            connection_key() if callable(connection_key) else connection_key
        )
        assert isinstance(resolved_connection_key, str)
        renewed_token = qas30_tianyan.reauthenticate_once(
            resolved_connection_key,
            transport=reauth_transport,
            token_state=token_state,
            receipt_path=reauth_receipt_path,
        )
    except qas30_tianyan.Qas30TianyanError as error:
        raise Qas30ExperimentError(
            "query reauthentication failed; existing-ID recovery is sticky stopped"
        ) from error
    recovery_path = (
        run_directory / "queries" / f"{query_id}-{len(observations) + 2:04d}.json"
    )
    return governed_query_due(
        run_id=str(receipt["runId"]),
        data_epoch=str(receipt["dataEpoch"]),
        freeze_manifest_sha256=str(receipt["freezeManifestSha256"]),
        source_commit_sha=str(receipt["sourceCommitSha"]),
        batch_id=str(receipt["batchId"]),
        candidate_id=str(candidate_id),
        query_id=query_id,
        live_batch_sha256=live_batch_sha256,
        batch_submit_receipt_sha256=str(receipt_stored),
        qcis_sha256=str(binding["qcisSha256"]),
        measurement_order=list(live_batch["commonMeasurementPhysicalOrder"]),
        predictor_feature_freeze_sha256=str(
            live_batch["predictorFeatureFreezeSha256"]
        ),
        selection_entity_sha256=str(live_batch["selectionEntitySha256"]),
        authorization=authorization,
        resource_snapshot=resource_snapshot,
        access_token=renewed_token,
        transport=recovery_query_transport,
        observation_path=recovery_path,
        now=current,
    )


def reconcile_local(run_directory: Path) -> dict[str, Any]:
    """Inspect durable intents/receipts without issuing network requests."""

    if not run_directory.is_dir():
        raise Qas30ExperimentError("run directory does not exist")
    receipts = sorted(run_directory.rglob("*.json"))
    intents = sorted(run_directory.rglob("*.json.intent"))
    receipt_names = {str(path) for path in receipts}
    unresolved = [
        str(path)
        for path in intents
        if str(path.with_name(path.name.removesuffix(".intent"))) not in receipt_names
    ]
    return {
        "schemaVersion": "qf.qas30.local-reconcile.v1",
        "receiptCount": len(receipts),
        "intentCount": len(intents),
        "unresolvedIntentPaths": unresolved,
        "externalRequestsIssued": 0,
        "state": "RECONCILIATION_REQUIRED" if unresolved else "LOCALLY_RECONCILED",
    }


def reconcile(run_directory: Path) -> dict[str, Any]:
    """Public local reconcile entry; it never queries or resubmits."""

    return reconcile_local(run_directory)


def resume(run_directory: Path) -> dict[str, Any]:
    """Recover only from durable local facts; never synthesize K or Query IDs."""

    reconciliation = reconcile_local(run_directory)
    states: list[str] = []
    query_ids: list[str] = []
    calibrations: list[str] = []
    for path in sorted(run_directory.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            states.append("CORRUPT_LOCAL_RECEIPT")
            continue
        if isinstance(value, dict):
            if isinstance(value.get("state"), str):
                states.append(value["state"])
            ids = value.get("providerQueryIds")
            if isinstance(ids, list):
                query_ids.extend(item for item in ids if isinstance(item, str))
            label = value.get("calibrationLabel")
            if label in {"K0", "K1", "K2"}:
                calibrations.append(label)
    return {
        "schemaVersion": "qf.qas30.resume.v1",
        "reconciliation": reconciliation,
        "observedStates": states,
        "knownQueryIds": query_ids,
        "capturedCalibrationLabels": calibrations,
        "externalRequestsIssued": 0,
    }


def analyse(
    blinded_arms: Mapping[
        str, Mapping[str, Mapping[str, Sequence[float]]]
    ],
    *,
    blind_manifest: Mapping[str, Any],
    comparison_assignment: Mapping[str, Any],
) -> dict[str, Any]:
    """Pair opaque blocks while nesting arm-specific candidates and shots."""

    forbidden = {"RIDGE", "FIXED", "RANDOM", "LLM", "P01", "P02", "P03", "P04"}
    serialized_input = _canonical_json(dict(blinded_arms)).upper()
    if any(word in serialized_input for word in forbidden):
        raise Qas30ExperimentError("blind analysis input leaks true-arm semantics")
    if blind_manifest.get("schemaVersion") != "qf.qas30.blind-analysis-manifest.v2":
        raise Qas30ExperimentError("blind analysis manifest schema is invalid")
    stored_sha = blind_manifest.get("blindManifestSha256")
    unhashed = {
        key: value for key, value in blind_manifest.items() if key != "blindManifestSha256"
    }
    if stored_sha != _sha256(unhashed):
        raise Qas30ExperimentError("blind analysis manifest hash changed")
    serialized_manifest = _canonical_json(dict(blind_manifest)).upper()
    if any(word in serialized_manifest for word in forbidden) or "RUNID" in serialized_manifest:
        raise Qas30ExperimentError("blind analysis manifest leaks true-arm semantics")
    labels = blind_manifest.get("blindCodes")
    if (
        not isinstance(labels, list)
        or len(labels) != 4
        or len(set(labels)) != 4
        or any(not qas30_protocol.re_opaque_code(label, "H") for label in labels)
        or set(blinded_arms) != set(labels)
    ):
        raise Qas30ExperimentError("blind analysis requires four unique opaque arms")

    normalized: dict[str, dict[str, dict[str, list[float]]]] = {}
    expected_block_codes: set[str] | None = None
    for label, arm_rows in blinded_arms.items():
        if not isinstance(arm_rows, Mapping) or not arm_rows:
            raise Qas30ExperimentError("blind arm requires paired block rows")
        block_codes = set(arm_rows)
        if any(
            not qas30_protocol.re_opaque_code(block_code, "K")
            for block_code in block_codes
        ):
            raise Qas30ExperimentError("blind block code is not opaque")
        if expected_block_codes is None:
            expected_block_codes = block_codes
        elif block_codes != expected_block_codes:
            raise Qas30ExperimentError(
                "blind arms do not share the same opaque block codes"
            )
        normalized[label] = {}
        for block_code, candidate_rows in arm_rows.items():
            if not isinstance(candidate_rows, Mapping) or len(candidate_rows) != 5:
                raise Qas30ExperimentError(
                    "each blind block/arm requires five opaque candidates"
                )
            if any(
                not qas30_protocol.re_opaque_code(candidate_code, "D")
                for candidate_code in candidate_rows
            ):
                raise Qas30ExperimentError("blind candidate code is not opaque")
            normalized[label][block_code] = {}
            for candidate_code, draws in candidate_rows.items():
                if (
                    isinstance(draws, str | bytes)
                    or not isinstance(draws, Sequence)
                    or not draws
                ):
                    raise Qas30ExperimentError(
                        "blind candidate shot-level score draws are invalid"
                    )
                values = [float(value) for value in draws]
                if not all(math.isfinite(value) for value in values):
                    raise Qas30ExperimentError(
                        "blind candidate shot-level score draws are non-finite"
                    )
                normalized[label][block_code][candidate_code] = values
    block_order = sorted(expected_block_codes or set())
    if len(block_order) not in qas30_protocol.REPLICATION_BUDGETS:
        raise Qas30ExperimentError(
            "blind analysis requires 10/20/30/40 complete paired blocks"
        )

    public_codes = blind_manifest.get("comparisonCodes")
    assignment_stored = comparison_assignment.get("comparisonAssignmentSha256")
    assignment_unhashed = {
        key: value
        for key, value in comparison_assignment.items()
        if key != "comparisonAssignmentSha256"
    }
    comparison_rows = comparison_assignment.get("comparisons")
    if (
        not isinstance(public_codes, list)
        or len(public_codes) != 4
        or len(set(public_codes)) != 4
        or blind_manifest.get("comparisonAssignmentSha256") != assignment_stored
        or comparison_assignment.get("schemaVersion")
        != "qf.qas30.blind-comparison-assignment.v1"
        or comparison_assignment.get("blindRunCode")
        != blind_manifest.get("blindRunCode")
        or assignment_stored != _sha256(assignment_unhashed)
        or not isinstance(comparison_rows, list)
        or len(comparison_rows) != 4
        or any(not isinstance(row, Mapping) for row in comparison_rows)
        or sorted(row.get("comparisonCode") for row in comparison_rows)
        != sorted(public_codes)
    ):
        raise Qas30ExperimentError("blind analysis requires four frozen comparisons")
    exact: dict[str, Any] = {}
    bootstrap: dict[str, Any] = {}
    comparison_bindings: list[dict[str, str]] = []
    for row in comparison_rows:
        if not isinstance(row, dict) or set(row) != {
            "comparisonCode",
            "firstBlindCode",
            "secondBlindCode",
        }:
            raise Qas30ExperimentError("blind comparison row is invalid")
        comparison_code = row["comparisonCode"]
        first_label = row["firstBlindCode"]
        second_label = row["secondBlindCode"]
        if (
            not qas30_protocol.re_opaque_code(comparison_code, "X")
            or first_label not in blinded_arms
            or second_label not in blinded_arms
            or first_label == second_label
            or comparison_code in exact
        ):
            raise Qas30ExperimentError("blind comparison identity is invalid")
        first_rows = [
            list(normalized[first_label][block_code].values())
            for block_code in block_order
        ]
        second_rows = [
            list(normalized[second_label][block_code].values())
            for block_code in block_order
        ]
        first_scores = [
            float(np.median([np.mean(candidate) for candidate in block]))
            for block in first_rows
        ]
        second_scores = [
            float(np.median([np.mean(candidate) for candidate in block]))
            for block in second_rows
        ]
        exact[comparison_code] = qas30_protocol.exact_median_permutation_test(
            first_scores, second_scores
        )
        bootstrap[comparison_code] = qas30_protocol.nested_bootstrap_median_difference(
            first_rows,
            second_rows,
            seed_material=f"{blind_manifest['blindRunCode']}:{comparison_code}",
            replicates=10_000,
        )
        comparison_bindings.append({"comparisonCode": comparison_code})
    if len(exact) != 4:
        raise Qas30ExperimentError("blind comparison set is incomplete")
    adjusted = qas30_protocol.holm_adjust(
        {name: result["pValue"] for name, result in exact.items()}
    )
    result = {
        "schemaVersion": "qf.qas30.blind-confirmatory-analysis.v2",
        "blindRunCode": blind_manifest["blindRunCode"],
        "blindManifestSha256": stored_sha,
        "sealedMappingSha256": blind_manifest["sealedMappingSha256"],
        "blockCodes": block_order,
        "comparisonBindings": comparison_bindings,
        "exactPairedSignFlip": exact,
        "pairedBootstrap": bootstrap,
        "holmAdjustedPValues": adjusted,
        "unblindState": "SEALED",
    }
    result["blindAnalysisSha256"] = _sha256(result)
    return result


def unblind(
    *,
    blind_analysis: Mapping[str, Any],
    sealed_mapping: Mapping[str, Any],
    independent_review_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Unblind only after a separate, exact independent-review approval receipt."""

    qas30_protocol.validate_sealed_blind_mapping(sealed_mapping)
    if blind_analysis.get("schemaVersion") != "qf.qas30.blind-confirmatory-analysis.v2":
        raise Qas30ExperimentError("blind analysis schema is invalid")
    blind_sha = blind_analysis.get("blindAnalysisSha256")
    unhashed = {
        key: value for key, value in blind_analysis.items() if key != "blindAnalysisSha256"
    }
    if blind_sha != _sha256(unhashed):
        raise Qas30ExperimentError("blind analysis hash changed")
    required_review = {
        "schemaVersion",
        "runId",
        "blindAnalysisSha256",
        "sealedMappingSha256",
        "approved",
        "reviewerId",
        "reviewedAt",
    }
    if set(independent_review_receipt) != required_review:
        raise Qas30ExperimentError("independent review receipt fields are invalid")
    if (
        independent_review_receipt.get("schemaVersion")
        != "qf.qas30.independent-unblind-review.v1"
        or independent_review_receipt.get("approved") is not True
        or independent_review_receipt.get("runId") != sealed_mapping.get("runId")
        or independent_review_receipt.get("blindAnalysisSha256") != blind_sha
        or independent_review_receipt.get("sealedMappingSha256")
        != sealed_mapping.get("sealedMappingSha256")
        or not isinstance(independent_review_receipt.get("reviewerId"), str)
        or not independent_review_receipt.get("reviewerId")
    ):
        raise Qas30ExperimentError("independent review did not authorize unblinding")
    _parse_timestamp(independent_review_receipt["reviewedAt"])
    if (
        blind_analysis.get("blindRunCode") != sealed_mapping.get("blindRunCode")
        or blind_analysis.get("sealedMappingSha256")
        != sealed_mapping.get("sealedMappingSha256")
    ):
        raise Qas30ExperimentError("blind analysis does not bind the sealed mapping")
    comparison_mapping = sealed_mapping["comparisonCodeToTrueComparison"]
    unblinded = []
    for row in blind_analysis.get("comparisonBindings", []):
        if not isinstance(row, Mapping) or set(row) != {"comparisonCode"}:
            raise Qas30ExperimentError("blind comparison binding is invalid")
        mapping = comparison_mapping.get(row["comparisonCode"])
        if not isinstance(mapping, Mapping):
            raise Qas30ExperimentError("blind comparison has no controlled mapping")
        unblinded.append(
            {
                "comparisonId": mapping["comparisonId"],
                "firstTrueArm": mapping["firstTrueArm"],
                "secondTrueArm": mapping["secondTrueArm"],
            }
        )
    result = {
        "schemaVersion": "qf.qas30.unblind-receipt.v1",
        "runId": sealed_mapping["runId"],
        "blindAnalysisSha256": blind_sha,
        "sealedMappingSha256": sealed_mapping["sealedMappingSha256"],
        "independentReviewSha256": _sha256(dict(independent_review_receipt)),
        "unblindedComparisons": unblinded,
        "unblindState": "UNBLINDED_AFTER_INDEPENDENT_REVIEW",
    }
    result["unblindReceiptSha256"] = _sha256(result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qf-quantum-worker qas30")
    parser.add_argument(
        "command",
        choices=(
            "prepare",
            "freeze",
            "capture-k",
            "submit-next",
            "query-due",
            "reconcile",
            "analyse",
            "unblind",
            "resume",
        ),
    )
    parser.add_argument("--run-directory", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--data-epoch")
    parser.add_argument("--source-commit")
    parser.add_argument("--parent-manifest", type=Path)
    parser.add_argument("--parent-canonical-ir", type=Path)
    parser.add_argument("--market-data-sha256")
    parser.add_argument("--normalization-rows", type=Path)
    parser.add_argument("--local-feature-evidence", type=Path)
    parser.add_argument("--blinded-arms", type=Path)
    parser.add_argument("--blind-manifest", type=Path)
    parser.add_argument("--comparison-assignment", type=Path)
    parser.add_argument("--blind-analysis", type=Path)
    parser.add_argument("--sealed-mapping", type=Path)
    parser.add_argument("--independent-review", type=Path)
    parser.add_argument("--input-json", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--credential-file", type=Path)
    return parser


def _command_io(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    if args.run_directory is None or args.input_json is None or args.output_json is None:
        raise Qas30ExperimentError(
            "--run-directory, --input-json, and --output-json are required"
        )
    return _read_json(args.input_json), args.output_json


def _command_now(payload: Mapping[str, Any]) -> datetime | None:
    value = payload.get("now")
    return None if value is None else _parse_timestamp(value)


def _emit_command_output(path: Path, payload: Mapping[str, Any]) -> None:
    _write_json_once(path, payload)
    print(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True))


_PRODUCTION_SECRET_FIELD_NAMES = {
    "accesstoken",
    "apikey",
    "basictoken",
    "connectionkey",
    "openid",
    "secret",
}


def _normalized_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _reject_production_secret_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                isinstance(key, str)
                and _normalized_field_name(key) in _PRODUCTION_SECRET_FIELD_NAMES
            ):
                raise Qas30ExperimentError(
                    "production command payload contains a credential field"
                )
            _reject_production_secret_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_production_secret_fields(item)


def _production_session(
    *,
    args: argparse.Namespace,
    inputs: Mapping[str, Any],
    run_id: str,
    source_commit_sha: str,
    action: str,
    identity: Mapping[str, Any],
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    qas30_tianyan.RequestsSingleAttemptTransport,
    qas30_tianyan.InMemoryTokenState,
    Callable[[], str],
    Callable[[], str],
    Path,
]:
    if "now" in inputs:
        raise Qas30ExperimentError("production commands use the runtime clock")
    _reject_production_secret_fields(inputs)
    try:
        qas30_production_transport.validate_production_transport_spec(
            inputs.get("productionTransport")
        )
    except qas30_production_transport.Qas30ProductionTransportError as error:
        raise Qas30ExperimentError(str(error)) from error
    if args.credential_file is None or args.run_directory is None:
        raise Qas30ExperimentError(
            "production command requires --credential-file and --run-directory"
        )
    authorization = inputs.get("authorization")
    resource_snapshot = inputs.get("resourceSnapshot")
    if not isinstance(authorization, Mapping) or not isinstance(
        resource_snapshot, Mapping
    ):
        raise Qas30ExperimentError("production governed inputs are incomplete")
    transport = qas30_production_transport.new_single_attempt_transport()
    token_state = qas30_tianyan.InMemoryTokenState()
    connection_key: str | None = None
    login_scope = _sha256(
        {
            "action": action,
            "identity": dict(identity),
            "authorization": dict(authorization),
        }
    )[:24]
    login_receipt_path = (
        args.run_directory / "auth" / f"login-{action.casefold()}-{login_scope}.json"
    )
    reauth_receipt_path = (
        args.run_directory / "auth" / f"reauth-query-{login_scope}.json"
    )

    def load_connection_key() -> str:
        nonlocal connection_key
        if connection_key is None:
            try:
                connection_key = (
                    qas30_production_transport.load_tianyan176_connection_key(
                        args.credential_file
                    )
                )
            except qas30_production_transport.Qas30ProductionTransportError as error:
                raise Qas30ExperimentError(str(error)) from error
        return connection_key

    def login() -> str:
        return governed_login_once(
            load_connection_key(),
            run_id=run_id,
            source_commit_sha=source_commit_sha,
            authorization=authorization,
            resource_snapshot=resource_snapshot,
            transport=transport,
            token_state=token_state,
            receipt_path=login_receipt_path,
        )

    return (
        authorization,
        resource_snapshot,
        transport,
        token_state,
        load_connection_key,
        login,
        reauth_receipt_path,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prepare":
        if (
            args.run_directory is None
            or args.run_id is None
            or args.source_commit is None
            or args.parent_manifest is None
            or args.parent_canonical_ir is None
            or args.market_data_sha256 is None
        ):
            raise Qas30ExperimentError("prepare inputs are incomplete")
        payload = prepare(
            run_directory=args.run_directory,
            run_id=args.run_id,
            source_commit_sha=args.source_commit,
            parent_manifest_bytes=args.parent_manifest.read_bytes(),
            parent_canonical_ir_bytes=args.parent_canonical_ir.read_bytes(),
            market_data_sha256=args.market_data_sha256,
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "freeze":
        if (
            args.run_directory is None
            or args.data_epoch is None
            or args.normalization_rows is None
            or args.local_feature_evidence is None
        ):
            raise Qas30ExperimentError("freeze inputs are incomplete")
        rows = json.loads(args.normalization_rows.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise Qas30ExperimentError("normalization rows must be a JSON list")
        local_feature_evidence = _read_json(args.local_feature_evidence)
        payload = freeze(
            run_directory=args.run_directory,
            data_epoch=args.data_epoch,
            pre_result_normalization_rows=rows,
            local_feature_evidence=local_feature_evidence,
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "analyse":
        if (
            args.blinded_arms is None
            or args.blind_manifest is None
            or args.comparison_assignment is None
        ):
            raise Qas30ExperimentError("analyse inputs are incomplete")
        arms = _read_json(args.blinded_arms)
        manifest = _read_json(args.blind_manifest)
        payload = analyse(
            arms,
            blind_manifest=manifest,
            comparison_assignment=_read_json(args.comparison_assignment),
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "unblind":
        if (
            args.blind_analysis is None
            or args.sealed_mapping is None
            or args.independent_review is None
        ):
            raise Qas30ExperimentError("unblind inputs are incomplete")
        payload = unblind(
            blind_analysis=_read_json(args.blind_analysis),
            sealed_mapping=_read_json(args.sealed_mapping),
            independent_review_receipt=_read_json(args.independent_review),
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "capture-k":
        inputs, output_path = _command_io(args)
        if "productionTransport" in inputs:
            frozen = _read_frozen_manifest(args.run_directory)
            calibration_label = inputs.get("calibrationLabel")
            read_time = inputs.get("readTime")
            if calibration_label not in {"K0", "K1", "K2"} or not isinstance(
                read_time, str
            ):
                raise Qas30ExperimentError(
                    "production capture-k calibration inputs are incomplete"
                )
            (
                authorization,
                resource_snapshot,
                transport,
                _token_state,
                _load_connection_key,
                login,
                _reauth_receipt_path,
            ) = _production_session(
                args=args,
                inputs=inputs,
                run_id=str(frozen["runId"]),
                source_commit_sha=str(frozen["sourceCommitSha"]),
                action="capture-k",
                identity={"calibrationLabel": calibration_label},
            )

            def capture_once() -> Mapping[str, Any]:
                access_token = login()
                observed_at = _now()
                authorization_sha256 = require_execution_authorization(
                    authorization,
                    run_id=str(frozen["runId"]),
                    source_commit_sha=str(frozen["sourceCommitSha"]),
                    action="CAPTURE_K",
                    now=observed_at,
                )
                require_fresh_green_snapshot(resource_snapshot, now=observed_at)
                return qas30_observation.calibration_config_once(
                    calibration_label=calibration_label,
                    read_time=read_time,
                    run_id=str(frozen["runId"]),
                    data_epoch=str(frozen["dataEpoch"]),
                    freeze_manifest_sha256=str(frozen["freezeManifestSha256"]),
                    source_commit_sha=str(frozen["sourceCommitSha"]),
                    authorization_envelope_sha256=authorization_sha256,
                    resource_sample_sha256=_sha256(dict(resource_snapshot)),
                    access_token=access_token,
                    transport=transport,
                    captured_at=_iso(observed_at),
                    receipt_path=(
                        args.run_directory
                        / "calibrations"
                        / f"{calibration_label}-official-observation.json"
                    ),
                )

            payload = capture_k(
                run_directory=args.run_directory,
                calibration_label=calibration_label,
                authorization=authorization,
                resource_snapshot=resource_snapshot,
                capture_once=capture_once,
            )
            _emit_command_output(output_path, payload)
            return 0
        if args.credential_file is not None:
            raise Qas30ExperimentError(
                "--credential-file is accepted only with productionTransport"
            )
        fixture = inputs.get("transportFixture")
        if (
            not isinstance(fixture, Mapping)
            or fixture.get("kind") != "CALIBRATION_SNAPSHOT_FIXTURE"
            or not isinstance(fixture.get("snapshot"), Mapping)
            or fixture["snapshot"].get("scientificStatus")
            != qas30_tianyan.FIXTURE_SCIENTIFIC_STATUS
        ):
            raise Qas30ExperimentError(
                "capture-k requires an explicit calibration snapshot fixture"
            )
        authorization = inputs.get("authorization")
        resource_snapshot = inputs.get("resourceSnapshot")
        if not isinstance(authorization, Mapping) or not isinstance(
            resource_snapshot, Mapping
        ):
            raise Qas30ExperimentError("capture-k governed inputs are incomplete")
        payload = capture_k(
            run_directory=args.run_directory,
            calibration_label=str(inputs.get("calibrationLabel", "")),
            authorization=authorization,
            resource_snapshot=resource_snapshot,
            capture_once=lambda: dict(fixture["snapshot"]),
            now=_command_now(inputs),
        )
        _emit_command_output(output_path, payload)
        return 0
    if args.command == "submit-next":
        inputs, output_path = _command_io(args)
        batch = inputs.get("batch")
        authorization = inputs.get("authorization")
        resource_snapshot = inputs.get("resourceSnapshot")
        if "productionTransport" in inputs:
            if not isinstance(batch, dict):
                raise Qas30ExperimentError(
                    "production submit-next batch is incomplete"
                )
            (
                authorization,
                resource_snapshot,
                transport,
                _token_state,
                _load_connection_key,
                login,
                _reauth_receipt_path,
            ) = _production_session(
                args=args,
                inputs=inputs,
                run_id=str(batch.get("runId", "")),
                source_commit_sha=str(batch.get("sourceCommitSha", "")),
                action="submit",
                identity={
                    "batchId": batch.get("batchId"),
                    "blockBatchIndex": batch.get("blockBatchIndex"),
                },
            )
            payload = submit_next(
                batch,
                run_directory=args.run_directory,
                authorization=authorization,
                resource_snapshot=resource_snapshot,
                access_token=login,
                transport=transport,
            )
            _emit_command_output(output_path, payload)
            return 0
        if args.credential_file is not None:
            raise Qas30ExperimentError(
                "--credential-file is accepted only with productionTransport"
            )
        transport_fixture = inputs.get("transportFixture")
        if (
            not isinstance(batch, dict)
            or not isinstance(authorization, Mapping)
            or not isinstance(resource_snapshot, Mapping)
            or not isinstance(transport_fixture, Mapping)
            or not isinstance(inputs.get("accessToken"), str)
        ):
            raise Qas30ExperimentError("submit-next governed inputs are incomplete")
        payload = submit_next(
            batch,
            run_directory=args.run_directory,
            authorization=authorization,
            resource_snapshot=resource_snapshot,
            access_token=inputs["accessToken"],
            transport=FixtureSingleAttemptTransport(transport_fixture),
            now=_command_now(inputs),
        )
        _emit_command_output(output_path, payload)
        return 0
    if args.command == "query-due":
        inputs, output_path = _command_io(args)
        authorization = inputs.get("authorization")
        resource_snapshot = inputs.get("resourceSnapshot")
        if "productionTransport" in inputs:
            query_id = inputs.get("queryId")
            batch_index = inputs.get("batchIndex")
            if (
                not isinstance(query_id, str)
                or not query_id
                or not isinstance(batch_index, int)
                or isinstance(batch_index, bool)
            ):
                raise Qas30ExperimentError(
                    "production query-due identity is incomplete"
                )
            query_attempt = (
                len(
                    list(
                        (args.run_directory / "queries").glob(
                            f"{query_id}-*.json"
                        )
                    )
                )
                + 1
            )
            frozen = _read_frozen_manifest(args.run_directory)
            (
                authorization,
                resource_snapshot,
                transport,
                token_state,
                load_connection_key,
                login,
                reauth_receipt_path,
            ) = _production_session(
                args=args,
                inputs=inputs,
                run_id=str(frozen["runId"]),
                source_commit_sha=str(frozen["sourceCommitSha"]),
                action="query",
                identity={
                    "batchIndex": batch_index,
                    "queryId": query_id,
                    "queryAttempt": query_attempt,
                },
            )
            payload = query_due(
                run_directory=args.run_directory,
                batch_index=batch_index,
                query_id=query_id,
                authorization=authorization,
                resource_snapshot=resource_snapshot,
                access_token=login,
                transport=transport,
                connection_key=load_connection_key,
                token_state=token_state,
                reauth_transport=transport,
                recovery_query_transport=transport,
                reauth_receipt_path=reauth_receipt_path,
            )
            _emit_command_output(output_path, payload)
            return 0
        if args.credential_file is not None:
            raise Qas30ExperimentError(
                "--credential-file is accepted only with productionTransport"
            )
        transport_fixture = inputs.get("transportFixture")
        if (
            not isinstance(authorization, Mapping)
            or not isinstance(resource_snapshot, Mapping)
            or not isinstance(transport_fixture, Mapping)
            or not isinstance(inputs.get("accessToken"), str)
            or not isinstance(inputs.get("queryId"), str)
            or not isinstance(inputs.get("batchIndex"), int)
            or isinstance(inputs.get("batchIndex"), bool)
        ):
            raise Qas30ExperimentError("query-due governed inputs are incomplete")
        payload = query_due(
            run_directory=args.run_directory,
            batch_index=inputs["batchIndex"],
            query_id=inputs["queryId"],
            authorization=authorization,
            resource_snapshot=resource_snapshot,
            access_token=inputs["accessToken"],
            transport=FixtureSingleAttemptTransport(transport_fixture),
            now=_command_now(inputs),
        )
        _emit_command_output(output_path, payload)
        return 0
    if args.command in {"reconcile", "resume"}:
        if args.run_directory is None:
            raise Qas30ExperimentError("--run-directory is required")
        payload = (
            reconcile_local(args.run_directory)
            if args.command == "reconcile"
            else resume(args.run_directory)
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    raise Qas30ExperimentError(
        f"{args.command} has no offline governed command implementation"
    )


if __name__ == "__main__":
    raise SystemExit(main())
