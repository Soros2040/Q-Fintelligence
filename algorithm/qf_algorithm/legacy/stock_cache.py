"""One frozen E03 frontend/fold/graph cache job, using one SDK evolution per node."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import numpy as np
from .research_common import Budget,digest,identity,write_json,utc
from .quantum_features import states,observables,cache_identity
from .stock_task import GRAPHS


def read_day(root,record):
    path=root/record['path']
    if digest(path)!=record['sha256']:raise ValueError('GRAPH_DAY_HASH')
    with np.load(path,allow_pickle=False) as z:return {k:z[k] for k in z.files}


def run(root,job_id):
    freeze=json.loads((root/'freeze.json').read_text())
    manifest=json.loads((root/'graph_manifest.json').read_text())
    if manifest['freeze_sha256']!=digest(root/'freeze.json'):raise ValueError('GRAPH_FREEZE_SHA')
    job=next(j for j in freeze['frontend_jobs'] if j['job_id']==job_id)
    fold=next(f for f in freeze['folds'] if f['id']==job['fold_id'])
    records={r['index']:r for r in manifest['records']}
    output=root/'caches'/job_id
    if (output/'manifest.json').exists():raise FileExistsError('CACHE_JOB_COMPLETED')
    train_features=[]
    for index in fold['train']:
        data=read_day(root,records[index])
        train_features.append(data['features'][data['feature_valid']])
    train=np.vstack(train_features)
    mean=np.nanmean(train,axis=0);scale=np.maximum(np.nanstd(train,axis=0),1e-8)
    scaler={'mean':mean.tolist(),'scale':scale.tolist(),'fit_dates':[records[i]['date'] for i in fold['train']],
            'fit_asset_date_count':len(train),'imputation':'TRAINING_FEATURE_MEAN','clip_standard_deviations':3,
            'feature_names':['return_5','return_20','downside_5','downside_20','volatility_20','amount_change20']}
    scaler_sha=identity(scaler)
    finite_dates=set(np.array(fold['evaluation'])[np.linspace(0,len(fold['evaluation'])-1,6).astype(int)].tolist()) if fold['id']=='final' else set()
    budget=Budget(output,{'cpu_seconds':job['cpu_seconds_cap'],'analytic_calls':job['analytic_calls_cap'],'shots':0},
        {'cache':{'cpu_seconds':job['cpu_seconds_cap'],'analytic_calls':job['analytic_calls_cap'],'shots':0}})
    write_json(output/'freeze.json',{'job':job,'fold':fold,'scaler':scaler,'scaler_sha256':scaler_sha,
        'stock_freeze_sha256':digest(root/'freeze.json'),'graph_manifest_sha256':digest(root/'graph_manifest.json'),
        'implementation_sha256':digest(__file__),'quantum_features_sha256':digest(Path(__file__).with_name('quantum_features.py')),
        'finite_subset_date_indices':sorted(finite_dates),'finite_subset_asset_positions':list(range(32)),
        'created_at':utc(),'models_evaluated':0})
    results=[]
    day_cpu_cap=min(20,job['cpu_seconds_cap']//len(job['date_indices']))
    if day_cpu_cap<1:raise ValueError('CACHE_ATOMIC_CPU_PLAN')
    for index in job['date_indices']:
        record=records[index];data=read_day(root,record)
        unit='date_'+record['date']
        budget.admit(unit,'cache',{'cpu_seconds':day_cpu_cap,'analytic_calls':300,'shots':0})
        graph=data['graphs'][GRAPHS.index(job['graph'])].astype(float)
        x=np.where(np.isfinite(data['features']),data['features'],mean)
        x=np.clip((x-mean)/scale,-3,3)*np.pi/3
        quantum_input=graph@x
        state=states(quantum_input,job['spec'])
        readout=observables(state)
        sin=np.sin(quantum_input)
        classic=np.column_stack([sin,sin[:,:-1]*sin[:,1:]])
        mechanism_error=None
        if job['spec']['layers']==1 and job['spec']['entangler']=='CZ':
            separable_z=-np.sin(quantum_input*job['spec']['input_scale'])
            exact_classical=np.column_stack([separable_z,separable_z[:,:-1]*separable_z[:,1:]])
            mechanism_error=float(np.max(np.abs(readout-exact_classical)))
            if mechanism_error>1e-9:raise ValueError('ONE_LAYER_Z_READOUT_IDENTITY')
        graph_sha=identity(graph.tolist());preaggregation_sha=identity({'scaler_sha256':scaler_sha,'graph_sha256':graph_sha,'operation':'FROZEN_LINEAR_GRAPH_PREAGGREGATION'})
        keys=[cache_identity(freeze['data_epoch'],scaler_sha,record['date'],str(asset),graph_sha,preaggregation_sha,value,job['spec'])
              for asset,value in zip(data['assets'],quantum_input)]
        probability=np.abs(state[:32])**2 if index in finite_dates and job['graph']=='directional' else np.empty((0,64))
        path=output/'days'/(record['date']+'.npz');path.parent.mkdir(exist_ok=True)
        np.savez_compressed(path,assets=data['assets'],readout=readout,classical_readout=classic,input_angles=quantum_input,
            cache_keys=np.array(keys),finite_subset_probabilities=probability,finite_subset_asset_positions=np.arange(len(probability)))
        results.append({'index':index,'date':record['date'],'path':str(path.relative_to(output)),'sha256':digest(path),
            'bytes':path.stat().st_size,'asset_count':300,'graph_sha256':graph_sha,'input_keys_sha256':identity(keys),
            'parent_day_sha256':record['sha256'],'finite_subset_units':len(probability),
            'one_layer_Z_classical_formula_max_error':mechanism_error})
        budget.finish(unit,{'analytic_calls':300,'shots':0})
        if len(results)%50==0:print(json.dumps({'cache_job':job_id,'completed':len(results),'total':len(job['date_indices'])}),flush=True)
    write_json(output/'manifest.json',{'job_id':job_id,'data_epoch':freeze['data_epoch'],'fold_id':fold['id'],
        'arm':job['arm'],'graph':job['graph'],'scaler_sha256':scaler_sha,'spec_sha256':identity(job['spec']),
        'cache_freeze_sha256':digest(output/'freeze.json'),'records':results,'actual_sdk_evolutions':sum(r['asset_count'] for r in results),
        'resource_ledger_sha256':digest(output/'resource_ledger.json'),'created_at':utc(),'scientific_status':'LOCAL_VERIFIED',
        'evolution_backend':'cqlib1.3.11.StatevectorSimulator','observables':['Z_0:5','ZZ_01:45']})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--stock-root',type=Path,required=True);p.add_argument('--job-id',required=True)
    a=p.parse_args();run(a.stock_root,a.job_id)
