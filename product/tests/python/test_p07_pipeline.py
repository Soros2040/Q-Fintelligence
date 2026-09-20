from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from qf_finance_worker import p07_pipeline


def test_p07_final_package_contains_actual_hardware_comparison(tmp_path: Path) -> None:
    workspace = tmp_path / "campaign"
    base = workspace / "outputs" / "v1"
    final = workspace / "outputs" / "v2"
    base.mkdir(parents=True)
    final.mkdir(parents=True)
    (base / "exact-enumeration.csv").write_text("rank,bitstring\n1,011010\n", encoding="utf-8")
    record = {
        "campaign_id": "p07_campaign_test",
        "acceptance_status": "COMPLETED",
        "provider": "openai/getoken",
        "model": "gpt-5.6",
        "formal_test_sealed": True,
        "verified_usage": {"total_tokens": 20_000_000},
        "runtime": {"wall_clock_seconds": 21_600},
        "capabilities": [{"status": "APPROVED"}, {"status": "APPROVED"}],
        "faults": [{"status": "RECOVERED"} for _ in range(5)],
        "hardware_batch": {
            "status": "COMPLETED",
            "queryIds": [str(index) for index in range(50)],
            "shots": 100,
        },
        "science": {
            "hardware_comparison": {
                "resultCount": 50,
                "exactObjectiveBest": -0.25,
                "exactObjectiveWorst": 0.35,
                "hardware": {
                    "meanFeasibleRate": 0.62,
                    "meanOptimalHitRate": 0.04,
                    "meanApproximationQuality": 0.71,
                    "optimalHitRateStdDev": 0.02,
                },
                "simulation": {
                    "meanFeasibleRate": 0.68,
                    "meanOptimalHitRate": 0.05,
                    "meanExpectedObjective": -0.10,
                },
                "scientificConclusion": "No general quantum advantage claim.",
            }
        },
    }
    record_path = final / "final-acceptance.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    result = p07_pipeline.build_final_package(
        {
            "workspace_root": str(workspace),
            "base_output_root": str(base),
            "final_output_root": str(final),
            "final_record_path": str(record_path),
        }
    )
    zip_path = workspace / result["zip_path"]
    assert result["status"] == "COMPLETED"
    assert result["zip_sha256"] == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert "openai/getoken/gpt-5.6" in (final / "final-report.md").read_text(encoding="utf-8")
    comparison = json.loads((final / "final-comparison.json").read_text(encoding="utf-8"))
    assert comparison["tianyan176_hardware"]["result_count"] == 50
    assert comparison["superiority_claim"] is False
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert "outputs/v1/exact-enumeration.csv" in names
    assert "outputs/v2/final-acceptance.json" in names
    assert "outputs/v2/final-comparison.png" in names
    assert "outputs/v2/final-comparison.svg" in names
    assert "outputs/v2/final-report.md" in names
    assert "outputs/v2/final-manifest.json" in names
