# P16 change authorship category: supervisor_infrastructure

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
import re
import sys
from csv import DictWriter
from datetime import UTC, datetime
from html import escape
from typing import Any

from cqlib import QuantumLanguage, TianYanPlatform
from cqlib.circuits import Circuit
from cqlib.simulator import StatevectorSimulator


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


def _platform(machine_name: str | None = None) -> TianYanPlatform:
    key = os.environ.get("TIANYAN_CONNECTION_KEY", "").strip()
    if not key:
        raise RuntimeError("TIANYAN_CONNECTION_KEY is not configured")
    return TianYanPlatform(login_key=key, machine_name=machine_name)


def _canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _walk(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    found = [(path, value)]
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_walk(child, (*path, str(key))))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(_walk(child, (*path, str(index))))
    return found


def _first_value(value: Any, aliases: set[str]) -> Any:
    normalized = {_normalized_key(alias) for alias in aliases}
    for path, child in _walk(value):
        if path and _normalized_key(path[-1]) in normalized:
            return child
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _qubit_name(value: object) -> str | None:
    text = str(value).strip()
    match = re.fullmatch(r"(?:Q|q)?(\d{1,3})", text)
    return f"Q{int(match.group(1))}" if match else None


def _normalize_edges(value: Any) -> list[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()

    def add(left: object, right: object) -> None:
        source = _qubit_name(left)
        target = _qubit_name(right)
        if source and target and source != target:
            edges.add(tuple(sorted((source, target), key=lambda item: int(item[1:]))))

    if isinstance(value, dict):
        for key, child in value.items():
            key_qubit = _qubit_name(key)
            if isinstance(child, (list, tuple)):
                if len(child) == 2 and all(_qubit_name(item) for item in child):
                    add(child[0], child[1])
                elif key_qubit:
                    for item in child:
                        add(key_qubit, item)
            elif isinstance(child, dict):
                left = _first_value(child, {"source", "from", "qubit1", "q1", "control"})
                right = _first_value(child, {"target", "to", "qubit2", "q2"})
                if left is not None and right is not None:
                    add(left, right)
                elif key_qubit:
                    for nested_key in child:
                        add(key_qubit, nested_key)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                add(item[0], item[1])
            elif isinstance(item, dict):
                left = _first_value(item, {"source", "from", "qubit1", "q1", "control"})
                right = _first_value(item, {"target", "to", "qubit2", "q2"})
                if left is not None and right is not None:
                    add(left, right)
    return sorted(edges, key=lambda edge: (int(edge[0][1:]), int(edge[1][1:])))


def _metric_map(raw: Any, aliases: set[str], qubits: list[str]) -> dict[str, float | None]:
    normalized_aliases = {_normalized_key(alias) for alias in aliases}
    values: dict[str, float | None] = {}
    for path, child in _walk(raw):
        if not path or _normalized_key(path[-1]) not in normalized_aliases:
            continue
        if isinstance(child, dict):
            param_list = child.get("param_list")
            qubit_used = child.get("qubit_used")
            percent = str(child.get("unit", "")).strip() == "%"
            if isinstance(param_list, (list, tuple)) and isinstance(
                qubit_used, (list, tuple)
            ):
                for raw_qubit, metric in zip(qubit_used, param_list, strict=False):
                    qubit = _qubit_name(raw_qubit)
                    parsed = _float_or_none(metric)
                    if qubit and parsed is not None:
                        values[qubit] = parsed / 100 if percent else parsed
                continue
            for key, metric in child.items():
                qubit = _qubit_name(key)
                if qubit:
                    values[qubit] = _float_or_none(metric)
        elif isinstance(child, (list, tuple)):
            for index, metric in enumerate(child):
                if index < len(qubits):
                    values[qubits[index]] = _float_or_none(metric)
        elif len(path) >= 2:
            qubit = _qubit_name(path[-2])
            if qubit:
                values[qubit] = _float_or_none(child)
    for path, child in _walk(raw):
        if not isinstance(child, dict):
            continue
        qubit = _qubit_name(path[-1]) if path else None
        if not qubit:
            qubit = _qubit_name(
                _first_value(child, {"qubit", "qubit_name", "qubit_id", "name"}) or ""
            )
        if not qubit:
            continue
        for key, metric in child.items():
            if _normalized_key(key) in normalized_aliases:
                values[qubit] = _float_or_none(metric)
    return {qubit: values.get(qubit) for qubit in qubits}


def _coupler_error_map(raw_config: Any, coupler_raw: Any) -> dict[tuple[str, str], float]:
    gate_to_edge: dict[str, tuple[str, str]] = {}
    if isinstance(coupler_raw, dict):
        for gate, qubits in coupler_raw.items():
            if isinstance(qubits, (list, tuple)) and len(qubits) == 2:
                source = _qubit_name(qubits[0])
                target = _qubit_name(qubits[1])
                if source and target:
                    gate_to_edge[str(gate)] = tuple(
                        sorted((source, target), key=lambda item: int(item[1:]))
                    )
    values: dict[tuple[str, str], float] = {}
    for path, child in _walk(raw_config):
        if not path or _normalized_key(path[-1]) not in {"gateerror", "twogateerror"}:
            continue
        if not isinstance(child, dict):
            continue
        param_list = child.get("param_list")
        couplers = child.get("qubit_used")
        if not isinstance(param_list, (list, tuple)) or not isinstance(
            couplers, (list, tuple)
        ):
            continue
        percent = str(child.get("unit", "")).strip() == "%"
        for gate, metric in zip(couplers, param_list, strict=False):
            edge = gate_to_edge.get(str(gate))
            parsed = _float_or_none(metric)
            if edge and parsed is not None:
                values[edge] = parsed / 100 if percent else parsed
    return values


def _fsim_records(raw_fsim: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not isinstance(raw_fsim, dict):
        return records
    for raw_key, parameters in raw_fsim.items():
        if isinstance(raw_key, tuple) and len(raw_key) == 2:
            left, right = raw_key
        else:
            names = re.findall(r"[Qq]?\d{1,3}", str(raw_key))
            if len(names) < 2:
                continue
            left, right = names[:2]
        source = _qubit_name(left)
        target = _qubit_name(right)
        if not source or not target:
            continue
        record = {
            "source": source,
            "target": target,
            "parameters": _json_safe(parameters),
            "two_gate_error": None,
        }
        if isinstance(parameters, dict):
            record["two_gate_error"] = _float_or_none(
                _first_value(parameters, {"two_gate_error", "gate_error", "error", "infidelity"})
            )
        records.append(record)
    return sorted(records, key=lambda item: (int(item["source"][1:]), int(item["target"][1:])))


def _calibration_csv(qubits: list[dict[str, Any]], couplers: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    fields = [
        "record_type",
        "qubit",
        "source",
        "target",
        "frequency",
        "t1",
        "t2",
        "readout_error",
        "single_gate_error",
        "two_gate_error",
    ]
    writer = DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for qubit in qubits:
        writer.writerow({"record_type": "qubit", **qubit})
    for coupler in couplers:
        writer.writerow(
            {
                "record_type": "coupler",
                "source": coupler["source"],
                "target": coupler["target"],
                "two_gate_error": coupler["two_gate_error"],
            }
        )
    return output.getvalue()


def _topology_svg(qubits: list[str], couplers: list[dict[str, Any]]) -> str:
    import math

    width = 1200
    height = 900
    radius = 360
    center_x = width / 2
    center_y = height / 2
    positions = {
        qubit: (
            center_x + radius * math.cos(2 * math.pi * index / max(1, len(qubits))),
            center_y + radius * math.sin(2 * math.pi * index / max(1, len(qubits))),
        )
        for index, qubit in enumerate(qubits)
    }
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="900" viewBox="0 0 1200 900">',
        '<rect width="1200" height="900" fill="#ffffff"/>',
        (
            '<text x="36" y="48" font-family="sans-serif" font-size="24" '
            'fill="#1f2937">tianyan176 calibration topology</text>'
        ),
    ]
    for edge in couplers:
        source = edge["source"]
        target = edge["target"]
        if source not in positions or target not in positions:
            continue
        x1, y1 = positions[source]
        x2, y2 = positions[target]
        error = edge.get("two_gate_error")
        color = "#ef4444" if isinstance(error, float) and error >= 0.02 else "#94a3b8"
        lines.append(
            f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
            f'stroke="{color}" stroke-width="1.5"/>'
        )
    for qubit, (x, y) in positions.items():
        lines.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="13" fill="#2563eb"/>')
        lines.append(
            f'<text x="{x + 16:.2f}" y="{y + 5:.2f}" font-family="sans-serif" '
            f'font-size="12" fill="#334155">{escape(qubit)}</text>'
        )
    lines.append("</svg>")
    return "\n".join(lines)


def _normalize_calibration(
    raw_config: Any,
    raw_fsim: Any,
    machine_status: str,
    retrieved_at: str,
) -> dict[str, Any]:
    overview = raw_config.get("overview", {}) if isinstance(raw_config, dict) else {}
    coupler_raw = _first_value(
        overview or raw_config,
        {"coupler_map", "couplermap", "topology", "coupling_map", "couplingmap"},
    )
    edges = _normalize_edges(coupler_raw)
    explicit_qubits = _first_value(
        overview or raw_config,
        {"active_qubits", "activequbits", "qubits", "qubit_list", "qubitlist"},
    )
    qubit_names = {
        qubit
        for edge in edges
        for qubit in edge
    }
    if isinstance(explicit_qubits, dict):
        qubit_names.update(
            qubit for key in explicit_qubits for qubit in [_qubit_name(key)] if qubit
        )
    elif isinstance(explicit_qubits, (list, tuple)):
        qubit_names.update(
            qubit for item in explicit_qubits for qubit in [_qubit_name(item)] if qubit
        )
    qubits = sorted(qubit_names, key=lambda item: int(item[1:]))
    frequency = _metric_map(raw_config, {"frequency", "qubit_frequency", "f01"}, qubits)
    t1 = _metric_map(raw_config, {"t1", "t_1"}, qubits)
    t2 = _metric_map(raw_config, {"t2", "t_2"}, qubits)
    readout_error = _metric_map(
        raw_config,
        {"readout_error", "readouterror", "readout_fidelity", "readoutfidelity"},
        qubits,
    )
    single_gate_error = _metric_map(
        raw_config,
        {"single_gate_error", "singlegateerror", "gate_error", "gateerror"},
        qubits,
    )
    qubit_records = [
        {
            "qubit": qubit,
            "frequency": frequency[qubit],
            "t1": t1[qubit],
            "t2": t2[qubit],
            "readout_error": readout_error[qubit],
            "single_gate_error": single_gate_error[qubit],
        }
        for qubit in qubits
    ]
    fsim = _fsim_records(raw_fsim)
    fsim_by_edge = {
        tuple(sorted((record["source"], record["target"]))): record
        for record in fsim
    }
    two_gate_errors = _coupler_error_map(raw_config, coupler_raw)
    couplers = []
    for source, target in edges:
        fsim_record = fsim_by_edge.get(tuple(sorted((source, target))))
        couplers.append(
            {
                "source": source,
                "target": target,
                "two_gate_error": (
                    fsim_record.get("two_gate_error")
                    if fsim_record and fsim_record.get("two_gate_error") is not None
                    else two_gate_errors.get(tuple(sorted((source, target))))
                ),
                "fsim": fsim_record.get("parameters") if fsim_record else None,
            }
        )
    qubit_section = raw_config.get("qubit", {}) if isinstance(raw_config, dict) else {}
    single_gate = (
        qubit_section.get("singleQubit", {})
        if isinstance(qubit_section, dict)
        else {}
    )
    if not single_gate and isinstance(raw_config, dict):
        single_gate = raw_config.get("singleQubitGate", {})
    two_gate = raw_config.get("twoQubitGate", {}) if isinstance(raw_config, dict) else {}
    gate_set = sorted(
        {
            str(key)
            for section in (single_gate, two_gate)
            if isinstance(section, dict)
            for key in section
        }
    )
    calibration_at_value = _first_value(
        raw_config,
        {"read_time", "readtime", "calibration_at", "calibrationtime", "update_time"},
    )
    calibration_at = (
        str(calibration_at_value)
        if isinstance(calibration_at_value, (str, int, float))
        else None
    )
    required = {
        "gate_set": bool(gate_set),
        "active_qubits": bool(qubits),
        "coupler_map": bool(couplers),
        "qubit_frequency": any(item["frequency"] is not None for item in qubit_records),
        "T1": any(item["t1"] is not None for item in qubit_records),
        "T2": any(item["t2"] is not None for item in qubit_records),
        "readout_error": any(item["readout_error"] is not None for item in qubit_records),
        "single_gate_error": any(item["single_gate_error"] is not None for item in qubit_records),
        "two_gate_error_or_fsim": bool(fsim)
        or any(item["two_gate_error"] is not None for item in couplers),
    }
    missing_fields = [key for key, present in required.items() if not present]
    warnings = []
    if missing_fields:
        warnings.append(f"missing calibration fields: {', '.join(missing_fields)}")
    if not couplers:
        warnings.append("topology is unavailable; hardware submission must pause")
    completeness = "COMPLETE" if not missing_fields else ("PARTIAL" if couplers else "UNAVAILABLE")
    return {
        "schema_version": "qf.machine-calibration-snapshot.v1",
        "machine_name": "tianyan176",
        "machine_status": machine_status,
        "retrieved_at": retrieved_at,
        "calibration_at": calibration_at,
        "read_time": calibration_at,
        "cqlib_version": importlib.metadata.version("cqlib"),
        "gate_set": gate_set,
        "active_qubits": qubits,
        "coupler_map": [{"source": source, "target": target} for source, target in edges],
        "topology": {"qubits": qubits, "couplers": couplers},
        "qubits": qubit_records,
        "couplers": couplers,
        "qubit_frequency": frequency,
        "T1": t1,
        "T2": t2,
        "readout_error": readout_error,
        "single_gate_error": single_gate_error,
        "two_gate_error": {
            f"{record['source']}-{record['target']}": record["two_gate_error"]
            for record in couplers
        },
        "fsim": fsim,
        "data_completeness": completeness,
        "missing_fields": missing_fields,
        "warnings": warnings,
    }


def _calibration_diff(previous: Any, current: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(previous, dict):
        return {
            "schema_version": "qf.machine-calibration-diff.v1",
            "previous_snapshot": None,
            "changed": True,
            "reason": "no previous snapshot",
            "topology_changed": True,
            "gate_set_changed": True,
            "selected_qubits_drift": [],
        }
    previous_qubits = {
        item.get("qubit"): item
        for item in previous.get("qubits", [])
        if isinstance(item, dict) and isinstance(item.get("qubit"), str)
    }
    drift = []
    for item in current.get("qubits", []):
        if not isinstance(item, dict):
            continue
        old = previous_qubits.get(item.get("qubit"))
        if not old:
            continue
        changes: dict[str, Any] = {}
        for metric in ("readout_error", "single_gate_error", "t1", "t2"):
            before = _float_or_none(old.get(metric))
            after = _float_or_none(item.get(metric))
            if before is None or after is None:
                continue
            absolute = after - before
            relative = absolute / abs(before) if before else None
            if abs(absolute) >= 0.005 or (relative is not None and abs(relative) >= 0.2):
                changes[metric] = {
                    "before": before,
                    "after": after,
                    "absolute": absolute,
                    "relative": relative,
                }
        if changes:
            drift.append({"qubit": item.get("qubit"), "changes": changes})
    topology_changed = previous.get("coupler_map") != current.get("coupler_map")
    gate_set_changed = previous.get("gate_set") != current.get("gate_set")
    return {
        "schema_version": "qf.machine-calibration-diff.v1",
        "previous_snapshot": previous.get("normalized_sha256"),
        "changed": bool(topology_changed or gate_set_changed or drift),
        "topology_changed": topology_changed,
        "gate_set_changed": gate_set_changed,
        "selected_qubits_drift": drift,
    }


def _calibration_snapshot(request: dict[str, Any]) -> dict[str, Any]:
    machine_name = str(request.get("machine_name", ""))
    if machine_name != "tianyan176":
        raise ValueError("calibration snapshot backend must remain tianyan176")
    discovered = _discover()
    backend = next(
        (
            item
            for item in discovered["backends"]
            if item.get("machine_name") == "tianyan176"
        ),
        None,
    )
    machine_status = (
        str(backend.get("status", "unknown")) if isinstance(backend, dict) else "unknown"
    )
    platform = _platform(machine_name)
    raw_config = platform.download_config(machine=machine_name, read_time=None)
    fsim_warning: str | None = None
    try:
        raw_fsim = platform.download_fsim_config(machine=machine_name, read_time=None)
    except Exception as error:  # noqa: BLE001 - optional public SDK dataset.
        raw_fsim = {}
        fsim_warning = (
            f"FSIM calibration unavailable: {type(error).__name__}: {str(error)[:240]}"
        )
    retrieved_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    raw = {
        "schema_version": "qf.machine-calibration-raw.v1",
        "machine_name": machine_name,
        "machine_status": machine_status,
        "retrieved_at": retrieved_at,
        "config": _json_safe(raw_config),
        "fsim_config": _json_safe(raw_fsim),
        "fsim_status": "AVAILABLE" if fsim_warning is None else "UNAVAILABLE",
        "fsim_warning": fsim_warning,
    }
    normalized = _normalize_calibration(raw_config, raw_fsim, machine_status, retrieved_at)
    if fsim_warning is not None:
        normalized["warnings"] = [*normalized["warnings"], fsim_warning]
    raw_sha256 = _sha256_json(raw)
    normalized_sha256 = _sha256_json(normalized)
    normalized["raw_sha256"] = raw_sha256
    normalized["normalized_sha256"] = normalized_sha256
    diff = _calibration_diff(request.get("previous_normalized"), normalized)
    manifest = {
        "schema_version": "qf.machine-calibration-manifest.v1",
        "machine_name": machine_name,
        "retrieved_at": retrieved_at,
        "calibration_at": normalized["calibration_at"],
        "cqlib_version": normalized["cqlib_version"],
        "raw_sha256": raw_sha256,
        "normalized_sha256": normalized_sha256,
        "data_completeness": normalized["data_completeness"],
        "missing_fields": normalized["missing_fields"],
        "warnings": normalized["warnings"],
        "artifact_roles": [
            "raw_json",
            "normalized_json",
            "calibration_csv",
            "topology_svg",
            "calibration_diff",
            "manifest",
        ],
    }
    return {
        "schema_version": "qf.machine-calibration-package.v1",
        "raw": raw,
        "normalized": normalized,
        "calibration_csv": _calibration_csv(
            normalized["qubits"],
            normalized["couplers"],
        ),
        "topology_svg": _topology_svg(
            normalized["active_qubits"],
            normalized["couplers"],
        ),
        "diff": diff,
        "manifest": manifest,
    }


def _noise_aware_mapping(request: dict[str, Any]) -> dict[str, Any]:
    snapshot = request.get("normalized_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("noise-aware mapping requires a normalized calibration snapshot")
    if snapshot.get("machine_name") != "tianyan176":
        raise ValueError("noise-aware mapping backend must remain tianyan176")
    qubit_count = int(request.get("logical_qubits", 6))
    if qubit_count != 6:
        raise ValueError("P15 portfolio mapping requires exactly six logical qubits")
    qubit_rows = {
        item.get("qubit"): item
        for item in snapshot.get("qubits", [])
        if isinstance(item, dict) and isinstance(item.get("qubit"), str)
    }
    adjacency: dict[str, set[str]] = {qubit: set() for qubit in qubit_rows}
    edge_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for item in snapshot.get("couplers", []):
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        target = item.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
        edge_rows[tuple(sorted((source, target)))] = item
    if len(adjacency) < qubit_count or not edge_rows:
        raise ValueError("calibration snapshot has no valid six-qubit topology")

    def node_cost(qubit: str) -> tuple[float, bool]:
        row = qubit_rows.get(qubit, {})
        readout = _float_or_none(row.get("readout_error"))
        single = _float_or_none(row.get("single_gate_error"))
        t1 = _float_or_none(row.get("t1"))
        t2 = _float_or_none(row.get("t2"))
        known = any(value is not None for value in (readout, single, t1, t2))
        cost = (readout if readout is not None else 0.05) + (
            single if single is not None else 0.01
        )
        if t1 and t1 > 0:
            cost += 1 / t1
        if t2 and t2 > 0:
            cost += 1 / t2
        return cost, known

    def edge_cost(left: str, right: str) -> tuple[float, bool]:
        row = edge_rows.get(tuple(sorted((left, right))), {})
        error = _float_or_none(row.get("two_gate_error"))
        return (error if error is not None else 0.03), error is not None

    candidates: dict[tuple[str, ...], dict[str, Any]] = {}
    ordered_qubits = sorted(adjacency, key=lambda item: int(item[1:]))

    def visit(path: list[str]) -> None:
        if len(path) == qubit_count:
            if path[0] not in adjacency.get(path[-1], set()):
                return
            rotations = [
                tuple(path[index:] + path[:index])
                for index in range(len(path))
            ]
            reversed_path = list(reversed(path))
            rotations.extend(
                tuple(reversed_path[index:] + reversed_path[:index])
                for index in range(len(path))
            )
            canonical = min(rotations)
            if canonical in candidates:
                return
            node_scores = [node_cost(qubit) for qubit in canonical]
            ring_edges = [
                (canonical[index], canonical[(index + 1) % len(canonical)])
                for index in range(len(canonical))
            ]
            edge_scores = [edge_cost(left, right) for left, right in ring_edges]
            score = sum(value for value, _ in node_scores) + sum(
                2 * value for value, _ in edge_scores
            )
            known = sum(1 for _, present in [*node_scores, *edge_scores] if present)
            candidates[canonical] = {
                "physical_qubits": list(canonical),
                "score": score,
                "known_metric_count": known,
                "ring_edges": [
                    {
                        "source": left,
                        "target": right,
                        "two_gate_error": edge_rows.get(
                            tuple(sorted((left, right))), {}
                        ).get("two_gate_error"),
                    }
                    for left, right in ring_edges
                ],
            }
            return
        for neighbor in sorted(adjacency.get(path[-1], set()), key=lambda item: int(item[1:])):
            if neighbor not in path:
                visit([*path, neighbor])

    for start in ordered_qubits:
        visit([start])
    if not candidates:
        raise ValueError("calibration topology has no six-qubit coupling cycle")
    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            float(item["score"]),
            [int(qubit[1:]) for qubit in item["physical_qubits"]],
        ),
    )
    selected = ranked[0]
    total_metrics = qubit_count * 2
    status = (
        "PASS"
        if int(selected["known_metric_count"]) >= total_metrics
        else "PARTIAL"
    )
    return {
        "schema_version": "qf.p15.noise-aware-mapping.v1",
        "machine_name": "tianyan176",
        "calibration_raw_sha256": snapshot.get("raw_sha256"),
        "calibration_normalized_sha256": snapshot.get("normalized_sha256"),
        "status": status,
        "selected": selected,
        "candidate_count": len(ranked),
        "top_candidates": ranked[:5],
        "warnings": (
            []
            if status == "PASS"
            else ["mapping used explicit missing-metric penalties; topology remained authoritative"]
        ),
    }


def _discover() -> dict[str, Any]:
    rows = _platform().query_quantum_computer_list()
    backends = []
    for row in rows:
        values = [str(value) for value in row]
        machine_name = next(
            (value for value in reversed(values) if value.startswith("tianyan")), ""
        )
        status = next(
            (
                value
                for value in values
                if value in {"running", "calibration", "under maintenance", "off-line", "unknown"}
            ),
            "unknown",
        )
        toll = next((value for value in values if value in {"free", "paid", "未知状态"}), "unknown")
        target_type = (
            "SIMULATOR"
            if machine_name
            in {
                "tianyan_sw",
                "tianyan_s",
                "tianyan_tn",
                "tianyan_tnn",
                "tianyan_sa",
            }
            else "HARDWARE"
        )
        backends.append(
            {
                "machine_name": machine_name,
                "status": status,
                "toll": toll,
                "target_type": target_type,
                "raw_nonsecret_values": values,
                "execution_time_bound_seconds": None,
            }
        )
    return {
        "schema_version": "qf.tianyan-discovery.v1",
        "cqlib_version": "1.3.11",
        "backends": backends,
        "hardware_runtime_bound_available": False,
    }


def _local_simulate(request: dict[str, Any]) -> dict[str, Any]:
    qcis = str(request["qcis"])
    circuit = Circuit.load(qcis)
    simulator = StatevectorSimulator(circuit, omp_threads=1)
    probabilities = {key: float(value) for key, value in simulator.probs().items()}
    samples = simulator.sample(
        shots=int(request.get("shots", 4096)), rng_seed=int(request.get("seed", 20260721))
    )
    return {
        "schema_version": "qf.cqlib-local-result.v1",
        "backend": "cqlib.StatevectorSimulator",
        "cqlib_version": "1.3.11",
        "qcis_sha256": hashlib.sha256(qcis.encode("utf-8")).hexdigest(),
        "probabilities": probabilities,
        "sample_counts": _json_safe(samples),
    }


def _validate(request: dict[str, Any]) -> dict[str, Any]:
    qcis = str(request["qcis"])
    machine_name = str(request["machine_name"])
    valid = _platform(machine_name).qcis_check_regular(qcis)
    return {
        "schema_version": "qf.tianyan-circuit-validation.v1",
        "machine_name": machine_name,
        "qcis_sha256": hashlib.sha256(qcis.encode("utf-8")).hexdigest(),
        "valid": bool(valid),
    }


def _validate_batch(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("machine_name") != "tianyan176":
        raise ValueError("P05 validation backend must remain tianyan176")
    circuits = request.get("circuits")
    phase = str(request.get("authorization_phase", "P05"))
    valid_count = (
        isinstance(circuits, list)
        and (
            (phase in {"P05", "P07"} and len(circuits) == 50)
            or (phase in {"P15", "P16"} and 1 <= len(circuits) <= 50)
        )
    )
    if not valid_count:
        raise ValueError(f"{phase} compatibility validation requires 1-50 circuits")
    validations = []
    platform = _platform("tianyan176")
    for index, qcis in enumerate(circuits):
        if not isinstance(qcis, str):
            raise ValueError(f"P05 circuit {index} is not text")
        validations.append(
            {
                "circuit_index": index,
                "qcis_sha256": hashlib.sha256(qcis.encode("utf-8")).hexdigest(),
                "valid": bool(platform.qcis_check_regular(qcis)),
            }
        )
    return {
        "schema_version": f"qf.{phase.lower()}.tianyan-batch-validation.v1",
        "authorization_phase": phase,
        "machine_name": "tianyan176",
        "validations": validations,
        "valid_count": sum(1 for item in validations if item["valid"]),
    }


def _transpile_batch_p16(request: dict[str, Any]) -> dict[str, Any]:
    """Map registered P16 virtual QCIS through cqlib without hardware submission."""
    if request.get("authorization_phase") != "P16":
        raise ValueError("P16 mapping proxy requires the P16 authorization phase")
    if request.get("machine_name") != "tianyan176":
        raise ValueError("P16 mapping backend must remain tianyan176")
    circuits = request.get("circuits")
    if not isinstance(circuits, list) or not 1 <= len(circuits) <= 50:
        raise ValueError("P16 mapping requires 1-50 circuits")
    if any(not isinstance(qcis, str) or not qcis.strip() for qcis in circuits):
        raise ValueError("P16 mapping circuits must be non-empty QCIS text")
    source_hashes = [hashlib.sha256(qcis.encode("utf-8")).hexdigest() for qcis in circuits]
    if len(set(source_hashes)) != len(source_hashes):
        raise ValueError("P16 mapping circuits must be distinct")

    from cqlib.mapping import transpile_qcis

    platform = _platform("tianyan176")
    mappings: list[dict[str, Any]] = []
    for index, qcis in enumerate(circuits):
        circuit, initial_layout, swap_mapping, virtual_to_final = transpile_qcis(
            qcis,
            platform,
        )
        mapped_qcis = str(circuit.qcis).upper()
        if not mapped_qcis.strip():
            raise RuntimeError(f"P16 mapped circuit {index} is empty")
        mappings.append(
            {
                "circuit_index": index,
                "source_qcis_sha256": source_hashes[index],
                "mapped_qcis": mapped_qcis,
                "mapped_qcis_sha256": hashlib.sha256(mapped_qcis.encode("utf-8")).hexdigest(),
                "initial_layout": _json_safe(initial_layout),
                "swap_mapping": _json_safe(swap_mapping),
                "virtual_to_final": _json_safe(virtual_to_final),
                "valid": bool(platform.qcis_check_regular(mapped_qcis)),
            }
        )
    return {
        "schema_version": "qf.p16.tianyan-mcts-mapping.v1",
        "authorization_phase": "P16",
        "machine_name": "tianyan176",
        "algorithm": "cqlib.mapping.transpile_qcis",
        "hardware_submitted": False,
        "mappings": mappings,
        "valid_count": sum(1 for item in mappings if item["valid"]),
    }


def _config_summary(request: dict[str, Any]) -> dict[str, Any]:
    machine_name = str(request["machine_name"])
    config = _platform(machine_name).download_config(machine=machine_name)
    overview = config.get("overview", {}) if isinstance(config, dict) else {}
    single_qubit = config.get("singleQubitGate", {}) if isinstance(config, dict) else {}
    two_qubit = config.get("twoQubitGate", {}) if isinstance(config, dict) else {}
    return {
        "schema_version": "qf.tianyan-config-summary.v1",
        "machine_name": machine_name,
        "overview": _json_safe(overview),
        "single_qubit_gate_families": sorted(str(key) for key in single_qubit),
        "two_qubit_gate_families": sorted(str(key) for key in two_qubit),
    }


def _submit(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("commit_authorized") is not True:
        raise ValueError("backend commit authorization is missing")
    approval_hash = str(request.get("approval_hash", ""))
    if len(approval_hash) != 64:
        raise ValueError("approval hash is invalid")
    target_type = request.get("target_type")
    purpose = request.get("purpose")
    shots = int(request["shots"])
    if target_type == "SIMULATOR":
        if purpose != "cloud_simulator" or shots > 5000:
            raise ValueError("simulator request exceeds the standing authorization")
    elif target_type == "HARDWARE":
        phase = str(request.get("authorization_phase", "P02"))
        estimate = request.get("estimated_execution_seconds")
        if phase == "P03":
            if machine_name := str(request.get("machine_name", "")):
                if machine_name != "tianyan176":
                    raise ValueError("P03 hardware backend must remain tianyan176")
            if purpose not in {"queue_state_machine_validation", "queue_state_machine_regression"}:
                raise ValueError("P03 hardware purpose is not authorized")
            if shots != 100 or float(estimate or 0) != 240:
                raise ValueError(
                    "P03 hardware request must reserve exactly 100 shots and 240 seconds"
                )
            previous = float(request.get("previous_p03_hardware_execution_seconds", 0))
            if previous < 0 or previous + float(estimate) > 600:
                raise ValueError(
                    "P03 hardware execution would exceed its independent 600 second limit"
                )
        elif phase == "P06":
            if str(request.get("machine_name", "")) != "tianyan176":
                raise ValueError("P06 hardware backend must remain tianyan176")
            if purpose != "p06_frontend_scientific_validation":
                raise ValueError("P06 hardware purpose is not authorized")
            if shots != 100 or float(estimate or 0) != 240:
                raise ValueError(
                    "P06 hardware request must reserve exactly 100 shots and 240 seconds"
                )
            previous = float(request.get("previous_p06_hardware_execution_seconds", 0))
            if previous != 0:
                raise ValueError("P06 permits at most one hardware Job")
        elif phase in {"P02", "P04"}:
            if purpose not in {"representative_qgnn_subcircuit", "representative_financial_qaoa"}:
                raise ValueError("hardware purpose is not authorized")
            if shots > 5000:
                raise ValueError("hardware shots exceed the per-job limit")
            previous = float(request.get("previous_hardware_execution_seconds", 0))
            if estimate is None or float(estimate) <= 0 or previous + float(estimate) > 600:
                raise ValueError(
                    "hardware execution cannot be proven within the cumulative 600 second limit"
                )
        else:
            raise ValueError("hardware authorization phase is not registered")
    else:
        raise ValueError("target type must be SIMULATOR or HARDWARE")
    qcis = str(request["qcis"])
    machine_name = str(request["machine_name"])
    query_ids = _platform(machine_name).submit_experiment(
        circuit=qcis,
        language=QuantumLanguage.QCIS,
        name=f"QF-{str(request.get('authorization_phase', 'P02'))}-{purpose}",
        num_shots=shots,
        machine_name=machine_name,
        is_verify=True,
    )
    if isinstance(query_ids, str):
        query_ids = [query_ids]
    if not isinstance(query_ids, list) or len(query_ids) != 1 or not query_ids[0]:
        raise RuntimeError("TianYan submission did not return exactly one query id")
    return {
        "schema_version": "qf.tianyan-submission.v1",
        "machine_name": machine_name,
        "target_type": target_type,
        "purpose": purpose,
        "shots": shots,
        "qcis_sha256": hashlib.sha256(qcis.encode("utf-8")).hexdigest(),
        "query_id": str(query_ids[0]),
        "status": "SUBMITTED",
    }


def _submit_batch(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("commit_authorized") is not True:
        raise ValueError("batch commit authorization is missing")
    phase = str(request.get("authorization_phase", ""))
    if phase not in {"P05", "P07", "P15", "P16"}:
        raise ValueError("batch submission authorization phase is not registered")
    if request.get("machine_name") != "tianyan176":
        raise ValueError("batch hardware backend must remain tianyan176")
    allowed_purposes_by_phase = {
        "P05": {
            "baseline_exploration",
            "hardware_informed_optimization",
            "independent_confirmation",
        },
        "P07": {"p07_validation_candidate_batch"},
        "P15": {"p15_evolution", "p15_independent_confirmation"},
        "P16": {"p16_quantum_advantage_validation"},
    }
    allowed_purposes = allowed_purposes_by_phase[phase]
    if request.get("purpose") not in allowed_purposes:
        raise ValueError(f"{phase} batch purpose is not authorized")
    approval_hash = str(request.get("approval_hash", ""))
    if len(approval_hash) != 64:
        raise ValueError(f"{phase} approval hash is invalid")
    shots = int(request.get("shots", 0))
    if shots != 100:
        raise ValueError(f"{phase} preregistration fixes every circuit at 100 shots")
    circuits = request.get("circuits")
    hashes = request.get("qcis_sha256")
    expected_count = len(circuits) if isinstance(circuits, list) else 0
    if (
        not isinstance(circuits, list)
        or not (
            (phase in {"P05", "P07"} and expected_count == 50)
            or (phase in {"P15", "P16"} and 1 <= expected_count <= 50)
        )
    ):
        raise ValueError(f"{phase} batch must contain its authorized 1-50 circuits")
    if not isinstance(hashes, list) or len(hashes) != expected_count:
        raise ValueError(f"{phase} batch circuit hashes must match its circuit count")
    for index, qcis in enumerate(circuits):
        if not isinstance(qcis, str) or not qcis.strip():
            raise ValueError(f"{phase} circuit {index} is empty")
        actual = hashlib.sha256(qcis.encode("utf-8")).hexdigest()
        if actual != hashes[index]:
            raise ValueError(f"{phase} circuit {index} hash mismatch")
    if len(set(hashes)) != expected_count:
        raise ValueError(f"{phase} batch circuits must be distinct")
    query_ids = _platform("tianyan176").submit_experiment(
        circuit=circuits,
        language=QuantumLanguage.QCIS,
        name=f"QF-{phase}-{request['purpose']}-B{int(request.get('batch_index', 0)):02d}",
        num_shots=shots,
        machine_name="tianyan176",
        is_verify=True,
    )
    if not isinstance(query_ids, list) or len(query_ids) != expected_count:
        count = len(query_ids) if isinstance(query_ids, list) else 0
        raise RuntimeError(
            f"TianYan batch submission returned {count} query ids"
        )
    if (
        any(not query_id for query_id in query_ids)
        or len(set(map(str, query_ids))) != expected_count
    ):
        raise RuntimeError("TianYan batch submission returned empty or duplicate query ids")
    return {
        "schema_version": f"qf.{phase.lower()}.tianyan-batch-submission.v1",
        "machine_name": "tianyan176",
        "purpose": request["purpose"],
        "batch_index": int(request.get("batch_index", 0)),
        "shots": shots,
        "query_ids": [str(query_id) for query_id in query_ids],
        "qcis_sha256": hashes,
        "status": "SUBMITTED",
    }


def _query(request: dict[str, Any]) -> dict[str, Any]:
    machine_name = str(request["machine_name"])
    query_id = str(request["query_id"])
    result = _platform(machine_name).query_experiment(
        query_id=query_id,
        max_wait_time=int(request.get("max_wait_seconds", 20)),
        sleep_time=int(request.get("poll_interval_seconds", 5)),
        readout_calibration=False,
    )
    return {
        "schema_version": "qf.tianyan-query-result.v1",
        "machine_name": machine_name,
        "query_id": query_id,
        "result": _json_safe(result),
    }


def _query_batch(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("machine_name") != "tianyan176":
        raise ValueError("P05 query backend must remain tianyan176")
    query_ids = request.get("query_ids")
    if not isinstance(query_ids, list) or not 1 <= len(query_ids) <= 50:
        raise ValueError("P05 query batch must contain between 1 and 50 Query IDs")
    results = _platform("tianyan176").query_experiment(
        query_id=[str(query_id) for query_id in query_ids],
        max_wait_time=int(request.get("max_wait_seconds", 20)),
        sleep_time=int(request.get("poll_interval_seconds", 3)),
        readout_calibration=False,
    )
    return {
        "schema_version": "qf.p05.tianyan-batch-query.v1",
        "machine_name": "tianyan176",
        "query_ids": [str(query_id) for query_id in query_ids],
        "results": _json_safe(results),
        "result_count": len(results) if isinstance(results, list) else 0,
    }


def _query_batch_p15(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("machine_name") != "tianyan176":
        raise ValueError("P15 query backend must remain tianyan176")
    query_ids = request.get("query_ids")
    if not isinstance(query_ids, list) or not 1 <= len(query_ids) <= 50:
        raise ValueError("P15 query batch must contain between 1 and 50 Query IDs")
    machine_config = request.get("machine_config")
    if not isinstance(machine_config, dict) or not machine_config:
        raise ValueError("P15 readout correction requires the immutable batch machine config")
    platform = _platform("tianyan176")
    ids = [str(query_id) for query_id in query_ids]
    raw_results = platform.query_experiment(
        query_id=ids,
        max_wait_time=int(request.get("max_wait_seconds", 20)),
        sleep_time=int(request.get("poll_interval_seconds", 3)),
        readout_calibration=False,
    )
    corrected_results = platform.query_experiment(
        query_id=ids,
        max_wait_time=int(request.get("max_wait_seconds", 20)),
        sleep_time=int(request.get("poll_interval_seconds", 3)),
        readout_calibration=True,
        machine_config=machine_config,
    )
    return {
        "schema_version": "qf.p15.tianyan-batch-query.v1",
        "machine_name": "tianyan176",
        "query_ids": ids,
        "raw_results": _json_safe(raw_results),
        "readout_corrected_results": _json_safe(corrected_results),
        "raw_result_count": len(raw_results) if isinstance(raw_results, list) else 0,
        "corrected_result_count": (
            len(corrected_results) if isinstance(corrected_results, list) else 0
        ),
        "readout_calibration": True,
    }


def _query_batch_p16(request: dict[str, Any]) -> dict[str, Any]:
    """Query only persisted P16 Query IDs with the immutable batch calibration."""
    if request.get("authorization_phase") != "P16":
        raise ValueError("P16 query requires the P16 authorization phase")
    if request.get("machine_name") != "tianyan176":
        raise ValueError("P16 query backend must remain tianyan176")
    query_ids = request.get("query_ids")
    if not isinstance(query_ids, list) or not 1 <= len(query_ids) <= 50:
        raise ValueError("P16 query batch must contain between 1 and 50 Query IDs")
    ids = [str(query_id) for query_id in query_ids]
    if any(not query_id for query_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("P16 query recovery requires distinct non-empty Query IDs")
    machine_config = request.get("machine_config")
    if not isinstance(machine_config, dict) or not machine_config:
        raise ValueError("P16 readout correction requires the immutable batch machine config")
    sdk_machine_config = (
        machine_config.get("config")
        if machine_config.get("schema_version") == "qf.machine-calibration-raw.v1"
        else machine_config
    )
    if not isinstance(sdk_machine_config, dict) or not sdk_machine_config:
        raise ValueError("P16 immutable calibration artifact contains no SDK machine config")
    platform = _platform("tianyan176")
    raw_results = platform.query_experiment(
        query_id=ids,
        max_wait_time=int(request.get("max_wait_seconds", 20)),
        sleep_time=int(request.get("poll_interval_seconds", 3)),
        readout_calibration=False,
    )
    corrected_results = platform.query_experiment(
        query_id=ids,
        max_wait_time=int(request.get("max_wait_seconds", 20)),
        sleep_time=int(request.get("poll_interval_seconds", 3)),
        readout_calibration=True,
        machine_config=sdk_machine_config,
    )
    return {
        "schema_version": "qf.p16.tianyan-batch-query.v1",
        "authorization_phase": "P16",
        "machine_name": "tianyan176",
        "query_ids": ids,
        "raw_results": _json_safe(raw_results),
        "readout_corrected_results": _json_safe(corrected_results),
        "raw_result_count": len(raw_results) if isinstance(raw_results, list) else 0,
        "corrected_result_count": (
            len(corrected_results) if isinstance(corrected_results, list) else 0
        ),
        "readout_calibration": True,
        "resubmitted": False,
    }


def main() -> int:
    request = json.load(sys.stdin)
    action = request.get("action")
    if action == "discover":
        result = _discover()
    elif action == "local_simulate":
        result = _local_simulate(request)
    elif action == "validate":
        result = _validate(request)
    elif action == "validate_batch":
        result = _validate_batch(request)
    elif action == "transpile_batch_p16":
        result = _transpile_batch_p16(request)
    elif action == "config_summary":
        result = _config_summary(request)
    elif action == "calibration_snapshot":
        result = _calibration_snapshot(request)
    elif action == "noise_aware_mapping":
        result = _noise_aware_mapping(request)
    elif action == "submit":
        result = _submit(request)
    elif action == "submit_batch":
        result = _submit_batch(request)
    elif action == "query":
        result = _query(request)
    elif action == "query_batch":
        result = _query_batch(request)
    elif action == "query_batch_p15":
        result = _query_batch_p15(request)
    elif action == "query_batch_p16":
        result = _query_batch_p16(request)
    else:
        raise ValueError("unregistered TianYan adapter action")
    json.dump(_json_safe(result), sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - adapter converts all failures to a bounded JSON error.
        json.dump(
            {
                "schema_version": "qf.tianyan-error.v1",
                "status": "FAILED",
                "error_type": type(error).__name__,
                "message": str(error)[:1000],
            },
            sys.stdout,
            ensure_ascii=False,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        raise SystemExit(1) from None
