from __future__ import annotations

import pytest
from qf_quantum_worker import tianyan_job


class _FakePlatform:
    def submit_experiment(self, **_: object) -> list[str]:
        return ["query-original"]


def _request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "commit_authorized": True,
        "authorization_phase": "P03",
        "approval_hash": "a" * 64,
        "target_type": "HARDWARE",
        "purpose": "queue_state_machine_validation",
        "shots": 100,
        "estimated_execution_seconds": 240,
        "previous_p03_hardware_execution_seconds": 0,
        "qcis": "H Q0\nM Q0",
        "machine_name": "tianyan176",
    }
    request.update(overrides)
    return request


def test_p03_submit_enforces_exact_backend_shots_and_independent_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tianyan_job, "_platform", lambda _machine=None: _FakePlatform())
    result = tianyan_job._submit(_request())
    assert result["query_id"] == "query-original"
    assert result["shots"] == 100

    with pytest.raises(ValueError, match="backend"):
        tianyan_job._submit(_request(machine_name="tianyan_sw"))
    with pytest.raises(ValueError, match="100 shots"):
        tianyan_job._submit(_request(shots=101))
    with pytest.raises(ValueError, match="independent 600"):
        tianyan_job._submit(_request(previous_p03_hardware_execution_seconds=480))


def test_p04_submit_accepts_only_registered_financial_hardware_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tianyan_job, "_platform", lambda _machine=None: _FakePlatform())
    request = _request(
        authorization_phase="P04",
        purpose="representative_financial_qaoa",
        previous_hardware_execution_seconds=0,
    )
    result = tianyan_job._submit(request)
    assert result["query_id"] == "query-original"
    with pytest.raises(ValueError, match="purpose"):
        tianyan_job._submit({**request, "purpose": "queue_state_machine_validation"})
