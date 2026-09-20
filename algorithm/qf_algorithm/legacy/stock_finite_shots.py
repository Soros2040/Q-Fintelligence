"""Finite-shot Z/ZZ readout from the frozen E03 SDK probability cache."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np
from .research_common import digest,write_json,utc,Budget

BITS=((np.arange(64)[:,None]>>np.arange(6))&1)
Z=1-2*BITS
OBS=np.column_stack([Z,Z[:,:-1]*Z[:,1:]])


def run(root,out):
    frozen=json.loads((root/'freeze.json').read_text());inputs=[];identities=[]
    for arm in ['selected','fixed']:
        cache=root/'caches'/('final_'+arm+'_directional');path=cache/'manifest.json'
        manifest=json.loads(path.read_text())
        identities.append({'arm':arm,'cache_manifest_sha256':digest(path)})
        for record in manifest['records']:
            if not record['finite_subset_units']:continue
            path=cache/record['path']
            if digest(path)!=record['sha256']:raise ValueError('FINITE_CACHE_SHA')
            with np.load(path,allow_pickle=False) as data:
                p=data['finite_subset_probabilities'];positions=data['finite_subset_asset_positions']
                for probability,pos in zip(p,positions):
                    if abs(probability.sum()-1)>1e-9 or probability.min()<-1e-12:raise ValueError('FINITE_PROBABILITY')
                    inputs.append({'arm':arm,'date':record['date'],'asset':str(data['assets'][pos]),'position':int(pos),
                        'probability':np.maximum(probability,0)/probability.sum(),'parent_sha256':record['sha256']})
    if len(inputs)!=384:raise ValueError('FINITE_FROZEN_UNIT_COVERAGE')
    levels=frozen['finite_shots_levels'];shots=len(inputs)*sum(levels)
    if shots>frozen['max_finite_shots']:raise ValueError('FINITE_SHOT_CAP')
    write_json(out/'freeze.json',{'created_at':utc(),'stock_freeze_sha256':digest(root/'freeze.json'),
        'inputs':identities,'units':384,'shot_levels':levels,'total_shots':shots,'sampling_seed':2026090805,
        'sampling_mode':'LOCAL_MULTINOMIAL_FROM_SDK_STATE_CACHE','new_analytic_evolutions':0,
        'common_random_numbers':'same date/asset/shot seed across fixed and selected arms',
        'implementation_sha256':digest(__file__)})
    budget=Budget(out,{'cpu_seconds':1000,'shots':shots,'analytic_calls':0},
        {'finite':{'cpu_seconds':1000,'shots':shots,'analytic_calls':0}})
    rows=[]
    for i,row in enumerate(inputs):
        unit=row['arm']+'_'+row['date']+'_'+str(row['position'])
        budget.admit(unit,'finite',{'cpu_seconds':2,'shots':sum(levels),'analytic_calls':0})
        probability=row['probability'];exact=probability@OBS
        for n in levels:
            seed=2026090805+int(row['date'])*37+row['position']*100003+n
            counts=np.random.default_rng(seed).multinomial(n,probability)
            estimate=counts@OBS/n
            rows.append({k:v for k,v in row.items() if k!='probability'}|{'shots':n,'counts':counts.tolist(),
                'exact_readout':exact.tolist(),'sample_readout':estimate.tolist(),
                'mean_absolute_readout_error':float(np.abs(exact-estimate).mean()),
                'maximum_absolute_readout_error':float(np.abs(exact-estimate).max())})
        budget.finish(unit,{'shots':sum(levels),'analytic_calls':0})
    summaries=[]
    for arm in ['selected','fixed']:
        for n in levels:
            values=[r['mean_absolute_readout_error'] for r in rows if r['arm']==arm and r['shots']==n]
            summaries.append({'arm':arm,'shots':n,'units':len(values),'mean_absolute_readout_error':float(np.mean(values)),
                'unit_error_quantiles_025_975':np.quantile(values,[.025,.975]).tolist()})
    formula=[]
    for path in (root/'caches').glob('*/manifest.json'):
        for r in json.loads(path.read_text())['records']:
            if r.get('one_layer_Z_classical_formula_max_error') is not None:formula.append(r['one_layer_Z_classical_formula_max_error'])
    write_json(out/'unit_results.json',{'records':rows,'state_axis':'q5...q0','observables':['Z0:5','Z0Z1:Z4Z5']})
    write_json(out/'result.json',{'status':'COMPLETE','scientific_status':'LOCAL_VERIFIED','units':384,'shots':shots,
        'new_quantum_evolutions':0,'freeze_sha256':digest(out/'freeze.json'),'unit_results_sha256':digest(out/'unit_results.json'),
        'resource_ledger_sha256':digest(out/'resource_ledger.json'),'summaries':summaries,
        'readout_classical_identity':{'daily_cache_records':len(formula),'maximum_error':max(formula),
            'single_Z':'-sin(input_scale * input_angle)','adjacent_ZZ':'product of corresponding single_Z',
            'scope':'The selected one-layer CZ frontend with Z/ZZ measurements has this exact classical readout representation.'},
        'inference_scope':'Descriptive readout error on the prospectively fixed cache subset; quantiles describe units.'})
    print(json.dumps({'state':'E03_FINITE_COMPLETE','units':384,'shots':shots,'analytic_calls':0}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--stock-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.stock_root,a.output)
