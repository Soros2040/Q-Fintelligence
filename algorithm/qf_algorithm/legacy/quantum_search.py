"""Frozen E05/E06 six-asset mechanisms, local cqlib ideal and explicit density noise.

Workers own disjoint ledger directories. This module has no network entry point.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import itertools
import json
import math
from pathlib import Path
import resource
import subprocess
import sys
import time

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sklearn.covariance import LedoitWolf
from cqlib.circuits import Circuit
from cqlib.simulator import StatevectorSimulator

from .bridge import qubo, objective, solve
from .research_common import Budget, digest, identity, write_json, utc, holm

ROOT = Path(__file__).resolve().parents[3]
from .local_noise import parse_qcis, simulate, NoiseRule

SHOTS = [256, 1024, 4096]
SEEDS = [1701, 1702, 1703, 1704, 1705]
DEV_SEEDS = [1601, 1602, 1603]
BIT_MATRIX = ((np.arange(64)[:, None] >> np.arange(6)) & 1).astype(float)
FEASIBLE = BIT_MATRIX.sum(axis=1) == 3


def text_sha256(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def noise_catalog():
    return [{'id': name, 'single_depolarizing': p, 'two_endpoint_depolarizing': 5*p,
             'readout_p01': 10*p, 'readout_p10': 10*p,
             'channel_definition': '(1-p)rho+p*I/2 per affected qubit after gate; independent endpoints'}
            for name, p in [('ideal', 0.), ('low', .001), ('medium', .003), ('high', .01)]]


def candidate_catalog():
    """3 depths x 10 predetermined connected mixer orders; candidate IDs are stable."""
    ring = [(q, (q+1) % 6) for q in range(6)]
    dense = list(itertools.combinations(range(6), 2))
    schedules = [ring, ring[::-1], ring[::2]+ring[1::2], [(q, q+1) for q in range(5)],
                 [(q, q+1) for q in range(5)][::-1], [(0, q) for q in range(1, 6)],
                 [(5, q) for q in range(5)], dense, dense[::-1], dense[::2]+dense[1::2]]
    return [{'candidate_id': f'qas_{10*(p-1)+i:02d}', 'depth': p, 'mixer_edges': edges,
             'initial_ones': [0, 1, 2], 'ansatz': 'FIXED_K_ORDERED_XY_PRODUCT', 'parameters': 2*p}
            for p in [1, 2, 3] for i, edges in enumerate(schedules)]


def base_instances(daily, assets):
    needed = {'signal_date', 'asset_id', 'trade_date', 'entry_date', 'exit_date',
              'return_fixed_contract', 'ts_code'}
    if not needed.issubset(daily):
        raise ValueError('HOLDING_SCHEMA')
    daily = daily[daily.asset_id.isin(assets)].copy()
    if daily.duplicated(['signal_date', 'asset_id', 'trade_date']).any():
        raise ValueError('DUPLICATE_HOLDING_DAY')
    rows = []
    for (signal, asset), group in daily.groupby(['signal_date', 'asset_id'], sort=True):
        group = group.sort_values('trade_date')
        returns = group.return_fixed_contract.to_numpy(float)
        if (len(group) != 5 or group.ts_code.nunique() != 1 or group.entry_date.nunique() != 1
                or group.exit_date.nunique() != 1 or not np.isfinite(returns).all()
                or np.any(returns <= -1) or group.trade_date.iloc[0] != group.entry_date.iloc[0]
                or group.trade_date.iloc[-1] != group.exit_date.iloc[0]):
            raise ValueError('FIXED_CONTRACT_FIVE_SESSION_WINDOW')
        rows.append({'signal': str(signal), 'asset': str(asset), 'exit': str(group.exit_date.iloc[0]),
                     'return_5': float(np.prod(1+returns)-1)})
    windows = pd.DataFrame(rows)
    panel = windows.pivot(index='signal', columns='asset', values='return_5').reindex(columns=assets)
    exits = windows.groupby('signal').exit.max().reindex(panel.index)
    complete = panel.notna().all(axis=1)
    panel, exits = panel.loc[complete], exits.loc[complete]
    eligible = [d for d in panel.index if '2019-01-01' <= d <= '2021-12-31'
                and int(((exits <= d) & (panel.index < d)).sum()) >= 126]
    if len(eligible) < 9:
        raise ValueError('DEVELOPMENT_HISTORY_COVERAGE')
    selected = [eligible[i] for i in np.linspace(0, len(eligible)-1, 9, dtype=int)]
    bases = []
    for number, anchor in enumerate(selected, 1):
        history = panel.loc[(exits <= anchor) & (panel.index < anchor)].tail(126)
        estimator = LedoitWolf().fit(history.to_numpy())
        covariance = estimator.covariance_
        if np.linalg.eigvalsh(covariance).min() < -1e-12:
            raise ValueError('COVARIANCE_PSD')
        bases.append({'base_id': f'B{number:02d}', 'anchor_date': anchor,
            'asset_order': assets, 'history_signal_dates': history.index.tolist(),
            'latest_history_exit': str(exits.loc[history.index].max()), 'history_observations': 126,
            'history_values_sha256': identity(history.to_numpy().tolist()),
            'mu': estimator.location_.tolist(), 'sigma': covariance.tolist(),
            'covariance_estimator': 'LedoitWolf', 'shrinkage': float(estimator.shrinkage_),
            'return_definition': 'actual compounded fixed-contract next-open through fifth-session close',
            'window_dependence': 'overlapping five-session observations; descriptive mechanism covariance',
            'k': 3, 'cost': .001, 'previous': [1/3, 1/3, 1/3, 0., 0., 0.]})
    return bases


def energies(instance):
    mu, sigma, previous = map(np.asarray, [instance['mu'], instance['sigma'], instance['previous']])
    matrix, const = qubo(mu, sigma, 3, instance['lambda'], .001, previous)
    if matrix.shape != (6, 6) or not np.allclose(matrix, matrix.T, atol=1e-12):
        raise ValueError('SYMMETRIC_QUBO')
    values = np.einsum('bi,ij,bj->b', BIT_MATRIX, matrix, BIT_MATRIX)+const
    direct = np.array([objective(x, mu, sigma, 3, instance['lambda'], .001, previous) for x in BIT_MATRIX])
    if not np.allclose(values, direct, rtol=1e-10, atol=1e-12):
        raise ValueError('BRIDGE_IDENTITY')
    bound = float(np.abs(np.diag(matrix)).sum()+2*np.abs(np.triu(matrix, 1)).sum())
    penalty = 1.01*bound+1e-12
    penalized = values+penalty*(BIT_MATRIX.sum(axis=1)-3)**2
    optimum = float(values[FEASIBLE].min())
    return matrix, values, penalized, optimum, penalty


def rzz(lines, i, j, angle):
    lines.extend([f'CX Q{i} Q{j}', f'RZ Q{j} {float(angle):.17g}', f'CX Q{i} Q{j}'])


def qaoa_qcis(instance, spec, parameters):
    parameters = np.asarray(parameters, float)
    if parameters.shape != (2*spec['depth'],) or not np.isfinite(parameters).all():
        raise ValueError('QAOA_PARAMETER_SHAPE')
    matrix, values, _, _, _ = energies(instance)
    scale = max(float(np.ptp(values[FEASIBLE])), 1e-12)
    off = matrix.copy(); np.fill_diagonal(off, 0)
    h = -.5*np.diag(matrix)-.5*off.sum(axis=1)
    # Q is symmetric: unordered Ising coupling J_ij=Q_ij/2.
    lines = [f'X Q{q}' for q in spec['initial_ones']]
    for layer in range(spec['depth']):
        gamma, beta = parameters[2*layer:2*layer+2]
        for q in range(6):
            lines.append(f'RZ Q{q} {float(2*gamma*h[q]/scale):.17g}')
        for i, j in itertools.combinations(range(6), 2):
            if abs(matrix[i, j]) > 0:
                rzz(lines, i, j, gamma*matrix[i, j]/scale)
        for i, j in spec['mixer_edges']:
            # exp[-i beta(XX+YY)/2]=RXX(beta) RYY(beta); the terms commute.
            lines.extend([f'H Q{i}', f'H Q{j}'])
            rzz(lines, i, j, beta)
            lines.extend([f'H Q{i}', f'H Q{j}', f'RZ Q{i} {-np.pi/2:.17g}',
                          f'RZ Q{j} {-np.pi/2:.17g}', f'H Q{i}', f'H Q{j}'])
            rzz(lines, i, j, beta)
            lines.extend([f'H Q{i}', f'H Q{j}', f'RZ Q{i} {np.pi/2:.17g}', f'RZ Q{j} {np.pi/2:.17g}'])
    lines.extend(f'M Q{q}' for q in range(6))
    return '\n'.join(lines)+'\n'


def sdk_probabilities(qcis):
    circuit = Circuit.load(qcis)
    qubit_order = [qubit.index for qubit in circuit.qubits]
    if sorted(qubit_order) != list(range(6)):
        raise ValueError('SDK_LOGICAL_SIX_QUBIT_AXIS')
    simulator = StatevectorSimulator(circuit, omp_threads=1)
    raw = simulator.probs()
    if set(raw) != {f'{i:06b}' for i in range(64)}:
        raise ValueError('SDK_BIT_ORDER')
    # StatevectorSimulator indexes its first-appearance circuit.qubits order.
    # Restore numeric logical q5...q0 explicitly for routed circuits.
    raw_probabilities = np.array([raw[f'{i:06b}'] for i in range(64)])
    numeric_indices = sum(((np.arange(64) >> position) & 1) << qubit
                          for position, qubit in enumerate(qubit_order))
    probabilities = np.empty(64)
    probabilities[numeric_indices] = raw_probabilities
    if not np.isfinite(probabilities).all() or probabilities.min() < -1e-12 or abs(probabilities.sum()-1) > 1e-9:
        raise ValueError('SDK_PROBABILITY_CONSERVATION')
    probabilities = np.maximum(probabilities, 0); probabilities /= probabilities.sum()
    return simulator, probabilities


def noisy_probabilities(qcis, profile):
    singles = ('H', 'X', 'Y', 'Z', 'S', 'S+', 'T', 'T+', 'RX', 'RY', 'RZ')
    rules = (NoiseRule('depolarizing', profile['single_depolarizing'], gates=singles),
             NoiseRule('depolarizing', profile['two_endpoint_depolarizing'], gates=('CX', 'CZ', 'SWAP')),
             NoiseRule('readout', params={'p01': profile['readout_p01'], 'p10': profile['readout_p10']}))
    result = simulate(parse_qcis(qcis), rules)
    if result.bit_labels != tuple(f'{i:06b}' for i in range(64)):
        raise ValueError('DENSITY_BIT_ORDER')
    return result.probabilities


def circuit_resources(qcis):
    spec = parse_qcis(qcis)
    clocks = {q: 0 for q in spec.qubits}
    for op in spec.operations:
        depth = 1+max(clocks[q] for q in op.qubits)
        for q in op.qubits:
            clocks[q] = depth
    return {'gate_count': len(spec.operations), 'two_qubit_gates': sum(len(op.qubits)==2 for op in spec.operations),
            'logical_depth': max(clocks.values()), 'swap_count': sum(op.name=='SWAP' for op in spec.operations),
            'gate_alphabet': sorted({op.name for op in spec.operations}),
            'resource_scope': 'numeric logical QCIS before device routing'}


def count_metrics(counts, values, penalized, optimum):
    if set(counts)-{f'{i:06b}' for i in range(64)} or any(type(v) is not int or v < 0 for v in counts.values()):
        raise ValueError('COUNT_SCHEMA')
    vector = np.array([counts.get(f'{i:06b}', 0) for i in range(64)])
    shots = int(vector.sum())
    if shots < 1:
        raise ValueError('EMPTY_SAMPLE')
    feasible_shots = int(vector[FEASIBLE].sum())
    feasible_gap = (float(vector[FEASIBLE] @ values[FEASIBLE]/feasible_shots-optimum)
                    if feasible_shots else None)
    hit = (values <= optimum+1e-12) & FEASIBLE
    return {'shots': shots, 'feasible_shots': feasible_shots, 'feasible_probability': feasible_shots/shots,
        'conditional_feasible_gap': feasible_gap, 'conditional_status': 'DEFINED' if feasible_shots else 'ZERO_FEASIBLE_COUNT',
        'penalized_expected_gap': float(vector @ penalized/shots-optimum),
        'optimal_state_probability': float(vector[hit].sum()/shots),
        'best_sample_gap': float(values[(vector>0)&FEASIBLE].min()-optimum) if feasible_shots else None,
        'unique_states': int((vector>0).sum())}


def sample_sdk(simulator, shots, seed):
    counts = {str(k): int(v) for k, v in simulator.sample(shots=shots, rng_seed=seed).items()}
    if sum(counts.values()) != shots:
        raise ValueError('SDK_SHOTS_CONSERVATION')
    return counts


def optimize(instance, spec, seed, cpu_limit=100):
    _, values, _, optimum, _ = energies(instance)
    span = max(float(np.ptp(values[FEASIBLE])), 1e-12)
    start = time.process_time(); calls = 0
    def evaluate(theta):
        nonlocal calls
        if calls >= 100 or time.process_time()-start >= cpu_limit:
            raise RuntimeError('OPTIMIZER_BUDGET_EXHAUSTED')
        check_memory(2048)
        calls += 1
        _, probabilities = sdk_probabilities(qaoa_qcis(instance, spec, theta))
        if probabilities[~FEASIBLE].sum() > 1e-9:
            raise ValueError('XY_CARDINALITY_CONSERVATION')
        return float((probabilities @ values-optimum)/span)
    initial = np.random.default_rng(seed).uniform(-np.pi, np.pi, size=2*spec['depth'])
    result = minimize(evaluate, initial, method='Nelder-Mead', options={'maxfev': 100, 'xatol': 1e-5, 'fatol': 1e-7})
    return result.x.tolist(), {'analytic_evolutions': calls, 'termination': str(result.message),
        'optimizer_success': bool(result.success), 'normalized_training_gap': float(result.fun),
        'cpu_seconds': time.process_time()-start, 'max_evaluations': 100}


def check_memory(limit):
    if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024 > limit:
        raise RuntimeError('PEAK_RSS_BUDGET_EXHAUSTED')


def phase_budget(out, seconds, additional=None):
    """Exclusive local preparation/inference ledger from its group's reserved remainder."""
    directory = Path(out).with_suffix(Path(out).suffix+'.budget')
    directory.mkdir(parents=True, exist_ok=False)
    cap = dict(additional or {}, cpu_seconds=seconds)
    budget = Budget(directory, cap, {'phase': cap})
    budget.admit('phase', 'phase', dict(cap, cpu_seconds=seconds-1))
    return budget


def freeze(campaign_path, out):
    campaign_path, out = Path(campaign_path), Path(out)
    campaign = json.loads(campaign_path.read_text())
    if campaign['hardware_submission_stage'] != 'PREPRODUCTION' or campaign['incremental_spend_max'] != 0:
        raise ValueError('CAMPAIGN_SCOPE')
    data_path = ROOT/'03_data/ready/d02/execution/holding_daily.parquet'
    protocol_path = ROOT/'03_data/ready/d02/execution_protocol.json'
    assets = json.loads(protocol_path.read_text())['assetOrder']
    bases = base_instances(pd.read_parquet(data_path), assets)
    instances = [dict(base, instance_id=f"{base['base_id']}_L{lam:g}", **{'lambda': lam})
                 for base in bases for lam in [.1, 1., 10.]]
    e05_jobs = [{'job_id': f"e05_{i['instance_id']}_p{p}_s{s}", 'experiment': 'E05',
                 'instance_id': i['instance_id'], 'depth': p, 'seed': s, 'cpu_seconds_cap': 180, 'memory_mib_cap': 1024,
                 'quantum_shots_cap': sum(SHOTS), 'classical_draws_cap': sum(SHOTS)}
                for i in instances for p in [1, 2, 3] for s in SEEDS]
    e06_jobs = [{'job_id': f"e06_dev_{c['candidate_id']}_s{s}", 'experiment': 'E06', 'role': 'development',
                 'instance_id': 'B01_L1', 'candidate_id': c['candidate_id'], 'seed': s,
                 'cpu_seconds_cap': 240, 'memory_mib_cap': 1024, 'quantum_shots_cap': 4096}
                for c in candidate_catalog() for s in DEV_SEEDS]
    e06_jobs += [{'job_id': f'e06_eval_B{b:02d}_{strategy}_r{rank}_s{s}', 'experiment': 'E06',
                  'role': 'evaluation', 'instance_id': f'B{b:02d}_L1', 'strategy': strategy,
                  'selection_rank': rank, 'seed': s, 'cpu_seconds_cap': 240, 'memory_mib_cap': 1024, 'quantum_shots_cap': 4096}
                 for b in range(2, 10) for strategy in ['fixed', 'resource', 'noise'] for rank in range(5) for s in SEEDS]
    source_files = [Path(__file__), Path(__file__).with_name('bridge.py'), Path(__file__).with_name('research_common.py'),
                    ROOT/'05_code/simulation/local_noise.py']
    manifest = {'schema_version': 'qf.e05-e06-freeze.v1', 'created_at': utc(),
        'campaign_id': campaign['campaign_id'], 'campaign_sha256': digest(campaign_path),
        'source_commit': campaign['source_commit'],
        'workspace_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'data_epoch': 'd02-fixed-contract-five-session-e05-e06-20260908',
        'task_id': 'D02_CORE6_FIXED_CONTRACT_FIVE_SESSION_PORTFOLIO_MECHANISM',
        'parents': [{'path': str(p.relative_to(ROOT)), 'sha256': digest(p)} for p in [data_path, protocol_path]],
        'implementation': [{'path': str(p.relative_to(ROOT)), 'sha256': digest(p)} for p in source_files],
        'packages': {p: importlib.metadata.version(p) for p in ['cqlib', 'numpy', 'scipy', 'scikit-learn']},
        'bit_order': {'logical_assets': assets, 'measured': list(range(6)), 'bitstrings': 'q5...q0'},
        'instances': instances, 'candidate_catalog': candidate_catalog(), 'noise_profiles': noise_catalog(),
        'jobs': e05_jobs+e06_jobs,
        'budgets': {'E05': {'unit_count': 405, 'unit_cpu_reserved': 72900, 'aggregate_cpu_reserved': 13500,
                           'total_cpu_cap': 86400, 'final_shots_reserved': 2177280, 'classical_draws_reserved': 2177280},
                    'E06': {'unit_count': 690, 'unit_cpu_reserved': 165600, 'aggregate_cpu_reserved': 7200,
                           'total_cpu_cap': 172800, 'development_shots': 368640, 'evaluation_shots': 2457600,
                           'total_shots_reserved': 2826240}},
        'primary': {'E05': {'depth': 3, 'shots': 4096, 'seed_aggregation': 'arithmetic mean within each of 27 fixed objectives',
                            'endpoint': 'conditional_feasible_expected_objective_gap_difference',
                            'resampling': 'paired objective-instance bootstrap; base-date cluster sensitivity'},
                    'E06': {'endpoint': 'penalized_noisy_expected_objective_gap_difference',
                            'penalty': '1.01*(sum abs Qii + 2 sum i<j abs Qij)+1e-12',
                            'aggregation': 'mean over frozen 5 candidates, 5 seeds and 4 noise profiles within each of 8 bases',
                            'resampling': 'paired bootstrap of 8 base problems',
                            'selection': 'fixed qas00..04; resource lowest (two-qubit gates,depth,id); noise lowest development mean normalized penalized gap'}},
        'execution_mode': 'LOCAL_CQLIB_STATEVECTOR_PLUS_EXPLICIT_DENSITY_CHANNELS',
        'scientific_status': 'DESIGN_READY', 'hardware_submission': 'UNAVAILABLE'}
    write_json(out, manifest)
    return {'freeze_path': str(out), 'sha256': digest(out), 'jobs': len(manifest['jobs'])}


def load_freeze(path):
    path = Path(path); doc = json.loads(path.read_text())
    if doc['schema_version'] != 'qf.e05-e06-freeze.v1':
        raise ValueError('FREEZE_SCHEMA')
    for item in doc['implementation']:
        if digest(ROOT/item['path']) != item['sha256']:
            raise ValueError('IMPLEMENTATION_SHA_CHANGED')
    for package, version in doc['packages'].items():
        if importlib.metadata.version(package) != version:
            raise ValueError('PACKAGE_VERSION_CHANGED:'+package)
    return doc


def job_for(doc, job_id):
    matches = [j for j in doc['jobs'] if j['job_id'] == job_id]
    if len(matches) != 1:
        raise ValueError('FROZEN_JOB_ID')
    job = matches[0]
    instance = next(i for i in doc['instances'] if i['instance_id'] == job['instance_id'])
    return job, instance


def run_job(freeze_path, runs, job_id, selection_path=None):
    doc = load_freeze(freeze_path); job, instance = job_for(doc, job_id)
    out = Path(runs)/job_id
    memory_cap = job.get('memory_mib_cap', 1024)
    check_memory(memory_cap)
    # The existence of a ledger makes an interrupted unit inspect-only; a new freeze can allocate a replacement.
    cap = job['cpu_seconds_cap']; is_e05 = job['experiment'] == 'E05'
    caps = {'cpu_seconds': cap, 'analytic_evolutions': 101, 'density_evolutions': 0 if is_e05 else 3,
            'shots': job['quantum_shots_cap'], 'classical_draws': job.get('classical_draws_cap', 0)}
    reservations = {'optimization': {'cpu_seconds': 110 if is_e05 else 100, 'analytic_evolutions': 100},
                    'measurement': {'cpu_seconds': 60 if is_e05 else 130, 'analytic_evolutions': 1,
                                    'density_evolutions': caps['density_evolutions'], 'shots': caps['shots'],
                                    'classical_draws': caps['classical_draws']}}
    selection_sha = None
    if is_e05:
        spec = dict(candidate_catalog()[0], depth=job['depth'], parameters=2*job['depth'])
    else:
        candidate_id = job.get('candidate_id')
        if job['role'] == 'evaluation':
            if selection_path is None:
                raise ValueError('SELECTION_FREEZE_REQUIRED')
            selection = json.loads(Path(selection_path).read_text())
            if selection['freeze_sha256'] != digest(freeze_path):
                raise ValueError('SELECTION_PARENT_SHA')
            candidate_id = selection['selected'][job['strategy']][job['selection_rank']]
            selection_sha = digest(selection_path)
        spec = next(c for c in doc['candidate_catalog'] if c['candidate_id'] == candidate_id)
    out.mkdir(parents=True, exist_ok=False)
    budget = Budget(out, caps, reservations)
    header = {'job': job, 'freeze_sha256': digest(freeze_path), 'selection_sha256': selection_sha,
              'campaign_id': doc['campaign_id'], 'run_id': doc['campaign_id']+'/'+job_id,
              'experiment_id': job['experiment'], 'workspace_commit': doc['workspace_commit'],
              'data_epoch': doc['data_epoch'], 'task_id': doc['task_id'],
              'source_commit': doc['source_commit'], 'instance_sha256': identity(instance), 'candidate': spec,
              'seed': job['seed'], 'created_at': utc(), 'execution_mode': doc['execution_mode']}
    write_json(out/'intent.json', header)
    try:
        worst = reservations['optimization']
        budget.admit('optimization', 'optimization', worst)
        parameters, optimization = optimize(instance, spec, job['seed'], worst['cpu_seconds']-1)
        check_memory(memory_cap)
        budget.finish('optimization', {'analytic_evolutions': optimization['analytic_evolutions']})
        qcis = qaoa_qcis(instance, spec, parameters)
        write_json(out/'optimized_circuit.json', {'qcis': qcis, 'qcis_sha256': text_sha256(qcis), 'parameters': parameters,
            'logical_measurement_order': list(range(6)), 'bitstrings': 'q5...q0', 'instance_id': instance['instance_id']})
        budget.admit('measurement', 'measurement', reservations['measurement'])
        simulator, ideal = sdk_probabilities(qcis)
        _, values, penalized, optimum, penalty = energies(instance)
        records = []
        if is_e05:
            for shots in SHOTS:
                counts = sample_sdk(simulator, shots, job['seed']*10000+shots)
                uniform = np.random.default_rng(job['seed']*10000+shots+1).multinomial(shots, np.ones(20)/20)
                classical_counts = {f'{i:06b}': int(v) for i, v in zip(np.flatnonzero(FEASIBLE), uniform) if v}
                records.append({'shots': shots, 'quantum_counts': counts, 'uniform_counts': classical_counts,
                                'quantum': count_metrics(counts, values, penalized, optimum),
                                'uniform': count_metrics(classical_counts, values, penalized, optimum)})
            x, local_info = solve(np.asarray(instance['mu']), np.asarray(instance['sigma']), 3, instance['lambda'],
                                  .001, np.asarray(instance['previous']), cpu_cap=2)
            local_gap = objective(x, np.asarray(instance['mu']), np.asarray(instance['sigma']), 3, instance['lambda'],
                                  .001, np.asarray(instance['previous']))-optimum
            comparisons = {'exact_optimum': optimum, 'uniform_exact_gap': float(values[FEASIBLE].mean()-optimum),
                           'local_search_gap': float(local_gap), 'local_search': local_info}
        else:
            for profile_number, profile in enumerate(doc['noise_profiles']):
                check_memory(memory_cap)
                probabilities = ideal if profile_number == 0 else noisy_probabilities(qcis, profile)
                seed = job['seed']*10000+profile_number
                # Sampling uses independent records for each strategy/candidate slot; identical
                # input probabilities receive common random numbers under the paired seed.
                if profile_number == 0:
                    counts = sample_sdk(simulator, 1024, seed)
                else:
                    sampled = np.random.default_rng(seed).multinomial(1024, probabilities)
                    counts = {f'{i:06b}': int(v) for i, v in enumerate(sampled) if v}
                records.append({'profile_id': profile['id'], 'counts': counts,
                    'sampling_backend': 'CQLIB_STATEVECTOR_SAMPLE' if profile_number == 0 else 'NUMPY_MULTINOMIAL_EXPLICIT_DENSITY',
                    'probabilities': probabilities.tolist(), 'metrics': count_metrics(counts, values, penalized, optimum),
                    'analytic_penalized_gap': float(probabilities @ penalized-optimum),
                    'tv_from_ideal': float(np.abs(probabilities-ideal).sum()/2)})
            comparisons = {'exact_optimum': optimum, 'penalty_A': penalty,
                           'normalization_span': max(float(np.ptp(values[FEASIBLE])), 1e-12)}
        check_memory(memory_cap)
        budget.finish('measurement', {'analytic_evolutions': 1, 'density_evolutions': caps['density_evolutions'],
                                      'shots': caps['shots'], 'classical_draws': caps['classical_draws']})
        result = dict(header, status='COMPLETE', scientific_status='LOCAL_VERIFIED',
                      optimization=optimization, parameters=parameters, circuit_sha256=text_sha256(qcis),
                      circuit_resources=circuit_resources(qcis), comparisons=comparisons,
                      ideal_probabilities=ideal.tolist(), records=records,
                      peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
                      resource_ledger_sha256=digest(budget.path))
        write_json(out/'result.json', result)
        return {'job_id': job_id, 'status': 'COMPLETE', 'result_sha256': digest(out/'result.json')}
    except Exception as error:
        write_json(out/'failure.json', dict(header, status='STOPPED', exception_type=type(error).__name__,
                                           reason=str(error)[:300]))
        raise


def results_for(doc, freeze_path, runs, predicate):
    results = []
    for job in [j for j in doc['jobs'] if predicate(j)]:
        path = Path(runs)/job['job_id']/'result.json'
        if not path.exists():
            raise ValueError('INCOMPLETE_FROZEN_UNIT:'+job['job_id'])
        result = json.loads(path.read_text())
        if result['job'] != job or result['freeze_sha256'] != digest(freeze_path) or result['status'] != 'COMPLETE':
            raise ValueError('RESULT_IDENTITY')
        ledger_path = path.with_name('resource_ledger.json')
        if digest(ledger_path) != result['resource_ledger_sha256']:
            raise ValueError('RESOURCE_LEDGER_SHA')
        ledger = json.loads(ledger_path.read_text())
        if ledger['active'] or ledger['used']['shots'] != job['quantum_shots_cap']:
            raise ValueError('RESULT_BUDGET_CONSERVATION')
        results.append((path, result))
    return results


def select_e06(freeze_path, runs, out):
    budget = phase_budget(out, 3000)
    doc = load_freeze(freeze_path)
    items = results_for(doc, freeze_path, runs, lambda j: j['experiment']=='E06' and j['role']=='development')
    candidates = []
    for spec in doc['candidate_catalog']:
        grouped = [r for _, r in items if r['candidate']['candidate_id']==spec['candidate_id']]
        values = [v['metrics']['penalized_expected_gap']/r['comparisons']['normalization_span']
                  for r in grouped for v in r['records']]
        if len(grouped)!=3 or len(values)!=12:
            raise ValueError('DEVELOPMENT_SELECTION_COVERAGE')
        resource_key = grouped[0]['circuit_resources']
        candidates.append({'candidate_id': spec['candidate_id'], 'development_score': float(np.mean(values)),
                           'two_qubit_gates': resource_key['two_qubit_gates'], 'depth': resource_key['logical_depth']})
    selected = {'fixed': [f'qas_{i:02d}' for i in range(5)],
                'resource': [r['candidate_id'] for r in sorted(candidates, key=lambda r:(r['two_qubit_gates'],r['depth'],r['candidate_id']))[:5]],
                'noise': [r['candidate_id'] for r in sorted(candidates, key=lambda r:(r['development_score'],r['candidate_id']))[:5]]}
    output = {'schema_version': 'qf.e06-selection.v1', 'freeze_sha256': digest(freeze_path),
              'selected': selected, 'development_scores': candidates,
              'development_result_shas': {p.parent.name: digest(p) for p, _ in items},
              'evaluation_base_ids': [f'B{i:02d}' for i in range(2, 10)], 'created_at': utc()}
    budget.finish('phase', {})
    output['resource_ledger_sha256'] = digest(budget.path)
    write_json(out, output)
    return {'path': str(out), 'sha256': digest(out), 'selected': selected}


def paired_problem_bootstrap(a, b, seed=2026090805):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if a.shape != b.shape or a.ndim != 1 or len(a)<2 or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError('PAIRED_PROBLEM_ENDPOINT')
    difference = a-b; effect = float(difference.mean())
    rng = np.random.default_rng(seed)
    estimates = np.array([difference[rng.integers(0,len(a),len(a))].mean() for _ in range(2000)])
    return {'effect': effect, 'ci95': np.quantile(estimates,[.025,.975]).tolist(),
            'p_raw': float((1+np.sum(np.abs(estimates-effect)>=abs(effect)))/2001),
            'bootstrap_replicates': 2000, 'n_problem_units': len(a), 'paired_effects': difference.tolist()}


def summarize(freeze_path, runs, out, experiment, selection_path=None):
    budget = phase_budget(out, 13500 if experiment=='E05' else 4200)
    doc = load_freeze(freeze_path)
    items = results_for(doc, freeze_path, runs, lambda j: j['experiment']==experiment)
    families, units = [], []
    if experiment == 'E05':
        for instance in doc['instances']:
            records = [r for _,r in items if r['job']['instance_id']==instance['instance_id'] and r['job']['depth']==3]
            measurements = [next(x for x in r['records'] if x['shots']==4096) for r in records]
            if len(records)!=5 or any(x['quantum']['conditional_feasible_gap'] is None for x in measurements):
                raise ValueError('PRIMARY_ENDPOINT_COVERAGE')
            units.append({'instance_id':instance['instance_id'], 'base_id':instance['base_id'],
                          'quantum':float(np.mean([x['quantum']['conditional_feasible_gap'] for x in measurements])),
                          'uniform':float(np.mean([x['uniform']['conditional_feasible_gap'] for x in measurements])),
                          'local':float(np.mean([r['comparisons']['local_search_gap'] for r in records]))})
        for comparator in ['uniform','local']:
            record = dict(comparison='qaoa_vs_'+comparator, **paired_problem_bootstrap(
                [r['quantum'] for r in units],[r[comparator] for r in units]))
            # Risk aversions share a historical base. This cluster sensitivity preserves all three lambdas.
            clustered = [[r for r in units if r['base_id']==f'B{i:02d}'] for i in range(1,10)]
            record['base_date_cluster_sensitivity'] = paired_problem_bootstrap(
                [np.mean([r['quantum'] for r in block]) for block in clustered],
                [np.mean([r[comparator] for r in block]) for block in clustered])
            families.append(record)
    else:
        if selection_path is None:
            raise ValueError('SELECTION_FREEZE_REQUIRED')
        selection_sha = digest(selection_path)
        for base in range(2,10):
            unit = {'base_id': f'B{base:02d}'}
            for strategy in ['fixed','resource','noise']:
                grouped = [r for _,r in items if r['job']['role']=='evaluation'
                           and r['job']['instance_id']==f'B{base:02d}_L1' and r['job']['strategy']==strategy]
                if len(grouped)!=25 or any(r['selection_sha256']!=selection_sha for r in grouped):
                    raise ValueError('PAIRED_STRATEGY_SELECTION_COVERAGE')
                unit[strategy] = float(np.mean([x['metrics']['penalized_expected_gap'] for r in grouped for x in r['records']]))
            units.append(unit)
        families = [dict(comparison='noise_qas_vs_'+comparator,
                          **paired_problem_bootstrap([r['noise'] for r in units],[r[comparator] for r in units]))
                    for comparator in ['fixed','resource']]
    holm(families)
    total_shots = sum(r['job']['quantum_shots_cap'] for _,r in items)
    total_cpu = sum(json.loads(p.with_name('resource_ledger.json').read_text())['used']['cpu_seconds'] for p,_ in items)
    expected_shots = 2177280 if experiment=='E05' else 2826240
    if total_shots != expected_shots or total_cpu > doc['budgets'][experiment]['unit_cpu_reserved']:
        raise ValueError('GLOBAL_BUDGET_CONSERVATION')
    output = {'schema_version':'qf.quantum-mechanism-summary.v1', 'experiment':experiment,
              'freeze_sha256':digest(freeze_path), 'task_id':doc['task_id'], 'data_epoch':doc['data_epoch'],
              'status':'COMPLETE', 'scientific_status':'LOCAL_VERIFIED', 'primary_definition':doc['primary'][experiment],
              'paired_units':units, 'comparisons':families, 'resources':{'shots':total_shots, 'unit_cpu_seconds':total_cpu,
              'classical_draws':2177280 if experiment=='E05' else 0, 'result_count':len(items)},
              'result_shas':{p.parent.name:digest(p) for p,_ in items},
              'inference_scope':'fixed historical six-asset objective mechanisms; population transfer depends on base sampling'}
    budget.finish('phase', {})
    output['resource_ledger_sha256'] = digest(budget.path)
    output['resources']['summary_cpu_seconds'] = json.loads(budget.path.read_text())['used']['cpu_seconds']
    if experiment=='E06':
        selection = json.loads(Path(selection_path).read_text())
        selection_ledger = Path(selection_path).with_suffix(Path(selection_path).suffix+'.budget')/'resource_ledger.json'
        if digest(selection_ledger) != selection['resource_ledger_sha256']:
            raise ValueError('SELECTION_RESOURCE_LEDGER_SHA')
        output['resources']['selection_cpu_seconds'] = json.loads(selection_ledger.read_text())['used']['cpu_seconds']
    write_json(out, output)
    return {'path':str(out), 'sha256':digest(out), 'results':len(items)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    p = sub.add_parser('freeze'); p.add_argument('--campaign',required=True); p.add_argument('--out',required=True)
    p = sub.add_parser('run-unit'); p.add_argument('--freeze',required=True); p.add_argument('--runs',required=True)
    p.add_argument('--job-id',required=True); p.add_argument('--selection')
    p = sub.add_parser('select-e06'); p.add_argument('--freeze',required=True); p.add_argument('--runs',required=True); p.add_argument('--out',required=True)
    p = sub.add_parser('summarize'); p.add_argument('--freeze',required=True); p.add_argument('--runs',required=True)
    p.add_argument('--out',required=True); p.add_argument('--experiment',choices=['E05','E06'],required=True); p.add_argument('--selection')
    args = parser.parse_args()
    if args.command=='freeze': result=freeze(args.campaign,args.out)
    elif args.command=='run-unit': result=run_job(args.freeze,args.runs,args.job_id,args.selection)
    elif args.command=='select-e06': result=select_e06(args.freeze,args.runs,args.out)
    else: result=summarize(args.freeze,args.runs,args.out,args.experiment,args.selection)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
