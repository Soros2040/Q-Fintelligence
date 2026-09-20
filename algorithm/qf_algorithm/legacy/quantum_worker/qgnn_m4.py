from __future__ import annotations

import hashlib
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

EXPECTED_ROOT = Path.cwd().resolve()
EXPECTED_WIDTH = 101
EXPECTED_CANDIDATES = 12
EXPECTED_TRAINING_DATES = 2
EXPECTED_VALIDATION_DATES = 12


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def read_verified(root: Path, digest: str) -> bytes:
    if not valid_sha256(digest):
        raise ValueError("M4 plan artifact digest is invalid")
    target = root / "sha256" / digest[:2] / digest
    value = target.read_bytes()
    if hashlib.sha256(value).hexdigest() != digest:
        raise ValueError(f"M4 plan artifact digest mismatch for {digest}")
    return value


def artifact_digest(manifest: dict[str, Any], role: str) -> str:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get(role), dict):
        raise ValueError(f"M4 plan is missing M1 artifact {role}")
    digest = str(artifacts[role].get("sha256", ""))
    if not valid_sha256(digest):
        raise ValueError(f"M4 plan M1 artifact {role} has an invalid digest")
    return digest


class M4PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(alias="schemaVersion")
    formal_run_id: str = Field(alias="formalRunId", min_length=1)
    authorization_sha256: str = Field(alias="authorizationSha256")
    protocol_freeze_sha256: str = Field(alias="protocolFreezeSha256")
    m1_manifest_sha256: str = Field(alias="m1ManifestSha256")
    m2_manifest_sha256: str = Field(alias="m2ManifestSha256")
    artifact_root: str = Field(alias="artifactRoot", min_length=1)
    candidate_compile_manifests: dict[str, str] = Field(alias="candidateCompileManifests")

    @model_validator(mode="after")
    def validate_contract(self) -> M4PlanRequest:
        if self.schema_version != "qf.qgnn-m4-low-budget-plan-request.v1":
            raise ValueError("unsupported M4 low-budget plan request schema")
        for digest in (
            self.authorization_sha256,
            self.protocol_freeze_sha256,
            self.m1_manifest_sha256,
            self.m2_manifest_sha256,
            *self.candidate_compile_manifests.values(),
        ):
            if not valid_sha256(digest):
                raise ValueError("M4 low-budget plan hashes must be lowercase SHA-256")
        if sorted(self.candidate_compile_manifests) != [
            f"m2-candidate-{index:02d}" for index in range(EXPECTED_CANDIDATES)
        ]:
            raise ValueError("M4 plan requires all 12 compiled candidate manifests")
        return self


def deterministic_index(label: str, length: int) -> int:
    if length <= 0:
        raise ValueError("M4 deterministic sampling received an empty stratum")
    return int(hashlib.sha256(label.encode("utf-8")).hexdigest()[:16], 16) % length


def parameter_values(ir: dict[str, Any]) -> dict[str, float]:
    operations = ir.get("operations")
    if not isinstance(operations, list):
        raise ValueError("M4 candidate Canonical IR has no operations")
    values: dict[str, set[float]] = {"omega_0": set(), "omega_1": set()}
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        source = str(operation.get("parameterSource", ""))
        if source in {"shared:omega_0", "shared:omega_1"}:
            values[source.split(":", 1)[1]].add(float(operation["angle"]))
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("M4 candidate does not expose exactly two shared parameters")
    return {key: next(iter(items)) for key, items in values.items()}


def perturbation(candidate_id: str, step: int, parameter: str) -> int:
    digest = hashlib.sha256(
        f"qf-m4-spsa:{candidate_id}:{step}:{parameter}:20260804".encode()
    ).digest()
    return 1 if digest[0] & 1 else -1


def build_plan(request: M4PlanRequest) -> dict[str, Any]:
    artifact_root = Path(request.artifact_root).resolve()
    expected_artifact_root = EXPECTED_ROOT / ".local" / "artifacts"
    if artifact_root != expected_artifact_root:
        raise ValueError("M4 low-budget plan must use the formal local artifact root")
    m1 = json.loads(read_verified(artifact_root, request.m1_manifest_sha256))
    m2 = json.loads(read_verified(artifact_root, request.m2_manifest_sha256))
    if (
        m1.get("formalTestState") != "SEALED"
        or m2.get("formalTestState") != "SEALED"
        or m1.get("protocolFreezeSha256") != request.protocol_freeze_sha256
        or m2.get("protocolFreezeSha256") != request.protocol_freeze_sha256
    ):
        raise ValueError("M4 plan parents are not bound to the same SEALED protocol")

    feature_angles_sha256 = artifact_digest(m1, "featureAngles")
    feature_table_sha256 = artifact_digest(m1, "featureTable")
    support_edges_sha256 = artifact_digest(m1, "inducedSupportEdges")
    feature_dates = sorted(
        pd.read_parquet(
            artifact_root / "sha256" / feature_angles_sha256[:2] / feature_angles_sha256,
            columns=["as_of_date"],
        )["as_of_date"].astype(str).unique().tolist()
    )
    training_dates = [date for date in feature_dates if "20160101" <= date <= "20221231"]
    validation_dates = [date for date in feature_dates if "20230101" <= date <= "20231231"]
    if len(training_dates) != 84 or len(validation_dates) != EXPECTED_VALIDATION_DATES:
        raise ValueError("M4 plan monthly training/validation dates are incomplete")
    midpoint = len(training_dates) // 2
    strata = [training_dates[:midpoint], training_dates[midpoint:]]
    selected_training_dates = [
        stratum[
            deterministic_index(
                f"{request.protocol_freeze_sha256}:M4_LOW:time-batch:{index}",
                len(stratum),
            )
        ]
        for index, stratum in enumerate(strata)
    ]
    if len(set(selected_training_dates)) != EXPECTED_TRAINING_DATES:
        raise ValueError("M4 training date strata did not produce two distinct dates")

    labels = pd.read_parquet(
        artifact_root / "sha256" / feature_table_sha256[:2] / feature_table_sha256,
        columns=["split", "label"],
    )
    training_labels = labels.loc[labels["split"] == "TRAIN", "label"].dropna().astype(float)
    if training_labels.empty or not training_labels.map(math.isfinite).all():
        raise ValueError("M4 training labels are unavailable or non-finite")
    lower = float(training_labels.quantile(0.05))
    upper = float(training_labels.quantile(0.95))
    if not (math.isfinite(lower) and math.isfinite(upper) and 0 <= lower < upper):
        raise ValueError("M4 training-only label scale is invalid")

    candidates = m2.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != EXPECTED_CANDIDATES:
        raise ValueError("M4 plan requires the frozen 12-candidate M2 catalog")
    candidate_plans = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError("M4 M2 candidate is not an object")
        candidate_id = f"m2-candidate-{index:02d}"
        if candidate.get("candidateId") != candidate_id:
            raise ValueError("M4 M2 candidate order or identity changed")
        canonical_ir_artifact = str(candidate.get("canonicalIrArtifactSha256", ""))
        ir = json.loads(read_verified(artifact_root, canonical_ir_artifact))
        initial_parameters = parameter_values(ir)
        steps = []
        for step in range(4):
            a_k = 0.08 / ((step + 1) ** 0.602)
            c_k = 0.05 / ((step + 1) ** 0.101)
            delta = {
                parameter: perturbation(candidate_id, step, parameter)
                for parameter in ("omega_0", "omega_1")
            }
            steps.append(
                {
                    "step": step,
                    "learningRate": a_k,
                    "perturbationScale": c_k,
                    "delta": delta,
                    "evaluations": ["PLUS", "MINUS"],
                    "timeBatches": selected_training_dates,
                    "dependsOnPreviousStepTerminalResults": step > 0,
                }
            )
        candidate_plans.append(
            {
                "candidateId": candidate_id,
                "canonicalIrSha256": candidate["canonicalIrSha256"],
                "canonicalIrArtifactSha256": canonical_ir_artifact,
                "initializationSeed": candidate["initializationSeed"],
                "initialParameters": initial_parameters,
                "parameterOrder": ["omega_0", "omega_1"],
                "optimizerSteps": steps,
                "compileManifestSha256": request.candidate_compile_manifests[candidate_id],
            }
        )

    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "schemaVersion": "qf.qgnn-m4-low-budget-plan.v1",
        "formalRunId": request.formal_run_id,
        "authorizationSha256": request.authorization_sha256,
        "protocolFreezeSha256": request.protocol_freeze_sha256,
        "m1ManifestSha256": request.m1_manifest_sha256,
        "m2ManifestSha256": request.m2_manifest_sha256,
        "formalTestState": "SEALED",
        "scientificWidth": EXPECTED_WIDTH,
        "target": "tianyan-287",
        "optimizer": {
            "name": "SPSA",
            "steps": 4,
            "objectiveEvaluationsPerStep": 2,
            "parameters": ["omega_0", "omega_1"],
            "a": 0.08,
            "alpha": 0.602,
            "c": 0.05,
            "gamma": 0.101,
            "gradientEstimate": "(L_plus-L_minus)/(2*c_k)*delta_inverse",
            "update": "theta_next=theta-a_k*gradient",
        },
        "trainingDateSampling": {
            "method": "SEEDED_TWO_STRATA_FROM_84_TRAINING_MONTH_END_DATES",
            "seed": 20260804,
            "dates": selected_training_dates,
            "formalValuesRead": 0,
        },
        "validationDates": validation_dates,
        "readout": {
            "primaryObservable": "Z_ALL_101",
            "rawRiskProbability": "p_i=(1-Z_i)/2",
            "prediction": "q05_train+p_i*(q95_train-q05_train)",
            "clip": [lower, upper],
            "trainingLabelQuantile05": lower,
            "trainingLabelQuantile95": upper,
            "labelScaleSource": "TRAIN_SPLIT_ONLY",
            "primaryLoss": "ASSET_DATE_MAE_FIVE_DAY_DOWNSIDE_RISK",
            "rawFeatureBypass": False,
        },
        "budget": {
            "candidates": EXPECTED_CANDIDATES,
            "optimizerSteps": 4,
            "objectiveEvaluationsPerStep": 2,
            "timeBatches": EXPECTED_TRAINING_DATES,
            "seeds": 1,
            "calibrationRepeats": 1,
            "shotsPerCircuit": 100000,
            "trainingObjectiveCircuits": 192,
            "validationCircuitsAfterTraining": 144,
            "maximumCircuitsPerProviderBatch": 50,
        },
        "candidatePlans": candidate_plans,
        "parents": {
            "featureAnglesSha256": feature_angles_sha256,
            "featureTableSha256": feature_table_sha256,
            "inducedSupportEdgesSha256": support_edges_sha256,
        },
        "testIntervalValuesRead": 0,
        "immutableBeforeFirstTrainingSubmission": True,
        "createdAt": created_at,
    }


def main() -> int:
    try:
        request = M4PlanRequest.model_validate_json(sys.stdin.read())
        result = build_plan(request)
        sys.stdout.buffer.write(canonical(result) + b"\n")
        return 0
    except Exception as error:  # noqa: BLE001 - structured worker boundary.
        sys.stdout.buffer.write(
            canonical(
                {
                    "schemaVersion": "qf.worker-error.v1",
                    "errorType": type(error).__name__,
                    "message": str(error),
                }
            )
            + b"\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
