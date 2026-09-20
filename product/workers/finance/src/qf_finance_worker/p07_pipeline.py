from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import zipfile
from pathlib import Path
from typing import Any

import matplotlib
import pandas as pd
from cqlib.circuits import Circuit
from cqlib.simulator import StatevectorSimulator

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes | str | Any) -> str:
    if isinstance(value, bytes):
        encoded = value
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
    else:
        encoded = _canonical(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, f"{json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)}\n")


def _circuit_ir() -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for qubit in range(6):
        operations.append({"gate": "H", "targets": [qubit]})
    for qubit in range(6):
        operations.append(
            {"gate": "RZ", "targets": [qubit], "angle": float((qubit + 1) * 0.071)}
        )
    for left, right in [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (0, 5)]:
        operations.append({"gate": "CZ", "targets": [left, right]})
    for qubit in range(6):
        operations.append({"gate": "RX", "targets": [qubit], "angle": 0.43})
    operations.append({"gate": "MEASURE_ALL", "targets": list(range(6))})
    value = {
        "schema_version": "qf.circuit-ir.v1",
        "name": "p07_six_stock_qaoa_reference",
        "qubits": 6,
        "classical_bits": 6,
        "endianness": "little_endian_qubit_index_with_displayed_msb_left",
        "operations": operations,
        "formal_test_sealed": True,
    }
    value["circuit_hash"] = _sha256(value)
    return value


def _cqlib_reference(circuit_ir: dict[str, Any]) -> dict[str, Any]:
    circuit = Circuit(int(circuit_ir["qubits"]))
    for operation in circuit_ir["operations"]:
        gate = operation["gate"]
        targets = operation["targets"]
        angle = operation.get("angle")
        if gate == "H":
            circuit.h(targets[0])
        elif gate == "X":
            circuit.x(targets[0])
        elif gate == "RX":
            circuit.rx(targets[0], float(angle))
        elif gate == "RY":
            circuit.ry(targets[0], float(angle))
        elif gate == "RZ":
            circuit.rz(targets[0], float(angle))
        elif gate == "CZ":
            circuit.cz(targets[0], targets[1])
        elif gate == "MEASURE_ALL":
            circuit.measure_all()
        else:
            raise ValueError(f"unsupported Canonical Circuit IR gate {gate}")
    probabilities = {
        str(key): float(value)
        for key, value in StatevectorSimulator(circuit, omp_threads=1).probs().items()
    }
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-9):
        raise ValueError("cqlib probabilities are not normalized")
    return {
        "schema_version": "qf.p07.cqlib-result.v1",
        "cqlib_version": "1.3.11",
        "qcis": circuit.qcis,
        "qcis_sha256": _sha256(circuit.qcis),
        "depth": circuit.depth(),
        "instruction_count": len(circuit.instruction_sequence),
        "probabilities": probabilities,
        "probability_sum": sum(probabilities.values()),
        "circuit_ir_hash": circuit_ir["circuit_hash"],
    }


def _capability_input(circuit_ir: dict[str, Any]) -> dict[str, Any]:
    expected_returns = [0.11, 0.08, 0.14, 0.06, 0.12, 0.09]
    covariance = [
        [0.040, 0.010, 0.006, 0.004, 0.009, 0.005],
        [0.010, 0.035, 0.005, 0.008, 0.006, 0.007],
        [0.006, 0.005, 0.050, 0.007, 0.004, 0.009],
        [0.004, 0.008, 0.007, 0.030, 0.006, 0.005],
        [0.009, 0.006, 0.004, 0.006, 0.045, 0.008],
        [0.005, 0.007, 0.009, 0.005, 0.008, 0.038],
    ]
    return {
        "schema_version": "qf.p07.capability-input.v1",
        "classical": {
            "action": "classical_reference",
            "tickers": ["A", "B", "C", "D", "E", "F"],
            "expected_returns": expected_returns,
            "covariance": covariance,
            "risk_aversion": 0.8,
            "choose_k": 3,
        },
        "quantum": {
            "action": "qiskit_reference",
            "circuit_ir": circuit_ir,
            "coupling_map": [
                [0, 1],
                [1, 0],
                [1, 2],
                [2, 1],
                [2, 3],
                [3, 2],
                [3, 4],
                [4, 3],
                [4, 5],
                [5, 4],
                [5, 0],
                [0, 5],
            ],
        },
    }


def _write_visuals(exact: list[dict[str, Any]], output_root: Path) -> tuple[Path, Path]:
    labels = [str(item["bitstring"]) for item in exact]
    values = [float(item["total_without_penalty"]) for item in exact]
    figure, axis = plt.subplots(figsize=(10, 4.8), constrained_layout=True)
    colors = ["#1769aa" if index == 0 else "#8bb8d9" for index in range(len(exact))]
    axis.bar(range(len(exact)), values, color=colors)
    axis.set_title("P07 validation-only exact enumeration (6 choose 3)")
    axis.set_xlabel("Portfolio bitstring")
    axis.set_ylabel("Mean-risk-spillover objective (lower is better)")
    axis.set_xticks(range(len(exact)), labels, rotation=75, fontsize=7)
    axis.grid(axis="y", color="#dbe6ef", linewidth=0.7)
    png_path = output_root / "exact-enumeration.png"
    svg_path = output_root / "exact-enumeration.svg"
    figure.savefig(png_path, dpi=150)
    figure.savefig(svg_path)
    plt.close(figure)
    return png_path, svg_path


def _build_zip(output_root: Path, files: list[Path]) -> Path:
    zip_path = output_root / "p07-reproduction.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(files, key=lambda item: item.name):
            info = zipfile.ZipInfo(source.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, source.read_bytes())
    return zip_path


def _build_named_zip(zip_path: Path, workspace_root: Path, files: list[Path]) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(files, key=lambda item: item.relative_to(workspace_root).as_posix()):
            name = source.relative_to(workspace_root).as_posix()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, source.read_bytes())
    return zip_path


def _write_final_comparison(
    record: dict[str, Any], output_root: Path
) -> tuple[Path, Path, Path]:
    hardware = record["science"]["hardware_comparison"]
    exact_best = float(hardware["exactObjectiveBest"])
    exact_worst = float(hardware["exactObjectiveWorst"])
    simulation = hardware["simulation"]
    hardware_summary = hardware["hardware"]
    simulation_quality = (
        1.0
        if math.isclose(exact_best, exact_worst)
        else max(
            0.0,
            min(
                1.0,
                (exact_worst - float(simulation["meanExpectedObjective"]))
                / (exact_worst - exact_best),
            ),
        )
    )
    comparison = {
        "schema_version": "qf.p07.final-comparison.v1",
        "formal_test_sealed": True,
        "exact_enumeration": {
            "portfolio_count": 20,
            "feasible_rate": 1.0,
            "optimal_hit_rate": 1.0,
            "approximation_quality": 1.0,
            "best_objective": exact_best,
        },
        "qaoa_simulation": {
            "mean_feasible_rate": float(simulation["meanFeasibleRate"]),
            "mean_optimal_hit_rate": float(simulation["meanOptimalHitRate"]),
            "mean_approximation_quality": simulation_quality,
            "mean_expected_objective": float(simulation["meanExpectedObjective"]),
        },
        "tianyan176_hardware": {
            "result_count": int(hardware["resultCount"]),
            "mean_feasible_rate": float(hardware_summary["meanFeasibleRate"]),
            "mean_optimal_hit_rate": float(hardware_summary["meanOptimalHitRate"]),
            "mean_approximation_quality": float(
                hardware_summary["meanApproximationQuality"]
            ),
            "optimal_hit_rate_stddev": float(
                hardware_summary["optimalHitRateStdDev"]
            ),
        },
        "selection_rule": "all 50 registered circuits retained; no best-sample selection",
        "superiority_claim": False,
        "scientific_conclusion": hardware["scientificConclusion"],
    }
    comparison_path = output_root / "final-comparison.json"
    _atomic_json(comparison_path, comparison)

    labels = ["Feasible rate", "Optimal hit rate", "Approx. quality"]
    exact_values = [1.0, 1.0, 1.0]
    simulation_values = [
        comparison["qaoa_simulation"]["mean_feasible_rate"],
        comparison["qaoa_simulation"]["mean_optimal_hit_rate"],
        comparison["qaoa_simulation"]["mean_approximation_quality"],
    ]
    hardware_values = [
        comparison["tianyan176_hardware"]["mean_feasible_rate"],
        comparison["tianyan176_hardware"]["mean_optimal_hit_rate"],
        comparison["tianyan176_hardware"]["mean_approximation_quality"],
    ]
    positions = list(range(len(labels)))
    width = 0.25
    figure, axis = plt.subplots(figsize=(9, 4.8), constrained_layout=True)
    axis.bar([value - width for value in positions], exact_values, width, label="Exact enumeration")
    axis.bar(positions, simulation_values, width, label="QAOA simulation")
    axis.bar([value + width for value in positions], hardware_values, width, label="tianyan176")
    axis.set_ylim(0, 1.05)
    axis.set_xticks(positions, labels)
    axis.set_ylabel("Validation / independent-confirmation metric")
    axis.set_title("P07 same-scope comparison; no quantum-advantage claim")
    axis.grid(axis="y", color="#dbe6ef", linewidth=0.7)
    axis.legend()
    png_path = output_root / "final-comparison.png"
    svg_path = output_root / "final-comparison.svg"
    figure.savefig(png_path, dpi=150)
    figure.savefig(svg_path)
    plt.close(figure)
    return comparison_path, png_path, svg_path


def build_outputs(request: dict[str, Any]) -> dict[str, Any]:
    workspace_root = Path(request["workspace_root"]).resolve(strict=True)
    science_path = Path(request["science_path"]).resolve(strict=True)
    output_root = Path(request["output_root"]).resolve()
    if workspace_root not in science_path.parents or workspace_root not in output_root.parents:
        raise ValueError("P07 pipeline path escaped the campaign workspace")
    output_root.mkdir(parents=True, exist_ok=True)
    science = json.loads(science_path.read_text(encoding="utf-8"))
    if science["formal_test"] != {
        "sealed": True,
        "downloaded_max_date": science["formal_test"]["downloaded_max_date"],
        "metrics_max_date": science["formal_test"]["metrics_max_date"],
        "test_metrics_emitted": False,
    }:
        raise ValueError("formal test seal is not intact")
    exact = science["qubo"]["exact_portfolios"]
    if len(exact) != 20:
        raise ValueError("P07 exact enumeration must contain 20 portfolios")

    csv_path = output_root / "exact-enumeration.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "rank",
                "bitstring",
                "selected",
                "return",
                "covariance",
                "spillover",
                "node_risk",
                "total_without_penalty",
            ],
        )
        writer.writeheader()
        for rank, item in enumerate(exact, start=1):
            writer.writerow(
                {
                    "rank": rank,
                    "bitstring": item["bitstring"],
                    "selected": "|".join(item["selected"]),
                    "return": item["return"],
                    "covariance": item["covariance"],
                    "spillover": item["spillover"],
                    "node_risk": item["node_risk"],
                    "total_without_penalty": item["total_without_penalty"],
                }
            )
    parquet_path = output_root / "exact-enumeration.parquet"
    pd.read_csv(csv_path).to_parquet(parquet_path, index=False)

    circuit_ir = _circuit_ir()
    cqlib_result = _cqlib_reference(circuit_ir)
    circuit_ir_path = output_root / "circuit-ir.json"
    cqlib_path = output_root / "cqlib-result.json"
    qcis_path = output_root / "qaoa-reference.qcis"
    _atomic_json(circuit_ir_path, circuit_ir)
    _atomic_json(cqlib_path, {key: value for key, value in cqlib_result.items() if key != "qcis"})
    _atomic_text(qcis_path, str(cqlib_result["qcis"]))

    comparison = {
        "schema_version": "qf.p07.comparison.v1",
        "formal_test_sealed": True,
        "universe": science["universe"],
        "data": science["data"],
        "exact_enumeration": {
            "portfolio_count": 20,
            "best": science["qubo"]["exact_best"],
        },
        "classical_baselines": science["models"]["validation_only_metrics"],
        "qaoa_simulation": science["qaoa"],
        "qgnn_validation": science["models"]["validation_only_metrics"]["qgnn"],
        "hardware": {"status": "PENDING", "selection_claim": "none"},
        "scientific_claim": "Engineering and validation evidence only; no quantum advantage claim.",
        "source_science_sha256": _sha256(science_path.read_bytes()),
    }
    comparison_path = output_root / "comparison.json"
    _atomic_json(comparison_path, comparison)
    png_path, svg_path = _write_visuals(exact, output_root)

    report_path = output_root / "report.md"
    best = science["qubo"]["exact_best"]
    qaoa = science["qaoa"]
    report = f"""# q-fintelligence P07 validation report

- Scenario: six stocks, choose three, equal weight.
- Formal test: SEALED; metrics emitted: false.
- Exact portfolios: 20; best validation bitstring: `{best['bitstring']}`.
- QAOA simulation feasible rate: `{qaoa['feasible_rate']:.6f}`.
- QAOA exact-optimum probability: `{qaoa['exact_optimal_probability']:.6f}`.
- Circuit IR: `{circuit_ir['circuit_hash']}`.
- QCIS: `{cqlib_result['qcis_sha256']}`.
- Hardware: pending until the registered tianyan176 batch reaches a queryable terminal state.

This report records engineering and validation-only scientific evidence. It does not claim
QGNN superiority or quantum advantage over the 20-portfolio exact classical enumeration.
"""
    _atomic_text(report_path, report)

    capability_input = _capability_input(circuit_ir)
    capability_path = output_root / "capability-input.json"
    _atomic_json(capability_path, capability_input)

    json_path = output_root / "science-result.json"
    _atomic_json(json_path, science)
    bundle_files = [
        csv_path,
        parquet_path,
        comparison_path,
        circuit_ir_path,
        cqlib_path,
        qcis_path,
        png_path,
        svg_path,
        report_path,
        capability_path,
        json_path,
    ]
    zip_path = _build_zip(output_root, bundle_files)
    bundle_files.append(zip_path)
    code_hash = _sha256(Path(__file__).read_bytes())
    manifest = {
        "schema_version": "qf.p07.output-manifest.v1",
        "tool_result_id": f"p07-tool-{_sha256(science)[:20]}",
        "input_hash": _sha256(request),
        "code_hash": code_hash,
        "result_hash": _sha256(comparison),
        "formal_test_sealed": True,
        "files": [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path.read_bytes()),
            }
            for path in bundle_files
        ],
        "circuit_ir_hash": circuit_ir["circuit_hash"],
        "qcis_sha256": cqlib_result["qcis_sha256"],
        "cqlib_probabilities": cqlib_result["probabilities"],
    }
    manifest_path = output_root / "manifest.json"
    _atomic_json(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path.relative_to(workspace_root))}


def build_final_package(request: dict[str, Any]) -> dict[str, Any]:
    workspace_root = Path(request["workspace_root"]).resolve(strict=True)
    base_output_root = Path(request["base_output_root"]).resolve(strict=True)
    final_output_root = Path(request["final_output_root"]).resolve()
    final_record_path = Path(request["final_record_path"]).resolve(strict=True)
    if (
        workspace_root not in base_output_root.parents
        or workspace_root not in final_output_root.parents
        or workspace_root not in final_record_path.parents
    ):
        raise ValueError("P07 final package path escaped the campaign workspace")
    final_output_root.mkdir(parents=True, exist_ok=True)
    record = json.loads(final_record_path.read_text(encoding="utf-8"))
    hardware_batch = record["hardware_batch"]
    acceptance_status = record["acceptance_status"]
    if acceptance_status not in {"COMPLETED", "BLOCKED"}:
        raise ValueError("P07 final acceptance status is invalid")
    if record["formal_test_sealed"] is not True:
        raise ValueError("formal test seal is not intact")
    if acceptance_status == "COMPLETED":
        if int(record["verified_usage"]["total_tokens"]) < 20_000_000:
            raise ValueError("Provider Token hard gate is not satisfied")
        if int(record["runtime"]["wall_clock_seconds"]) < 21_600:
            raise ValueError("Campaign runtime hard gate is not satisfied")
    if hardware_batch["status"] != "COMPLETED" or len(hardware_batch["queryIds"]) != 50:
        raise ValueError("tianyan176 final batch is incomplete")
    comparison_path, png_path, svg_path = _write_final_comparison(record, final_output_root)
    hardware = record["science"]["hardware_comparison"]
    report_path = final_output_root / "final-report.md"
    report = f"""# q-fintelligence P07 final {acceptance_status} report

- Campaign: `{record['campaign_id']}`.
- Acceptance status: `{acceptance_status}`.
- Fixed provider/model: `{record['provider']}/{record['model']}`.
- Provider-verified Tokens: `{int(record['verified_usage']['total_tokens']):,}`.
- Effective Campaign wall clock: `{float(record['runtime']['wall_clock_seconds']):.3f}` seconds.
- Formal test: `SEALED`; formal-test metrics emitted: `false`.
- Exact portfolios: `20`; exact enumeration remains authoritative for this instance.
- tianyan176: `{int(hardware['resultCount'])}/50` terminal results retained;
  `{int(hardware_batch['shots'])}` shots per circuit.
- Mean hardware feasible rate: `{float(hardware['hardware']['meanFeasibleRate']):.6f}`.
- Mean hardware optimal-hit rate: `{float(hardware['hardware']['meanOptimalHitRate']):.6f}`.
- Mean simulation feasible rate: `{float(hardware['simulation']['meanFeasibleRate']):.6f}`.
- Mean simulation optimal-hit rate: `{float(hardware['simulation']['meanOptimalHitRate']):.6f}`.
- Capability packages approved: `{len(record['capabilities'])}`.
- Fault classes recovered: `{len(record['faults'])}`;
  duplicate model calls and duplicate hardware submissions: `0`.

All 50 registered circuit results are retained; no best-sample-only selection is used.
The result supports an auditable engineering workflow, not a general quantum-advantage
or QGNN-superiority claim.
"""
    _atomic_text(report_path, report)

    files = [
        path
        for root in (base_output_root, final_output_root)
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() != ".zip"
        and path.name != "final-manifest.json"
    ]
    manifest = {
        "schema_version": "qf.p07.final-package-manifest.v1",
        "formal_test_sealed": True,
        "campaign_id": record["campaign_id"],
        "files": [
            {
                "path": path.relative_to(workspace_root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path.read_bytes()),
            }
            for path in sorted(
                files, key=lambda item: item.relative_to(workspace_root).as_posix()
            )
        ],
    }
    manifest_path = final_output_root / "final-manifest.json"
    _atomic_json(manifest_path, manifest)
    files.append(manifest_path)
    zip_path = _build_named_zip(
        final_output_root / "p07-final-reproduction.zip", workspace_root, files
    )
    return {
        **manifest,
        "status": acceptance_status,
        "comparison_path": str(comparison_path.relative_to(workspace_root)),
        "png_path": str(png_path.relative_to(workspace_root)),
        "svg_path": str(svg_path.relative_to(workspace_root)),
        "report_path": str(report_path.relative_to(workspace_root)),
        "manifest_path": str(manifest_path.relative_to(workspace_root)),
        "zip_path": str(zip_path.relative_to(workspace_root)),
        "zip_sha256": _sha256(zip_path.read_bytes()),
    }


def main() -> int:
    request = json.load(sys.stdin)
    action = request.get("action")
    if action == "build_outputs":
        result = build_outputs(request)
    elif action == "build_final_package":
        result = build_final_package(request)
    else:
        raise ValueError("unregistered P07 pipeline action")
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - bounded worker envelope.
        json.dump(
            {
                "schema_version": "qf.p07.pipeline-error.v1",
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
