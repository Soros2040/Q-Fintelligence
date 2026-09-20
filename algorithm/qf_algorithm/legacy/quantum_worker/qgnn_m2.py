from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from cqlib.circuits import Circuit
from cqlib.simulator import StatevectorSimulator
from pydantic import BaseModel, ConfigDict, Field, model_validator

EXPECTED_ROOT = Path.cwd().resolve()
EXPECTED_WIDTH = 105
EXPECTED_EDGES = 210
EXPECTED_CANDIDATES = 12
FORMAL_CUTOFF = "2023-12-31"
FEATURE_GATES = (
    ("RY", "angle_ret5", "ret5"),
    ("RZ", "angle_ret20", "ret20"),
    ("RX", "angle_downside5", "downside5"),
    ("RY", "angle_downside20", "downside20"),
    ("RZ", "angle_amount_change20", "amount_change20"),
)
SHA256_LENGTH = 64


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def valid_sha256(value: str) -> bool:
    return len(value) == SHA256_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


class M2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(alias="schemaVersion")
    formal_run_id: str = Field(alias="formalRunId", min_length=1)
    authorization_sha256: str = Field(alias="authorizationSha256")
    protocol_freeze_sha256: str = Field(alias="protocolFreezeSha256")
    protocol_spec_sha256: str = Field(alias="protocolSpecSha256")
    m1_manifest_sha256: str = Field(alias="m1ManifestSha256")
    # V4.2 freezes a six-qubit golden task while the legacy Control Plane keeps
    # its own 16-105 gate. The deterministic Canonical IR worker is versioned
    # for the wider adapter contract and remains bounded by the live W_joint
    # before any hardware submission.
    target_width: int = Field(default=EXPECTED_WIDTH, alias="targetWidth", ge=6, le=176)
    execution_profile: Literal["LEGACY_M2_12", "V42_M3_4"] = Field(
        default="LEGACY_M2_12", alias="executionProfile"
    )
    candidate_count: int = Field(default=EXPECTED_CANDIDATES, alias="candidateCount", ge=2, le=12)
    candidate_ids: list[str] = Field(default_factory=list, alias="candidateIds")
    artifact_root: str = Field(alias="artifactRoot", min_length=1)
    state_path: str = Field(alias="statePath", min_length=1)

    @model_validator(mode="after")
    def validate_contract(self) -> M2Request:
        if self.schema_version != "qf.qgnn-m2-request.v1":
            raise ValueError("unsupported M2 request schema")
        for value in (
            self.authorization_sha256,
            self.protocol_freeze_sha256,
            self.protocol_spec_sha256,
            self.m1_manifest_sha256,
        ):
            if not valid_sha256(value):
                raise ValueError("M2 request hashes must be lowercase SHA-256")
        if self.execution_profile == "LEGACY_M2_12":
            if self.candidate_count != EXPECTED_CANDIDATES or self.candidate_ids:
                raise ValueError("legacy M2 requires its exact 12-candidate catalog")
        elif (
            self.target_width != 6
            or self.candidate_count != 4
            or self.candidate_ids
            != ["DIR_L1_R0", "DIR_L1_R1", "DIR_L2_R0", "DIR_L2_R1"]
        ):
            raise ValueError("V4.2 M3 requires the frozen four-candidate six-qubit catalog")
        return self


class M2Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(alias="schemaVersion")
    status: str
    formal_run_id: str = Field(alias="formalRunId")
    formal_test_state: str = Field(alias="formalTestState")
    manifest_sha256: str = Field(alias="manifestSha256")
    counts: dict[str, Any]


class StateWriter:
    def __init__(self, path: Path, initial: dict[str, Any]) -> None:
        self.path = path
        self.value = initial
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write()

    def update(self, **values: Any) -> None:
        self.value.update(values)
        self.value["updatedAt"] = now()
        self.write()

    def write(self) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(canonical(self.value) + b"\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)


class CasStore:
    def __init__(self, root: Path, created_at: str) -> None:
        self.root = root
        self.created_at = created_at
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self,
        value: bytes,
        media_type: str,
        producer: str,
        parents: list[str],
    ) -> dict[str, Any]:
        digest = sha256_bytes(value)
        destination = self.root / "sha256" / digest[:2] / digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            current_digest, current_size = sha256_file(destination)
            if current_digest != digest or current_size != len(value):
                raise ValueError(f"content-addressed collision for {digest}")
        else:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(value)
            try:
                os.chmod(temporary, 0o600)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "schemaVersion": "qf.v1",
            "sha256": digest,
            "bytes": len(value),
            "mediaType": media_type,
            "relativePath": f"sha256/{digest[:2]}/{digest}",
            "createdAt": self.created_at,
            "producer": producer,
            "parentHashes": sorted(set(parents)),
        }


def read_verified_artifact(root: Path, digest: str) -> bytes:
    if not valid_sha256(digest):
        raise ValueError("invalid artifact digest")
    path = root / "sha256" / digest[:2] / digest
    value = path.read_bytes()
    if sha256_bytes(value) != digest:
        raise ValueError(f"artifact hash mismatch for {digest}")
    return value


def artifact_path(root: Path, artifact: dict[str, Any]) -> Path:
    digest = str(artifact.get("sha256", ""))
    relative = str(artifact.get("relativePath", ""))
    if not valid_sha256(digest) or relative != f"sha256/{digest[:2]}/{digest}":
        raise ValueError("M1 artifact reference is malformed")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("M1 artifact reference escaped the CAS root")
    observed_digest, observed_size = sha256_file(path)
    if observed_digest != digest or observed_size != int(artifact.get("bytes", -1)):
        raise ValueError("M1 artifact evidence mismatch")
    return path


def normalize_float(value: float) -> float:
    rounded = round(float(value), 10)
    return 0.0 if abs(rounded) < 5e-11 else rounded


def canonical_ir_hash(ir: dict[str, Any]) -> str:
    without_hash = {key: value for key, value in ir.items() if key != "canonicalIrSha256"}
    return sha256_bytes(canonical(without_hash))


def reverse_edge_hash(source: int, affected: int, weight: float) -> tuple[str, str]:
    def value(control: int, target: int) -> dict[str, Any]:
        ir = {
            "schemaVersion": "qf.canonical-circuit-ir.v1",
            "width": 2,
            "operations": [
                {
                    "op": "CRY",
                    "controls": [control],
                    "targets": [target],
                    "angle": normalize_float(weight),
                    "directionSemantics": "source_control_to_affected_target",
                }
            ],
        }
        ir["canonicalIrSha256"] = canonical_ir_hash(ir)
        return ir

    return value(source, affected)["canonicalIrSha256"], value(affected, source)[
        "canonicalIrSha256"
    ]


def invert_measurement_bitstring(
    bitstring: str,
    measurement_order: list[int],
    asset_order: list[str],
) -> dict[str, int]:
    if len(bitstring) != len(measurement_order) or set(bitstring) - {"0", "1"}:
        raise ValueError("bitstring does not match measurement order")
    if sorted(measurement_order) != list(range(len(asset_order))):
        raise ValueError("measurement order is not a logical-qubit permutation")
    result: dict[str, int] = {}
    for column, logical_qubit in enumerate(measurement_order):
        result[asset_order[logical_qubit]] = int(bitstring[column])
    return result


def add_operation(circuit: Circuit, operation: dict[str, Any]) -> None:
    gate = str(operation["op"])
    targets = [int(value) for value in operation.get("targets", [])]
    controls = [int(value) for value in operation.get("controls", [])]
    angle = float(operation.get("angle", 0.0))
    if gate == "RX":
        circuit.rx(targets[0], angle)
    elif gate == "RY":
        circuit.ry(targets[0], angle)
    elif gate == "RZ":
        circuit.rz(targets[0], angle)
    elif gate == "CRY":
        circuit.cry(controls[0], targets[0], angle)
    elif gate == "MEASURE_ALL":
        circuit.measure_all()
    else:
        raise ValueError(f"unsupported M2 gate {gate}")


def validate_operation(operation: dict[str, Any], width: int) -> None:
    gate = str(operation.get("op", ""))
    if gate not in {"RX", "RY", "RZ", "CRY", "MEASURE_ALL"}:
        raise ValueError(f"unsupported M2 operation {gate}")
    targets = [int(value) for value in operation.get("targets", [])]
    controls = [int(value) for value in operation.get("controls", [])]
    wires = [*controls, *targets]
    if any(wire < 0 or wire >= width for wire in wires) or len(set(wires)) != len(wires):
        raise ValueError("M2 operation has invalid or repeated wires")
    if gate == "CRY":
        if len(controls) != 1 or len(targets) != 1:
            raise ValueError("directed CRY requires one distinct control and target")
    elif gate == "MEASURE_ALL":
        if controls or targets != list(range(width)):
            raise ValueError("MEASURE_ALL must preserve logical-qubit order")
    elif controls or len(targets) != 1:
        raise ValueError("single-qubit rotation has invalid wires")
    if gate != "MEASURE_ALL" and not math.isfinite(float(operation.get("angle", math.nan))):
        raise ValueError("M2 operation angle must be finite")


def build_candidate_ir(
    candidate_index: int,
    decision_date: str,
    basket_version: str,
    asset_order: list[str],
    angle_rows: pd.DataFrame,
    edge_rows: pd.DataFrame,
    target_width: int = EXPECTED_WIDTH,
    shared_angle_override: tuple[float, float] | None = None,
    candidate_id_override: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(asset_order) != target_width or len(set(asset_order)) != target_width:
        raise ValueError(f"M2 requires exactly {target_width} unique assets")
    if candidate_index < 0 or candidate_index >= EXPECTED_CANDIDATES:
        raise ValueError("M2 candidate index is outside the frozen search space")
    message_layers = 1 + (candidate_index % 2)
    reupload_count = (candidate_index // 2) % 2
    shared_axis = ("RX", "RY", "RZ")[(candidate_index // 4) % 3]
    initialization_seed = (2026080401, 2026080402, 2026080403, 2026080404)[candidate_index % 4]
    shared_angles = (
        (
            normalize_float(0.031 + (candidate_index + 1) * 0.001),
            normalize_float(-0.027 - (candidate_index + 1) * 0.001),
        )
        if shared_angle_override is None
        else tuple(normalize_float(value) for value in shared_angle_override)
    )
    if len(shared_angles) != 2 or not all(math.isfinite(value) for value in shared_angles):
        raise ValueError("M2 shared-angle override must contain two finite values")
    by_asset = angle_rows.set_index("asset_id", drop=False)
    if set(by_asset.index) != set(asset_order):
        raise ValueError("M2 feature-angle assets do not match the frozen basket")
    operations: list[dict[str, Any]] = []

    def feature_layer(selected_gates: tuple[tuple[str, str, str], ...], layer: int) -> None:
        for qubit, asset_id in enumerate(asset_order):
            row = by_asset.loc[asset_id]
            for gate, column, feature in selected_gates:
                operations.append(
                    {
                        "op": gate,
                        "controls": [],
                        "targets": [qubit],
                        "angle": normalize_float(float(row[column])),
                        "parameterSource": f"feature:{feature}",
                        "assetId": asset_id,
                        "layer": layer,
                    }
                )

    def shared_update(layer: int, angle: float) -> None:
        for qubit, asset_id in enumerate(asset_order):
            operations.append(
                {
                    "op": shared_axis,
                    "controls": [],
                    "targets": [qubit],
                    "angle": angle,
                    "parameterSource": f"shared:omega_{layer}",
                    "assetId": asset_id,
                    "layer": layer,
                }
            )

    feature_layer(FEATURE_GATES[:3], 0)
    shared_update(0, shared_angles[0])
    feature_layer(FEATURE_GATES[3:], 1)
    asset_to_qubit = {asset_id: index for index, asset_id in enumerate(asset_order)}
    edge_records = edge_rows.sort_values(
        ["target_asset_id", "training_aggregate_support_weight", "source_asset_id"],
        ascending=[True, False, True],
        kind="mergesort",
    ).to_dict(orient="records")
    if len(edge_records) != target_width * 2:
        raise ValueError(f"M2 requires exactly two incoming edges per {target_width} targets")
    incoming = {asset_id: 0 for asset_id in asset_order}
    for _ in range(message_layers):
        for edge in edge_records:
            source = str(edge["source_asset_id"])
            affected = str(edge["target_asset_id"])
            if source not in asset_to_qubit or affected not in asset_to_qubit or source == affected:
                raise ValueError("M2 directed edge is outside the basket or self-directed")
            incoming[affected] += 1
            operations.append(
                {
                    "op": "CRY",
                    "controls": [asset_to_qubit[source]],
                    "targets": [asset_to_qubit[affected]],
                    "angle": normalize_float(float(edge["theta_radians"])),
                    "parameterSource": "graph:theta(weight)",
                    "sourceAssetId": source,
                    "affectedAssetId": affected,
                    "directionSemantics": "source_control_to_affected_target",
                    "weight": normalize_float(float(edge["weight"])),
                    "layer": 2,
                }
            )
    if any(count != 2 * message_layers for count in incoming.values()):
        raise ValueError(
            "M2 directed support is not exactly two incoming edges per target and layer"
        )
    shared_update(1, shared_angles[1])
    if reupload_count == 1:
        feature_layer(FEATURE_GATES, 3)
    operations.append(
        {
            "op": "MEASURE_ALL",
            "controls": [],
            "targets": list(range(target_width)),
            "measurementOrder": list(range(target_width)),
        }
    )
    for operation in operations:
        validate_operation(operation, target_width)
    ir: dict[str, Any] = {
        "schemaVersion": "qf.canonical-circuit-ir.v1",
        "candidateId": candidate_id_override or f"m2-candidate-{candidate_index:02d}",
        "status": "UNTRAINED_M2_STRUCTURE_ONLY",
        "formalTestState": "SEALED",
        "decisionDate": decision_date,
        "basketVersion": basket_version,
        "width": target_width,
        "classicalBits": target_width,
        "assetOrder": asset_order,
        "logicalQubitOrder": list(range(target_width)),
        "measurementOrder": list(range(target_width)),
        "bitstringEndianness": "LEFT_TO_RIGHT_COLUMNS_MATCH_MEASUREMENT_ORDER",
        "messageLayers": message_layers,
        "reuploadCount": reupload_count,
        "sharedUpdateAxis": shared_axis,
        "initializationSeed": initialization_seed,
        "operations": operations,
        "canonicalization": {
            "floatPrecisionDecimals": 10,
            "fixedEdgeTraversal": "target_then_weight_desc_source_asc",
            "preserveControlTargetOrder": True,
            "removeIdentityAndZeroAngle": True,
            "mergeAdjacentSameAxisRotations": True,
            "sortDisjointCommutingGates": True,
        },
        "rawFeatureBypass": False,
        "originalFeatureClassicalInputs": [],
    }
    ir["canonicalIrSha256"] = canonical_ir_hash(ir)
    circuit = Circuit(target_width)
    for operation in operations:
        add_operation(circuit, operation)
    qcis = circuit.qcis
    compile_report = {
        "schemaVersion": "qf.m2-cqlib-object-report.v1",
        "candidateId": ir["candidateId"],
        "canonicalIrSha256": ir["canonicalIrSha256"],
        "cqlibVersion": "1.3.11",
        "width": int(circuit.num_qubits),
        "depth": int(circuit.depth()),
        "instructionCount": len(circuit.instruction_sequence),
        "qcisSha256": sha256_bytes(qcis.encode("utf-8")),
        "statevectorAllocated": False,
        "simulatorUsed": False,
        "formalTestState": "SEALED",
        "qcis": qcis,
    }
    return ir, compile_report


def z_expectation(probabilities: dict[str, float], bit_column: int = 0) -> float:
    return float(
        sum(
            (1.0 if bitstring[bit_column] == "0" else -1.0) * probability
            for bitstring, probability in probabilities.items()
        )
    )


def run_preflight_once() -> dict[str, Any]:
    circuit = Circuit(3)
    circuit.ry(0, 0.31)
    circuit.rz(1, -0.17)
    circuit.rx(2, 0.22)
    circuit.cry(0, 1, 0.19)
    circuit.cry(2, 1, -0.11)
    circuit.measure_all()
    probabilities = {
        str(key): float(value)
        for key, value in StatevectorSimulator(circuit, omp_threads=1).probs().items()
    }
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-10):
        raise ValueError("M2 PREFLIGHT_ONLY probabilities do not normalize")

    theta = 0.37
    epsilon = 1e-5

    def single_qubit_expectation(angle: float) -> float:
        probe = Circuit(1)
        probe.ry(0, angle)
        probe.measure_all()
        values = {
            str(key): float(value)
            for key, value in StatevectorSimulator(probe, omp_threads=1).probs().items()
        }
        return z_expectation(values)

    finite_difference = (
        single_qubit_expectation(theta + epsilon) - single_qubit_expectation(theta - epsilon)
    ) / (2 * epsilon)
    analytic = -math.sin(theta)
    if not math.isclose(finite_difference, analytic, rel_tol=1e-6, abs_tol=1e-7):
        raise ValueError("M2 finite-difference gradient preflight failed")
    forward_hash, reverse_hash = reverse_edge_hash(0, 1, 0.19)
    if forward_hash == reverse_hash:
        raise ValueError("M2 reverse edge did not change Canonical IR")
    inversion = invert_measurement_bitstring("010", [2, 0, 1], ["A", "B", "C"])
    if inversion != {"C": 0, "A": 1, "B": 0}:
        raise ValueError("M2 measurement-order asset inversion failed")
    return {
        "schemaVersion": "qf.qgnn-m2-preflight.v1",
        "status": "PREFLIGHT_ONLY",
        "formalTestState": "SEALED",
        "width": 3,
        "simulator": "cqlib.StatevectorSimulator",
        "simulatorRuns": 5,
        "probabilitySum": sum(probabilities.values()),
        "probabilities": probabilities,
        "scientificMetric": None,
        "qasRank": None,
        "reverseEdge": {
            "forwardCanonicalIrSha256": forward_hash,
            "reverseCanonicalIrSha256": reverse_hash,
            "different": True,
        },
        "gradientCheck": {
            "parameter": "single_qubit_shared_RY",
            "theta": theta,
            "epsilon": epsilon,
            "finiteDifference": finite_difference,
            "analytic": analytic,
            "absoluteError": abs(finite_difference - analytic),
            "status": "PASS",
        },
        "measurementInversion": inversion,
    }


def build_m2(request: M2Request) -> dict[str, Any]:
    project_root = Path.cwd().resolve()
    if project_root != EXPECTED_ROOT:
        raise ValueError("M2 must execute in the authoritative WSL project root")
    artifact_root = Path(request.artifact_root).resolve()
    state_path = Path(request.state_path).resolve()
    if not artifact_root.is_relative_to(project_root / ".local"):
        raise ValueError("M2 artifact root must remain under project .local")
    if not state_path.is_relative_to(project_root / ".local" / "state"):
        raise ValueError("M2 state path must remain under .local/state")
    v42_profile = request.execution_profile == "V42_M3_4"
    completion_status = "M3_CANDIDATES_READY" if v42_profile else "M2_PREFLIGHT_READY"
    manifest_schema = (
        "qf.v42-qgnn-m3-candidate-manifest.v1"
        if v42_profile
        else "qf.qgnn-m2-manifest.v1"
    )
    candidate_layout = (
        list(zip(request.candidate_ids, (0, 2, 1, 3), strict=True))
        if v42_profile
        else [
            (f"m2-candidate-{index:02d}", index)
            for index in range(EXPECTED_CANDIDATES)
        ]
    )
    if state_path.exists():
        previous = json.loads(state_path.read_bytes())
        if previous.get("status") == completion_status:
            parsed = M2Result.model_validate(previous["result"])
            return parsed.model_dump(by_alias=True)
    created_at = now()
    state = StateWriter(
        state_path,
        {
            "schemaVersion": (
                "qf.v42-qgnn-m3-candidate-build-state.v1"
                if v42_profile
                else "qf.qgnn-m2-build-state.v1"
            ),
            "formalRunId": request.formal_run_id,
            "authorizationSha256": request.authorization_sha256,
            "protocolFreezeSha256": request.protocol_freeze_sha256,
            "protocolSpecSha256": request.protocol_spec_sha256,
            "m1ManifestSha256": request.m1_manifest_sha256,
            "status": "IN_PROGRESS",
            "phase": "VERIFY_M1",
            "formalTestState": "SEALED",
            "createdAt": created_at,
            "updatedAt": created_at,
        },
    )
    parents = [
        request.authorization_sha256,
        request.protocol_freeze_sha256,
        request.protocol_spec_sha256,
        request.m1_manifest_sha256,
    ]
    m1 = json.loads(read_verified_artifact(artifact_root, request.m1_manifest_sha256))
    if (
        m1.get("schemaVersion") != "qf.qgnn-m1-manifest.v2"
        or m1.get("status") != "PASS_REAL_DATA"
        or m1.get("formalTestState") != "SEALED"
        or m1.get("maximumMaterializedDate") != FORMAL_CUTOFF
        or m1.get("formalRunId") != request.formal_run_id
        or m1.get("authorizationSha256") != request.authorization_sha256
        or m1.get("protocolFreezeSha256") != request.protocol_freeze_sha256
        or m1.get("protocolSpecSha256") != request.protocol_spec_sha256
    ):
        raise ValueError("M2 parent M1 manifest failed the governance or SEALED gate")
    training_count = int(m1["counts"]["trainingDecisionDates"])
    reference_basket = m1["baskets"][training_count - 1]
    decision_date = str(reference_basket["asOfDate"])
    if decision_date > "2022-12-31":
        raise ValueError("M2 reference circuit must use training-only data")
    basket_version = str(reference_basket["basketVersion"])
    asset_order = [str(value) for value in reference_basket["assetIds"]]
    artifacts = m1["artifacts"]
    angles = pd.read_parquet(artifact_path(artifact_root, artifacts["featureAngles"]))
    edges = pd.read_parquet(artifact_path(artifact_root, artifacts["inducedSupportEdges"]))
    mappings = pd.read_parquet(artifact_path(artifact_root, artifacts["assetNodeQubitMap"]))
    compact_date = decision_date.replace("-", "")
    angle_rows = angles.loc[angles["as_of_date"].astype(str) == compact_date].copy()
    edge_rows = edges.loc[edges["as_of_date"].astype(str) == compact_date].copy()
    mapping_rows = mappings.loc[mappings["as_of_date"].astype(str) == compact_date].sort_values(
        "logical_qubit_id"
    )
    if (
        len(angle_rows) != request.target_width
        or len(edge_rows) != request.target_width * 2
        or len(mapping_rows) != request.target_width
        or mapping_rows["logical_qubit_id"].astype(int).tolist()
        != list(range(request.target_width))
        or mapping_rows["asset_id"].astype(str).tolist() != asset_order
    ):
        raise ValueError(
            "M2 training reference angles, graph or mapping are not the frozen "
            f"{request.target_width}-wide sample"
        )
    cas = CasStore(artifact_root, created_at)
    model_spec = {
        "schemaVersion": "qf.qgnn-model-spec.v1",
        "status": (
            "M3_QAS_CANDIDATES_PRE_TRAINING"
            if v42_profile
            else "M2_DIRECTION_SENSITIVE_STRUCTURE"
        ),
        "formalTestState": "SEALED",
        "width": request.target_width,
        "assetsPerQubit": 1,
        "edgeRegisterQubits": 0,
        "auxiliaryQubits": 0,
        "featureOrder": [item[2] for item in FEATURE_GATES],
        "featureGateAxes": [item[0] for item in FEATURE_GATES],
        "featureProjection": "NONE_FROZEN",
        "projectionTrainable": False,
        "directedMessage": {
            "gate": "CRY",
            "control": "source",
            "target": "affected",
            "weightSensitive": True,
            "incomingEdgesPerTarget": 2,
            "messageLayerSearchSpace": [1, 2],
            "forbiddenStandaloneDirectedClaims": ["CZ", "ZZ", "SYMMETRIC_FSIM"],
        },
        "readout": {
            "function": "shared_h_omega(z_i,zz_sparse_i)",
            "quantumInputs": ["Z_i", "ZZ_frozen_sparse_edges"],
            "rawFeatureInputs": [],
            "rawFeatureBypass": False,
            "netSpilloverFeatureBypass": False,
            "sharedAcrossAssets": True,
        },
        "operationPool": ["I", "H", "RX", "RY", "RZ", "CRY_DIRECTED_DECOMPOSABLE"],
        "candidateCount": request.candidate_count,
        "candidateStatus": (
            "UNTRAINED_M3_CANDIDATE"
            if v42_profile
            else "UNTRAINED_M2_STRUCTURE_ONLY"
        ),
        "canonicalization": {
            "duplicatePolicy": "ONE_FORMAL_LABEL_PER_CANONICAL_IR_HASH",
            "fixedEdgeTraversal": "target_then_weight_desc_source_asc",
            "floatPrecisionDecimals": 10,
            "mergeAdjacentSameAxisRotations": True,
            "preserveControlTargetOrder": True,
            "removeIdentityAndZeroAngle": True,
            "sortDisjointCommutingGates": True,
        },
    }
    model_artifact = cas.put_bytes(
        canonical(model_spec) + b"\n",
        "application/json",
        "qf.qgnn-model-spec.v1",
        parents,
    )
    state.update(
        phase=f"BUILD_{request.target_width}_CANONICAL_IR",
        referenceDecisionDate=decision_date,
    )
    candidate_catalog: list[dict[str, Any]] = []
    all_artifacts: dict[str, dict[str, Any]] = {"modelSpec": model_artifact}
    for catalog_index, (candidate_id, architecture_index) in enumerate(candidate_layout):
        ir, report = build_candidate_ir(
            architecture_index,
            decision_date,
            basket_version,
            asset_order,
            angle_rows,
            edge_rows,
            request.target_width,
            candidate_id_override=candidate_id,
        )
        if v42_profile:
            ir["status"] = "UNTRAINED_M3_CANDIDATE"
            ir["canonicalIrSha256"] = canonical_ir_hash(ir)
            report["canonicalIrSha256"] = ir["canonicalIrSha256"]
        qcis = str(report.pop("qcis"))
        ir_artifact = cas.put_bytes(
            canonical(ir) + b"\n",
            "application/json",
            "qf.canonical-circuit-ir.v1",
            [*parents, model_artifact["sha256"]],
        )
        qcis_artifact = cas.put_bytes(
            qcis.encode("utf-8"),
            "text/x-qcis",
            "qf.m2-cqlib-generic-qcis.v1",
            [*parents, ir_artifact["sha256"]],
        )
        if qcis_artifact["sha256"] != report["qcisSha256"]:
            raise ValueError("M2 cqlib QCIS digest changed during CAS storage")
        candidate_catalog.append(
            {
                "schemaVersion": "qf.qas-candidate-spec.v1",
                "candidateId": ir["candidateId"],
                "status": (
                    "UNTRAINED_M3_CANDIDATE"
                    if v42_profile
                    else "UNTRAINED_M2_STRUCTURE_ONLY"
                ),
                "formalTestState": "SEALED",
                "canonicalIrSha256": ir["canonicalIrSha256"],
                "canonicalIrArtifactSha256": ir_artifact["sha256"],
                "genericQcisArtifactSha256": qcis_artifact["sha256"],
                "messageLayers": ir["messageLayers"],
                "reuploadCount": ir["reuploadCount"],
                "sharedUpdateAxis": ir["sharedUpdateAxis"],
                "initializationSeed": ir["initializationSeed"],
                "compileReport": report,
                "hardwareLabels": 0,
                "qasRank": None,
            }
        )
        all_artifacts[f"candidate{catalog_index:02d}CanonicalIr"] = ir_artifact
        all_artifacts[f"candidate{catalog_index:02d}GenericQcis"] = qcis_artifact
        state.update(
            phase=f"BUILD_{request.target_width}_CANONICAL_IR",
            candidatesCompleted=catalog_index + 1,
            candidatesTotal=request.candidate_count,
        )
    hashes = [str(candidate["canonicalIrSha256"]) for candidate in candidate_catalog]
    if len(set(hashes)) != request.candidate_count:
        raise ValueError("M2 canonicalization collapsed distinct frozen candidates")
    catalog_artifact = cas.put_bytes(
        canonical(
            {
                "schemaVersion": "qf.qas-candidate-catalog.v1",
                "status": (
                    "M3_FROZEN_CANDIDATES_PRE_TRAINING"
                    if v42_profile
                    else "M2_STRUCTURE_ONLY_NO_QAS_RANKING"
                ),
                "formalTestState": "SEALED",
                "candidates": candidate_catalog,
            }
        )
        + b"\n",
        "application/json",
        "qf.qas-candidate-catalog.v1",
        [*parents, model_artifact["sha256"]],
    )
    all_artifacts["candidateCatalog"] = catalog_artifact
    state.update(phase="RUN_ONE_TIME_SMALL_GRAPH_PREFLIGHT")
    preflight = run_preflight_once()
    preflight_artifact = cas.put_bytes(
        canonical(preflight) + b"\n",
        "application/json",
        "qf.qgnn-m2-preflight.v1",
        [*parents, model_artifact["sha256"], catalog_artifact["sha256"]],
    )
    all_artifacts["smallGraphPreflight"] = preflight_artifact
    manifest = {
        "schemaVersion": manifest_schema,
        "formalRunId": request.formal_run_id,
        "authorizationSha256": request.authorization_sha256,
        "protocolFreezeSha256": request.protocol_freeze_sha256,
        "protocolSpecSha256": request.protocol_spec_sha256,
        "m1ManifestSha256": request.m1_manifest_sha256,
        "status": completion_status,
        "formalTestState": "SEALED",
        "referenceDecisionDate": decision_date,
        "referenceDataRole": "TRAINING_ONLY_STRUCTURE_AND_GENERIC_CQLIB_OBJECT",
        "modelSpec": model_spec,
        "candidates": candidate_catalog,
        "preflight": preflight,
        "artifacts": all_artifacts,
        "gates": {
            "directionSensitive": "PASS",
            "reverseEdgeCanonicalIrDifferent": "PASS",
            "sourceAffectedAxes": "PASS",
            "edgeWeightSensitive": "PASS",
            "featureGateAxesAndOrder": "PASS",
            "measurementEndianness": "PASS",
            "nodePermutationReversible": "PASS",
            "finiteDifferenceGradient": "PASS",
            "rawFeatureBypassAbsent": "PASS",
            "fullWidthObjectWithoutStatevector": "PASS",
            "unsupportedGateFailClosed": "PASS",
            "invalidWidthEdgeAndDuplicateWireFailClosed": "PASS",
            "unfrozenProjectionFailClosed": "PASS",
            "formalValuesRead": 0,
            "hardwareJobs": 0,
            "qasRanksProduced": 0,
        },
        "counts": {
            "qubits": request.target_width,
            "classicalBits": request.target_width,
            "edgeRegisterQubits": 0,
            "auxiliaryQubits": 0,
            "assets": request.target_width,
            "incomingEdgesPerTarget": 2,
            "directedEdgesPerLayer": request.target_width * 2,
            "candidates": request.candidate_count,
            "distinctCanonicalIrHashes": len(set(hashes)),
            "fullWidthCqlibObjects": request.candidate_count,
            "fullWidthStatevectorRuns": 0,
            "smallGraphPreflightRuns": 1,
            "hardwareJobs": 0,
        },
        "createdAt": created_at,
    }
    manifest_artifact = cas.put_bytes(
        canonical(manifest) + b"\n",
        "application/json",
        manifest_schema,
        [*parents, *[artifact["sha256"] for artifact in all_artifacts.values()]],
    )
    result = {
        "schemaVersion": (
            "qf.v42-qgnn-m3-candidate-result.v1"
            if v42_profile
            else "qf.qgnn-m2-result.v1"
        ),
        "status": completion_status,
        "formalRunId": request.formal_run_id,
        "formalTestState": "SEALED",
        "manifestSha256": manifest_artifact["sha256"],
        "counts": manifest["counts"],
    }
    M2Result.model_validate(result)
    state.update(status=completion_status, phase="COMPLETE", result=result)
    return result


def main() -> None:
    try:
        request = M2Request.model_validate_json(sys.stdin.read())
        result = build_m2(request)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as error:
        sys.stderr.write(
            f"[qgnn-m2] {error.__class__.__name__}: {str(error).replace(chr(10), ' ')[:500]}\n"
        )
        sys.stdout.write(
            json.dumps(
                {
                    "schemaVersion": "qf.qgnn-m2-error.v1",
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
