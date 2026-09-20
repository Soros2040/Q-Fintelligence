"""Shared artifact, accounting and paired-inference contracts for the remote campaign."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time
import numpy as np


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()


def packed(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def identity(value):
    return hashlib.sha256(packed(value)).hexdigest()


def write_json(path, value, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = packed(value)
    if len(raw) > 8388608:
        raise ValueError('ARTIFACT_SHARD_LIMIT')
    if path.exists() and not replace:
        if path.read_bytes() == raw:
            return digest(path)
        raise FileExistsError(str(path))
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with temporary.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return digest(path)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Budget:
    """One process per ledger; workers receive separate frozen disjoint allocations."""
    def __init__(self, root, caps, reservation):
        self.path = Path(root) / 'resource_ledger.json'
        self.caps = caps
        self.used = {key: 0 for key in caps}
        self.reservation = reservation
        self.cpu_start = time.process_time()
        self.wall_start = time.monotonic()
        self.active = {}
        if self.path.exists():
            raise FileExistsError('NEW_RUN_ID_REQUIRED')
        for phase, values in reservation.items():
            for key, value in values.items():
                if value < 0 or key not in caps:
                    raise ValueError('RESERVATION_SCHEMA')
        if any(sum(p.get(k, 0) for p in reservation.values()) > v for k, v in caps.items()):
            raise ValueError('RESERVATION_EXCEEDS_GROUP_CAP')
        self.phase_used = {p: {key: 0 for key in caps} for p in reservation}
        self.save()

    def admit(self, unit, phase, worst):
        self.used['cpu_seconds'] = time.process_time() - self.cpu_start
        if unit in self.active or phase not in self.reservation:
            raise ValueError('ATOMIC_IDENTITY')
        for key, amount in worst.items():
            active = sum(v['worst'].get(key, 0) for v in self.active.values())
            phase_active = sum(v['worst'].get(key, 0) for v in self.active.values() if v['phase'] == phase)
            if self.used.get(key, 0) + active + amount > self.caps[key]:
                raise ValueError('GROUP_BUDGET_EXHAUSTED:' + key)
            if self.phase_used[phase][key] + phase_active + amount > self.reservation[phase].get(key, 0):
                raise ValueError('PHASE_BUDGET_EXHAUSTED:' + key)
        self.active[unit] = {'phase': phase, 'worst': worst, 'cpu_start': time.process_time()}
        self.save()

    def finish(self, unit, actual):
        record = self.active.pop(unit)
        actual = dict(actual, cpu_seconds=time.process_time() - record['cpu_start'])
        for key, amount in actual.items():
            if amount > record['worst'].get(key, 0) + 1e-8:
                raise ValueError('ATOMIC_RESOURCE_OVERRUN:' + key)
            if key != 'cpu_seconds':
                self.used[key] += amount
            self.phase_used[record['phase']][key] += amount
        self.save()

    def save(self):
        self.used['cpu_seconds'] = time.process_time() - self.cpu_start
        write_json(self.path, {'caps': self.caps, 'used': self.used, 'phase_reservations': self.reservation,
            'phase_used': self.phase_used, 'active': self.active, 'wall_seconds': time.monotonic() - self.wall_start,
            'updated_at': utc(), 'cpu_accounting': 'PROCESS_SELF_INCLUDING_THREADS_SINGLE_PROCESS_LEDGER'}, replace=True)


def cvar95(loss, axis=0):
    """Empirical CVaR with fractional boundary mass (Rockafellar-Uryasev)."""
    values = np.sort(np.asarray(loss), axis=axis)
    values = np.moveaxis(values, axis, 0)
    n = len(values)
    mass = n * .05
    whole = int(np.floor(mass))
    fraction = mass - whole
    total = values[-whole:].sum(axis=0) if whole else np.zeros(values.shape[1:])
    if fraction > 1e-12:
        total = total + fraction * values[-whole - 1]
    return total / mass


def select_block_length(development, holding_sessions=1):
    values = np.asarray(development, dtype=float)
    values = values - values.mean()
    denominator = float(values @ values)
    threshold = 1.96 / np.sqrt(len(values))
    correlations = [float(values[:-lag] @ values[lag:] / denominator) if denominator else 0.0
                    for lag in range(1, min(81, max(2, len(values)//4)))]
    dependence = max([lag for lag, r in enumerate(correlations, 1) if abs(r) > threshold], default=1)
    needed = max(holding_sessions, dependence)
    length = next(x for x in [1, 5, 10, 20, 40, 80] if x >= needed)
    return {'length': length, 'rule': 'FIRST_GRID_LENGTH_COVERING_SIGNIFICANT_DEVELOPMENT_ACF_AND_HOLDING',
            'acf': correlations, 'threshold': threshold, 'development_n': len(values)}


def paired_inference(a, b, block_length, endpoint='mean', replicates=2000, seed=2026090701):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if a.shape != b.shape or a.ndim not in [1, 2] or len(a) < 2 or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError('PAIRED_PATH_CONTRACT')
    score = (lambda x: float(cvar95(x, axis=0).mean())) if endpoint == 'cvar95' else (lambda x: float(x.mean()))
    observed = score(a) - score(b)
    rng = np.random.default_rng(seed)
    n = len(a)
    estimates = np.empty(replicates)
    for i in range(replicates):
        # Geometric block lengths define the stationary bootstrap. Blocks wrap at n.
        indices = np.empty(n, dtype=int)
        position = 0
        while position < n:
            start = int(rng.integers(0, n))
            length = min(int(rng.geometric(1.0/block_length)), n-position)
            indices[position:position+length] = (start + np.arange(length)) % n
            position += length
        estimates[i] = score(a[indices]) - score(b[indices])
    centered = estimates - observed
    p = (1 + int(np.sum(np.abs(centered) >= abs(observed)))) / (replicates + 1)
    return {'effect': observed, 'ci95': np.quantile(estimates, [.025, .975]).tolist(), 'p_raw': p,
            'n_dates': n, 'n_seeds': a.shape[1] if a.ndim == 2 else 1, 'block_length': block_length,
            'bootstrap_replicates': replicates, 'endpoint': endpoint,
            'tail_mass_per_seed': n * .05 if endpoint == 'cvar95' else None,
            'seed_aggregation': 'ENDPOINT_PER_SEED_THEN_MEAN', 'bootstrap': 'PAIRED_STATIONARY_TIME_BLOCK'}


def holm(records):
    ordered = sorted(records, key=lambda r: r['p_raw'])
    previous = 0.0
    for index, record in enumerate(ordered):
        previous = max(previous, min(1.0, (len(ordered) - index) * record['p_raw']))
        record['p_holm'] = previous
    return records
