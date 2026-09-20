"""One calculation and artifact interface shared by CLI and notebooks."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
from dataclasses import dataclass

from .paths import DataLayout, PACKAGE
from .config import validate_compute_parameters
ROOT = PACKAGE.parent
ALGORITHMS = PACKAGE / "legacy"

import numpy as np
import pandas as pd

TOPICS = ("quickstart", "data", "risk", "bridge", "qaoa", "qas", "results")
GROUPS = {
    "quickstart": ["E01", "E05"], "data": ["E01", "E02", "E03"],
    "risk": ["E02", "E03"], "bridge": ["E01", "E04"], "qaoa": ["E05"],
    "qas": ["E06"], "results": ["E01", "E02", "E03", "E04", "E05", "E06", "E07"],
}

# Value-like sentinels distinguish omitted inputs from explicit compute-only inputs.
class _DefaultFloat(float):
    pass


class _DefaultMode(str):
    pass


_DEFAULT_GAMMA = _DefaultFloat(.6)
_DEFAULT_BETA = _DefaultFloat(.25)
_DEFAULT_MODE = _DefaultMode('fixed')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean(value):
    if isinstance(value, np.ndarray):
        return clean(value.tolist())
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def identity(value):
    return hashlib.sha256(json.dumps(clean(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def environment():
    names = ["numpy", "pandas", "scipy", "scikit-learn", "cqlib", "matplotlib", "jupyterlab", "ipykernel", "nbformat", "nbclient"]
    versions = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"python": platform.python_version(), "platform": platform.system(), "packages": versions,
            "software_version": "2.1.0", "threads": {k: os.environ.get(k) for k in ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"]}}


@dataclass
class ResultBundle:
    topic: str
    mode: str
    summary: dict
    tables: dict[str, pd.DataFrame]
    figures: list[Path]
    output_dir: Path

    def table(self, name):
        return self.tables[name].copy()


class Context:
    def __init__(self, data_root, output_dir, seed, shots):
        self.layout = DataLayout.resolve(data_root)
        self.data = self.layout.root
        self.campaign = self.layout.campaign
        self.output = Path(output_dir).resolve()
        self.seed = seed
        self.shots = shots
        self.parents = {}

    def source(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.data) and not path.is_relative_to(self.layout.d02):
            raise ValueError("Input must be inside the selected data directories")
        self.parents[str(path)] = digest(path)
        return path

    def json(self, path):
        return load_json(self.source(path))


def _plot(ctx, name, draw):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False, "savefig.dpi": 160, "figure.facecolor": "white"}):
        fig, ax = plt.subplots(figsize=(8.2, 4.4), constrained_layout=True)
        draw(ax)
        path = ctx.output / (name + ".png")
        fig.savefig(path)
        plt.close(fig)
    return path


def _demo_market(seed):
    rng = np.random.default_rng(seed)
    n, a = 180, 6
    market = rng.normal(0.0003, 0.006, (n, 1))
    returns = market * np.linspace(.6, 1.2, a) + rng.normal(0, .008, (n, a))
    returns += np.linspace(-.0001, .0004, a)
    opening = 100 * np.cumprod(1 + returns, axis=0)
    dates = pd.bdate_range("2024-01-02", periods=n).strftime("%Y%m%d").to_numpy()
    return {"dates": dates, "assets": np.array([f"ASSET_{x}" for x in "ABCDEF"]), "open": opening,
            "close": opening * (1 + rng.normal(0, .001, opening.shape)), "returns": returns,
            "adj_factor": np.ones((n, a)), "vol": np.full((n, a), 10000.),
            "up_limit": opening * 1.1, "down_limit": opening * .9}


def _demo_risk(ctx):
    from .legacy.stock_task import graph_and_risk
    from .legacy.quantum_features import states, catalog, observables
    from .legacy.graph_head import GraphHead
    from sklearn.linear_model import Ridge
    market = _demo_market(ctx.seed)
    returns = market["returns"]
    indices = np.arange(126, 170)
    inputs, graphs, labels, risks = [], [], [], []
    for t in indices:
        r = returns[t-125:t+1]
        direction, _, _, _, _, _, sigma, _, _ = graph_and_risk(r, r)
        features = np.column_stack([returns[t], r[-5:].mean(0), r[-10:].mean(0), r[-20:].mean(0),
                                    r[-60:].mean(0), r[-20:].std(0)])
        graph = .5 * np.eye(6) + .5 * direction
        inputs.append(graph @ features)
        graphs.append(graph)
        labels.append(returns[t+1])
        risks.append(sigma)
    x, graph, y = np.asarray(inputs), np.asarray(graphs), np.asarray(labels)
    cut = 32
    center, scale = x[:cut].mean((0, 1)), np.maximum(x[:cut].std((0, 1)), 1e-8)
    angles = np.clip((x-center)/scale, -3, 3) * np.pi / 3
    np.savez_compressed(ctx.output / 'feature_trace.npz', aggregated_features=x, train_mean=center,
                        train_scale=scale, angles=angles, all_indices=indices,
                        train_indices=indices[:cut], evaluation_indices=indices[cut:])
    vector = states(angles.reshape(-1, 6), catalog()[0])
    readout = observables(vector).reshape(len(x), 6, 11)
    head = GraphHead(11, 16, 1, .001, ctx.seed)
    history = head.fit(readout[:cut], graph[:cut], y[:cut], np.ones_like(y[:cut], dtype=bool), epochs=40)
    prediction = head.predict(readout[cut:], graph[cut:])
    baseline = Ridge(alpha=1).fit(x[:cut].reshape(-1, 6), y[:cut].ravel()).predict(x[cut:].reshape(-1, 6)).reshape(-1, 6)
    rows = [{"date": str(market["dates"][indices[cut+i]]), "asset": str(asset), "target": y[cut+i, j],
             "local_readout_prediction": prediction[i, j], "ridge_prediction": baseline[i, j]}
            for i in range(len(prediction)) for j, asset in enumerate(market["assets"])]
    np.savez_compressed(ctx.output / "risk_model.npz", **head.export())
    np.savez_compressed(ctx.output / "risk_inputs.npz", angles=angles, readout=readout, graphs=graph, labels=y, covariance=np.array(risks))
    table = pd.DataFrame(rows)
    summary = {"data_source": "TEAM_SYNTHETIC_SIX_ASSET", "target": "NEXT_OPEN_SIMPLE_RETURN", "train_dates": cut,
               "evaluation_dates": len(prediction), "assets": 6, "sdk_evolutions": len(vector), "epochs": 40,
               "local_readout_mae": float(np.abs(prediction-y[cut:]).mean()), "ridge_mae": float(np.abs(baseline-y[cut:]).mean()),
               "minimum_covariance_eigenvalue": float(min(np.linalg.eigvalsh(s).min() for s in risks))}
    fig = _plot(ctx, "risk_predictions", lambda ax: (ax.plot(table.groupby("date").target.mean().to_numpy(), "k-", label="Observed"),
        ax.plot(table.groupby("date").local_readout_prediction.mean().to_numpy(), "--", color=".45", label="Local readout"),
        ax.set(xlabel="Evaluation date index", ylabel="Mean simple return"), ax.legend()))
    return summary, {"predictions": table, "training": pd.DataFrame({"epoch": np.arange(1, 41), "loss": history}),
                     "covariance": pd.DataFrame(risks[-1], columns=market["assets"])}, [fig]


def _instance(ctx):
    if hasattr(ctx, "instance"):
        instance = dict(ctx.instance)
    else:
        freeze = ctx.json(ctx.campaign / "quantum_freeze.json")
        instance = dict(freeze["instances"][0])
    if getattr(ctx, 'risk_lambda', None) is not None:
        instance['lambda'] = ctx.risk_lambda
    if getattr(ctx, 'cost', None) is not None:
        instance['cost'] = ctx.cost
    return instance


def _demo_data(ctx):
    from .legacy.run_e02 import load_task
    base = ctx.layout.d02
    for name in ["features.parquet", "labels.parquet", "protocol.json", "splits.csv"]:
        ctx.source(base / name)
    frame, columns, target = load_task(base)
    selection = ctx.json(ctx.layout.coverage)
    roles = frame.groupby("role").agg(rows=("asset", "size"), dates=("date", "nunique"), assets=("asset", "nunique")).reset_index()
    summary = {"source": "D02_CORE6", "rows": len(frame), "features": columns, "target": target,
               "covered_files": selection["files"], "covered_bytes": selection["bytes"]}
    return summary, {"preview": frame[["date", "asset", "role"]+columns+[target]].head(18), "partitions": roles,
                     "data_inventory": pd.DataFrame(selection["records"]).groupby("role", as_index=False).agg(files=("path", "size"), bytes=("bytes", "sum"))}, []


def _demo_bridge(ctx):
    from .legacy.bridge import objective, qubo, ising, exhaustive, solve
    from .quantum import portfolio_solution
    from .legacy.stock_finance import prepare_market, simulate_path
    instance = _instance(ctx)
    mu, sigma, previous = map(np.asarray, [instance["mu"], instance["sigma"], instance["previous"]])
    k, lam, cost = instance["k"], instance["lambda"], instance["cost"]
    q, offset = qubo(mu, sigma, k, lam, cost, previous)
    h, j, shift = ising(q, offset)
    bits = ((np.arange(64)[:, None] >> np.arange(6)) & 1).astype(float)
    rows = []
    for i, x in enumerate(bits):
        z = 1-2*x
        rows.append({"bitstring_q5_q0": f"{i:06b}", "selected": int(x.sum()), "objective": objective(x, mu, sigma, k, lam, cost, previous),
                     "qubo": float(x@q@x+offset), "ising": float(h@z+z@j@z+shift)})
    table = pd.DataFrame(rows)
    exact, basket = exhaustive(mu, sigma, k, lam, cost, previous)
    selected, solver = solve(mu, sigma, k, lam, cost, previous, cpu_cap=1.)
    write_json(ctx.output / 'bridge_instance.json', instance)
    write_json(ctx.output / 'portfolio_solution.json', portfolio_solution(instance, selected, basket))
    market = prepare_market(_demo_market(ctx.seed))
    priority = list(map(int, np.flatnonzero(selected)))
    def planner(index, positions, pending, nav, held):
        return {"signal_index": int(index), "priority_global": priority, "signal_nav": nav}
    path = simulate_path(market, list(range(140, 152)), planner, cost, k=k)
    write_json(ctx.output / "holdings_ledger.json", path)
    error = max(float(np.abs(table.objective-table.qubo).max()), float(np.abs(table.objective-table.ising).max()))
    if error > 1e-10:
        raise ValueError("Objective representations do not agree")
    summary = {"instance": instance["instance_id"], "assets": 6, "k": k, "binary_states": 64,
               "feasible_states": int((table.selected == k).sum()), "maximum_identity_error": error,
               "exact_objective": exact, "optimal_assets": [instance["asset_order"][i] for i in basket],
               "solver_objective": solver["objective"], "portfolio_example": path["summary"]}
    fig = _plot(ctx, "portfolio_path", lambda ax: (ax.plot(path["nav_path"], "k-o", markersize=3), ax.set(xlabel="Opening event", ylabel="Net asset value")))
    return summary, {"objective_states": table, "qubo": pd.DataFrame(q, columns=instance["asset_order"]),
                     "holding_intervals": pd.DataFrame(path["interval_records"])}, [fig]


def _demo_quantum(ctx, qas=False):
    from .quantum import candidate_catalog, noisy_probabilities, noise_catalog, energies, count_metrics, circuit_resources, FEASIBLE, evaluate_candidate
    instance = _instance(ctx)
    write_json(ctx.output / 'bridge_instance.json', instance)
    _, values, penalized, optimum, _ = energies(instance)
    candidates = [candidate_catalog()[i] for i in ([0, 3, 5] if qas else [0])]
    rows, distributions = [], []
    traces, names, profiles, ideals, noise_probabilities = [], [], [], [], []
    for c in candidates:
        qcis, exact, trace = evaluate_candidate(instance, c, penalized, ctx.qaoa_gamma, ctx.qaoa_beta, ctx.qaoa_mode)
        traces.append(trace)
        (ctx.output / (c["candidate_id"] + ".qcis")).write_text(qcis, encoding="utf-8")
        for profile in noise_catalog() if qas else [noise_catalog()[0]]:
            probs = exact if profile["id"] == "ideal" else noisy_probabilities(qcis, profile)
            names.append(c['candidate_id']); profiles.append(profile['id'])
            ideals.append(exact); noise_probabilities.append(probs)
            rng = np.random.default_rng(ctx.seed + int(c["candidate_id"].split("_")[1]))
            counts = rng.multinomial(ctx.shots, probs)
            measured = count_metrics({f"{i:06b}": int(n) for i, n in enumerate(counts)}, values, penalized, optimum)
            rows.append({"candidate": c["candidate_id"], "noise": profile["id"], "exact_feasible_probability": float(probs[FEASIBLE].sum()),
                         "qaoa_mode": ctx.qaoa_mode, "optimizer_evaluations": trace['nfev'],
                         "optimizer_success": trace['success'], "optimizer_status": trace['status'],
                         "optimizer_message": trace['message'],
                         "exact_penalized_gap": float(probs@penalized-optimum), "tv_from_ideal": float(np.abs(probs-exact).sum()/2),
                         **measured, **{k: v for k, v in circuit_resources(qcis).items() if isinstance(v, (int, float))}})
            distributions.extend({"candidate": c["candidate_id"], "noise": profile["id"], "bitstring_q5_q0": f"{i:06b}",
                                  "probability": float(p), "count": int(counts[i]), "objective": float(values[i]), "feasible": bool(FEASIBLE[i])} for i, p in enumerate(probs))
    result = pd.DataFrame(rows)
    np.savez_compressed(ctx.output / 'quantum_probabilities.npz', candidate_names=names,
                        noise_profiles=profiles, ideal=ideals, noisy=noise_probabilities)
    write_json(ctx.output / 'optimization_trace.json', {'schemaVersion': 'qf.optimization-trace.v1',
               'mode': ctx.qaoa_mode, 'initial_parameters': [ctx.qaoa_gamma, ctx.qaoa_beta],
               'maxiter': 18 if ctx.qaoa_mode == 'optimize' else 0, 'rhobeg': .35 if ctx.qaoa_mode == 'optimize' else None,
               'candidates': traces})
    if any(result.shots != ctx.shots):
        raise ValueError("Shots conservation failed")
    summary = {"instance": instance["instance_id"], "candidate_count": len(candidates), "shots_per_setting": ctx.shots,
               "parameters": [ctx.qaoa_gamma, ctx.qaoa_beta], "qaoa_mode": ctx.qaoa_mode,
               "effective_parameters": {t['candidate']: t['parameters'] for t in traces},
               "risk_lambda": instance['lambda'], "cost": instance['cost'],
               "exact_optimum": optimum, "execution": "LOCAL_CQLIB_AND_DENSITY_MATRIX",
               "settings": len(result), "total_shots": int(result.shots.sum())}
    if qas:
        def draw_noise(ax):
            for name, part in result.groupby("candidate", sort=False):
                ax.plot(part.noise, part.exact_feasible_probability, marker="o", label=name)
            ax.set(xlabel="Noise profile", ylabel="Feasible probability", ylim=(0, 1.04))
            ax.legend()
        fig = _plot(ctx, "noise_comparison", draw_noise)
    else:
        prob = pd.DataFrame(distributions)
        fig = _plot(ctx, "qaoa_distribution", lambda ax: (ax.bar(np.arange(64), prob.probability, color=".3"), ax.set(xlabel="Basis state q5...q0", ylabel="Probability")))
    return summary, {"metrics": result, "distribution": pd.DataFrame(distributions)}, [fig]


def _reports(ctx):
    registry = ctx.json(ctx.layout.registry)
    output = []
    for group in registry["groups"]:
        if group["experiment_id"] not in {f"E{i:02d}" for i in range(1, 8)}:
            continue
        path = ctx.campaign / group["primary_result"].removeprefix("remote_results/")
        report = ctx.json(path)
        if group.get("result_sha256") and digest(path) != group["result_sha256"]:
            raise ValueError("Registered result hash mismatch: " + group["experiment_id"])
        output.append((group, report, path))
    return output


def _review(ctx, topic):
    records = _reports(ctx)
    inventory, scalar_rows = [], []
    tables = {}
    for group, report, path in records:
        if group["experiment_id"] not in GROUPS[topic]:
            continue
        eid = group["experiment_id"]
        inventory.append({"experiment": eid, "title": group["title"], "task": group.get("task_id"),
                          "data_epoch": group.get("data_epoch"), "status": group.get("scientific_status"),
                          "result": str(path.relative_to(ctx.data)), "sha256": digest(path)})
        for key, value in report.items():
            if isinstance(value, (int, float, str, bool)) or value is None:
                scalar_rows.append({"experiment": eid, "field": key, "value": value})
            elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value) and len(value) <= 100:
                tables[eid.lower()+"_"+key] = pd.json_normalize(value)
    tables["experiments"] = pd.DataFrame(inventory)
    tables["fields"] = pd.DataFrame(scalar_rows)
    figures = []
    summary = {"mode": "FROZEN_RESULTS", "experiments": [r["experiment"] for r in inventory], "verified_result_files": len(inventory)}
    if topic == "data":
        s, ts, _ = _demo_data(ctx)
        summary.update(s); tables.update(ts)
    if topic in {"results", "bridge"}:
        from .legacy.research_common import cvar95
        finance = ctx.campaign / "finance_actions_v2"
        freeze = ctx.json(finance / "freeze.json")
        rows = []
        for job in freeze["jobs"]:
            if job.get("phase") != "test" or job["group"] not in GROUPS[topic]:
                continue
            directory = finance / "paths" / job["job_id"]
            result = ctx.json(directory / "result.json")
            series_path = ctx.source(directory / "series.npz")
            if digest(series_path) != result["series_sha256"]:
                raise ValueError("Financial series hash mismatch")
            with np.load(series_path, allow_pickle=False) as arrays:
                value = float(cvar95(arrays["net_losses"]))
                dates = len(arrays["net_losses"])
            expected = result["summary"]["cvar95_net_loss"]
            if abs(value-expected) > 1e-12:
                raise ValueError("CVaR recomputation mismatch")
            rows.append({"experiment": job["group"], "method": job["method"], "seed": job["seed"], "cost": job["cost"],
                         "dates": dates, "cvar95": value, "terminal_nav": result["summary"]["terminal_nav"], "cvar_error": abs(value-expected)})
        table = pd.DataFrame(rows)
        tables["financial_paths"] = table
        summary["financial_paths_recomputed"] = len(table)
        if len(table):
            means = table[table.cost == .001].groupby(["experiment", "method"], as_index=False).cvar95.mean()
            tables["financial_summary"] = means
            figures.append(_plot(ctx, "financial_cvar", lambda ax: (ax.barh(np.arange(len(means)), means.cvar95, color=".4"),
                ax.set(yticks=np.arange(len(means)), yticklabels=means.experiment+" / "+means.method, xlabel="Mean seed CVaR95"))))
    if topic == "qas":
        path = ctx.layout.hardware / "hardware_local_comparison/analysis.json"
        if path.exists():
            hardware = ctx.json(path)
            write_json(ctx.output / "hardware_analysis.json", hardware)
            summary["hardware_analysis"] = "hardware_analysis.json"
            summary["hardware_circuit_count"] = hardware["circuit_count"]
            summary["hardware_shots"] = hardware["hardware_shots"]
            tables["hardware_statistics"] = pd.DataFrame.from_dict(hardware["statistics"], orient="index").rename_axis("metric").reset_index()
            tables["hardware_records"] = pd.DataFrame(hardware["records"])
    if topic in {"risk", "results"}:
        coverage = ctx.json(ctx.layout.coverage)
        models = [r for r in coverage["records"] if r["path"].endswith("/model.npz")]
        arrays = []
        for row in models:
            source = ctx.source(ctx.campaign / row["path"])
            if digest(source) != row["sha256"]:
                raise ValueError("Frozen model state changed: " + row["path"])
            with np.load(source, allow_pickle=False) as state:
                for key in state.files:
                    value = state[key]
                    arrays.append({"model": row["path"], "array": key, "shape": " x ".join(map(str,value.shape)),
                        "dtype": str(value.dtype), "elements": value.size,
                        "all_finite": bool(np.isfinite(value).all()) if value.dtype.kind in "biufc" else None})
        tables["model_arrays"] = pd.DataFrame(arrays)
        summary["model_states_loaded"] = len(models)
    return summary, tables, figures


def _demo_results(ctx):
    from .legacy.research_common import paired_inference, cvar95
    market = _demo_market(ctx.seed)
    a = -market["returns"][126:170, :3].mean(1)
    b = -market["returns"][126:170, 3:].mean(1)
    inference = paired_inference(a, b, 5, endpoint="cvar95", replicates=2000, seed=ctx.seed)
    table = pd.DataFrame({"date": market["dates"][126:170], "portfolio_a_loss": a, "portfolio_b_loss": b})
    summary = {"data_source": "TEAM_SYNTHETIC_SIX_ASSET", "statistical_unit": "DATE", "dates": len(a),
               "a_cvar95": float(cvar95(a)), "b_cvar95": float(cvar95(b)), "paired_cvar_comparison": inference}
    fig = _plot(ctx, "example_paths", lambda ax: (ax.plot(np.cumprod(1-a), "k-", label="Portfolio A"),
        ax.plot(np.cumprod(1-b), "--", color=".5", label="Portfolio B"), ax.set(xlabel="Date index", ylabel="Wealth"), ax.legend()))
    return summary, {"paired_paths": table}, [fig]


def _load_bundle(directory):
    expected = (directory / "result.sha256").read_text().split()[0]
    if digest(directory / "result.json") != expected:
        raise ValueError("Saved result metadata changed")
    document = load_json(directory / "result.json")
    for row in document["artifacts"]:
        if digest(directory / row["path"]) != row["sha256"]:
            raise ValueError("Saved output changed: " + row["path"])
    return ResultBundle(document["topic"], document["mode"], document["summary"],
                        {name: pd.read_csv(directory / path, dtype={"bitstring_q5_q0": str}) for name, path in document["tables"].items()},
                        [directory / p for p in document["figures"]], directory)


def run_topic(topic="quickstart", mode="compute", *, data_root=None, output_dir=None, run_id=None, seed=2026090801, shots=1024,
              risk_lambda=None, cost=None, qaoa_gamma=_DEFAULT_GAMMA, qaoa_beta=_DEFAULT_BETA, qaoa_mode=_DEFAULT_MODE):
    if topic not in TOPICS or mode not in {"compute", "review"}:
        raise ValueError("Choose a documented topic and compute/review mode")
    if not isinstance(shots, int) or isinstance(shots, bool) or not 1 <= shots <= 100000:
        raise ValueError("shots must be an integer from 1 to 100000")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be an unsigned 32-bit integer")
    portfolio_topic = mode == 'compute' and topic in {'quickstart', 'bridge', 'qaoa', 'qas'}
    quantum_topic = mode == 'compute' and topic in {'quickstart', 'qaoa', 'qas'}
    if not portfolio_topic and (risk_lambda is not None or cost is not None):
        raise ValueError('COMPUTE_PARAMETERS_NOT_APPLICABLE:' + mode + ':' + topic)
    if not quantum_topic and (qaoa_gamma is not _DEFAULT_GAMMA or qaoa_beta is not _DEFAULT_BETA or qaoa_mode is not _DEFAULT_MODE):
        raise ValueError('QAOA_PARAMETERS_NOT_APPLICABLE:' + mode + ':' + topic)
    validate_compute_parameters(1. if risk_lambda is None else risk_lambda, .001 if cost is None else cost,
                                qaoa_gamma, qaoa_beta, qaoa_mode)
    run_id = run_id or dt.datetime.now(dt.timezone.utc).strftime("run_%Y%m%dT%H%M%S_%fZ")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", run_id):
        raise ValueError("Use an alphanumeric run_id with underscores or hyphens")
    data = DataLayout.resolve(data_root).root
    destination = Path(output_dir or Path.cwd() / "outputs").resolve() / run_id / topic
    if DataLayout.resolve(data).protected(destination):
        raise ValueError("Choose an output directory separate from source and data")
    ctx = Context(data, destination, seed, shots)
    ctx.risk_lambda, ctx.cost = risk_lambda, cost
    ctx.qaoa_gamma, ctx.qaoa_beta, ctx.qaoa_mode = float(qaoa_gamma), float(qaoa_beta), str(qaoa_mode)
    request = {"topic": topic, "mode": mode, "seed": seed, "shots": shots, "data_root": str(data), "run_id": run_id,
               "api_sha256": digest(__file__),
               "implementation_sha256": identity({str(p.relative_to(PACKAGE)): digest(p) for p in PACKAGE.rglob('*.py')})}
    if portfolio_topic:
        effective = _instance(ctx)
        validate_compute_parameters(effective['lambda'], effective['cost'], qaoa_gamma, qaoa_beta, qaoa_mode)
        request.update(risk_lambda=effective['lambda'], cost=effective['cost'])
    if quantum_topic:
        request.update(qaoa_gamma=ctx.qaoa_gamma, qaoa_beta=ctx.qaoa_beta, qaoa_mode=ctx.qaoa_mode)
    request_sha = identity(request)
    if destination.exists():
        if (destination / "result.json").exists():
            _load_bundle(destination)
        if (destination / "result.json").exists() and load_json(destination / "result.json")["request_sha256"] == request_sha:
            previous = load_json(destination / "result.json")
            for path, expected in previous["parents"].items():
                if digest(path) != expected:
                    raise ValueError("Saved input changed; create a new run")
            return _load_bundle(destination)
        raise FileExistsError("Choose a new run_id for this calculation")
    destination.mkdir(parents=True, exist_ok=False)
    start = time.process_time()
    write_json(destination / "intent.json", {"request": request, "request_sha256": request_sha, "state": "RUNNING"})
    try:
        if mode == "review":
            summary, tables, figures = _review(ctx, topic)
        elif topic == "data":
            summary, tables, figures = _demo_data(ctx)
        elif topic == "risk":
            summary, tables, figures = _demo_risk(ctx)
        elif topic == "bridge":
            summary, tables, figures = _demo_bridge(ctx)
        elif topic in {"qaoa", "qas"}:
            summary, tables, figures = _demo_quantum(ctx, topic == "qas")
        elif topic == "results":
            summary, tables, figures = _demo_results(ctx)
        else:
            rsummary, rt, rf = _demo_risk(ctx)
            instance = dict(_instance(ctx))
            prediction = rt["predictions"]
            last = prediction[prediction.date == prediction.date.max()]
            instance.update(instance_id="DEMO_GRAPH_RISK_BRIDGE", mu=last.local_readout_prediction.tolist(),
                            sigma=rt["covariance"].to_numpy().tolist(), asset_order=last.asset.tolist())
            ctx.instance = instance
            write_json(ctx.output / "risk_bridge_instance.json", instance)
            summary, tables, figures = _demo_bridge(ctx)
            summary["risk_learning"] = rsummary
            tables.update(rt); figures += rf
            qsummary, qt, qf = _demo_quantum(ctx)
            summary["qaoa"] = qsummary
            tables.update(qt); figures += qf
        csv_files = {}
        for name, table in tables.items():
            filename = name + ".csv"
            table.to_csv(destination / filename, index=False, encoding="utf-8")
            csv_files[name] = filename
        artifacts = [{"path": p.relative_to(destination).as_posix(), "sha256": digest(p), "bytes": p.stat().st_size}
                     for p in sorted(destination.iterdir()) if p.is_file()]
        baseline = load_json(PACKAGE / "provenance/delivery_baseline.json")
        write_json(destination / "result.json", {"schemaVersion": "qf.software-result.v1", "status": "COMPLETE",
            "experimentId": "SOFTWARE_" + topic.upper(), "sourceCommit": baseline["workspaceCommit"],
            "workspaceCommit": baseline["workspaceCommit"], "inputManifestSHA256": digest(ctx.layout.coverage),
            "topic": topic, "mode": mode, "run_id": run_id, "data_epoch": "software-example-20260908-v1" if mode == "compute" else "FROZEN_SOURCE_EPOCHS",
            "executionMode": "LOCAL_DEMONSTRATION" if mode == "compute" else "EXISTING_RESULTS_REANALYSIS",
            "scientificStatus": "LOCAL_VERIFIED", "archiveStatus": "RESULT_GENERATED", "releaseStatus": "LOCAL_REVIEW",
            "external_calls": 0, "request_sha256": request_sha, "configuration": request,
            "summary": summary, "tables": csv_files, "figures": [p.name for p in figures], "parents": ctx.parents,
            "implementation": {str(p.relative_to(PACKAGE)): digest(p) for p in PACKAGE.rglob('*.py')},
            "environment": environment(), "cpu_seconds": time.process_time()-start, "artifacts": artifacts})
        (destination / "result.sha256").write_text(digest(destination / "result.json") + "  result.json\n", encoding="utf-8")
        return _load_bundle(destination)
    except Exception as exc:
        write_json(destination / "failure.json", {"status": "FAILED", "error_type": type(exc).__name__, "message": str(exc)})
        raise


def verify(data_root=None, full=False):
    runtime = environment()
    if sys.version_info[:2] != (3, 11):
        raise ValueError("Use Python 3.11 with the supplied requirements.lock")
    for name, version in {"numpy": "2.4.6", "pandas": "3.0.3", "scipy": "1.17.1", "scikit-learn": "1.9.0", "cqlib": "1.3.11"}.items():
        if runtime["packages"].get(name) != version:
            raise ValueError("Install the locked dependency version: " + name + "==" + version)
    if any(runtime["packages"][k] is None for k in ["numpy", "pandas", "scipy", "scikit-learn", "cqlib", "matplotlib"]):
        raise ValueError("Install all supplied Notebook and calculation dependencies")
    data = DataLayout.resolve(data_root).root
    ctx = Context(data, ROOT, 0, 1)
    reports = _reports(ctx)
    coverage = load_json(ctx.layout.coverage)
    checked = 0
    if full:
        for row in coverage["records"]:
            p = ctx.campaign / row["path"]
            if not p.resolve().is_relative_to(ctx.campaign) or not p.is_file() or p.stat().st_size != row["bytes"] or digest(p) != row["sha256"]:
                raise ValueError("Data coverage mismatch: " + row["path"])
            checked += 1
        manifest = ROOT / "MANIFEST.sha256"
        if manifest.exists():
            for line in manifest.read_text().splitlines():
                expected, name = line.split("  ", 1)
                p = ROOT / name
                if not p.resolve().is_relative_to(ROOT) or digest(p) != expected:
                    raise ValueError("Package checksum mismatch: " + name)
                checked += 1
    return {"status": "PASS", "environment": environment(), "registered_experiments": len(reports),
            "coverage_files": coverage["files"], "coverage_bytes": coverage["bytes"], "hashes_verified": checked,
            "verification": "FULL" if full else "ENVIRONMENT_AND_REGISTERED_RESULTS"}
