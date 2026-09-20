"""Freeze and build full-axis graphs and risk estimates for the stock task."""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import multiprocessing
from pathlib import Path
import time
import numpy as np
from sklearn.covariance import LedoitWolf
from .research_common import digest, identity, write_json, utc

GRAPHS=['self','correlation','directional','reverse','random']
DATA=None


def load_panels(root):
    manifest=json.loads((root/'manifest.json').read_text())
    chunks=[]
    for shard in manifest['shards']:
        path=root/shard['path']
        if digest(path)!=shard['sha256']:raise ValueError('PANEL_HASH')
        with np.load(path,allow_pickle=False) as z:chunks.append({k:z[k] for k in z.files if k!='assets'})
    data={key:np.concatenate([x[key] for x in chunks]) for key in chunks[0]}
    data['assets']=np.array(manifest['assets']);data['manifest']=manifest
    return data


def evenly(indices,n):
    indices=np.array(indices,dtype=int)
    return indices[np.linspace(0,len(indices)-1,min(n,len(indices))).astype(int)].tolist()


def freeze(data_root,mapping_root,output):
    meta=json.loads((data_root/'manifest.json').read_text())
    dates=meta['dates']
    selection=json.loads((mapping_root/'selection.json').read_text())
    folds=[]
    for year in [2019,2020,2021]:
        train=[i for i,d in enumerate(dates[:-2]) if '20160101'<=d<f'{year}0101' and dates[i+2]<f'{year}0101']
        validation=[i for i,d in enumerate(dates[:-2]) if f'{year}0101'<=d<=f'{year}1231' and dates[i+2]<=f'{year}1231']
        folds.append({'id':f'dev_{year}','train':evenly(train,64),'evaluation':evenly(validation,20),'role':'DEVELOPMENT'})
    train=[i for i,d in enumerate(dates[:-2]) if '20160101'<=d<='20221231' and dates[i+2]<='20221231']
    validation=[i for i,d in enumerate(dates[:-2]) if '20230101'<=d<='20231231' and dates[i+2]<='20231231']
    test=[i for i,d in enumerate(dates[:-2]) if '20240101'<=d<='20251231']
    final={'id':'final','train':evenly(train,64),'validation':validation,'evaluation':test,'role':'FINAL'}
    folds.append(final)
    all_dates=sorted({i for f in folds for key in ['train','validation','evaluation'] for i in f.get(key,[])})
    frontend_units=[{'arm':'selected','graph':g,'spec':selection['selected']} for g in GRAPHS]+[{'arm':'fixed','graph':'directional','spec':selection['fixed']}]
    quantum_jobs=[]
    for fold in folds:
        for frontend in frontend_units:
            keys=sorted(set(fold['train']+fold['evaluation']+fold.get('validation',[])))
            quantum_jobs.append({'job_id':fold['id']+'_'+frontend['arm']+'_'+frontend['graph'],
                'fold_id':fold['id'],**frontend,'date_indices':keys,'expected_asset_dates':len(keys)*300,
                'cpu_seconds_cap':9000,'analytic_calls_cap':len(keys)*300})
    calls=sum(j['analytic_calls_cap'] for j in quantum_jobs)
    if calls>10000000:raise ValueError('E03_COMPLETE_CACHE_BUDGET')
    parameters={'schema_version':'qf.stock-execution-freeze.v1','created_at':utc(),'models_evaluated':0,
        'data_epoch':meta['data_epoch'],'data_manifest_sha256':digest(data_root/'manifest.json'),
        'mapping_selection_sha256':digest(mapping_root/'selection.json'),'folds':folds,'date_indices':all_dates,
        'frontend_jobs':quantum_jobs,'analytic_call_reservation':calls,'max_analytic_calls':10000000,
        'max_finite_shots':2097152,'finite_subset_units':384,'finite_shots_levels':[256,1024,4096],
        'graphs':GRAPHS,'graph_window_sessions':126,'VAR_ridge_fraction':.1,'VAR_max_row_sum':.98,'FEVD_horizon':5,
        'graph_incoming_edges':2,'preaggregation':'HALF_SELF_HALF_NORMALIZED_INCOMING',
        'risk_estimation_window_sessions':126,'risk_factor_rank':3,'risk_horizon_sessions':1,
        'risk_estimation_inputs':'ADJUSTED_OPEN_RETURN_WINDOWS_COMPLETED_BY_SIGNAL_DAY_OPEN',
        'head_candidates':[{'layers':layer,'width':16,'l2':alpha,'epochs':40} for layer in [1,2] for alpha in [.001,.01]],
        'final_seeds':[1701,1702,1703,1704,1705],
        'classical_feature_map':'SIN_SIX_COORDINATES_PLUS_FIVE_ADJACENT_SIN_PRODUCTS',
        'head_comparison':'IDENTICAL_11_DIMENSION_READOUT_AND_SHARED_GCN_ARCHITECTURE_GRID',
        'head_selection':{'selected':'FOUR_HEAD_CONFIGS_ON_DIRECTIONAL_GRAPH','fixed':'FOUR_HEAD_CONFIGS_ON_DIRECTIONAL_GRAPH',
            'classical':'FIVE_GRAPHS_WITH_FIRST_HEAD_CONFIG_THEN_FOUR_HEAD_CONFIGS_ON_CHOSEN_GRAPH',
            'fold_seed':1601,'selection_cpu_seconds_each':8000,'final_cpu_seconds_per_seed_graph':900,
            'final_jobs':35,'total_head_cpu_reservation':55500},
        'cpu_reservations_E03':{'graphs':36000,'quantum_caches':216000,'heads':60000,'statistics':12000},
        'cpu_seconds_max_E03':345600,'memory_mib_E03':8192,'graph_worker_count':6,
        'implementation_sha256':digest(__file__),'information_rule':'GRAPH_RETURNS_THROUGH_SIGNAL_CLOSE_RISK_OPEN_RETURNS_THROUGH_SIGNAL_OPEN',
        'planning_block_proxy':meta['block_length_freeze'],
        'statistical_block_rule':'Before test endpoint aggregation, freeze length from complete 2023 classical validation date MAE for E03 and classical validation net-loss paths for E04/E07; inspect grid 1/5/10/20/40 and extend to80 for longer observed dependence. Report adjacent grid lengths.'}
    # Separate E04/E07 ledgers reserve every date/seed/method/cost/parameter unit before comparison.
    for group,method_count,cost_count,reserved in [('E04',4,5,120000),('E07',3,1,60000)]:
        dev_jobs=len(validation)*5*method_count*3
        test_jobs=len(test)*5*method_count*cost_count
        cap=min(60,reserved//(dev_jobs+test_jobs))
        if cap<1:raise ValueError('SOLVER_COMPLETE_TASK_BUDGET')
        parameters['solver_'+group]={'development_jobs':dev_jobs,'final_jobs':test_jobs,'total_jobs':dev_jobs+test_jobs,
            'reserved_cpu_seconds':reserved,'cpu_seconds_cap_per_job':cap,'development_reservation':dev_jobs*cap,
            'final_reservation':test_jobs*cap,'cost_levels':[0,.0005,.001,.002,.005] if group=='E04' else [.001],
            'lambda_candidates':[.1,1,10],'selection_rule':'CLASSICAL_REFERENCE_VALIDATION_CVAR_AT_PRIMARY_COST',
            'k':5,'primary_cost':.001,'max_swap_passes':4}
    write_json(output/'freeze.json',parameters)
    print(json.dumps({'state':'STOCK_SCOPE_FROZEN','dates_to_build':len(all_dates),'test_dates':len(test),'analytic_calls_reserved':calls}),flush=True)


def normalize(graph):
    sums=graph.sum(axis=1,keepdims=True)
    return np.divide(graph,sums,out=np.zeros_like(graph),where=sums>0)


def top_incoming(graph,k=2):
    out=np.zeros_like(graph);work=graph.copy();np.fill_diagonal(work,0)
    for i in range(len(work)):
        selected=np.argsort(-work[i],kind='stable')[:k]
        out[i,selected]=np.maximum(work[i,selected],0)
    return normalize(out)


def randomize(graph,seed):
    out=graph.copy();rng=np.random.default_rng(seed)
    rows,cols=np.nonzero(out)
    edges=list(zip(rows.tolist(),cols.tolist()))
    if len(edges)<2:return out
    for _ in range(10*len(edges)):
        a,b=rng.integers(len(edges),size=2)
        t1,s1=edges[a];t2,s2=edges[b]
        if t1==t2 or s1==s2 or t1==s2 or t2==s1 or out[t1,s2]!=0 or out[t2,s1]!=0:continue
        out[t1,s2],out[t2,s1]=out[t1,s1],out[t2,s2]
        out[t1,s1]=out[t2,s2]=0
        edges[a]=(t1,s2);edges[b]=(t2,s1)
    if not np.array_equal((out>0).sum(0),(graph>0).sum(0)) or not np.array_equal((out>0).sum(1),(graph>0).sum(1)):
        raise ValueError('DIRECTED_DEGREE_CONSERVATION')
    return out


def graph_and_risk(close_returns,open_returns):
    n=close_returns.shape[1]
    r=np.nan_to_num(close_returns,nan=0,posinf=0,neginf=0)
    scale=np.maximum(r.std(axis=0),1e-5);standard=(r-r.mean(axis=0))/scale
    x,y=standard[:-1],standard[1:]
    gram=x.T@x
    ridge=.1*max(float(np.trace(gram))/n,1e-8)
    coefficient=np.linalg.solve(gram+ridge*np.eye(n),x.T@y).T
    norm=float(np.max(np.abs(coefficient).sum(axis=1)))
    stabilization=max(1.,norm/.98);coefficient/=stabilization
    residual=y-x@coefficient.T
    residual_cov=residual.T@residual/max(1,len(residual)-1)+np.eye(n)*1e-8
    diagonal=np.diag(residual_cov)
    numerator=np.zeros((n,n));denominator=np.zeros(n);phi=np.eye(n)
    for horizon in range(5):
        product=phi@residual_cov
        numerator+=product**2/diagonal[None,:]
        denominator+=(product*phi).sum(axis=1)
        phi=coefficient@phi
    fevd=normalize(numerator/np.maximum(denominator[:,None],1e-12))
    directed=top_incoming(fevd)
    correlation=np.nan_to_num(np.corrcoef(r,rowvar=False),nan=0)
    corr=top_incoming(np.abs(correlation))
    rr=np.nan_to_num(open_returns,nan=0,posinf=0,neginf=0)
    centered=rr-rr.mean(axis=0)
    _,singular,vh=np.linalg.svd(centered,full_matrices=False)
    loading=vh[:3].T
    factor=np.diag(singular[:3]**2/max(1,len(rr)-1))
    common=loading@factor@loading.T
    d=np.maximum(np.var(rr,axis=0,ddof=1)-np.diag(common),1e-8)
    sigma=common+np.diag(d)
    shrinkage=LedoitWolf().fit(rr).covariance_+np.eye(n)*1e-8
    return directed,corr,fevd,loading,factor,d,sigma,shrinkage,stabilization


def build_day(task):
    index,root=task;start=time.process_time();data=DATA
    date=str(data['dates'][index]);asset_indices=np.flatnonzero(data['active'][index]);n=len(asset_indices)
    if n!=300 or index<126:raise ValueError('FULL_AXIS_OR_WINDOW')
    close_returns=data['returns'][index-125:index+1][:,asset_indices]
    adjusted=data['open'][index-126:index+1][:,asset_indices]*data['adj_factor'][index-126:index+1][:,asset_indices]
    open_returns=adjusted[1:]/adjusted[:-1]-1
    directed,corr,fevd,loading,factor,specific,sigma,shrinkage,stabilization=graph_and_risk(close_returns,open_returns)
    reverse=normalize(directed.T)
    random=randomize(directed,int(date)+2026090804)
    pure=[np.zeros((n,n)),corr,directed,reverse,random]
    graphs=np.stack([np.eye(n) if g==0 else .5*np.eye(n)+.5*graph for g,graph in enumerate(pure)])
    path=root/'days'/(date+'.npz');path.parent.mkdir(exist_ok=True)
    if path.exists():raise FileExistsError('DAY_RESULT_ALREADY_EXISTS')
    np.savez_compressed(path,date=np.array(date),asset_indices=asset_indices,assets=data['assets'][asset_indices],
        features=data['features'][index,asset_indices],feature_valid=data['feature_valid'][index,asset_indices],
        labels=data['labels'][index,asset_indices],label_valid=data['label_valid'][index,asset_indices],
        graphs=graphs.astype(np.float32),fevd_full=fevd.astype(np.float32),Lambda=loading,F=factor,d=specific,
        Sigma=sigma,shrinkage_Sigma=shrinkage,history_observations=np.sum(np.isfinite(open_returns),axis=0),
        uncertainty_standard_error_mu=np.nanstd(open_returns,axis=0,ddof=1)/np.sqrt(np.maximum(np.sum(np.isfinite(open_returns),axis=0),1)))
    if path.stat().st_size>8388608:raise ValueError('DAY_ARTIFACT_SHARD_LIMIT')
    return {'date':date,'index':index,'path':str(path.relative_to(root)),'sha256':digest(path),'bytes':path.stat().st_size,
        'asset_axis_sha256':identity(data['assets'][asset_indices].tolist()),'VAR_stabilization_divisor':stabilization,
        'cpu_seconds':time.process_time()-start,'risk_horizon_sessions':1,'risk_window_last_open':date,
        'graph_window_last_close':date,'minimum_specific_variance':float(specific.min())}


def build(data_root,root,workers):
    global DATA
    DATA=load_panels(data_root)
    freeze=json.loads((root/'freeze.json').read_text())
    if digest(data_root/'manifest.json')!=freeze['data_manifest_sha256']:raise ValueError('DATA_FREEZE_HASH')
    if workers>freeze['graph_worker_count']:raise ValueError('GRAPH_WORKER_RESERVATION')
    jobs=[(index,root) for index in freeze['date_indices']]
    results=[]
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers,mp_context=multiprocessing.get_context('fork')) as pool:
        for record in pool.map(build_day,jobs,chunksize=1):
            results.append(record)
            write_json(root/'graph_progress.json',{'completed':len(results),'total':len(jobs),'cpu_seconds':sum(r['cpu_seconds'] for r in results)},replace=True)
            if len(results)%50==0:print(json.dumps({'graph_dates_completed':len(results),'total':len(jobs)}),flush=True)
    write_json(root/'graph_manifest.json',{'data_epoch':freeze['data_epoch'],'freeze_sha256':digest(root/'freeze.json'),
        'records':results,'cpu_seconds':sum(r['cpu_seconds'] for r in results),'scientific_status':'LOCAL_VERIFIED',
        'created_at':utc(),'graph_variants':GRAPHS,'graph_family':'RIDGE_VAR1_GENERALIZED_FEVD_H5','models_evaluated':0})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['freeze','build']);p.add_argument('--data',type=Path,required=True)
    p.add_argument('--mapping',type=Path);p.add_argument('--output',type=Path,required=True);p.add_argument('--workers',type=int,default=6)
    a=p.parse_args()
    if a.mode=='freeze':freeze(a.data,a.mapping,a.output)
    else:build(a.data,a.output,a.workers)
