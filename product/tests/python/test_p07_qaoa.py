from __future__ import annotations

import hashlib
import json

from qf_quantum_worker import p07_qaoa


def test_p07_batch_uses_canonical_ir_and_fifty_distinct_cqlib_outputs() -> None:
    batch = p07_qaoa.generate()
    assert batch["schema_version"] == "qf.p07.qaoa-batch.v1"
    assert batch["circuit_count"] == 50
    circuits = batch["circuits"]
    ir_hashes = {item["circuit_ir"]["circuit_hash"] for item in circuits}
    qcis_hashes = {item["qcis_sha256"] for item in circuits}
    assert len(ir_hashes) == 50
    assert len(qcis_hashes) == 50
    assert {item["manifest"]["family"] for item in circuits} == {"qaoa_p1", "qaoa_p2"}
    for item in circuits:
        circuit_ir = item["circuit_ir"]
        identity = dict(circuit_ir)
        actual = identity.pop("circuit_hash")
        expected = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assert actual == expected
        assert circuit_ir["schema_version"] == "qf.circuit-ir.v1"
        assert circuit_ir["formal_test_sealed"] is True
        assert item["manifest"]["shots"] == 100
        assert abs(item["manifest"]["local_preflight"]["probability_sum"] - 1) < 1e-9
