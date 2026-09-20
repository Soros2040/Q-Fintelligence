from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

ALLOWED_IMPORTS = {
    "__future__",
    "collections",
    "hashlib",
    "itertools",
    "json",
    "math",
    "statistics",
    "typing",
}
REGISTERED_TOOL_NAMES = {
    "financial_result_diagnostics",
    "quantum_result_diagnostics",
    "multiformat_data_quality",
    "cardinality_qaoa_builder",
    "hardware_batch_analyzer",
    "portfolio_qubo_pipeline",
    "qaoa_counterexample_diagnostics",
}
BLOCKED_CALLS = {
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "globals",
    "input",
    "locals",
    "open",
    "vars",
    "__import__",
}
BLOCKED_ATTRIBUTES = {
    "call",
    "check_call",
    "check_output",
    "connect",
    "create_connection",
    "fork",
    "kill",
    "popen",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "run",
    "socket",
    "system",
    "unlink",
}
SECRET_PREFIXES = (
    "OPENAI_",
    "DEEPSEEK_",
    "TUSHARE_",
    "TIANYAN_",
    "HTTP_",
    "HTTPS_",
    "http_",
    "https_",
)


def _blocked(*_args, **_kwargs):
    raise RuntimeError("network and subprocess access are disabled in generated tools")


def _resolve_inside(root: Path, candidate: str) -> Path:
    resolved = Path(candidate).resolve(strict=True)
    if root != resolved and root not in resolved.parents:
        raise ValueError("generated tool path escaped the Run workspace")
    return resolved


def _validate_source(path: Path, *, allow_pytest: bool) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    execute_found = False
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", maxsplit=1)[0]
                generated_modules = REGISTERED_TOOL_NAMES
                if (
                    root not in ALLOWED_IMPORTS
                    and not (allow_pytest and root == "pytest")
                    and not (allow_pytest and root in generated_modules)
                ):
                    raise ValueError(f"generated source import is not allowed: {root}")
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", maxsplit=1)[0]
            if root not in ALLOWED_IMPORTS and not (allow_pytest and root == "pytest"):
                generated_modules = REGISTERED_TOOL_NAMES
                if not (allow_pytest and root in generated_modules):
                    raise ValueError(f"generated source import is not allowed: {root}")
            imports.append(node.module or "")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.ClassDef)):
            raise ValueError(f"generated source node is not allowed: {type(node).__name__}")
        elif isinstance(node, ast.FunctionDef) and node.name == "execute":
            execute_found = True
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_CALLS:
                raise ValueError(f"generated source call is not allowed: {node.func.id}")
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr.lower() in BLOCKED_ATTRIBUTES
            ):
                raise ValueError(
                    f"generated source attribute call is not allowed: {node.func.attr}"
                )
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("generated source dunder attribute access is not allowed")
    if not allow_pytest and not execute_found:
        raise ValueError("generated tool must define execute(payload)")
    return {
        "path": path.name,
        "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "imports": sorted(set(imports)),
        "execute_found": execute_found,
        "ast_nodes": sum(1 for _ in ast.walk(tree)),
    }


def _load_tool(path: Path) -> ModuleType:
    module_name = f"qf_generated_{path.stem}_{hashlib.sha256(str(path).encode()).hexdigest()[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("generated tool could not be loaded")
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()) as stdout:
        spec.loader.exec_module(module)
    if stdout.getvalue():
        raise ValueError("generated tool emitted stdout during registration")
    if not callable(getattr(module, "execute", None)):
        raise ValueError("generated tool execute function is not callable")
    return module


def _invoke(module: ModuleType, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        result = module.execute(payload)
    if captured.getvalue():
        raise ValueError("generated tool wrote to stdout")
    if not isinstance(result, dict):
        raise ValueError("generated tool result must be an object")
    if result.get("schema_version") != "qf.generated-tool-result.v1":
        raise ValueError("generated tool result schema version is invalid")
    if result.get("tool_name") != tool_name or result.get("status") != "COMPLETED":
        raise ValueError("generated tool result identity or status is invalid")
    if not isinstance(result.get("diagnostics"), dict):
        raise ValueError("generated tool result diagnostics must be an object")
    return result


def _perturb_payload(base: dict[str, Any], index: int) -> dict[str, Any]:
    scale = 1.0 + ((index % 17) - 8) * 0.0001

    def visit(value: Any) -> Any:
        if isinstance(value, bool) or value is None or isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value) * scale
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            return {str(key): visit(item) for key, item in value.items()}
        return value

    payload = visit(base)
    if not isinstance(payload, dict):
        raise ValueError("base payload must be an object")
    payload["validation_sample_index"] = index
    return payload


def _sanitize_runtime() -> None:
    for name in list(os.environ):
        if name.startswith(SECRET_PREFIXES) or name in {
            "ALL_PROXY",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        }:
            os.environ.pop(name, None)
    socket.create_connection = _blocked
    socket.socket.connect = _blocked
    subprocess.Popen = _blocked
    subprocess.run = _blocked
    subprocess.call = _blocked
    subprocess.check_call = _blocked
    subprocess.check_output = _blocked


def main() -> int:
    request = json.load(sys.stdin)
    action = request.get("action")
    if action not in {"validate", "invoke", "active_validation_block"}:
        raise ValueError("tool factory action is not registered")
    root = Path(str(request["workspace_root"])).resolve(strict=True)
    tool_path = _resolve_inside(root, str(request["tool_path"]))
    test_path = (
        _resolve_inside(root, str(request["test_path"])) if request.get("test_path") else None
    )
    tool_name = str(request["tool_name"])
    if tool_name not in REGISTERED_TOOL_NAMES:
        raise ValueError("generated tool name is not registered")
    validation = _validate_source(tool_path, allow_pytest=False)
    if test_path is not None:
        validation["test"] = _validate_source(test_path, allow_pytest=True)
    _sanitize_runtime()
    if action == "validate":
        result = {
            "schema_version": "qf.generated-tool-validation.v1",
            "status": "COMPLETED",
            "tool_name": tool_name,
            "validation": validation,
        }
    else:
        module = _load_tool(tool_path)
        payload = request.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("generated tool payload must be an object")
        if action == "invoke":
            result = _invoke(module, tool_name, payload)
        else:
            minimum_seconds = int(request.get("minimum_compute_seconds", 60))
            if minimum_seconds < 1 or minimum_seconds > 300:
                raise ValueError("tool validation block must be between 1 and 300 seconds")
            started = time.perf_counter()
            index = int(request.get("sample_start", 0))
            start_index = index
            digest = hashlib.sha256()
            representatives: list[dict[str, Any]] = []
            while time.perf_counter() - started < minimum_seconds:
                output = _invoke(module, tool_name, _perturb_payload(payload, index))
                digest.update(json.dumps(output, sort_keys=True).encode("utf-8"))
                if len(representatives) < 8:
                    representatives.append(output)
                index += 1
            result = {
                "schema_version": "qf.generated-tool-active-validation.v1",
                "status": "COMPLETED",
                "tool_name": tool_name,
                "sample_start": start_index,
                "sample_end_exclusive": index,
                "invocations": index - start_index,
                "active_compute_seconds": time.perf_counter() - started,
                "result_digest": digest.hexdigest(),
                "representative_results": representatives,
            }
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - bounded JSON process envelope.
        json.dump(
            {
                "schema_version": "qf.generated-tool-error.v1",
                "status": "FAILED",
                "error_type": type(error).__name__,
                "message": str(error)[:1000],
            },
            sys.stdout,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        raise SystemExit(1) from None
