from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from cqlib.circuits import Circuit
from cqlib.simulator import StatevectorSimulator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qf_algorithm.legacy.quantum_worker import qgnn_m2

EXPECTED_ROOT = Path.cwd().resolve()
CANDIDATE_ARCHITECTURE = {
    "DIR_L1_R0": 0,
    "DIR_L1_R1": 2,
    "DIR_L2_R0": 1,
    "DIR_L2_R1": 3,
}
EXPECTED_CANDIDATES = list(CANDIDATE_ARCHITECTURE)
EXPECTED_SEEDS = [2026080901, 2026080902, 2026080903]


class V42M3TrainingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(alias="schemaVersion")
    run_id: str = Field(alias="runId", min_length=1)
    authorization_sha256: str = Field(alias="authorizationSha256")
    protocol_freeze_sha256: str = Field(alias="protocolFreezeSha256")
    m1_manifest_sha256: str = Field(alias="m1ManifestSha256")
    candidate_manifest_sha256: str = Field(alias="candidateManifestSha256")
    candidate_ids: list[str] = Field(alias="candidateIds")
    training_seeds: list[int] = Field(alias="trainingSeeds")
    artifact_root: str = Field(alias="artifactRoot", min_length=1)
    state_path: str = Field(alias="statePath", min_length=1)
    optimizer_steps: int = Field(default=12, alias="optimizerSteps", ge=8, le=32)
    training_dates_per_seed: int = Field(
        default=18, alias="trainingDatesPerSeed", ge=12, le=36
    )

    @model_validator(mode="after")
    def validate_contract(self) -> V42M3TrainingRequest:
        if self.schema_version != "qf.v42-m3-training-request.v1":
            raise ValueError("unsupported V4.2 M3 training request")
        for value in (
            self.authorization_sha256,
            self.protocol_freeze_sha256,
            self.m1_manifest_sha256,
            self.candidate_manifest_sha256,
        ):
            if not qgnn_m2.valid_sha256(value):
                raise ValueError("V4.2 M3 training hashes must be lowercase SHA-256")
        if self.candidate_ids != EXPECTED_CANDIDATES:
            raise ValueError("V4.2 M3 training requires the frozen four-candidate order")
        if self.training_seeds != EXPECTED_SEEDS:
            raise ValueError("V4.2 M3 training requires the frozen three-seed order")
        return self


def stratified_training_dates(dates: list[str], seed: int, count: int) -> list[str]:
    ordered = sorted(set(dates))
    if count < 2 or len(ordered) < count:
        raise ValueError("training-date strata require at least one date per stratum")
    rng = np.random.default_rng(seed)
    strata = np.array_split(np.asarray(ordered, dtype=object), count)
    selected = [str(rng.choice(stratum)) for stratum in strata if len(stratum) > 0]
    if len(selected) != count or len(set(selected)) != count:
        raise ValueError("seeded training-date strata are not distinct and complete")
    return sorted(selected)


def fit_shared_readout(z_values: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, float]:
    z = np.asarray(z_values, dtype=float).reshape(-1)
    y = np.asarray(labels, dtype=float).reshape(-1)
    valid = np.isfinite(z) & np.isfinite(y)
    if int(valid.sum()) < 12:
        raise ValueError("shared quantum readout has insufficient finite training rows")
    design = np.column_stack([z[valid], np.ones(int(valid.sum()), dtype=float)])
    ridge = np.diag([1e-6, 1e-8])
    parameters = np.linalg.solve(design.T @ design + ridge, design.T @ y[valid])
    prediction = np.maximum(0.0, design @ parameters)
    return parameters, float(np.mean(np.abs(prediction - y[valid])))


def predict_shared_readout(z_values: np.ndarray, parameters: np.ndarray) -> np.ndarray:
    z = np.asarray(z_values, dtype=float).reshape(-1)
    design = np.column_stack([z, np.ones(len(z), dtype=float)])
    return np.maximum(0.0, design @ np.asarray(parameters, dtype=float))


def spsa_schedule(step: int) -> tuple[float, float]:
    if step < 0:
        raise ValueError("SPSA step must be nonnegative")
    return 0.08 / ((step + 1) ** 0.602), 0.05 / ((step + 1) ** 0.101)


def _date_rows(
    date: str,
    baskets_by_date: dict[str, dict[str, Any]],
    angles: pd.DataFrame,
    edges: pd.DataFrame,
    features: pd.DataFrame,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, np.ndarray]:
    basket = baskets_by_date[date]
    compact = date.replace("-", "")
    asset_order = [str(value) for value in basket["assetIds"]]
    angle_rows = angles.loc[angles["as_of_date"].astype(str) == compact].copy()
    edge_rows = edges.loc[edges["as_of_date"].astype(str) == compact].copy()
    labels = features.loc[
        (features["as_of_date"].astype(str) == compact)
        & features["asset_id"].astype(str).isin(asset_order),
        ["asset_id", "label"],
    ].copy()
    labels["asset_id"] = labels["asset_id"].astype(str)
    by_asset = labels.set_index("asset_id")["label"]
    ordered_labels = np.asarray([float(by_asset.get(asset, math.nan)) for asset in asset_order])
    if len(angle_rows) != 6 or len(edge_rows) != 12:
        raise ValueError(f"V4.2 M3 date {date} is not a complete six-asset graph sample")
    return basket, angle_rows, edge_rows, ordered_labels


def _simulate_date(
    candidate_id: str,
    date: str,
    basket: dict[str, Any],
    angle_rows: pd.DataFrame,
    edge_rows: pd.DataFrame,
    omega: np.ndarray,
) -> tuple[np.ndarray, str]:
    architecture = CANDIDATE_ARCHITECTURE[candidate_id]
    ir, _report = qgnn_m2.build_candidate_ir(
        architecture,
        date,
        str(basket["basketVersion"]),
        [str(value) for value in basket["assetIds"]],
        angle_rows,
        edge_rows,
        target_width=6,
        shared_angle_override=(float(omega[0]), float(omega[1])),
        candidate_id_override=candidate_id,
    )
    circuit = Circuit(6)
    for operation in ir["operations"]:
        qgnn_m2.add_operation(circuit, operation)
    probabilities = {
        str(key): float(value)
        for key, value in StatevectorSimulator(circuit, omp_threads=1).probs().items()
    }
    if not math.isclose(sum(probabilities.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("V4.2 M3 simulator probabilities do not normalize")
    z = np.asarray([qgnn_m2.z_expectation(probabilities, index) for index in range(6)])
    return z, str(ir["canonicalIrSha256"])


def _observations(
    candidate_id: str,
    dates: list[str],
    omega: np.ndarray,
    baskets_by_date: dict[str, dict[str, Any]],
    angles: pd.DataFrame,
    edges: pd.DataFrame,
    features: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, list[str], int]:
    z_rows: list[float] = []
    label_rows: list[float] = []
    hashes: list[str] = []
    inference_only_rows = 0
    for date in dates:
        basket, angle_rows, edge_rows, labels = _date_rows(
            date, baskets_by_date, angles, edges, features
        )
        z, circuit_hash = _simulate_date(
            candidate_id, date, basket, angle_rows, edge_rows, omega
        )
        valid = np.isfinite(labels)
        z_rows.extend(z[valid].tolist())
        label_rows.extend(labels[valid].tolist())
        inference_only_rows += int((~valid).sum())
        hashes.append(circuit_hash)
    return (
        np.asarray(z_rows, dtype=float),
        np.asarray(label_rows, dtype=float),
        hashes,
        inference_only_rows,
    )


def _train_seed(
    candidate_id: str,
    seed: int,
    training_dates: list[str],
    validation_dates: list[str],
    request: V42M3TrainingRequest,
    baskets_by_date: dict[str, dict[str, Any]],
    angles: pd.DataFrame,
    edges: pd.DataFrame,
    features: pd.DataFrame,
) -> dict[str, Any]:
    sampled_dates = stratified_training_dates(
        training_dates, seed, request.training_dates_per_seed
    )
    architecture = CANDIDATE_ARCHITECTURE[candidate_id]
    omega = np.asarray(
        [0.031 + (architecture + 1) * 0.001, -0.027 - (architecture + 1) * 0.001],
        dtype=float,
    )
    rng = np.random.default_rng(seed)
    trace: list[dict[str, Any]] = []
    simulations = 0
    for step in range(request.optimizer_steps):
        learning_rate, perturbation_scale = spsa_schedule(step)
        delta = rng.choice(np.asarray([-1.0, 1.0]), size=2, replace=True)
        losses: list[float] = []
        for sign in (1.0, -1.0):
            probe = omega + sign * perturbation_scale * delta
            z, labels, _hashes, _missing = _observations(
                candidate_id,
                sampled_dates,
                probe,
                baskets_by_date,
                angles,
                edges,
                features,
            )
            _readout, loss = fit_shared_readout(z, labels)
            losses.append(loss)
            simulations += len(sampled_dates)
        gradient = (losses[0] - losses[1]) / (2 * perturbation_scale) * delta
        omega = np.clip(omega - learning_rate * gradient, -math.pi, math.pi)
        trace.append(
            {
                "step": step,
                "learningRate": learning_rate,
                "perturbationScale": perturbation_scale,
                "lossPlus": losses[0],
                "lossMinus": losses[1],
                "omega": omega.tolist(),
            }
        )
    train_z, train_labels, train_hashes, training_inference_only = _observations(
        candidate_id,
        sampled_dates,
        omega,
        baskets_by_date,
        angles,
        edges,
        features,
    )
    readout, training_mae = fit_shared_readout(train_z, train_labels)
    validation_z, validation_labels, validation_hashes, validation_inference_only = (
        _observations(
            candidate_id,
            validation_dates,
            omega,
            baskets_by_date,
            angles,
            edges,
            features,
        )
    )
    validation_prediction = predict_shared_readout(validation_z, readout)
    validation_mae = float(np.mean(np.abs(validation_prediction - validation_labels)))
    simulations += len(sampled_dates) + len(validation_dates)
    return {
        "seed": seed,
        "status": "TRAINED_VALIDATED_SIMULATOR_ONLY",
        "optimizer": "SPSA",
        "optimizerSteps": request.optimizer_steps,
        "sampledTrainingDates": sampled_dates,
        "validationDates": validation_dates,
        "omega": omega.tolist(),
        "sharedReadout": {"scale": float(readout[0]), "bias": float(readout[1])},
        "trainingMae": training_mae,
        "validationMae": validation_mae,
        "trainingCircuitSha256": train_hashes,
        "validationCircuitSha256": validation_hashes,
        "trainingInferenceOnlyRows": training_inference_only,
        "validationInferenceOnlyRows": validation_inference_only,
        "simulatorCircuitRuns": simulations,
        "trace": trace,
    }


def build_training(request: V42M3TrainingRequest) -> dict[str, Any]:
    project_root = Path.cwd().resolve()
    artifact_root = Path(request.artifact_root).resolve()
    state_path = Path(request.state_path).resolve()
    if project_root != EXPECTED_ROOT:
        raise ValueError("V4.2 M3 training must run in the authoritative WSL project root")
    if not artifact_root.is_relative_to(project_root / ".local"):
        raise ValueError("V4.2 M3 artifact root must remain under project .local")
    if not state_path.is_relative_to(project_root / ".local" / "state"):
        raise ValueError("V4.2 M3 state path must remain under .local/state")
    if state_path.exists():
        previous = json.loads(state_path.read_bytes())
        if previous.get("status") == "M3_QGNN_QAS_TRAINED":
            return previous["result"]
    created_at = qgnn_m2.now()
    state = qgnn_m2.StateWriter(
        state_path,
        {
            "schemaVersion": "qf.v42-m3-training-state.v1",
            "runId": request.run_id,
            "authorizationSha256": request.authorization_sha256,
            "protocolFreezeSha256": request.protocol_freeze_sha256,
            "status": "IN_PROGRESS",
            "phase": "VERIFY_PARENTS",
            "formalTestState": "SEALED",
            "createdAt": created_at,
            "updatedAt": created_at,
        },
    )
    m1 = json.loads(qgnn_m2.read_verified_artifact(artifact_root, request.m1_manifest_sha256))
    candidates = json.loads(
        qgnn_m2.read_verified_artifact(artifact_root, request.candidate_manifest_sha256)
    )
    if (
        m1.get("schemaVersion") != "qf.qgnn-m1-manifest.v2"
        or m1.get("status") != "PASS_REAL_DATA"
        or m1.get("formalTestState") != "SEALED"
        or m1.get("maximumMaterializedDate") != "2023-12-31"
        or m1.get("authorizationSha256") != request.authorization_sha256
        or m1.get("protocolFreezeSha256") != request.protocol_freeze_sha256
        or m1.get("leakageAudit", {}).get("formalValueFieldsProjected") != 0
        or candidates.get("schemaVersion") != "qf.v42-qgnn-m3-candidate-manifest.v1"
        or candidates.get("status") != "M3_CANDIDATES_READY"
        or [item.get("candidateId") for item in candidates.get("candidates", [])]
        != request.candidate_ids
    ):
        raise ValueError("V4.2 M3 training parents failed the SEALED data/candidate gate")
    artifacts = m1["artifacts"]
    features = pd.read_parquet(
        qgnn_m2.artifact_path(artifact_root, artifacts["featureTable"])
    )
    angles = pd.read_parquet(
        qgnn_m2.artifact_path(artifact_root, artifacts["featureAngles"])
    )
    edges = pd.read_parquet(
        qgnn_m2.artifact_path(artifact_root, artifacts["inducedSupportEdges"])
    )
    baskets = m1["baskets"]
    baskets_by_date = {str(item["asOfDate"]): item for item in baskets}
    training_dates = sorted(date for date in baskets_by_date if date <= "2022-12-31")
    validation_dates = sorted(
        date for date in baskets_by_date if "2023-01-01" <= date <= "2023-12-31"
    )
    if len(training_dates) != 84 or len(validation_dates) != 12:
        raise ValueError("V4.2 M3 training/validation monthly split is not 84/12")
    candidate_results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for candidate_id in request.candidate_ids:
        seed_results: list[dict[str, Any]] = []
        for seed in request.training_seeds:
            try:
                seed_results.append(
                    _train_seed(
                        candidate_id,
                        seed,
                        training_dates,
                        validation_dates,
                        request,
                        baskets_by_date,
                        angles,
                        edges,
                        features,
                    )
                )
            except Exception as error:  # Preserve failed lineages; do not select only winners.
                failures.append(
                    {
                        "candidateId": candidate_id,
                        "seed": seed,
                        "errorType": error.__class__.__name__,
                        "errorMessageSha256": qgnn_m2.sha256_bytes(str(error).encode()),
                    }
                )
            state.update(
                phase="TRAIN_QGNN_QAS",
                candidateId=candidate_id,
                seedsCompleted=len(seed_results),
                failures=len(failures),
            )
        if len(seed_results) != len(request.training_seeds):
            raise ValueError(f"V4.2 M3 candidate {candidate_id} did not complete all seeds")
        validation_values = np.asarray(
            [float(item["validationMae"]) for item in seed_results], dtype=float
        )
        candidate_results.append(
            {
                "candidateId": candidate_id,
                "architectureIndex": CANDIDATE_ARCHITECTURE[candidate_id],
                "messageLayers": 1 if "L1" in candidate_id else 2,
                "reuploadCount": 1 if "R1" in candidate_id else 0,
                "seedResults": seed_results,
                "validationMaeMedian": float(np.median(validation_values)),
                "validationMaeStd": float(np.std(validation_values)),
                "successfulSeeds": len(seed_results),
            }
        )
    ranked = sorted(
        candidate_results,
        key=lambda item: (
            item["validationMaeMedian"],
            item["validationMaeStd"],
            item["messageLayers"],
            item["reuploadCount"],
            item["candidateId"],
        ),
    )
    for rank, candidate in enumerate(ranked, start=1):
        candidate["qasRank"] = rank
        candidate["selectedTopK"] = rank <= 2
    report = {
        "schemaVersion": "qf.v42-m3-qgnn-qas-training-manifest.v1",
        "status": "M3_QGNN_QAS_TRAINED",
        "runId": request.run_id,
        "authorizationSha256": request.authorization_sha256,
        "protocolFreezeSha256": request.protocol_freeze_sha256,
        "m1ManifestSha256": request.m1_manifest_sha256,
        "candidateManifestSha256": request.candidate_manifest_sha256,
        "formalTestState": "SEALED",
        "trainingInterval": "2016-01-01/2022-12-31",
        "validationInterval": "2023-01-01/2023-12-31",
        "formalRowsRead": 0,
        "rawFeatureBypass": False,
        "sharedReadout": True,
        "simulatorRole": "TRAINING_AND_PREFLIGHT_ONLY_NOT_HARDWARE_METRIC",
        "hardwareJobs": 0,
        "candidateResults": ranked,
        "selectedTopK": [item["candidateId"] for item in ranked[:2]],
        "failedLineages": failures,
        "createdAt": created_at,
    }
    parents = [
        request.authorization_sha256,
        request.protocol_freeze_sha256,
        request.m1_manifest_sha256,
        request.candidate_manifest_sha256,
    ]
    artifact = qgnn_m2.CasStore(artifact_root, created_at).put_bytes(
        qgnn_m2.canonical(report) + b"\n",
        "application/json",
        report["schemaVersion"],
        parents,
    )
    result = {
        "schemaVersion": "qf.v42-m3-training-result.v1",
        "status": "M3_QGNN_QAS_TRAINED",
        "formalTestState": "SEALED",
        "manifestSha256": artifact["sha256"],
        "candidateCount": 4,
        "selectedTopK": report["selectedTopK"],
        "hardwareJobs": 0,
    }
    state.update(status="M3_QGNN_QAS_TRAINED", phase="COMPLETE", result=result)
    return result


def main() -> None:
    try:
        request = V42M3TrainingRequest.model_validate_json(sys.stdin.read())
        result = build_training(request)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as error:
        sys.stderr.write(
            f"[v42-m3-training] {error.__class__.__name__}: "
            f"{str(error).replace(chr(10), ' ')[:500]}\n"
        )
        sys.stdout.write(
            json.dumps(
                {
                    "schemaVersion": "qf.v42-m3-training-error.v1",
                    "status": "FAILED_CLOSED",
                    "errorType": error.__class__.__name__,
                    "formalTestState": "SEALED",
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
