"""Frozen six-coordinate quantum maps evaluated through the cqlib SDK."""
from __future__ import annotations
import numpy as np
from cqlib.circuits import Circuit
from cqlib.simulator import StatevectorSimulator
from .research_common import identity


def catalog():
    return [{'candidate_id': f'map_{i:02d}', 'qubits': 6, 'layers': 1 + i // 6,
        'input_scale': [.5, 1., 1.5][i % 3], 'entangler': 'CZ' if (i // 3) % 2 == 0 else 'CRY',
        'edge_angle': .25 * np.pi, 'seed': 2026090802 + i, 'fixed_reference': i == 0,
        'training': 'FROZEN_QUANTUM_PARAMETERS'} for i in range(12)]


def circuit_for(values, spec, measured=False):
    values = np.asarray(values, dtype=float)
    if values.shape != (6,) or not np.isfinite(values).all():
        raise ValueError('QUANTUM_FEATURE_INPUT')
    circuit = Circuit(6)
    for layer in range(spec['layers']):
        for q, x in enumerate(values):
            if layer == 0:
                circuit.h(q)
            circuit.ry(q, float(x * spec['input_scale']))
            circuit.rz(q, float(values[(q + 1) % 6] * .5))
        for q in range(5):
            if spec['entangler'] == 'CZ':
                circuit.cz(q, q+1)
            else:
                circuit.cry(q, q+1, float(spec['edge_angle']))
    if measured:
        for q in range(6):
            circuit.measure(q)
    return circuit


def states(values, spec):
    output = np.empty((len(values), 64), dtype=np.complex128)
    for i, row in enumerate(values):
        obj = StatevectorSimulator(circuit_for(row, spec), omp_threads=1).statevector()
        output[i] = [obj[f'{j:06b}'] for j in range(64)]
    if not np.allclose(np.sum(np.abs(output)**2, axis=1), 1, atol=1e-9):
        raise ValueError('SDK_STATE_NORMALIZATION')
    return output


def observables(state):
    # cqlib basis bitstrings are q5...q0. Readouts below are explicit q0...q5.
    basis = np.arange(64)
    z = np.stack([1 - 2 * ((basis >> q) & 1) for q in range(6)], axis=1)
    joint = np.column_stack([z, z[:, :-1] * z[:, 1:]])
    return np.abs(state)**2 @ joint


def fidelity_kernel(state, anchor_state):
    kernel = np.abs(state.conj() @ anchor_state.T)**2
    if kernel.min(initial=0) < -1e-12 or kernel.max(initial=0) > 1 + 1e-9:
        raise ValueError('FIDELITY_KERNEL_RANGE')
    return np.clip(kernel, 0, 1)


def cache_identity(data_epoch, fold_sha, date, asset, graph_sha, preaggregation_sha, values, spec):
    return identity({'data_epoch': data_epoch, 'task_id': 'STOCK_MONTHLY_300_NEXT_MONTH_VISIBLE',
        'fold_preprocessing_sha': fold_sha, 'signal_date': date, 'asset_id': asset,
        'graph_variant_sha': graph_sha, 'preaggregation_sha': preaggregation_sha,
        'input_sha': identity(np.asarray(values).tolist()), 'quantum_frontend_sha': identity(spec),
        'parameter_sha': identity({k: spec[k] for k in ['layers', 'input_scale', 'entangler', 'edge_angle', 'seed']}),
        'observable_setting_sha': identity(['Z_0:5', 'ZZ_01:45']),
        'backend_evolution_policy_sha': identity({'sdk': 'cqlib1.3.11', 'evolutions_per_input': 1, 'joint_Z_basis': True})})
