#!/usr/bin/env python3
"""量融智枢算法软件命令行入口。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [("verify", "核验环境、数据及结果"), ("demo", "运行专题计算示例"),
                            ("analyze", "读取并复算冻结结果"), ("recompute", "执行指定实验的冻结计算任务")]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", type=Path)
        p.add_argument("--data-root", type=Path)
        p.add_argument("--output-dir", type=Path)
        p.add_argument("--run-id")
        if name == "verify":
            p.add_argument("--full", action="store_true")
        elif name in {"demo", "analyze"}:
            p.add_argument("--topic", choices=["quickstart", "data", "risk", "bridge", "qaoa", "qas", "results"])
            p.add_argument("--seed", type=int)
            p.add_argument("--shots", type=int)
            p.add_argument('--risk-lambda', type=float)
            p.add_argument('--cost', type=float)
            p.add_argument('--qaoa-gamma', type=float)
            p.add_argument('--qaoa-beta', type=float)
            p.add_argument('--qaoa-mode', choices=['fixed', 'optimize'])
        else:
            p.add_argument("--experiment", choices=[f"E{i:02d}" for i in range(1, 8)], required=True)
            p.add_argument("--job-id", help="仅执行冻结清单中的指定任务")
            p.add_argument("--stage", choices=["all", "prepare", "execute", "summarize"], default="all")
            p.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    from qf_algorithm import api
    options = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    allowed = {"data_root", "output_dir", "run_id"}
    if args.command in {'demo', 'analyze'}:
        allowed |= {'topic', 'seed', 'shots', 'risk_lambda', 'cost', 'qaoa_gamma', 'qaoa_beta', 'qaoa_mode'}
    if set(options) - allowed:
        parser.error("Unsupported configuration fields: " + ", ".join(sorted(set(options)-allowed)))
    for key in allowed:
        if getattr(args, key, None) is not None:
            options[key] = getattr(args, key)
    if args.command == "verify":
        result = api.verify(options.get("data_root"), full=args.full)
    elif args.command == "recompute":
        from qf_algorithm.recompute import recompute
        result = recompute(args.experiment, data_root=options.get("data_root"), output_dir=options.get("output_dir"),
                           run_id=options.get("run_id"), stage=args.stage, job_id=args.job_id, resume=args.resume)
    else:
        options.setdefault("topic", "quickstart" if args.command == "demo" else "results")
        bundle = api.run_topic(mode="compute" if args.command == "demo" else "review", **options)
        result = {"status": "COMPLETE", "topic": bundle.topic, "mode": bundle.mode, "output_dir": str(bundle.output_dir),
                  "summary": bundle.summary, "tables": {k: len(t) for k, t in bundle.tables.items()},
                  "figures": [p.name for p in bundle.figures]}
    print(json.dumps(api.clean(result), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(json.dumps({"status": "ERROR", "type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
