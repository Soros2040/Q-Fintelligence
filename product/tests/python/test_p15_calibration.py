from __future__ import annotations

from typing import Any

import pytest
from qf_quantum_worker import tianyan_job


class _CalibrationPlatform:
    def __init__(self) -> None:
        self.query_modes: list[bool] = []

    def query_quantum_computer_list(self) -> list[list[str]]:
        return [["176 qubits", "running", "free", "tianyan176"]]

    def download_config(self, *, machine: str, read_time: None) -> dict[str, Any]:
        assert machine == "tianyan176"
        assert read_time is None
        edges = {
            "G0": ["Q0", "Q1"],
            "G1": ["Q1", "Q2"],
            "G2": ["Q2", "Q3"],
            "G3": ["Q3", "Q4"],
            "G4": ["Q4", "Q5"],
            "G5": ["Q5", "Q0"],
        }
        qubits = {
            f"Q{index}": {
                "frequency": 5.0 + index / 100,
                "T1": 80.0 + index,
                "T2": 60.0 + index,
                "readout_error": 0.01 + index / 10_000,
                "single_gate_error": 0.001 + index / 100_000,
            }
            for index in range(6)
        }
        return {
            "read_time": "2026-07-25 12:00:00",
            "overview": {"coupler_map": edges},
            "qubitCalibration": qubits,
            "singleQubitGate": {"X": {}, "Y2P": {}},
            "twoQubitGate": {"CZ": {}},
        }

    def download_fsim_config(
        self,
        *,
        machine: str,
        read_time: None,
    ) -> dict[tuple[str, str], dict[str, float]]:
        assert machine == "tianyan176"
        return {
            (f"Q{index}", f"Q{(index + 1) % 6}"): {
                "theta": 0.5,
                "phi": 0.02,
                "two_gate_error": 0.008 + index / 10_000,
            }
            for index in range(6)
        }

    def query_experiment(self, **kwargs: Any) -> list[dict[str, Any]]:
        corrected = bool(kwargs["readout_calibration"])
        self.query_modes.append(corrected)
        if corrected:
            assert isinstance(kwargs["machine_config"], dict)
        return [
            {"query_id": query_id, "probability": {"000111": 1.0}}
            for query_id in kwargs["query_id"]
        ]


def test_calibration_snapshot_normalizes_hashes_diff_and_noise_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _CalibrationPlatform()
    monkeypatch.setattr(tianyan_job, "_platform", lambda _machine=None: platform)

    package = tianyan_job._calibration_snapshot({"machine_name": "tianyan176"})
    normalized = package["normalized"]
    assert normalized["machine_status"] == "running"
    assert normalized["data_completeness"] == "COMPLETE"
    assert normalized["active_qubits"] == [f"Q{index}" for index in range(6)]
    assert len(normalized["couplers"]) == 6
    assert normalized["raw_sha256"]
    assert normalized["normalized_sha256"]
    assert package["diff"]["reason"] == "no previous snapshot"
    assert package["calibration_csv"].startswith("record_type,qubit")
    assert "<svg" in package["topology_svg"]

    mapping = tianyan_job._noise_aware_mapping(
        {"normalized_snapshot": normalized, "logical_qubits": 6}
    )
    assert mapping["status"] == "PASS"
    assert len(mapping["selected"]["physical_qubits"]) == 6
    assert mapping["selected"]["physical_qubits"][0] == "Q0"
    assert mapping["candidate_count"] >= 1


def test_p15_readout_query_uses_the_immutable_machine_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _CalibrationPlatform()
    monkeypatch.setattr(tianyan_job, "_platform", lambda _machine=None: platform)
    result = tianyan_job._query_batch_p15(
        {
            "machine_name": "tianyan176",
            "query_ids": ["query-a", "query-b"],
            "machine_config": {"overview": {"coupler_map": {}}},
            "max_wait_seconds": 1,
            "poll_interval_seconds": 1,
        }
    )
    assert result["raw_result_count"] == 2
    assert result["corrected_result_count"] == 2
    assert result["readout_calibration"] is True
    assert platform.query_modes == [False, True]


def test_missing_optional_fsim_is_recorded_without_losing_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _CalibrationPlatform()

    def missing_fsim(**_kwargs: Any) -> dict[str, Any]:
        raise KeyError("FSIM configuration not found")

    monkeypatch.setattr(platform, "download_fsim_config", missing_fsim)
    monkeypatch.setattr(tianyan_job, "_platform", lambda _machine=None: platform)
    package = tianyan_job._calibration_snapshot({"machine_name": "tianyan176"})
    assert package["raw"]["fsim_status"] == "UNAVAILABLE"
    assert package["normalized"]["data_completeness"] == "PARTIAL"
    assert len(package["normalized"]["couplers"]) == 6
    assert any(
        "FSIM calibration unavailable" in warning
        for warning in package["normalized"]["warnings"]
    )


def test_live_cqlib_param_list_schema_is_complete_without_optional_fsim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _CalibrationPlatform()
    qubits = [f"Q{index}" for index in range(6)]
    gates = [f"G{index}" for index in range(6)]

    def live_config(*, machine: str, read_time: None) -> dict[str, Any]:
        assert machine == "tianyan176"
        assert read_time is None
        return {
            "read_time": "2026-07-25 13:31:17",
            "overview": {
                "coupler_map": {
                    gate: [qubits[index], qubits[(index + 1) % 6]]
                    for index, gate in enumerate(gates)
                }
            },
            "qubit": {
                "qubitCalibration": {
                    "f01": {
                        "qubit_used": qubits,
                        "param_list": [4.88 + index / 100 for index in range(6)],
                    },
                    "T1": {
                        "qubit_used": qubits,
                        "param_list": [25.0 + index for index in range(6)],
                    },
                    "T2": {
                        "qubit_used": qubits,
                        "param_list": [30.0 + index for index in range(6)],
                    },
                    "readout_error": {
                        "qubit_used": qubits,
                        "param_list": [3.6 + index / 10 for index in range(6)],
                        "unit": "%",
                    },
                },
                "singleQubit": {
                    "X": {
                        "gate_error": {
                            "qubit_used": qubits,
                            "param_list": [0.1 + index / 100 for index in range(6)],
                            "unit": "%",
                        }
                    }
                },
            },
            "twoQubitGate": {
                "CZ": {
                    "gate_error": {
                        "qubit_used": gates,
                        "param_list": [1.1 + index / 100 for index in range(6)],
                        "unit": "%",
                    }
                }
            },
        }

    def missing_fsim(**_kwargs: Any) -> dict[str, Any]:
        raise KeyError("FSIM configuration not found")

    monkeypatch.setattr(platform, "download_config", live_config)
    monkeypatch.setattr(platform, "download_fsim_config", missing_fsim)
    monkeypatch.setattr(tianyan_job, "_platform", lambda _machine=None: platform)

    package = tianyan_job._calibration_snapshot({"machine_name": "tianyan176"})
    normalized = package["normalized"]
    assert normalized["data_completeness"] == "COMPLETE"
    assert normalized["missing_fields"] == []
    assert normalized["qubit_frequency"]["Q0"] == pytest.approx(4.88)
    assert normalized["T1"]["Q0"] == pytest.approx(25.0)
    assert normalized["T2"]["Q0"] == pytest.approx(30.0)
    assert normalized["readout_error"]["Q0"] == pytest.approx(0.036)
    assert normalized["single_gate_error"]["Q0"] == pytest.approx(0.001)
    assert normalized["two_gate_error"]["Q0-Q1"] == pytest.approx(0.011)
    assert "X" in normalized["gate_set"]
    assert "CZ" in normalized["gate_set"]
    assert any(
        "FSIM calibration unavailable" in warning
        for warning in normalized["warnings"]
    )


def test_p15_mapping_fails_closed_without_topology() -> None:
    with pytest.raises(ValueError, match="topology"):
        tianyan_job._noise_aware_mapping(
            {
                "normalized_snapshot": {
                    "machine_name": "tianyan176",
                    "qubits": [],
                    "couplers": [],
                },
                "logical_qubits": 6,
            }
        )
