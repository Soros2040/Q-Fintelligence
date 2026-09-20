from __future__ import annotations

import math

from qf_quantum_worker import p06_workspace


def test_p06_data_quality_preserves_formal_test_seal() -> None:
    result = p06_workspace.inspect_dataset(
        {
            "source_artifact_sha256": "1" * 64,
            "parsed_upload": {
                "content": {
                    "third_party_validator": "pandera==0.32.1",
                    "rows": 3,
                    "columns": ["asset", "return"],
                    "missing": {"asset": 0, "return": 0},
                    "duplicate_rows": 0,
                    "dtypes": {"asset": "object", "return": "float64"},
                    "preview": [{"asset": "600036.SH", "return": 0.12}],
                }
            },
        }
    )
    assert result["quality_gate"] == "PASS"
    assert result["formal_test_sealed"] is True
    assert result["source_sha256"] == "1" * 64


def test_p06_controlled_qaoa_is_bounded_and_constraint_preserving() -> None:
    result = p06_workspace.build_qaoa(
        {"source_artifact_sha256": "2" * 64, "gamma": 0.24, "beta": 0.08}
    )
    manifest = result["manifest"]
    assert manifest["backend"] == "tianyan176"
    assert manifest["shots"] == 100
    assert manifest["hardware_submitted"] is False
    assert manifest["formal_test_sealed"] is True
    assert manifest["qubits"] == 6
    assert math.isclose(manifest["local_preflight"]["probability_sum"], 1.0)
    assert manifest["local_preflight"]["feasible_probability"] >= 1 - 1e-9
    assert "OPENQASM" not in result["qcis"]
    assert len(manifest["qcis_sha256"]) == 64


def test_p06_controlled_qaoa_rejects_unbounded_parameters() -> None:
    try:
        p06_workspace.build_qaoa({"gamma": 1.2, "beta": 0.08})
    except ValueError as error:
        assert "bounded validation range" in str(error)
    else:
        raise AssertionError("unbounded P06 parameters should fail closed")
