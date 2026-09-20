"""Recalculate the fifteen registered primary comparisons from frozen endpoints."""
from pathlib import Path
import numpy as np
import pandas as pd
from .api import Context, _reports, digest, load_json, write_json
from .paths import DataLayout
from .legacy.research_common import paired_inference, holm
from .legacy.quantum_search import paired_problem_bootstrap
from .legacy.stock_report import matrix_for, financial_contrasts


def recompute_primary_statistics(data_root=None, *, output_dir):
    layout = DataLayout.resolve(data_root)
    out = Path(output_dir).resolve()
    if layout.protected(out):
        raise ValueError('OUTPUT_OVERLAPS_ARCHIVE')
    out.mkdir(parents=True, exist_ok=False)
    ctx = Context(layout.root, out, 2026090701, 1024)
    reports = {g['experiment_id']: (report, path) for g, report, path in _reports(ctx)}
    rows = []

    def verify_file(path, expected):
        ctx.source(path)
        if digest(path) != expected:
            raise ValueError('FROZEN_STATISTICAL_INPUT_SHA:' + str(path))

    def accept(group, computed, expected):
        holm(computed)
        for record, archived in zip(computed, expected, strict=True):
            if record['comparison'] != archived['comparison']:
                raise ValueError('COMPARISON_IDENTITY')
            errors = {key: float(np.max(np.abs(np.asarray(record[key])-np.asarray(archived[key]))))
                      for key in ['effect', 'ci95', 'p_raw', 'p_holm']}
            if max(errors.values()) > 1e-12:
                raise ValueError('STATISTICAL_RECOMPUTATION_MISMATCH:' + record['comparison'])
            rows.append({'experiment': group, **record, 'maximum_numeric_error': max(errors.values())})

    e02, path = reports['E02']
    predictions = path.with_name('predictions.csv')
    verify_file(predictions, e02['predictions_sha256'])
    frame = pd.read_csv(predictions)
    target = frame[e02['target']].to_numpy()
    paths = {}
    for arm in ['selected', 'fixed', 'classical']:
        losses = pd.DataFrame(np.abs(frame[[f'{arm}_seed{s}' for s in [1701, 1702, 1703, 1704, 1705]]].to_numpy()-target[:, None]))
        losses['date'] = frame.date.to_numpy()
        paths[arm] = losses.groupby('date').mean().to_numpy()
    accept('E02', [{'comparison': 'selected_vs_'+arm, **paired_inference(paths['selected'], paths[arm], e02['block_length_development_freeze']['length'])}
                   for arm in ['fixed', 'classical']], e02['comparison_family'])
    e03, path = reports['E03']
    series = path.with_name('E03_prediction_error_paths.npz')
    verify_file(series, e03['series_sha256'])
    computed = []
    with np.load(series, allow_pickle=False) as arrays:
        for row in e03['primary_comparisons']:
            computed.append({'comparison': row['comparison'], **paired_inference(arrays['selected_directional__mae'],
                             arrays[row['reference']+'__mae'], row['block_length'])})
    accept('E03', computed, e03['primary_comparisons'])
    for group in ['E04', 'E07']:
        report, path = reports[group]
        finance = path.parent
        freeze = ctx.json(finance / 'freeze.json')
        verify_file(finance / 'freeze.json', report['finance_freeze_sha256'])
        records = []
        for job in freeze['jobs']:
            if job['group'] != group or job['phase'] != 'test' or job['cost'] != freeze['primary_cost']:
                continue
            directory = finance / 'paths' / job['job_id']
            result = ctx.json(directory / 'result.json')
            verify_file(directory / 'result.json', report['result_shas'][job['job_id']])
            verify_file(directory / 'series.npz', result['series_sha256'])
            if result['job'] != job or result['finance_freeze_sha256'] != report['finance_freeze_sha256']:
                raise ValueError('FROZEN_FINANCIAL_JOB_IDENTITY')
            with np.load(directory / 'series.npz', allow_pickle=False) as data:
                arrays = {key: data[key].copy() for key in data.files}
            records.append({'job': job, 'result': result, 'series': arrays})
        computed = []
        for row in report['primary_comparisons']:
            a, _ = matrix_for(records, row['method'], freeze['seeds'], freeze['primary_cost'], report['lambda'])
            b, _ = matrix_for(records, row['reference'], freeze['seeds'], freeze['primary_cost'], report['lambda'])
            computed.append({'comparison': row['comparison'], **paired_inference(a, b, row['block_length'], endpoint='cvar95')})
        accept(group, computed, report['primary_comparisons'])
    for group in ['E05', 'E06']:
        report, _ = reports[group]
        unit = report['paired_units']
        a = 'quantum' if group == 'E05' else 'noise'
        comparisons = ['uniform', 'local'] if group == 'E05' else ['fixed', 'resource']
        computed = [{'comparison': ('qaoa_vs_' if group == 'E05' else 'noise_qas_vs_')+b,
                     **paired_problem_bootstrap([r[a] for r in unit], [r[b] for r in unit])} for b in comparisons]
        accept(group, computed, report['comparisons'])
    if len(rows) != 15:
        raise ValueError('FIFTEEN_PRIMARY_COMPARISONS_REQUIRED')
    report = {'schemaVersion': 'qf.primary-statistics-recomputation.v2', 'status': 'PASS', 'comparisons': rows,
              'comparison_count': 15, 'maximum_numeric_error': max(r['maximum_numeric_error'] for r in rows),
              'parents': ctx.parents, 'external_calls': 0, 'scientificStatus': 'LOCAL_VERIFIED',
              'scope': 'E02 predictions, E03 date-error arrays, E04/E07 holding-interval losses, E05/E06 frozen paired objective endpoints; 2000 fixed-seed replicates and within-experiment Holm'}
    write_json(out / 'primary_statistics.json', report)
    pd.DataFrame(rows).to_csv(out / 'primary_statistics.csv', index=False)
    return report
