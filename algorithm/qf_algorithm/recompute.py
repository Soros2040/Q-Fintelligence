"""Prepare and execute versioned local experiment reproductions, one task at a time."""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from .api import ROOT, ALGORITHMS, digest, identity, load_json, write_json
from .paths import DataLayout, PACKAGE


def _remap(value, campaign):
    marker = "/campaigns/e01_e07_remote_20260907T160709Z"
    prefix = value.split(marker)[0] + marker if isinstance(value, str) and marker in value else "\0"
    if isinstance(value, str) and value.startswith(prefix):
        return str(campaign) + value[len(prefix):]
    if isinstance(value, dict):
        return {k: _remap(v, campaign) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, campaign) for v in value]
    return value


def prepare(experiment, data, out, run_id):
    layout = DataLayout.resolve(data)
    campaign = layout.campaign
    epoch = "reproduce_" + run_id
    tasks, parents = [], {}

    def read(path):
        parents[str(path)] = digest(path)
        return load_json(path)

    def task(name, script, *args, result):
        tasks.append({"id": name, "command": [sys.executable, "-B", "-m", "qf_algorithm.legacy." + Path(script).stem, *map(str, args)], "result": str(result)})

    if experiment == "E01":
        task("E01_math", "run_e01.py", "--output", out / "math", result=out / "math/result.json")
    elif experiment == "E02":
        task("E02_mapping", "run_e02.py", "--data", layout.d02, "--output", out / "mapping", result=out / "mapping/result.json")
    elif experiment in {"E05", "E06"}:
        original = campaign / "quantum_freeze.json"
        freeze = read(original)
        freeze.update(campaign_id=run_id, data_epoch=epoch, parent_freeze_sha256=digest(original))
        freeze["parent_implementation"] = freeze["implementation"]
        freeze["implementation"] = [{"path": str(p), "sha256": digest(p)} for p in ALGORITHMS.glob("*.py")]
        fp, runs, selection = out / "quantum_freeze.json", out / "quantum_units", out / "e06_selection.json"
        write_json(fp, freeze)
        jobs = [j for j in freeze["jobs"] if j["experiment"] == experiment]
        for job in jobs:
            if job.get("role") == "evaluation" and not any(t["id"] == "E06_select" for t in tasks):
                task("E06_select", "quantum_search.py", "select-e06", "--freeze", fp, "--runs", runs, "--out", selection, result=selection)
            args = ["run-unit", "--freeze", fp, "--runs", runs, "--job-id", job["job_id"]]
            if job.get("role") == "evaluation":
                args += ["--selection", selection]
            task(job["job_id"], "quantum_search.py", *args, result=runs / job["job_id"] / "result.json")
        args = ["summarize", "--freeze", fp, "--runs", runs, "--out", out / "summary.json", "--experiment", experiment]
        if experiment == "E06":
            args += ["--selection", selection]
        task(experiment+"_summary", "quantum_search.py", *args, result=out / "summary.json")
    elif experiment == "E03":
        stock, finance = out / "stock", out / "finance"
        data_root = campaign / "derived/stock_v1"
        freeze = read(campaign / "stock_v1/freeze.json")
        freeze.update(data_epoch=epoch, parent_freeze_sha256=digest(campaign / "stock_v1/freeze.json"))
        write_json(stock / "freeze.json", freeze)
        task("E03_graphs", "stock_task.py", "build", "--data", data_root, "--output", stock, "--workers", 1, result=stock / "graph_manifest.json")
        for job in freeze["frontend_jobs"]:
            task("cache_"+job["job_id"], "stock_cache.py", "--stock-root", stock, "--job-id", job["job_id"], result=stock / "caches" / job["job_id"] / "manifest.json")
        for family in ["selected", "fixed", "classical"]:
            task("select_"+family, "stock_heads.py", "select", "--stock-root", stock, "--family", family, result=stock / "heads" / ("selection_"+family+".json"))
            # Selected graph is a frozen parent choice; the source verifies it again at prediction.
            parent_selection = read(campaign / "stock_v1/heads" / ("selection_"+family+".json"))
            graphs = freeze["graphs"] if family == "selected" else [parent_selection["graph"]]
            for graph in graphs:
                for seed in freeze["final_seeds"]:
                    name = f"{family}_{graph}_seed{seed}"
                    task("predict_"+name, "stock_heads.py", "predict", "--stock-root", stock, "--family", family, "--graph", graph, "--seed", seed,
                         result=stock / "predictions" / name / "manifest.json")
        task("E03_finance_freeze", "stock_finance.py", "freeze", "--stock-root", stock, "--data", data_root,
             "--campaign", layout.configuration, "--output", finance, result=finance / "freeze.json")
        task("E03_partition", "stock_finance.py", "partition", "--stock-root", stock, "--finance-root", finance, result=finance / "prediction_partitions/manifest.json")
        task("E03_selection", "stock_report.py", "select-prediction", "--finance-root", finance, result=finance / "selection_E03_stats.json")
        task("E03_summary", "stock_report.py", "report-prediction", "--finance-root", finance, result=finance / "report_E03_stats.json")
    elif experiment in {"E04", "E07"}:
        original = campaign / "finance_actions_v2/freeze.json"
        freeze = _remap(read(original), campaign)
        freeze.update(data_epoch=epoch, parent_freeze_sha256=digest(original), software_run_id=run_id)
        freeze["parent_implementation"] = freeze["implementation"]
        freeze["implementation"] = [{"name": p["name"], "sha256": digest(ALGORITHMS / p["name"])} for p in freeze["implementation"]]
        finance = out / "finance"
        write_json(finance / "freeze.json", freeze)
        for group in ["E04", "E07"]:
            selection = _remap(read(campaign / "finance_actions_v2" / ("selection_"+group+".json")), campaign)
            selection["finance_freeze_sha256"] = digest(finance / "freeze.json")
            write_json(finance / ("selection_"+group+".json"), selection)
        for job in freeze["jobs"]:
            if job["group"] != experiment:
                continue
            task(job["job_id"], "stock_finance_actions.py", "run-path", "--stock-root", campaign / "stock_v1", "--data", campaign / "derived/stock_v1",
                 "--finance-root", finance, "--job-id", job["job_id"], result=finance / "paths" / job["job_id"] / "result.json")
        task(experiment+"_summary", "stock_report.py", "report-finance", "--finance-root", finance, "--group", experiment, result=finance / ("report_"+experiment+".json"))
    else:
        raise ValueError("Unknown experiment")
    baseline = load_json(PACKAGE / "provenance/delivery_baseline.json")
    plan = {"schemaVersion": "qf.local-reproduction-plan.v1", "experiment": experiment, "run_id": run_id,
            "experimentId": experiment, "sourceCommit": baseline["workspaceCommit"], "workspaceCommit": baseline["workspaceCommit"],
            "executionMode": "LOCAL_FROZEN_RECOMPUTATION", "scientificStatus": "DESIGN_READY",
            "archiveStatus": "RUN_DIRECTORY", "releaseStatus": "LOCAL_REVIEW",
            "data_epoch": epoch, "data_root": str(data), "parents": parents,
            "implementation": {str(p): digest(p) for p in ALGORITHMS.glob("*.py")},
            "tasks": tasks, "resource_policy": {"processes": 1, "blas_threads": 1, "external_calls": 0},
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    write_json(out / "plan.json", plan)
    write_json(out / "state.json", {"status": "PREPARED", "plan_sha256": digest(out / "plan.json"), "completed": {}, "active": None})
    return plan


def recompute(experiment, *, data_root=None, output_dir=None, run_id=None, stage="all", job_id=None, resume=False):
    if experiment not in {f"E{i:02d}" for i in range(1, 8)} or stage not in {"all", "prepare", "execute", "summarize"}:
        raise ValueError("Choose a documented experiment and stage")
    run_id = run_id or dt.datetime.now(dt.timezone.utc).strftime("recompute_%Y%m%dT%H%M%S_%fZ")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", run_id):
        raise ValueError("Invalid run_id")
    data = DataLayout.resolve(data_root).root
    out = Path(output_dir or Path.cwd() / "outputs").resolve() / run_id / experiment
    if DataLayout.resolve(data).protected(out):
        raise ValueError("Choose an output directory separate from source and data")
    if out.exists():
        if not resume:
            raise FileExistsError("Use a new run_id, or --resume to continue completed task boundaries")
        plan = load_json(out / "plan.json")
        if plan["data_root"] != str(data) or plan["experiment"] != experiment:
            raise ValueError("Reproduction identity mismatch")
    else:
        out.mkdir(parents=True, exist_ok=False)
        plan = prepare(experiment, data, out, run_id)
    state = load_json(out / "state.json")
    if state["plan_sha256"] != digest(out / "plan.json"):
        raise ValueError("Reproduction plan changed")
    for path, expected in {**plan["parents"], **plan["implementation"]}.items():
        if digest(path) != expected:
            raise ValueError("Frozen reproduction input changed")
    if state.get("active") or state["status"] == "FAILED":
        raise ValueError("This run stopped inside a task; use a new run_id")
    if stage == "prepare":
        return {"status": "PREPARED", "experiment": experiment, "tasks": len(plan["tasks"]), "output_dir": str(out)}
    tasks = plan["tasks"]
    if job_id:
        tasks = [t for t in tasks if t["id"] == job_id]
        if not tasks:
            raise ValueError("job-id is absent from the frozen task list")
    elif stage == "execute":
        tasks = [t for t in tasks if not t["id"].endswith("_summary")]
    elif stage == "summarize":
        tasks = [t for t in tasks if t["id"].endswith("_summary")]
    env = os.environ.copy()
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
    env["QF_DATA_ROOT"] = str(data)
    for task in tasks:
        tid = task["id"]
        if tid in state["completed"]:
            for path, expected in state["completed"][tid]["outputs"].items():
                if digest(path) != expected:
                    raise ValueError("Completed task output changed")
            continue
        state.update(status="RUNNING", active=tid)
        write_json(out / "state.json", state)
        log = out / "logs" / (tid+".log")
        log.parent.mkdir(exist_ok=True)
        tick = time.monotonic()
        with log.open("x", encoding="utf-8") as stream:
            process = subprocess.run(task["command"], cwd=out, env=env, stdout=stream, stderr=subprocess.STDOUT)
        if process.returncode or not Path(task["result"]).is_file():
            state.update(status="FAILED", active=tid, returncode=process.returncode)
            write_json(out / "state.json", state)
            raise RuntimeError("Task stopped: " + tid + "; inspect " + str(log))
        state["completed"][tid] = {"outputs": {task["result"]: digest(task["result"])}, "log_sha256": digest(log), "wall_seconds": time.monotonic()-tick}
        state.update(status="TASK_COMPLETE", active=None)
        write_json(out / "state.json", state)
        print(json.dumps({"task": tid, "completed": len(state["completed"]), "total": len(plan["tasks"])}), flush=True)
    state["status"] = "COMPLETE" if len(state["completed"]) == len(plan["tasks"]) else "PREPARED"
    state["scientificStatus"] = "LOCAL_VERIFIED" if state["status"] == "COMPLETE" else "DESIGN_READY"
    write_json(out / "state.json", state)
    return {"status": state["status"], "experiment": experiment, "completed": len(state["completed"]), "total": len(plan["tasks"]), "output_dir": str(out)}
