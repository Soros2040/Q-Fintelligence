"""Dedicated V5.3 Tianyan176 adapter.

This module deliberately does not import the historical phase dispatcher.  It accepts a
small versioned action contract, binds every mutation to the V5.3 run/envelope/lease, and
durably spools a complete Query ID before returning it to the caller.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.metadata
import json
import os
import re
import socket
import stat
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from cqlib import QuantumLanguage, TianYanPlatform
from cqlib.exceptions import CqlibRequestError

from qf_algorithm.legacy.quantum_worker.v53_jcs import V53JcsError, canonical_v53_json

TARGET = "tianyan176"
SCHEMA_VERSION = "qf.v53.tianyan-action.v1"
PRIVATE_INPUT_PATH = Path("/run/qf/tianyan176-private-input.json")
RELAY_SOCKET_PATH = "/run/qf/tianyan176-relay/tianyan176-relay.sock"
PRIVATE_INPUT_SCHEMA_VERSION = "qf.v53.tianyan-private-input.v1"
RELAY_AUTHENTICATION_PREFIX = b"QF-V53-TIANYAN-RELAY/1 "
RELAY_CLIENT_AUTHENTICATION_DOMAIN = b"client\x00"
RELAY_SERVER_AUTHENTICATION_DOMAIN = b"server\x00"
PROXY_ENVIRONMENT_NAMES = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)
QUERY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
PURPOSES = {
    "DEVELOPMENT_PREFLIGHT",
    "VALIDATION_FULL",
    "VALIDATION_RHO_ZERO",
    "VALIDATION_GRAPH_FREE",
    "RECOVERY_EXACTLY_ONCE",
    "FORMAL_EVIDENCE_FULL",
    "FORMAL_EVIDENCE_RHO_ZERO",
    "FORMAL_EVIDENCE_GRAPH_FREE",
    "UX_ACCEPTANCE",
}
PARTITIONS_BY_RUN_PURPOSE = {
    "FORMAL_EVIDENCE": {"DEVELOPMENT", "VALIDATION", "FORMAL_HOLDOUT"},
    "DEVELOPMENT_PREFLIGHT": {"DEVELOPMENT"},
    "PRODUCT_VALIDATION": {"VALIDATION"},
    "NON_FORMAL_ACCEPTANCE": {"NON_FORMAL_ACCEPTANCE"},
}


class V53TianyanError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReceiptPersistenceError(V53TianyanError):
    def __init__(self, query_id: str) -> None:
        super().__init__(
            "V53_QUERY_ID_RECEIPT_PERSIST_FAILED",
            "provider returned a Query ID but its durable receipt could not be persisted",
        )
        self.query_id = query_id


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def _canonical_json(value: Any) -> str:
    try:
        return canonical_v53_json(_json_safe(value))
    except V53JcsError as error:
        raise V53TianyanError(
            "V53_TIANYAN_JSON_INVALID",
            "Tianyan evidence is outside the finite canonical JSON domain",
        ) from error


def _sha256(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64 or not value.endswith("Z"):
        raise V53TianyanError(
            "V53_TIANYAN_TIMESTAMP_INVALID", f"{field} must be one canonical UTC timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise V53TianyanError(
            "V53_TIANYAN_TIMESTAMP_INVALID", f"{field} must be one canonical UTC timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise V53TianyanError(
            "V53_TIANYAN_TIMESTAMP_INVALID", f"{field} must be one canonical UTC timestamp"
        )
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise V53TianyanError(
            "V53_TIANYAN_TIMESTAMP_INVALID", f"{field} must be one canonical UTC timestamp"
        )
    return value


def _data_project_root(request: dict[str, Any]) -> Path:
    requested = request.get("dataProjectRoot")
    configured = os.environ.get("QF_V53_DATA_PROJECT_ROOT", "")
    if not isinstance(requested, str) or not requested or requested != configured:
        raise V53TianyanError(
            "V53_TIANYAN_DATA_ROOT_INVALID",
            "dataProjectRoot must match the immutable ordinary-user runtime envelope",
        )
    candidate = Path(requested)
    if not candidate.is_absolute():
        raise V53TianyanError(
            "V53_TIANYAN_DATA_ROOT_INVALID", "dataProjectRoot must be absolute"
        )
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise V53TianyanError(
            "V53_TIANYAN_DATA_ROOT_INVALID", "dataProjectRoot is unavailable"
        ) from error
    if (
        resolved != candidate
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise V53TianyanError(
            "V53_TIANYAN_DATA_ROOT_INVALID",
            "dataProjectRoot must be a current-user real directory",
        )
    return candidate


def _private_input() -> tuple[str, bytes]:
    try:
        metadata = PRIVATE_INPUT_PATH.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
            or metadata.st_size < 1
            or metadata.st_size > 65_536
        ):
            raise OSError("credential metadata is outside the fd-only contract")
        descriptor = os.open(PRIVATE_INPUT_PATH, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_mode != metadata.st_mode
                or opened.st_uid != metadata.st_uid
                or opened.st_nlink != metadata.st_nlink
                or opened.st_size != metadata.st_size
            ):
                raise OSError("credential identity changed while opening")
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    raise OSError("credential read made no progress")
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                len(raw) != metadata.st_size
                or after.st_dev != opened.st_dev
                or after.st_ino != opened.st_ino
                or after.st_mode != opened.st_mode
                or after.st_uid != opened.st_uid
                or after.st_gid != opened.st_gid
                or after.st_nlink != opened.st_nlink
                or after.st_size != opened.st_size
                or after.st_mtime_ns != opened.st_mtime_ns
                or after.st_ctime_ns != opened.st_ctime_ns
            ):
                raise OSError("credential identity changed while reading")
        finally:
            os.close(descriptor)
        encoded = raw.decode("utf-8", errors="strict")
        payload = json.loads(encoded)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise V53TianyanError(
            "V53_TIANYAN_UNCONFIGURED",
            "tianyan176 credential is missing",
        ) from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "connectionKey", "relayAuthenticationSecret"}
        or payload.get("schemaVersion") != PRIVATE_INPUT_SCHEMA_VERSION
        or _canonical_json(payload) != encoded
    ):
        raise V53TianyanError("V53_TIANYAN_UNCONFIGURED", "tianyan176 credential is missing")
    key = payload.get("connectionKey")
    relay_secret_hex = payload.get("relayAuthenticationSecret")
    if (
        not isinstance(key, str)
        or not key
        or len(key.encode("utf-8")) > 65_536
        or key != key.strip()
        or "\x00" in key
        or "\r" in key
        or "\n" in key
        or not isinstance(relay_secret_hex, str)
        or not re.fullmatch(r"[a-f0-9]{64}", relay_secret_hex)
    ):
        raise V53TianyanError("V53_TIANYAN_UNCONFIGURED", "tianyan176 credential is missing")
    return key, bytes.fromhex(relay_secret_hex)


def _configured_key() -> str:
    return _private_input()[0]


def _install_governed_network_resolution(relay_authentication_secret: bytes) -> None:
    if len(relay_authentication_secret) != 32:
        raise V53TianyanError(
            "V53_TIANYAN_NETWORK_BOUNDARY_INVALID",
            "Tianyan relay authentication is invalid",
        )
    if any(os.environ.get(name) for name in PROXY_ENVIRONMENT_NAMES):
        raise V53TianyanError(
            "V53_TIANYAN_NETWORK_BOUNDARY_INVALID",
            "Tianyan worker proxy environment must remain empty",
        )
    try:
        metadata = os.lstat(RELAY_SOCKET_PATH)
    except OSError as error:
        raise V53TianyanError(
            "V53_TIANYAN_NETWORK_BOUNDARY_INVALID",
            "Tianyan relay socket is unavailable",
        ) from error
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise V53TianyanError(
            "V53_TIANYAN_NETWORK_BOUNDARY_INVALID",
            "Tianyan relay socket metadata is invalid",
        )

    from urllib3.util import connection as urllib3_connection

    def governed_create_connection(
        address: tuple[str, int],
        timeout: object = urllib3_connection._DEFAULT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
        socket_options: object = None,
    ) -> socket.socket:
        del socket_options
        if address != ("qc.zdxlz.com", 443) or source_address is not None:
            raise OSError("destination is outside tianyan176")
        relay = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if timeout is not urllib3_connection._DEFAULT_TIMEOUT:
                relay.settimeout(timeout)  # type: ignore[arg-type]
            relay.connect(RELAY_SOCKET_PATH)
            challenge = os.urandom(32)
            request_mac = hmac.new(
                relay_authentication_secret,
                RELAY_CLIENT_AUTHENTICATION_DOMAIN + challenge,
                hashlib.sha256,
            ).hexdigest().encode("ascii")
            relay.sendall(
                RELAY_AUTHENTICATION_PREFIX
                + challenge.hex().encode("ascii")
                + b" "
                + request_mac
                + b"\n"
            )
            expected = b"OK " + hmac.new(
                relay_authentication_secret,
                RELAY_SERVER_AUTHENTICATION_DOMAIN + challenge,
                hashlib.sha256,
            ).hexdigest().encode("ascii") + b"\n"
            response = bytearray()
            while len(response) < len(expected):
                chunk = relay.recv(len(expected) - len(response))
                if not chunk:
                    raise OSError("Tianyan relay authentication ended early")
                response.extend(chunk)
            if not hmac.compare_digest(bytes(response), expected):
                raise OSError("Tianyan relay authentication failed")
            return relay
        except BaseException:
            relay.close()
            raise

    urllib3_connection.create_connection = governed_create_connection

    def reject_name_resolution(*_args: object, **_kwargs: object) -> list[object]:
        raise socket.gaierror(socket.EAI_NONAME, "name resolution is disabled")

    socket.getaddrinfo = reject_name_resolution  # type: ignore[assignment]


def _platform() -> TianYanPlatform:
    connection_key, relay_authentication_secret = _private_input()
    _install_governed_network_resolution(relay_authentication_secret)
    return TianYanPlatform(login_key=connection_key, machine_name=TARGET)


def _require_identifier(request: dict[str, Any], name: str) -> str:
    value = request.get(name)
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise V53TianyanError("V53_TIANYAN_ENVELOPE_INVALID", f"{name} is invalid")
    return value


def _require_sha(request: dict[str, Any], name: str) -> str:
    value = request.get(name)
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise V53TianyanError("V53_TIANYAN_ENVELOPE_INVALID", f"{name} is invalid")
    return value


def _validate_common(request: dict[str, Any]) -> None:
    if request.get("schemaVersion") != SCHEMA_VERSION:
        raise V53TianyanError("V53_TIANYAN_SCHEMA_INVALID", "action schema is invalid")
    if request.get("target") != TARGET:
        raise V53TianyanError("V53_TIANYAN_TARGET_FORBIDDEN", "target must be exact tianyan176")
    _data_project_root(request)
    _require_identifier(request, "runId")
    subject_kind = request.get("subjectKind")
    if subject_kind == "TASK":
        _require_identifier(request, "taskId")
    elif subject_kind == "RUN":
        if request.get("taskId") is not None:
            raise V53TianyanError(
                "V53_TIANYAN_ENVELOPE_INVALID", "RUN subject must not carry taskId"
            )
    else:
        raise V53TianyanError("V53_TIANYAN_ENVELOPE_INVALID", "subjectKind must be RUN or TASK")
    _require_identifier(request, "actionId")
    _require_identifier(request, "attemptId")
    _require_identifier(request, "leaseId")
    _require_identifier(request, "fenceToken")
    _require_sha(request, "requestSha256")
    _require_sha(request, "authorizationSha256")
    _require_sha(request, "freezeManifestSha256")
    run_purpose = request.get("runPurpose")
    evidence_partition = request.get("evidencePartition")
    if (
        run_purpose not in PARTITIONS_BY_RUN_PURPOSE
        or evidence_partition not in PARTITIONS_BY_RUN_PURPOSE[run_purpose]
    ):
        raise V53TianyanError(
            "V53_TIANYAN_EVIDENCE_PARTITION_INVALID",
            "run purpose and evidence partition are not an exact pair",
        )
    if request.get("intentState") != "COMMITTING":
        raise V53TianyanError(
            "V53_TIANYAN_INTENT_NOT_COMMITTING",
            "external action requires a durable COMMITTING attempt",
        )


def _doctor() -> dict[str, Any]:
    try:
        _configured_key()
        configured = True
    except V53TianyanError:
        configured = False
    try:
        version = importlib.metadata.version("cqlib")
    except importlib.metadata.PackageNotFoundError:
        version = "missing"
    return {
        "schemaVersion": "qf.v53.tianyan-doctor.v1",
        "target": TARGET,
        "credential": "configured" if configured else "missing",
        "cqlibVersion": version,
        "networkCalled": False,
    }


def _discover(request: dict[str, Any]) -> dict[str, Any]:
    _validate_common(request)
    platform = _platform()
    rows = _json_safe(platform.query_quantum_computer_list())
    if not isinstance(rows, list):
        raise V53TianyanError(
            "V53_TIANYAN_DISCOVERY_SHAPE_INVALID",
            "provider discovery did not return a machine list",
        )
    selected = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("machine_name", row.get("name", row.get("code", row.get("machineName"))))
        if str(name) == TARGET:
            selected = row
            break
    if selected is None:
        raise V53TianyanError(
            "V53_TIANYAN176_NOT_DISCOVERED",
            "the live provider catalog omitted exact tianyan176",
        )
    config = _json_safe(platform.download_config(machine=TARGET, read_time=None))
    config_json = _canonical_json(config)
    selected_json = _canonical_json(selected)
    return {
        "schemaVersion": "qf.v53.tianyan-capability.v1",
        "target": TARGET,
        "retrievedAt": _now(),
        "machine": selected,
        "machineSha256": _sha256(selected_json),
        "config": config,
        "configSha256": _sha256(config_json),
        "cqlibVersion": importlib.metadata.version("cqlib"),
    }


def _qubit_name(value: Any) -> str | None:
    match = re.fullmatch(r"(?:Q|q)?(\d{1,4})", str(value).strip())
    return None if match is None else f"Q{int(match.group(1))}"


def _integer_mapping(value: Any, label: str) -> dict[int, int]:
    if not isinstance(value, dict):
        raise V53TianyanError("V53_TIANYAN_MAPPING_INVALID", f"{label} is invalid")
    result: dict[int, int] = {}
    for key, item in value.items():
        key_name = _qubit_name(key)
        item_name = _qubit_name(item)
        if key_name is None or item_name is None:
            raise V53TianyanError(
                "V53_TIANYAN_MAPPING_INVALID", f"{label} contains an invalid wire"
            )
        normalized_key = int(key_name[1:])
        normalized_item = int(item_name[1:])
        if normalized_key in result:
            raise V53TianyanError(
                "V53_TIANYAN_MAPPING_INVALID", f"{label} contains duplicate wires"
            )
        result[normalized_key] = normalized_item
    return result


def _topology_qubits(config: Any) -> set[str]:
    if not isinstance(config, dict):
        raise V53TianyanError("V53_TIANYAN_CONFIG_INVALID", "tianyan176 config is not an object")
    overview = config.get("overview")
    couplers = overview.get("coupler_map") if isinstance(overview, dict) else None
    if not isinstance(couplers, dict):
        raise V53TianyanError("V53_TIANYAN_CONFIG_INVALID", "tianyan176 config has no coupler map")
    qubits: set[str] = set()
    for pair in couplers.values():
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        for value in pair:
            name = _qubit_name(value)
            if name is not None:
                qubits.add(name)
    if len(qubits) < 6:
        raise V53TianyanError(
            "V53_TIANYAN_CONFIG_INVALID", "tianyan176 topology exposes fewer than six wires"
        )
    return qubits


class _FrozenMappingPlatform:
    """Give cqlib one immutable topology snapshot normalized to zero-based wires."""

    def __init__(self, config: dict[str, Any], *, shift_from_one_based: bool) -> None:
        normalized = deepcopy(config)
        if shift_from_one_based:
            overview = normalized.get("overview")
            couplers = overview.get("coupler_map") if isinstance(overview, dict) else None
            if not isinstance(couplers, dict):
                raise V53TianyanError(
                    "V53_TIANYAN_CONFIG_INVALID", "tianyan176 config has no coupler map"
                )
            shifted: dict[str, list[str]] = {}
            for coupler, pair in couplers.items():
                if not isinstance(pair, list) or len(pair) != 2:
                    continue
                names = [_qubit_name(value) for value in pair]
                if any(name is None or int(name[1:]) < 1 for name in names):
                    raise V53TianyanError(
                        "V53_TIANYAN_CONFIG_INVALID",
                        "one-based tianyan176 topology contains an invalid wire",
                    )
                shifted[str(coupler)] = [
                    f"Q{int(name[1:]) - 1}" for name in names if name is not None
                ]
            overview["coupler_map"] = shifted
            for field in ("disabledQubits", "disabled_qubits"):
                disabled = normalized.get(field)
                if isinstance(disabled, str):
                    values = []
                    for value in disabled.split(","):
                        if not value.strip():
                            continue
                        name = _qubit_name(value)
                        if name is None or int(name[1:]) < 1:
                            raise V53TianyanError(
                                "V53_TIANYAN_CONFIG_INVALID",
                                "disabled tianyan176 wire is invalid",
                            )
                        values.append(f"Q{int(name[1:]) - 1}")
                    normalized[field] = ",".join(values)
        self.config = normalized

    def download_config(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return deepcopy(self.config)


def _shift_qcis_to_one_based(qcis: str) -> str:
    return re.sub(
        r"\bQ(\d+)\b",
        lambda match: f"Q{int(match.group(1)) + 1}",
        qcis,
        flags=re.IGNORECASE,
    )


def _mapping_seed(
    *,
    source_bundle_sha256: str,
    config_sha256: str,
    variant: str,
    source_qcis_sha256: str,
) -> int:
    """Derive one stable legacy-NumPy seed from immutable mapping inputs."""

    material = {
        "algorithm": "cqlib.mapping.transpile_qcis",
        "configSha256": config_sha256,
        "sourceBundleSha256": source_bundle_sha256,
        "sourceQcisSha256": source_qcis_sha256,
        "variant": variant,
    }
    return int(_sha256(_canonical_json(material))[:8], 16)


def _deterministic_transpile(
    qcis: str,
    mapping_platform: Any,
    *,
    seed: int,
) -> tuple[Any, Any, Any, Any]:
    """Contain cqlib's process-global random state around one frozen mapping."""

    # cqlib 0.2.x exposes a ``seed`` argument but its implementation does not
    # apply it to the Topgraph -> simulated-annealing fallback.  The fallback
    # consumes NumPy's legacy module-global RNG, so save/seed/restore that exact
    # state in this single-process worker boundary.
    previous_state = np.random.get_state()
    try:
        np.random.seed(seed)
        from cqlib.mapping import transpile_qcis

        return transpile_qcis(qcis, mapping_platform)
    finally:
        np.random.set_state(previous_state)


def _map(request: dict[str, Any], *, frozen: bool = False) -> dict[str, Any]:
    if frozen:
        if request.get("schemaVersion") != "qf.v53.local-frozen-map-action.v1":
            raise V53TianyanError(
                "V53_TIANYAN_SCHEMA_INVALID", "local frozen mapping schema is invalid"
            )
        if request.get("target") != TARGET:
            raise V53TianyanError("V53_TIANYAN_TARGET_FORBIDDEN", "target must be exact tianyan176")
    else:
        _validate_common(request)
    expected_config_sha256 = _require_sha(request, "expectedConfigSha256")
    expected_bundle_sha256 = _require_sha(request, "sourceBundleSha256")
    mapped_at = _canonical_timestamp(request.get("mappedAt"), "mappedAt") if frozen else _now()
    circuits = request.get("circuits")
    variants = (
        ("full",)
        if not frozen and request.get("evidencePartition") == "DEVELOPMENT"
        else ("full", "rho_zero", "graph_free")
    )
    if not isinstance(circuits, list) or len(circuits) != len(variants):
        raise V53TianyanError(
            "V53_TIANYAN_MAPPING_INPUT_INVALID",
            f"mapping requires exactly {len(variants)} frozen variant(s)",
        )
    if frozen:
        candidate_config = request.get("frozenConfig")
        if not isinstance(candidate_config, dict):
            raise V53TianyanError(
                "V53_TIANYAN_CONFIG_INVALID", "local mapping requires one frozen config object"
            )
        raw_config = _json_safe(candidate_config)
    else:
        platform = _platform()
        raw_config = _json_safe(platform.download_config(machine=TARGET, read_time=None))
    config_sha256 = _sha256(_canonical_json(raw_config))
    if config_sha256 != expected_config_sha256:
        raise V53TianyanError(
            "V53_TIANYAN_CONFIG_HASH_MISMATCH",
            "live mapping config differs from the frozen capability snapshot",
        )
    topology = _topology_qubits(raw_config)
    indices = sorted(int(name[1:]) for name in topology)
    shift_from_one_based = 0 not in indices and min(indices) >= 1
    mapping_platform = _FrozenMappingPlatform(raw_config, shift_from_one_based=shift_from_one_based)
    mapped_rows: list[dict[str, Any]] = []
    for index, (expected_variant, row) in enumerate(zip(variants, circuits, strict=True)):
        if not isinstance(row, dict) or row.get("variant") != expected_variant:
            raise V53TianyanError(
                "V53_TIANYAN_MAPPING_INPUT_INVALID", "mapping variant order is invalid"
            )
        qcis = row.get("qcis")
        qcis_sha256 = row.get("qcisSha256")
        if (
            not isinstance(qcis, str)
            or not qcis.strip()
            or len(qcis.encode("utf-8")) > 1_048_576
            or not isinstance(qcis_sha256, str)
            or not SHA256_PATTERN.fullmatch(qcis_sha256)
            or _sha256(qcis) != qcis_sha256
        ):
            raise V53TianyanError(
                "V53_TIANYAN_MAPPING_INPUT_INVALID", f"variant {index} QCIS is invalid"
            )
        mapping_seed = _mapping_seed(
            source_bundle_sha256=expected_bundle_sha256,
            config_sha256=config_sha256,
            variant=expected_variant,
            source_qcis_sha256=qcis_sha256,
        )
        circuit, initial_raw, swap_raw, final_raw = _deterministic_transpile(
            qcis,
            mapping_platform,
            seed=mapping_seed,
        )
        mapped_qcis = str(circuit.qcis).upper()
        if shift_from_one_based:
            mapped_qcis = _shift_qcis_to_one_based(mapped_qcis)
        mapped_qcis_valid = bool(mapped_qcis.strip()) and (
            frozen or bool(platform.qcis_check_regular(mapped_qcis))
        )
        if not mapped_qcis_valid:
            raise V53TianyanError(
                "V53_TIANYAN_MAPPED_QCIS_REJECTED",
                f"mapped {expected_variant} circuit failed exact target validation",
            )
        initial = _integer_mapping(initial_raw, "initialLayout")
        final = _integer_mapping(final_raw, "virtualToFinal")
        _integer_mapping(swap_raw, "swapMapping")
        if set(initial) != set(range(6)) or set(final) != set(range(6)):
            raise V53TianyanError(
                "V53_TIANYAN_MAPPING_INVALID", "SDK mapping does not cover six logical wires"
            )

        def physical_name(value: int) -> str:
            return f"Q{value + 1}" if shift_from_one_based else f"Q{value}"

        measurement_order = re.findall(r"(?m)^\s*M\s+(Q\d+)\s*$", mapped_qcis)
        if len(measurement_order) != 6 or len(set(measurement_order)) != 6:
            raise V53TianyanError(
                "V53_TIANYAN_MEASUREMENT_ORDER_INVALID",
                "mapped QCIS must expose six unique measurement columns",
            )
        measurement_index = {
            physical: position for position, physical in enumerate(measurement_order)
        }
        inversion = []
        for logical in range(6):
            final_physical = physical_name(final[logical])
            if final_physical not in measurement_index:
                raise V53TianyanError(
                    "V53_TIANYAN_MEASUREMENT_ORDER_INVALID",
                    "final logical wire is missing from measurement order",
                )
            inversion.append(
                {
                    "logicalQubit": logical,
                    "selectionIndex": logical,
                    "initialPhysicalQubit": physical_name(initial[logical]),
                    "finalPhysicalQubit": final_physical,
                    "providerBitPosition": measurement_index[final_physical],
                }
            )
        if len({item["providerBitPosition"] for item in inversion}) != 6:
            raise V53TianyanError(
                "V53_TIANYAN_MEASUREMENT_ORDER_INVALID",
                "measurement inversion is not bijective",
            )
        mapped_rows.append(
            {
                "variant": expected_variant,
                "sourceQcisSha256": qcis_sha256,
                "mappedQcis": mapped_qcis,
                "mappedQcisSha256": _sha256(mapped_qcis),
                "mappingSeed": mapping_seed,
                "measurementPhysicalOrder": measurement_order,
                "selectionInversion": inversion,
                "initialLayout": _json_safe(initial_raw),
                "swapMapping": _json_safe(swap_raw),
                "virtualToFinal": _json_safe(final_raw),
                "valid": True,
            }
        )
    payload: dict[str, Any] = {
        "schemaVersion": "qf.v53.tianyan-mapping.v1",
        "target": TARGET,
        "sourceBundleSha256": expected_bundle_sha256,
        "configSha256": config_sha256,
        "topologyIndexing": "ONE_BASED" if shift_from_one_based else "ZERO_BASED",
        "algorithm": "cqlib.mapping.transpile_qcis",
        "mappings": mapped_rows,
        "mappedAt": mapped_at,
        "hardwareSubmitted": False,
        "queryIdsCreated": 0,
    }
    payload["mappingSha256"] = _sha256(_canonical_json(payload))
    return payload


def _validate(request: dict[str, Any]) -> dict[str, Any]:
    _validate_common(request)
    qcis = request.get("qcis")
    if not isinstance(qcis, str) or not qcis.strip() or len(qcis.encode("utf-8")) > 1_048_576:
        raise V53TianyanError("V53_TIANYAN_QCIS_INVALID", "QCIS bytes are invalid")
    expected_hash = _require_sha(request, "qcisSha256")
    if _sha256(qcis) != expected_hash:
        raise V53TianyanError("V53_TIANYAN_QCIS_HASH_MISMATCH", "QCIS hash mismatch")
    valid = bool(_platform().qcis_check_regular(qcis))
    if not valid:
        raise V53TianyanError("V53_TIANYAN_QCIS_REJECTED", "tianyan176 rejected the QCIS")
    return {
        "schemaVersion": "qf.v53.tianyan-validation.v1",
        "target": TARGET,
        "valid": True,
        "qcisSha256": expected_hash,
        "validatedAt": _now(),
    }


def _ensure_private_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise V53TianyanError("V53_RECEIPT_PATH_UNSAFE", "receipt directory is unsafe")
    if metadata.st_uid != os.getuid():
        raise V53TianyanError("V53_RECEIPT_PATH_UNSAFE", "receipt directory owner is invalid")
    os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _receipt_path(request: dict[str, Any]) -> Path:
    run_id = _require_identifier(request, "runId")
    action_id = (
        _require_identifier(request, "submitActionId")
        if request.get("action") == "query"
        else _require_identifier(request, "actionId")
    )
    root = _data_project_root(request)
    receipts = root / "99_local_cache" / "v53-real-runs" / run_id / "receipts"
    _ensure_private_directory(receipts, create=False)
    expected = receipts / f"{_sha256(action_id)}.json"
    provided = request.get("receiptPath")
    if not isinstance(provided, str) or Path(provided).absolute() != expected.absolute():
        raise V53TianyanError("V53_RECEIPT_PATH_UNSAFE", "receipt path escaped its action binding")
    return expected


def _persist_query_receipt(
    request: dict[str, Any], query_id: str, qcis_sha256: str, shots: int
) -> dict[str, Any]:
    final_path = _receipt_path(request)
    if final_path.exists():
        raise V53TianyanError(
            "V53_QUERY_ID_RECEIPT_ALREADY_EXISTS",
            "the action already has a durable Query ID receipt",
        )
    payload = {
        "schemaVersion": "qf.v53.tianyan-query-id-receipt.v1",
        "runId": request["runId"],
        "runPurpose": request["runPurpose"],
        "subjectKind": request["subjectKind"],
        "evidencePartition": request["evidencePartition"],
        "taskId": request["taskId"],
        "actionId": request["actionId"],
        "attemptId": request["attemptId"],
        "logicalActionId": request["logicalActionId"],
        "authorizationSha256": request["authorizationSha256"],
        "freezeManifestSha256": request["freezeManifestSha256"],
        "approvalReceiptId": request["approvalReceiptId"],
        "idempotencyKey": request["idempotencyKey"],
        "leaseId": request["leaseId"],
        "fenceToken": request["fenceToken"],
        "requestSha256": request["requestSha256"],
        "target": TARGET,
        "purpose": request["purpose"],
        "qcisSha256": qcis_sha256,
        "shots": shots,
        "queryId": query_id,
        "persistedAt": _now(),
        "resubmissionAllowed": False,
    }
    receipt = {**payload, "receiptSha256": _sha256(_canonical_json(payload))}
    encoded = (_canonical_json(receipt) + "\n").encode("utf-8")
    temporary = final_path.with_suffix(f".tmp.{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("durable receipt write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, final_path)
    os.chmod(final_path, 0o600)
    _fsync_directory(final_path.parent)
    return receipt


def _submission_guard_path(request: dict[str, Any]) -> Path:
    return _receipt_path(request).with_suffix(".committing.json")


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.with_suffix(f".tmp.{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("durable guard write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    _fsync_directory(path.parent)


def _claim_submission(request: dict[str, Any]) -> Path:
    receipt_path = _receipt_path(request)
    guard_path = _submission_guard_path(request)
    if receipt_path.exists() or guard_path.exists():
        raise V53TianyanError(
            "V53_TIANYAN_ACTION_ALREADY_COMMITTED",
            "the logical action already crossed or may have crossed the submit boundary",
        )
    payload = {
        "schemaVersion": "qf.v53.tianyan-submission-guard.v1",
        "state": "SUBMITTING",
        "runId": request["runId"],
        "runPurpose": request["runPurpose"],
        "subjectKind": request["subjectKind"],
        "evidencePartition": request["evidencePartition"],
        "taskId": request["taskId"],
        "actionId": request["actionId"],
        "attemptId": request["attemptId"],
        "logicalActionId": request["logicalActionId"],
        "authorizationSha256": request["authorizationSha256"],
        "freezeManifestSha256": request["freezeManifestSha256"],
        "approvalReceiptId": request["approvalReceiptId"],
        "idempotencyKey": request["idempotencyKey"],
        "leaseId": request["leaseId"],
        "fenceToken": request["fenceToken"],
        "requestSha256": request["requestSha256"],
        "target": TARGET,
        "purpose": request["purpose"],
        "claimedAt": _now(),
        "queryId": None,
        "resubmissionAllowed": False,
    }
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    try:
        descriptor = os.open(
            guard_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError as error:
        raise V53TianyanError(
            "V53_TIANYAN_ACTION_ALREADY_COMMITTED",
            "the logical action already crossed or may have crossed the submit boundary",
        ) from error
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("durable guard write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(guard_path.parent)
    return guard_path


def _record_query_in_guard(
    guard_path: Path,
    request: dict[str, Any],
    query_id: str,
    qcis_sha256: str,
    shots: int,
) -> None:
    payload = {
        "schemaVersion": "qf.v53.tianyan-submission-guard.v1",
        "state": "QUERY_ID_RETURNED",
        "runId": request["runId"],
        "runPurpose": request["runPurpose"],
        "subjectKind": request["subjectKind"],
        "evidencePartition": request["evidencePartition"],
        "taskId": request["taskId"],
        "actionId": request["actionId"],
        "attemptId": request["attemptId"],
        "logicalActionId": request["logicalActionId"],
        "authorizationSha256": request["authorizationSha256"],
        "freezeManifestSha256": request["freezeManifestSha256"],
        "approvalReceiptId": request["approvalReceiptId"],
        "idempotencyKey": request["idempotencyKey"],
        "leaseId": request["leaseId"],
        "fenceToken": request["fenceToken"],
        "requestSha256": request["requestSha256"],
        "target": TARGET,
        "purpose": request["purpose"],
        "queryId": query_id,
        "qcisSha256": qcis_sha256,
        "shots": shots,
        "recordedAt": _now(),
        "resubmissionAllowed": False,
    }
    _write_private_json(guard_path, payload)


def _load_query_receipt(request: dict[str, Any]) -> dict[str, Any]:
    receipt_path = _receipt_path(request)
    if not receipt_path.exists():
        guard_path = _submission_guard_path(request)
        if not guard_path.exists() or guard_path.is_symlink():
            raise V53TianyanError(
                "V53_TIANYAN_QUERY_RECEIPT_REQUIRED",
                "query-only reconciliation requires a durable Query ID record",
            )
        guard_metadata = guard_path.stat()
        if guard_metadata.st_uid != os.getuid() or stat.S_IMODE(guard_metadata.st_mode) != 0o600:
            raise V53TianyanError(
                "V53_RECEIPT_PATH_UNSAFE", "submission guard mode or owner is invalid"
            )
        try:
            guard = json.loads(guard_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise V53TianyanError(
                "V53_TIANYAN_QUERY_RECEIPT_INVALID", "submission guard is invalid"
            ) from error
        direct_fields = (
            "runId",
            "runPurpose",
            "subjectKind",
            "evidencePartition",
            "taskId",
            "authorizationSha256",
            "freezeManifestSha256",
            "approvalReceiptId",
            "target",
        )
        submit_bindings = {
            "actionId": "submitActionId",
            "attemptId": "submitAttemptId",
            "logicalActionId": "submitLogicalActionId",
            "idempotencyKey": "submitIdempotencyKey",
            "leaseId": "submitLeaseId",
            "fenceToken": "submitFenceToken",
            "requestSha256": "submitRequestSha256",
        }
        query_id = guard.get("queryId") if isinstance(guard, dict) else None
        qcis_sha256 = guard.get("qcisSha256") if isinstance(guard, dict) else None
        shots = guard.get("shots") if isinstance(guard, dict) else None
        if (
            not isinstance(guard, dict)
            or guard.get("schemaVersion") != "qf.v53.tianyan-submission-guard.v1"
            or guard.get("state") != "QUERY_ID_RETURNED"
            or any(guard.get(name) != request.get(name) for name in direct_fields)
            or any(
                guard.get(receipt_name) != request.get(request_name)
                for receipt_name, request_name in submit_bindings.items()
            )
            or not isinstance(query_id, str)
            or not QUERY_ID_PATTERN.fullmatch(query_id)
            or not isinstance(qcis_sha256, str)
            or not SHA256_PATTERN.fullmatch(qcis_sha256)
            or not isinstance(shots, int)
            or isinstance(shots, bool)
            or not 1 <= shots <= 100_000
        ):
            raise V53TianyanError(
                "V53_TIANYAN_QUERY_RECEIPT_INVALID",
                "submission guard cannot be promoted to a query-only receipt",
            )
        promotion_request = {**request, **guard, "action": "submit"}
        _persist_query_receipt(promotion_request, query_id, qcis_sha256, shots)
    if receipt_path.is_symlink():
        raise V53TianyanError(
            "V53_TIANYAN_QUERY_RECEIPT_REQUIRED",
            "query-only reconciliation requires the durable submit receipt",
        )
    metadata = receipt_path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise V53TianyanError("V53_RECEIPT_PATH_UNSAFE", "query receipt mode or owner is invalid")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise V53TianyanError(
            "V53_TIANYAN_QUERY_RECEIPT_INVALID", "query receipt is invalid"
        ) from error
    if not isinstance(receipt, dict):
        raise V53TianyanError("V53_TIANYAN_QUERY_RECEIPT_INVALID", "query receipt is invalid")
    direct_fields = (
        "runId",
        "runPurpose",
        "subjectKind",
        "evidencePartition",
        "taskId",
        "authorizationSha256",
        "freezeManifestSha256",
        "approvalReceiptId",
        "target",
    )
    submit_bindings = {
        "actionId": "submitActionId",
        "attemptId": "submitAttemptId",
        "logicalActionId": "submitLogicalActionId",
        "idempotencyKey": "submitIdempotencyKey",
        "leaseId": "submitLeaseId",
        "fenceToken": "submitFenceToken",
        "requestSha256": "submitRequestSha256",
    }
    if any(receipt.get(name) != request.get(name) for name in direct_fields) or any(
        receipt.get(receipt_name) != request.get(request_name)
        for receipt_name, request_name in submit_bindings.items()
    ):
        raise V53TianyanError(
            "V53_TIANYAN_QUERY_RECEIPT_MISMATCH",
            "query receipt does not bind this immutable action envelope",
        )
    payload = {key: value for key, value in receipt.items() if key != "receiptSha256"}
    if receipt.get("receiptSha256") != _sha256(_canonical_json(payload)):
        raise V53TianyanError("V53_TIANYAN_QUERY_RECEIPT_INVALID", "query receipt hash is invalid")
    return receipt


def _validate_submit(request: dict[str, Any]) -> tuple[str, int]:
    _validate_common(request)
    for name in ["logicalActionId", "idempotencyKey", "leaseId"]:
        _require_identifier(request, name)
    if request.get("evidencePartition") != "DEVELOPMENT":
        _require_identifier(request, "approvalReceiptId")
    if request.get("intentState") != "COMMITTING" or request.get("commitAuthorized") is not True:
        raise V53TianyanError(
            "V53_TIANYAN_INTENT_NOT_COMMITTING",
            "submit requires a durable COMMITTING intent",
        )
    purpose = request.get("purpose")
    if purpose not in PURPOSES:
        raise V53TianyanError("V53_TIANYAN_PURPOSE_FORBIDDEN", "submit purpose is not allowed")
    shots = request.get("shots")
    circuit_count = request.get("circuitCount")
    estimate = request.get("estimatedExecutionSeconds")
    if not isinstance(shots, int) or isinstance(shots, bool) or not 1 <= shots <= 100_000:
        raise V53TianyanError("V53_TIANYAN_SHOTS_INVALID", "shots exceed the V5.3 action envelope")
    if circuit_count != 1:
        raise V53TianyanError("V53_TIANYAN_CIRCUIT_COUNT_INVALID", "this action binds one circuit")
    if (
        not isinstance(estimate, (int, float))
        or isinstance(estimate, bool)
        or not 1 <= estimate <= 600
    ):
        raise V53TianyanError(
            "V53_TIANYAN_DURATION_INVALID", "estimated duration must be 1-600 seconds"
        )
    qcis = request.get("qcis")
    if not isinstance(qcis, str) or not qcis.strip() or len(qcis.encode("utf-8")) > 1_048_576:
        raise V53TianyanError("V53_TIANYAN_QCIS_INVALID", "QCIS bytes are invalid")
    expected_hash = _require_sha(request, "qcisSha256")
    if _sha256(qcis) != expected_hash:
        raise V53TianyanError("V53_TIANYAN_QCIS_HASH_MISMATCH", "QCIS hash mismatch")
    _receipt_path(request)
    return qcis, shots


def _submit(request: dict[str, Any]) -> dict[str, Any]:
    qcis, shots = _validate_submit(request)
    platform = _platform()
    if not platform.qcis_check_regular(qcis):
        raise V53TianyanError("V53_TIANYAN_QCIS_REJECTED", "tianyan176 rejected the QCIS")
    guard_path = _claim_submission(request)
    query_ids = platform.submit_experiment(
        circuit=qcis,
        language=QuantumLanguage.QCIS,
        name=f"QF-V53-{request['purpose']}-{request['actionId']}"[:120],
        num_shots=shots,
        machine_name=TARGET,
        is_verify=True,
    )
    if isinstance(query_ids, str):
        query_ids = [query_ids]
    if not isinstance(query_ids, list) or len(query_ids) != 1:
        raise V53TianyanError(
            "V53_TIANYAN_QUERY_ID_INVALID",
            "provider did not return exactly one Query ID",
        )
    query_id = str(query_ids[0])
    if not QUERY_ID_PATTERN.fullmatch(query_id):
        raise V53TianyanError(
            "V53_TIANYAN_QUERY_ID_INVALID",
            "provider returned an invalid Query ID",
        )
    try:
        _record_query_in_guard(guard_path, request, query_id, _sha256(qcis), shots)
        receipt = _persist_query_receipt(request, query_id, _sha256(qcis), shots)
    except Exception as error:
        raise ReceiptPersistenceError(query_id) from error
    return {
        "schemaVersion": "qf.v53.tianyan-submission.v1",
        "target": TARGET,
        "actionId": request["actionId"],
        "queryId": query_id,
        "status": "SUBMITTED",
        "shots": shots,
        "qcisSha256": _sha256(qcis),
        "receiptSha256": receipt["receiptSha256"],
        "resubmissionAllowed": False,
    }


def _has_result_status(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "resultStatus" and isinstance(child, list) and child:
                return True
            if _has_result_status(child):
                return True
    elif isinstance(value, list):
        return any(_has_result_status(child) for child in value)
    return False


def _extract_status(value: Any) -> str:
    aliases = {"status", "state", "task_status", "taskStatus"}
    success = {
        "3",
        "COMPLETED",
        "COMPLETE",
        "FINISHED",
        "SUCCESS",
        "SUCCEEDED",
        "DONE",
        "成功",
        "已完成",
        "运行成功",
        "执行成功",
        "实验成功",
        "任务成功",
    }
    failure = {
        "4",
        "5",
        "FAILED",
        "FAILURE",
        "ERROR",
        "CANCELLED",
        "CANCELED",
        "REJECTED",
        "失败",
        "运行失败",
        "执行失败",
        "实验失败",
        "任务失败",
    }
    observed: list[str] = []
    pending: list[Any] = [value]
    while pending:
        current = pending.pop(0)
        if isinstance(current, dict):
            for key, child in current.items():
                if key in aliases and isinstance(child, (str, int)):
                    observed.append(str(child).strip().upper())
                pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)
    if any(status in failure for status in observed):
        return "FAILED"
    if any(status in success for status in observed) or _has_result_status(value):
        return "COMPLETED"
    if observed:
        return observed[0]
    return "UNKNOWN"


def _query(request: dict[str, Any]) -> dict[str, Any]:
    _validate_common(request)
    query_id = request.get("queryId")
    if not isinstance(query_id, str) or not QUERY_ID_PATTERN.fullmatch(query_id):
        raise V53TianyanError("V53_TIANYAN_QUERY_ID_INVALID", "query requires a complete Query ID")
    receipt = _load_query_receipt(request)
    if receipt.get("queryId") != query_id:
        raise V53TianyanError(
            "V53_TIANYAN_QUERY_RECEIPT_MISMATCH",
            "query ID does not match the durable submit receipt",
        )
    try:
        result = _json_safe(
            _platform().query_experiment(
                query_id=query_id,
                max_wait_time=int(request.get("maxWaitSeconds", 20)),
                sleep_time=int(request.get("pollIntervalSeconds", 5)),
                readout_calibration=False,
            )
        )
        provider_status = _extract_status(result)
    except CqlibRequestError as error:
        # Only a transport-less bounded SDK polling expiry is ordinary pending.
        # HTTP errors remain explicit query-only observations and can never
        # cross the submit boundary.
        status_code = getattr(error, "status_code", None)
        if status_code is None:
            result = []
            provider_status = "PENDING"
        elif status_code in {408, 429} or 500 <= status_code <= 599:
            fallback_seconds = request.get("pollIntervalSeconds", 5)
            if not isinstance(fallback_seconds, int) or isinstance(fallback_seconds, bool):
                fallback_seconds = 5
            result = {
                "schemaVersion": "qf.v53.tianyan-query-transient.v1",
                "statusCode": status_code,
                "retryAfterSeconds": min(60, max(1, fallback_seconds)),
                "retryAfterSource": "BOUNDED_BACKOFF_FALLBACK",
                "resubmissionAllowed": False,
            }
            provider_status = "QUERY_ONLY_TRANSIENT"
        else:
            result = {
                "schemaVersion": "qf.v53.tianyan-query-failure.v1",
                "statusCode": status_code,
                "retryAllowed": False,
                "resubmissionAllowed": False,
            }
            provider_status = "FAILED"
    raw_json = _canonical_json(result)
    return {
        "schemaVersion": "qf.v53.tianyan-query-result.v1",
        "target": TARGET,
        "actionId": request["actionId"],
        "queryId": query_id,
        "providerStatus": provider_status,
        "rawResult": result,
        "rawResultSha256": _sha256(raw_json),
        "queriedAt": _now(),
        "resubmitted": False,
    }


def main() -> int:
    request = json.load(sys.stdin)
    if not isinstance(request, dict):
        raise V53TianyanError("V53_TIANYAN_INPUT_INVALID", "request must be one JSON object")
    action = request.get("action")
    if action == "doctor":
        result = _doctor()
    elif action == "discover":
        result = _discover(request)
    elif action == "map":
        result = _map(request)
    elif action == "map_frozen":
        result = _map(request, frozen=True)
    elif action == "validate":
        result = _validate(request)
    elif action == "submit":
        result = _submit(request)
    elif action == "query":
        result = _query(request)
    else:
        raise V53TianyanError("V53_TIANYAN_ACTION_FORBIDDEN", "action is not registered")
    json.dump(_json_safe(result), sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def cli_main() -> int:
    try:
        return main()
    except Exception as error:  # noqa: BLE001 - boundary redacts provider failures.
        code = error.code if isinstance(error, V53TianyanError) else "V53_TIANYAN_PROVIDER_FAILED"
        payload: dict[str, Any] = {
            "schemaVersion": "qf.v53.tianyan-error.v1",
            "status": "FAILED",
            "code": code,
            "message": str(error)
            if isinstance(error, V53TianyanError)
            else "provider operation failed",
            "resubmissionAllowed": False,
        }
        if isinstance(error, ReceiptPersistenceError):
            payload["queryId"] = error.query_id
            payload["receiptPersisted"] = False
        print(code, file=sys.stderr)
        json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True)
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())
