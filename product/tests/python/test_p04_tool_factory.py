from __future__ import annotations

from pathlib import Path

import pytest
from qf_finance_worker import tool_factory_job

SAFE_TOOL = '''
from __future__ import annotations

import statistics


def execute(payload: dict) -> dict:
    values = [float(value) for value in payload["values"]]
    return {
        "schema_version": "qf.generated-tool-result.v1",
        "tool_name": "financial_result_diagnostics",
        "status": "COMPLETED",
        "diagnostics": {"mean": statistics.fmean(values)},
    }
'''


def test_generated_tool_ast_gate_and_structured_invocation(tmp_path: Path) -> None:
    tool = tmp_path / "financial_result_diagnostics.py"
    tool.write_text(SAFE_TOOL, encoding="utf-8")
    validation = tool_factory_job._validate_source(tool, allow_pytest=False)
    assert validation["execute_found"] is True
    module = tool_factory_job._load_tool(tool)
    result = tool_factory_job._invoke(
        module,
        "financial_result_diagnostics",
        {"values": [1, 2, 3]},
    )
    assert result["diagnostics"]["mean"] == 2.0


def test_generated_tool_ast_gate_rejects_filesystem_and_process_access(tmp_path: Path) -> None:
    malicious = tmp_path / "quantum_result_diagnostics.py"
    malicious.write_text(
        "import subprocess\n\ndef execute(payload):\n    return subprocess.run(payload)\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="import is not allowed"):
        tool_factory_job._validate_source(malicious, allow_pytest=False)
