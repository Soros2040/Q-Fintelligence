"""Read archived hardware/cloud paired records and verify their table arithmetic."""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
from .api import digest, load_json
from .paths import DataLayout

ACCEPTED_RELATIVE = Path('90_checks/competition_v2/hardware_cloud/runs/software_hardware_cloud_20260909T045500Z')
CLOUD_RELATIVE = Path('01_docs/experiment_closeout_20260908/supporting/completion_20260909/cloud_analysis')


def _check(rows, name, actual, expected, tolerance=1e-12):
    if isinstance(actual, (str, bool, np.bool_)):
        error = 0. if actual == expected else 1.
    else:
        error = float(np.max(np.abs(np.asarray(actual)-np.asarray(expected))))
    passed = np.isfinite(error) and error <= tolerance
    rows.append({'check': name, 'actual': actual, 'expected': expected, 'maximum_absolute_error': error,
                 'tolerance': tolerance, 'status': 'PASS' if passed else 'FAIL'})
    if not passed:
        raise ValueError('PAIRED_MEASUREMENT_VALIDATION:'+name)


def _load_section(directory, family, *, manifest=None, manifest_base=None):
    summary_name = 'analysis.json' if family == 'hardware' else 'summary.json'
    summary_path, table_path = directory/summary_name, directory/'per_circuit.csv'
    if not summary_path.is_file() or not table_path.is_file():
        return {'available': False, 'reason': 'ARCHIVED_PAIRED_RECORDS_NOT_PRESENT',
                'expected_directory': str(directory), 'records': pd.DataFrame(), 'checks': pd.DataFrame(), 'sources': []}
    manifest_path = manifest or directory/'artifact_manifest.json'
    if not manifest_path.is_file():
        raise ValueError('PAIRED_MEASUREMENT_MANIFEST_MISSING:'+str(manifest_path))
    seal = load_json(manifest_path)
    base = manifest_base or directory
    if 'files' in seal:
        expected = {str((base/row['path']).resolve()): row['sha256'] for row in seal['files']}
    else:
        expected = {str(summary_path.resolve()): seal['analysis_sha256'], str(table_path.resolve()): seal['table_sha256']}
    checks, sources = [], []
    for path in (summary_path, table_path):
        if str(path.resolve()) not in expected:
            raise ValueError('PAIRED_MEASUREMENT_UNSEALED_FILE:'+str(path))
        observed = digest(path)
        _check(checks, path.name+' SHA256', observed, expected[str(path.resolve())], 0)
        sources.append({'path': str(path.resolve()), 'sha256': observed, 'bytes': path.stat().st_size,
                        'role': 'ARCHIVED_PAIRED_SUMMARY' if path == summary_path else 'ARCHIVED_PER_CIRCUIT_TABLE'})
    sources.append({'path': str(manifest_path.resolve()), 'sha256': digest(manifest_path),
                    'bytes': manifest_path.stat().st_size, 'role': 'ARCHIVED_ARTIFACT_MANIFEST'})
    document = load_json(summary_path)
    records = pd.read_csv(table_path, dtype={'hardware_query_id': str, 'cloud_query_id': str, 'query_id': str,
                                             'circuit_id': str, 'family': str})
    expected_count = document['circuit_count'] if family == 'hardware' else document['valid_circuits']
    _check(checks, 'record count', len(records), expected_count, 0)
    _check(checks, 'unique circuit IDs', records.circuit_id.nunique(), len(records), 0)
    for field in ['hardware_query_id'] + (['cloud_query_id'] if family == 'cloud' else []):
        _check(checks, 'unique '+field, records[field].nunique(), len(records), 0)
    if family == 'cloud':
        _check(checks, 'counts axis validations', bool(records.counts_axis_valid.eq(True).all()), True, 0)
        _check(checks, 'shots conservation validations', bool(records.shots_conserved.eq(True).all()), True, 0)
        for name in ['cloud_shots', 'hardware_shots', 'finite_shots']:
            _check(checks, name+' per circuit', bool(records[name].eq(1024).all()), True, 0)
    for name, expected_family in document['family_results'].items():
        selected = records[records.family == name]
        _check(checks, name+' family size', len(selected), expected_family.get('circuits', expected_family.get('valid_circuits')), 0)
        block = 'hardware_batch_id' if family == 'hardware' else 'cloud_batch_id'
        _check(checks, name+' submission blocks', selected[block].nunique(), expected_family['submission_blocks'], 0)
        if family == 'cloud':
            _check(checks, name+' cloud shots', int(selected.cloud_shots.sum()), expected_family['cloud_shots'], 0)
        for metric, saved in expected_family['metrics'].items():
            values = selected[metric].to_numpy(float)
            _check(checks, name+'/'+metric+' finite', bool(np.isfinite(values).all()), True, 0)
            for statistic, calculate in [('mean', np.mean), ('median', np.median), ('minimum', np.min), ('maximum', np.max)]:
                if statistic in saved:
                    _check(checks, name+'/'+metric+'/'+statistic, float(calculate(values)), saved[statistic])
    for field in [c for c in records if c.endswith('_h2')]:
        hellinger = field[:-3]+'_hellinger'
        if hellinger in records:
            valid = records[[field, hellinger]].dropna()
            _check(checks, field+' squared Hellinger identity', float(np.max(abs(valid[field]-valid[hellinger]**2))), 0.)
    contrasts = {'hardware_noise_minus_ideal_tv': ('hardware_vs_local_noise_tv', 'hardware_vs_ideal_tv'),
                 'hardware_excess_over_finite_tv': ('hardware_vs_ideal_tv', 'finite_vs_ideal_tv'),
                 'cloud_noise_minus_ideal_tv': ('cloud_vs_local_noise_tv', 'cloud_vs_ideal_tv'),
                 'cloud_excess_over_finite_tv': ('cloud_vs_ideal_tv', 'finite_vs_ideal_tv'),
                 'hardware_cloud_minus_ideal_tv': ('cloud_vs_hardware_tv', 'hardware_vs_ideal_tv')}
    for name, (left, right) in contrasts.items():
        if name in records:
            _check(checks, name+' paired arithmetic', float(np.max(abs(records[name]-(records[left]-records[right])))), 0.)
    return {'available': True, 'summary': {key: value for key, value in document.items() if key != 'records'},
            'records': records, 'checks': pd.DataFrame(checks), 'sources': sources,
            'scope': 'READ_ONLY_ARCHIVED_PAIRED_RECORDS_AND_POINT_ESTIMATE_RECHECK',
            'intervals': 'ARCHIVED_CONDITIONAL_DESCRIPTIVE_INTERVALS', 'external_calls': 0}


def load_paired_measurements(data_root=None, *, acceptance_root=None):
    """Return real record tables, source SHAs and checks for the archived paired analyses.

    The 1,000-circuit hardware family and 999-valid-circuit cloud family retain
    their own coverage. Missing optional archives have an explicit availability
    result. Present archives must pass their stored file identities and arithmetic.
    """
    layout = DataLayout.resolve(data_root)
    roots = [layout.root] + ([layout.root.parent] if layout.root.name == 'data' else [])
    if acceptance_root is not None:
        accepted = Path(acceptance_root).resolve()
    else:
        accepted = next((root/ACCEPTED_RELATIVE for root in roots if (root/ACCEPTED_RELATIVE/'output_manifest.json').is_file()), None)
    if accepted is not None:
        hardware = _load_section(accepted/'hardware_full', 'hardware', manifest=accepted/'output_manifest.json', manifest_base=accepted)
        cloud = _load_section(accepted/'cloud_paired', 'cloud', manifest=accepted/'output_manifest.json', manifest_base=accepted)
    else:
        hardware = _load_section(layout.hardware/'hardware_local_comparison', 'hardware')
        cloud_directory = next((root/CLOUD_RELATIVE for root in roots if (root/CLOUD_RELATIVE).is_dir()), roots[0]/CLOUD_RELATIVE)
        cloud = _load_section(cloud_directory, 'cloud')
    pairs = []
    if hardware['available'] and cloud['available']:
        a, b = hardware['records'], cloud['records']
        joined = b.merge(a, on='circuit_id', suffixes=('_cloud', '_hardware'), validate='one_to_one')
        _check(pairs, 'cloud subset matches hardware circuits', len(joined), len(b), 0)
        for field in ['hardware_query_id', 'hardware_result_sha256', 'local_result_sha256', 'input_sha256']:
            _check(pairs, 'cross-table '+field, bool(joined[field+'_cloud'].eq(joined[field+'_hardware']).all()), True, 0)
        for field in ['hardware_vs_ideal_tv', 'hardware_vs_ideal_h2', 'finite_vs_ideal_tv', 'local_noise_vs_ideal_tv']:
            _check(pairs, 'cross-table '+field, float(np.max(abs(joined[field+'_cloud']-joined[field+'_hardware']))), 0.)
    return {'hardware': hardware, 'cloud': cloud, 'paired_checks': pd.DataFrame(pairs), 'external_calls': 0}
