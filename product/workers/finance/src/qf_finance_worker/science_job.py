from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from qf_finance_worker.science import (
    dump_json,
    load_json,
    run_active_reproducibility_block,
    run_reproducibility_sweep,
    run_science_pipeline,
)

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
    raise RuntimeError("network and subprocess access are disabled in the science runner")


def main() -> int:
    request = json.load(sys.stdin)
    allowed = {"run_pipeline", "reproducibility_sweep", "active_reproducibility_block"}
    action = request.get("action")
    if action not in allowed:
        raise ValueError("science action is not registered")
    root = Path(request["workspace_root"]).resolve(strict=True)
    input_path = Path(request["input_path"]).resolve(strict=True)
    output_path = Path(request["output_path"]).resolve()
    if root not in input_path.parents or root not in output_path.parents:
        raise ValueError("science path escaped the campaign workspace")
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
    if action == "run_pipeline":
        result = run_science_pipeline(load_json(input_path), int(request.get("seed", 20260721)))
    elif action == "reproducibility_sweep":
        result = run_reproducibility_sweep(load_json(input_path), int(request["seed"]))
    else:
        result = run_active_reproducibility_block(
            load_json(input_path),
            int(request["seed_start"]),
            int(request["minimum_compute_seconds"]),
        )
    digest = dump_json(output_path, result)
    json.dump(
        {
            "schema_version": "qf.tool-result-envelope.v1",
            "status": "COMPLETED",
            "output_path": str(output_path.relative_to(root)),
            "sha256": digest,
            "warnings": [],
        },
        sys.stdout,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - CLI converts all failures to one JSON envelope.
        json.dump(
            {
                "schema_version": "qf.tool-result-envelope.v1",
                "status": "FAILED",
                "error_type": type(error).__name__,
                "message": str(error)[:1000],
            },
            sys.stdout,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        raise SystemExit(1) from None
