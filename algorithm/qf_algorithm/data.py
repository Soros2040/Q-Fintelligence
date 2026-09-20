"""Validated chronological panel contract for new and archived inputs."""
from __future__ import annotations
import datetime as dt
from pathlib import Path
import numpy as np
import pandas as pd
from .api import clean, digest, identity, load_json
from .paths import DataLayout

SCHEMA = 'qf.normalized-panel.v2'
FEATURES = ['return_1', 'mean_return_5', 'mean_return_10', 'mean_return_20', 'mean_return_60', 'volatility_20']


def validate_panel(document):
    """Fail closed on axis, causality, non-finite, and return-unit violations."""
    if document.get('schemaVersion') != SCHEMA or document.get('return_unit') != 'SIMPLE_FRACTION':
        raise ValueError('NORMALIZED_PANEL_SCHEMA_OR_RETURN_UNIT')
    if not isinstance(document.get('data_epoch'), str) or not document['data_epoch']:
        raise ValueError('DATA_EPOCH_REQUIRED')
    assets = document.get('asset_order', [])
    if len(assets) != 6 or len(set(assets)) != 6 or any(not isinstance(x, str) or not x for x in assets):
        raise ValueError('SIX_UNIQUE_ASSET_AXIS_REQUIRED')
    dates = document.get('dates', [])
    try:
        parsed = [dt.date.fromisoformat(d) for d in dates]
    except (TypeError, ValueError):
        raise ValueError('ISO_DATE_REQUIRED') from None
    if len(dates) < 146 or parsed != sorted(set(parsed)):
        raise ValueError('STRICT_CHRONOLOGICAL_DATES_AND_146_OBSERVATIONS_REQUIRED')
    t = len(dates)
    arrays = {}
    for name in ['close_returns', 'open_returns', 'labels']:
        value = np.asarray(document.get(name), dtype=float)
        if value.shape != (t, 6) or not np.isfinite(value).all():
            raise ValueError('FINITE_PANEL_AXIS:' + name)
        if name.endswith('returns') and np.any(value <= -1):
            raise ValueError('SIMPLE_RETURN_MUST_EXCEED_MINUS_ONE')
        arrays[name] = value
    arrays['label_valid'] = np.asarray(document.get('label_valid'))
    if arrays['label_valid'].shape != (t, 6) or arrays['label_valid'].dtype != bool:
        raise ValueError('BOOLEAN_LABEL_MASK_REQUIRED')
    available = document.get('label_available_dates', [])
    if len(available) != t:
        raise ValueError('LABEL_AVAILABLE_DATE_AXIS')
    for i, day in enumerate(available):
        if day is None and not arrays['label_valid'][i].any():
            continue
        if not isinstance(day, str) or dt.date.fromisoformat(day) <= parsed[i]:
            raise ValueError('LABEL_CLOCK_MUST_FOLLOW_SIGNAL')
        if i < t-2 and arrays['label_valid'][i].any() and dt.date.fromisoformat(day) < parsed[i+2]:
            raise ValueError('LABEL_CLOCK_PRECEDES_REALIZED_EXIT_OPEN')
    if document.get('target') != 'NEXT_OPEN_SIMPLE_RETURN':
        raise ValueError('TARGET_REQUIRES_NEXT_OPEN_SIMPLE_RETURN')
    # The supplied labels must match the observable opening-return array.
    mask = arrays['label_valid'][:-2]
    if not np.allclose(arrays['labels'][:-2][mask], arrays['open_returns'][2:][mask], atol=1e-12, rtol=1e-10):
        raise ValueError('LABEL_RETURN_CLOCK_MISMATCH')
    if arrays['label_valid'][-2:].any():
        raise ValueError('LAST_UNOBSERVED_LABEL_MUST_BE_MASKED')
    if 'features' in document:
        arrays['features'] = np.asarray(document['features'], dtype=float)
        if arrays['features'].shape != (t, 6, 6) or not np.isfinite(arrays['features']).all():
            raise ValueError('FINITE_SIX_FEATURE_AXIS_REQUIRED')
    else:
        r = arrays['close_returns']
        arrays['features'] = np.stack([r, *[np.array([r[max(0, i-n+1):i+1].mean(0) for i in range(t)]) for n in [5, 10, 20, 60]],
                                     np.array([r[max(0, i-19):i+1].std(0) for i in range(t)])], axis=-1)
    return arrays


def from_returns(dates, assets, close_returns, open_returns, *, data_epoch, provenance):
    close_returns, opening = map(np.asarray, [close_returns, open_returns])
    y = np.vstack([opening[2:], np.zeros((2, 6))])
    mask = np.ones_like(y, dtype=bool); mask[-2:] = False
    document = {'schemaVersion': SCHEMA, 'data_epoch': data_epoch, 'asset_order': list(assets), 'dates': list(dates),
                'return_unit': 'SIMPLE_FRACTION', 'target': 'NEXT_OPEN_SIMPLE_RETURN',
                'information_rule': 'CLOSE_GRAPH_THROUGH_SIGNAL_CLOSE_OPEN_RISK_THROUGH_SIGNAL_OPEN',
                'close_returns': close_returns.tolist(), 'open_returns': opening.tolist(), 'labels': y.tolist(),
                'label_valid': mask.tolist(), 'label_available_dates': list(dates[2:]) + [None, None], 'parents': provenance}
    validate_panel(document)
    return document


def example_panel(seed=2026090902):
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0003, .006, (164, 1)) * np.linspace(.6, 1.2, 6) + rng.normal(0, .008, (164, 6))
    return from_returns(pd.bdate_range('2024-01-02', periods=len(r)).strftime('%Y-%m-%d').tolist(),
                        ['ASSET_' + x for x in 'ABCDEF'], r, r * .99,
                        data_epoch=f'software-synthetic-{seed}', provenance={'generator': 'TEAM_SYNTHETIC', 'seed': seed})


def frozen_panel(data_root=None):
    """Derive a bounded six-asset development window from the frozen actual panel.

    The six-asset window supports software integration and stage-by-stage reproduction.
    Selection uses the earliest window with 164 complete raw return observations.
    """
    layout = DataLayout.resolve(data_root)
    root = layout.campaign / 'derived/stock_v1'
    manifest_path = layout.require(root / 'manifest.json')
    manifest = load_json(manifest_path)
    parts, parents = [], {str(manifest_path): digest(manifest_path)}
    for shard in manifest['shards']:
        path = layout.require(root / shard['path'])
        if digest(path) != shard['sha256']:
            raise ValueError('FROZEN_PANEL_SHA')
        parents[str(path)] = digest(path)
        with np.load(path, allow_pickle=False) as a:
            parts.append({key: a[key].copy() for key in ['dates', 'returns', 'open', 'adj_factor']})
    data = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    price = data['open'] * data['adj_factor']
    opening = price[1:] / price[:-1] - 1
    close = data['returns'][1:]
    for start in range(len(close)-163):
        span = slice(start, start+164)
        good = np.flatnonzero(np.isfinite(opening[span]).all(0) & np.isfinite(close[span]).all(0)
                             & (opening[span] > -1).all(0) & (close[span] > -1).all(0))
        if len(good) >= 6:
            chosen = good[:6]
            days = [str(d) for d in data['dates'][1:][span]]
            days = [d if '-' in d else f'{d[:4]}-{d[4:6]}-{d[6:]}' for d in days]
            return from_returns(days, [manifest['assets'][i] for i in chosen], close[span][:, chosen], opening[span][:, chosen],
                                data_epoch=manifest['data_epoch'] + '-software-six-asset-integration', provenance=parents)
    raise ValueError('NO_COMPLETE_SIX_ASSET_FROZEN_WINDOW')


def load_panel(value=None, *, frozen=False, data_root=None, seed=2026090902):
    document = frozen_panel(data_root) if frozen else (example_panel(seed) if value is None else load_json(value) if isinstance(value, (str, Path)) else value)
    validate_panel(document)
    return clean(document)
