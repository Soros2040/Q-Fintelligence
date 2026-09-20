"""Recompute selected public evidence arithmetic, without external calls.

This does not reconstruct archived confidence intervals or original experiments.
"""
import csv
import json
import math
from pathlib import Path
import re
from statistics import fmean

ROOT = Path(__file__).resolve().parents[1]


def verify():
    comparisons = 0
    def equal(actual, expected, label):
        nonlocal comparisons
        comparisons += 1
        if not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-10):
            raise AssertionError(f'{label}: public arithmetic mismatch')
    docs = {}
    for i in range(1,8):
        key = f'E{i:02d}'
        doc = json.loads((ROOT/'evidence'/f'{key}.json').read_text(encoding='utf-8'))
        assert doc['experiment_id'] == key
        assert re.fullmatch(r'[0-9a-f]{64}',doc['source_result_sha256'])
        assert doc['task_id'] and doc['statistical_unit']
        docs[key] = doc
    rows_by_source = {}
    for source, expected_count in [('hardware',1000),('cloud',999)]:
        summary = json.loads((ROOT/'evidence'/f'{source}.json').read_text(encoding='utf-8'))
        assert re.fullmatch(r'[0-9a-f]{64}',summary['source_result_sha256'])
        with (ROOT/'evidence'/f'{source}-per-circuit.csv').open(encoding='utf-8',newline='') as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == expected_count
        assert len({row['circuit_id'] for row in rows}) == expected_count
        rows_by_source[source] = rows
        for family, group in summary['family_results'].items():
            selected = [row for row in rows if row['family'] == family]
            assert len(selected) == group['circuits' if source == 'hardware' else 'valid_circuits']
            for metric, stats in group['metrics'].items():
                if metric in rows[0]:
                    values = [float(row[metric]) for row in selected if row[metric] != '']
                    if values:
                        equal(fmean(values),stats['mean'],f'{source}/{family}/{metric}')
        if source == 'cloud':
            assert all(row['shots_conserved'] == 'True' and row['counts_axis_valid'] == 'True' for row in rows)
            assert sum(int(row['cloud_shots']) for row in rows) == summary['valid_cloud_shots'] == 1022976
            assert summary['accepted_circuits'] == 1000 and summary['terminal_failed_circuits'] == 1
            assert summary['pending_circuits'] == 0
        else:
            assert summary['hardware_shots'] == expected_count*1024
        assert summary['observed_calibration_snapshots'] == 1
    hw = {row['circuit_id']:row for row in rows_by_source['hardware']}
    for row in rows_by_source['cloud']:
        equal(float(row['hardware_vs_ideal_tv']),float(hw[row['circuit_id']]['hardware_vs_ideal_tv']),'matched hardware TV')
    for exp,left,rights,count in [('E05','quantum',{'qaoa_vs_uniform':'uniform','qaoa_vs_local':'local'},27),
                                 ('E06','noise',{'noise_qas_vs_fixed':'fixed','noise_qas_vs_resource':'resource'},8)]:
        doc = docs[exp]
        assert len(doc['paired_units']) == count
        for comparison in doc['comparisons']:
            right = rights[comparison['comparison']]
            effects = [unit[left]-unit[right] for unit in doc['paired_units']]
            equal(fmean(effects),comparison['effect'],f'{exp}/{right}')
            assert len(effects) == len(comparison['paired_effects'])
            for got,want in zip(effects,comparison['paired_effects']):
                equal(got,want,f'{exp}/{right}/unit')
    endpoints = {v['method']:v for v in docs['E07']['endpoints'] if v['cost'] == 0.001}
    for method, endpoint in endpoints.items():
        equal(fmean(endpoint['per_seed_cvar95']),endpoint['mean_seed_cvar95'],f'E07/{method}/mean')
    equal(endpoints['full']['mean_seed_cvar95']-endpoints['classical']['mean_seed_cvar95'],0.00941191823706828,'E07/full-classical')
    equal(endpoints['full']['mean_seed_cvar95'],endpoints['fixed']['mean_seed_cvar95'],'E07/full-fixed')
    return {'status':'PASS','experiment_summaries':7,'hardware_rows':1000,'cloud_rows':999,
            'numeric_comparisons':comparisons,'absolute_tolerance':1e-10,'external_calls':0,
            'scope':'selected public point estimates and paired arithmetic; archived inference not rerun'}


if __name__ == '__main__':
    print(json.dumps(verify(),indent=2))
