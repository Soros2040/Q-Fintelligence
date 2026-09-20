# Authorship category: supervisor_infrastructure
# Seals the cqlib calibration-wrapper and Tianyan mapping proxy boundary.

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from qf_quantum_worker import tianyan_job


class _MappingPlatform:
    def qcis_check_regular(self, qcis: str) -> bool:
        return "CZ Q0 Q6" in qcis and "M Q0" in qcis and "M Q6" in qcis


def test_p16_mapping_uses_cqlib_result_and_never_submits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _MappingPlatform()
    monkeypatch.setattr(tianyan_job, "_platform", lambda _machine=None: platform)

    source = "X2P Q0\nCZ Q0 Q1\nM Q0\nM Q1"
    mapped = "X2P Q0\nCZ Q0 Q6\nM Q0\nM Q6"

    def fake_transpile(qcis: str, selected_platform: Any) -> tuple[Any, ...]:
        assert qcis == source
        assert selected_platform is platform
        return (
            SimpleNamespace(qcis=mapped),
            {0: 0, 1: 6},
            [],
            {0: 0, 1: 6},
        )

    monkeypatch.setattr("cqlib.mapping.transpile_qcis", fake_transpile)
    result = tianyan_job._transpile_batch_p16(
        {
            "authorization_phase": "P16",
            "machine_name": "tianyan176",
            "circuits": [source],
        }
    )

    assert result["algorithm"] == "cqlib.mapping.transpile_qcis"
    assert result["hardware_submitted"] is False
    assert result["valid_count"] == 1
    mapping = result["mappings"][0]
    assert mapping["source_qcis_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert mapping["mapped_qcis"] == mapped
    assert mapping["mapped_qcis_sha256"] == hashlib.sha256(mapped.encode()).hexdigest()
    assert mapping["initial_layout"] == {"0": 0, "1": 6}
    assert mapping["valid"] is True


@pytest.mark.parametrize(
    ("case_input", "message"),
    [
        (
            {"authorization_phase": "P15", "machine_name": "tianyan176", "circuits": ["M Q0"]},
            "P16 authorization phase",
        ),
        (
            {"authorization_phase": "P16", "machine_name": "other", "circuits": ["M Q0"]},
            "remain tianyan176",
        ),
        (
            {"authorization_phase": "P16", "machine_name": "tianyan176", "circuits": []},
            "1-50 circuits",
        ),
        (
            {
                "authorization_phase": "P16",
                "machine_name": "tianyan176",
                "circuits": ["M Q0", "M Q0"],
            },
            "must be distinct",
        ),
    ],
)
def test_p16_mapping_fails_closed(case_input: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        tianyan_job._transpile_batch_p16(case_input)


class _HardwarePlatform:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []

    def submit_experiment(self, **kwargs: Any) -> list[str]:
        self.submissions.append(kwargs)
        return [f"p16-query-{index}" for index, _ in enumerate(kwargs["circuit"])]

    def query_experiment(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.queries.append(kwargs)
        return [
            {"query_id": query_id, "probability": {"0": 1.0}}
            for query_id in kwargs["query_id"]
        ]


def test_p16_submit_batch_is_phase_purpose_count_and_shots_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _HardwarePlatform()
    monkeypatch.setattr(tianyan_job, "_platform", lambda _machine=None: platform)
    circuits = ["M Q0", "X2P Q0\nM Q0"]
    hashes = [hashlib.sha256(qcis.encode()).hexdigest() for qcis in circuits]

    result = tianyan_job._submit_batch(
        {
            "commit_authorized": True,
            "authorization_phase": "P16",
            "approval_hash": "a" * 64,
            "machine_name": "tianyan176",
            "purpose": "p16_quantum_advantage_validation",
            "batch_index": 3,
            "shots": 100,
            "circuits": circuits,
            "qcis_sha256": hashes,
        }
    )

    assert result["schema_version"] == "qf.p16.tianyan-batch-submission.v1"
    assert result["query_ids"] == ["p16-query-0", "p16-query-1"]
    assert platform.submissions[0]["num_shots"] == 100
    assert platform.submissions[0]["name"] == "QF-P16-p16_quantum_advantage_validation-B03"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"purpose": "p15_evolution"}, "purpose"),
        ({"shots": 101}, "100 shots"),
        ({"circuits": [], "qcis_sha256": []}, "1-50 circuits"),
        (
            {
                "circuits": ["M Q0", "M Q0"],
                "qcis_sha256": [
                    hashlib.sha256(b"M Q0").hexdigest(),
                    hashlib.sha256(b"M Q0").hexdigest(),
                ],
            },
            "distinct",
        ),
    ],
)
def test_p16_submit_batch_fails_closed(override: dict[str, Any], message: str) -> None:
    request: dict[str, Any] = {
        "commit_authorized": True,
        "authorization_phase": "P16",
        "approval_hash": "a" * 64,
        "machine_name": "tianyan176",
        "purpose": "p16_quantum_advantage_validation",
        "batch_index": 0,
        "shots": 100,
        "circuits": ["M Q0"],
        "qcis_sha256": [hashlib.sha256(b"M Q0").hexdigest()],
    }
    request.update(override)
    with pytest.raises(ValueError, match=message):
        tianyan_job._submit_batch(request)


def test_p16_query_uses_only_original_ids_and_immutable_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _HardwarePlatform()
    monkeypatch.setattr(tianyan_job, "_platform", lambda _machine=None: platform)
    result = tianyan_job._query_batch_p16(
        {
            "authorization_phase": "P16",
            "machine_name": "tianyan176",
            "query_ids": ["qid-a", "qid-b"],
            "machine_config": {
                "schema_version": "qf.machine-calibration-raw.v1",
                "config": {"overview": {"calibrationTime": "frozen"}},
            },
            "max_wait_seconds": 1,
            "poll_interval_seconds": 1,
        }
    )

    assert result["query_ids"] == ["qid-a", "qid-b"]
    assert result["raw_result_count"] == 2
    assert result["corrected_result_count"] == 2
    assert result["resubmitted"] is False
    assert platform.queries[0]["readout_calibration"] is False
    assert platform.queries[1]["readout_calibration"] is True
    assert platform.queries[1]["machine_config"]["overview"]["calibrationTime"] == "frozen"
    assert "schema_version" not in platform.queries[1]["machine_config"]


@pytest.mark.parametrize(
    ("case_input", "message"),
    [
        (
            {
                "authorization_phase": "P15",
                "machine_name": "tianyan176",
                "query_ids": ["qid"],
                "machine_config": {"overview": {}},
            },
            "P16 authorization phase",
        ),
        (
            {
                "authorization_phase": "P16",
                "machine_name": "tianyan176",
                "query_ids": ["qid", "qid"],
                "machine_config": {"overview": {}},
            },
            "distinct",
        ),
        (
            {
                "authorization_phase": "P16",
                "machine_name": "tianyan176",
                "query_ids": ["qid"],
                "machine_config": {},
            },
            "immutable batch machine config",
        ),
        (
            {
                "authorization_phase": "P16",
                "machine_name": "tianyan176",
                "query_ids": ["qid"],
                "machine_config": {"schema_version": "qf.machine-calibration-raw.v1"},
            },
            "contains no SDK machine config",
        ),
    ],
)
def test_p16_query_fails_closed(case_input: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        tianyan_job._query_batch_p16(case_input)
