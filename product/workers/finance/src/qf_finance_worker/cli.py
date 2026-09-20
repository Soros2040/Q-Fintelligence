from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict


class WorkerHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["qf.v1"] = "qf.v1"
    project: Literal["q-fintelligence"] = "q-fintelligence"
    worker: Literal["finance"] = "finance"
    process_namespace: Literal["qfintelligence"] = "qfintelligence"
    status: Literal["ok"] = "ok"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qf-finance-worker")
    parser.add_argument("command", choices=["health"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.environ.get("QF_PROCESS_NAMESPACE", "qfintelligence") != "qfintelligence":
        print("invalid QF_PROCESS_NAMESPACE", file=sys.stderr)
        return 2
    if args.command == "health":
        print(json.dumps(WorkerHealth().model_dump(), ensure_ascii=False, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
