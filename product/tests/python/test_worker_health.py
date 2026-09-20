from qf_finance_worker.cli import WorkerHealth as FinanceHealth
from qf_quantum_worker.cli import WorkerHealth as QuantumHealth


def test_workers_have_distinct_roles_and_shared_namespace() -> None:
    finance = FinanceHealth()
    quantum = QuantumHealth()

    assert finance.worker == "finance"
    assert quantum.worker == "quantum"
    assert finance.process_namespace == quantum.process_namespace == "qfintelligence"
    assert finance.schema_version == quantum.schema_version == "qf.v1"
