"""Paired stationary-block inference on frozen prediction and real-calendar simulation paths."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np
from .research_common import digest,write_json,utc,cvar95,paired_inference,select_block_length,holm
from .stock_finance import METHODS,phase_budget,read_prediction,save_npz,validate_implementation

BLOCK_GRID=[1,5,10,20,40,80]
BOOTSTRAP_SEED=2026090701


def adjacent_lengths(length):
    if length not in BLOCK_GRID:raise ValueError('FROZEN_BLOCK_GRID')
    i=BLOCK_GRID.index(length)
    return BLOCK_GRID[max(0,i-1):min(len(BLOCK_GRID),i+2)]


def stationary_indices(rng,n,length):
    indices=np.empty(n,int);position=0
    while position<n:
        start=int(rng.integers(n));size=min(int(rng.geometric(1./length)),n-position)
        indices[position:position+size]=(start+np.arange(size))%n;position+=size
    return indices


def mdd_per_seed(open_returns,initial):
    values=np.asarray(open_returns,float)
    if values.ndim==1:values=values[:,None]
    first=np.asarray(initial,float).reshape(1,-1)
    if first.shape[1]!=values.shape[1] or not np.isfinite(values).all() or np.any(values<=-1) or np.any(first<=0):
        raise ValueError('MDD_PATH_IDENTITY')
    wealth=np.concatenate([np.ones_like(first),first,first*np.cumprod(1+values,axis=0)],axis=0)
    return np.max(1-wealth/np.maximum.accumulate(wealth,axis=0),axis=0)


def mdd_sensitivity(a,b,initial_a,initial_b,length,replicates=2000):
    a,b=np.asarray(a,float),np.asarray(b,float)
    if a.shape!=b.shape or a.ndim!=2:raise ValueError('PAIRED_MDD_SHAPE')
    observed=float(mdd_per_seed(a,initial_a).mean()-mdd_per_seed(b,initial_b).mean())
    rng=np.random.default_rng(BOOTSTRAP_SEED);values=np.empty(replicates)
    for r in range(replicates):
        indices=stationary_indices(rng,len(a),length)
        values[r]=mdd_per_seed(a[indices],initial_a).mean()-mdd_per_seed(b[indices],initial_b).mean()
    return {'effect':observed,'path_bootstrap_interval95':np.quantile(values,[.025,.975]).tolist(),
            'bootstrap_replicates':replicates,'block_length':length,'scope':'PATH_SENSITIVITY_WITH_FIXED_INITIAL_ENTRY_COST',
            'seed_aggregation':'MDD_PER_SEED_THEN_MEAN'}


def load_paths(root,group,phase):
    root=Path(root);doc=json.loads((root/'freeze.json').read_text());records=[]
    validate_implementation(doc)
    for job in doc['jobs']:
        if job['group']!=group or job['phase']!=phase:continue
        directory=root/'paths'/job['job_id'];path=directory/'result.json'
        result=json.loads(path.read_text())
        if result['job']!=job or result['finance_freeze_sha256']!=digest(root/'freeze.json') or result['status']!='COMPLETE':
            raise ValueError('FINANCIAL_RESULT_IDENTITY')
        if digest(directory/'ledger.json')!=result['ledger_sha256'] or digest(directory/'series.npz')!=result['series_sha256']:
            raise ValueError('HOLDING_PATH_SHA')
        if digest(directory/'resource_ledger.json')!=result['resource_ledger_sha256']:raise ValueError('FINANCIAL_RESOURCE_SHA')
        budget=json.loads((directory/'resource_ledger.json').read_text())
        if budget['active'] or budget['used']['solver_jobs']!=job['solver_jobs'] or budget['used']['solver_cpu_seconds']>job['solver_jobs']*job['solver_cpu_cap']:
            raise ValueError('COMPLETE_SOLVER_ACCOUNTING')
        with np.load(directory/'series.npz',allow_pickle=False) as z:series={k:z[k] for k in z.files}
        if series['date_indices'].tolist()!=job['signal_indices'] or not np.isfinite(series['net_losses']).all():raise ValueError('FINANCIAL_SERIES_COVERAGE')
        if not np.allclose(series['net_returns'],-series['net_losses'],atol=0,rtol=0):raise ValueError('SIMPLE_RETURN_SIGN')
        if abs(float(cvar95(series['net_losses']))-result['summary']['cvar95_net_loss'])>1e-12:raise ValueError('CVAR_RECOMPUTATION')
        if abs(float(mdd_per_seed(series['mdd_open_returns'],series['initial_net_factor'])[0])-result['summary']['maximum_drawdown'])>1e-10:
            raise ValueError('MDD_RECOMPUTATION')
        records.append({'job':job,'result':result,'series':series,'result_sha256':digest(path),'resource':budget['used']})
    return doc,records


def matrix_for(records,method,seeds,cost,lam=None,key='net_losses'):
    chosen=[]
    for seed in seeds:
        rows=[r for r in records if r['job']['method']==method and r['job']['seed']==seed and r['job']['cost']==cost
              and (lam is None or r['result']['lambda']==lam)]
        if len(rows)!=1:raise ValueError('COMPLETE_PAIRED_METHOD_SEEDS')
        chosen.append(rows[0])
    dates=chosen[0]['series']['date_indices']
    if any(not np.array_equal(r['series']['date_indices'],dates) for r in chosen):raise ValueError('PAIRED_DATE_AXIS')
    return np.column_stack([r['series'][key] for r in chosen]),chosen


def select_finance(root,group):
    root=Path(root);cap={'cpu_seconds':2000};budget=phase_budget(root/('selection_'+group+'.budget'),cap)
    budget.admit('select','work',{'cpu_seconds':1999})
    doc,records=load_paths(root,group,'development');reference='classical_factor' if group=='E04' else 'classical'
    candidates=[];losses={}
    for lam in doc['groups'][group]['solver_contract']['lambda_candidates']:
        values,rows=matrix_for(records,reference,doc['seeds'],doc['primary_cost'],lam)
        score=float(cvar95(values,axis=0).mean());losses[lam]=values
        candidates.append({'lambda':lam,'mean_seed_cvar95':score,'per_seed_cvar95':cvar95(values,axis=0).tolist()})
    selected=min(candidates,key=lambda row:(row['mean_seed_cvar95'],row['lambda']))
    proxy=losses[selected['lambda']].mean(axis=1);block=select_block_length(proxy,holding_sessions=1)
    dev_scale=max(float(np.std(proxy,ddof=1)),1e-8)
    scale=abs(selected['mean_seed_cvar95']) if abs(selected['mean_seed_cvar95'])>1e-8 else dev_scale
    budget.finish('select',{})
    record={'schema_version':'qf.stock-financial-selection.v1','group':group,'created_at':utc(),
        'finance_freeze_sha256':digest(root/'freeze.json'),'lambda':selected['lambda'],'primary_cost':doc['primary_cost'],
        'lambda_scores':candidates,'block':block,'adjacent_block_lengths':adjacent_lengths(block['length']),
        'selection_rule':doc['selection'],'bootstrap_seed':BOOTSTRAP_SEED,'bootstrap_replicates':2000,
        'design_effect_absolute_5percent':.05*scale,'design_scale_rule':'absolute classical validation CVaR; near-zero uses validation loss standard deviation',
        'development_result_shas':{r['job']['job_id']:r['result_sha256'] for r in records},
        'development_solver_jobs':sum(r['resource']['solver_jobs'] for r in records),
        'resource_ledger_sha256':digest(budget.path),'evaluation_stage':'DEVELOPMENT_SELECTION_FROZEN'}
    write_json(root/('selection_'+group+'.json'),record)
    return {'group':group,'lambda':record['lambda'],'block_length':block['length'],'sha256':digest(root/('selection_'+group+'.json'))}


def financial_contrasts(group):
    if group=='E04':return [('quantum_mean_vs_classical_mean_cvar','selected_factor','classical_factor'),
                           ('factor_vs_shrinkage_cvar','selected_factor','selected_shrinkage'),
                           ('quantum_risk_vs_factor_cvar','selected_readout_risk','selected_factor')]
    return [('integrated_vs_classical_cvar','full','classical'),('integrated_vs_fixed_map_cvar','full','fixed')]


def report_finance(root,group):
    root=Path(root);selection_path=root/('selection_'+group+'.json');selection=json.loads(selection_path.read_text())
    cap={'cpu_seconds':8000 if group=='E04' else 10000};budget=phase_budget(root/('report_'+group+'.budget'),cap)
    budget.admit('report','work',{'cpu_seconds':cap['cpu_seconds']-1})
    doc,records=load_paths(root,group,'test')
    if selection['finance_freeze_sha256']!=digest(root/'freeze.json') or any(r['result']['selection_sha256']!=digest(selection_path) for r in records):
        raise ValueError('TEST_REQUIRES_PRIOR_SELECTION_SHA')
    length=selection['block']['length'];endpoints=[];comparisons=[];sensitivity=[]
    for cost in doc['groups'][group]['solver_contract']['cost_levels']:
        matrices={};mdd={};initial={}
        for method in METHODS[group]:
            losses,rows=matrix_for(records,method,doc['seeds'],cost,selection['lambda'])
            matrices[method]=losses;mdd[method]=np.column_stack([r['series']['mdd_open_returns'] for r in rows])
            initial[method]=np.array([float(r['series']['initial_net_factor']) for r in rows])
            endpoints.append({'method':method,'cost':cost,'per_seed_cvar95':cvar95(losses,axis=0).tolist(),
                'mean_seed_cvar95':float(cvar95(losses,axis=0).mean()),'per_seed_mean_return':(-losses.mean(axis=0)).tolist(),
                'per_seed_maximum_drawdown':mdd_per_seed(mdd[method],initial[method]).tolist(),
                'per_seed_turnover':[float(r['series']['turnover_fractions'].sum()) for r in rows],
                'per_seed_delayed_exit_count':[r['result']['summary']['delayed_exit_count'] for r in rows],
                'n_calendar_intervals':len(losses),'empirical_tail_mass_per_seed':len(losses)*.05})
        for name,method,reference in financial_contrasts(group):
            inference=paired_inference(matrices[method],matrices[reference],length,endpoint='cvar95',replicates=2000,seed=BOOTSTRAP_SEED)
            record=dict(comparison=name,method=method,reference=reference,cost=cost,**inference)
            if cost==doc['primary_cost']:
                record['block_sensitivity']=[paired_inference(matrices[method],matrices[reference],b,endpoint='cvar95',replicates=2000,seed=BOOTSTRAP_SEED)
                    for b in selection['adjacent_block_lengths'] if b!=length]
                record['mean_return_difference']=paired_inference(-matrices[method],-matrices[reference],length,endpoint='mean',replicates=2000,seed=BOOTSTRAP_SEED)
                record['mdd_path_sensitivity']=mdd_sensitivity(mdd[method],mdd[reference],initial[method],initial[reference],length)
                comparisons.append(record)
            else:sensitivity.append(dict(record,scope='PREDEFINED_COST_SENSITIVITY'))
    holm(comparisons)
    jobs=sum(r['resource']['solver_jobs'] for r in records)
    expected=doc['groups'][group]['solver_contract']['final_jobs']
    if jobs!=expected:raise ValueError('FINAL_SOLVER_COUNT')
    budget.finish('report',{})
    out={'schema_version':'qf.stock-financial-report.v1','group':group,'finance_freeze_sha256':digest(root/'freeze.json'),
        'selection_sha256':digest(selection_path),'lambda':selection['lambda'],'endpoints':endpoints,'primary_comparisons':comparisons,
        'cost_sensitivity':sensitivity,'result_shas':{r['job']['job_id']:r['result_sha256'] for r in records},
        'resource_ledger_sha256':digest(budget.path),'final_solver_jobs':jobs,'final_solver_cpu_seconds':sum(r['resource']['solver_cpu_seconds'] for r in records),
        'final_path_cpu_seconds':sum(r['resource']['cpu_seconds'] for r in records),
        'task_id':doc['task_id'],'data_epoch':doc['data_epoch'],'execution_mode':doc['execution_mode'],'scientific_status':'LOCAL_VERIFIED',
        'hardware_calls':0,'new_quantum_calls':0,'bootstrap_scope':'stationarity, observed dependence and finite tail mass condition paired stationary-block interpretation',
        'portfolio_scope':'fractional long-only adjusted-price total-return proxy; daily-volume and strict-open-limit execution proxy',
        'terminal_scope':'main horizon shared across methods; delayed liquidation tail is reported in each trade ledger'}
    write_json(root/('report_'+group+'.json'),out)
    return {'group':group,'sha256':digest(root/('report_'+group+'.json')),'primary_comparisons':comparisons}


def prediction_errors(arrays):
    """Common asset-date mask over all paired methods and seeds; one statistic per date."""
    if not arrays:raise ValueError('EMPTY_PREDICTION_COLLECTION')
    first=arrays[0];mask=np.ones(first['mu'].shape,bool)
    for a in arrays:
        if not np.array_equal(a['date_indices'],first['date_indices']) or not np.array_equal(a['assets'],first['assets']):raise ValueError('PREDICTION_COMMON_AXIS')
        if not np.allclose(a['labels'],first['labels'],equal_nan=True,atol=0,rtol=0):raise ValueError('COMMON_TARGET_LABEL')
        mask &= a['prediction_mask']&np.isfinite(a['labels'])&np.isfinite(a['mu'])
    counts=mask.sum(axis=1)
    if np.any(counts==0):raise ValueError('COMMON_DATE_LABEL_EMPTY')
    output=[]
    for a in arrays:
        delta=np.where(mask,a['mu']-a['labels'],0.)
        mse=np.sum(delta**2,axis=1)/counts
        output.append({'mae':np.abs(delta).sum(axis=1)/counts,'mse':mse,'rmse':np.sqrt(mse)})
    return output,counts


def prediction_collection(root,phase,seeds):
    keys=[('selected',g) for g in ['self','correlation','directional','reverse','random']]+[('classical',None),('fixed','directional')]
    all_arrays=[];bindings=[];identities=[]
    for family,graph in keys:
        for seed in seeds:
            a,b=read_prediction(root,family,seed,phase,graph)
            all_arrays.append(a);bindings.append(b);identities.append((family,graph,seed))
    errors,counts=prediction_errors(all_arrays)
    values={}
    for family,graph in keys:
        matching=[e for e,key in zip(errors,identities) if key[:2]==(family,graph)]
        name=family+'_'+(graph or 'validation_graph')
        values[name]={metric:np.column_stack([m[metric] for m in matching]) for metric in ['mae','mse','rmse']}
    return values,counts,bindings,all_arrays[0]['date_indices']


def select_prediction(root):
    root=Path(root);doc=json.loads((root/'freeze.json').read_text());cap={'cpu_seconds':2000}
    validate_implementation(doc)
    budget=phase_budget(root/'selection_E03_stats.budget',cap);budget.admit('select','work',{'cpu_seconds':1999})
    values,counts,bindings,dates=prediction_collection(root,'development',doc['seeds'])
    proxy=values['classical_validation_graph']['mae'].mean(axis=1);block=select_block_length(proxy,holding_sessions=1)
    budget.finish('select',{})
    record={'schema_version':'qf.stock-prediction-statistics-selection.v1','finance_freeze_sha256':digest(root/'freeze.json'),
        'phase':'2023_VALIDATION','block':block,'adjacent_block_lengths':adjacent_lengths(block['length']),
        'prediction_bindings':bindings,'date_indices':dates.tolist(),'common_assets_per_date':counts.tolist(),
        'resource_ledger_sha256':digest(budget.path),'bootstrap_seed':BOOTSTRAP_SEED,'created_at':utc()}
    write_json(root/'selection_E03_stats.json',record)
    return {'block_length':block['length'],'sha256':digest(root/'selection_E03_stats.json')}


def report_prediction(root):
    root=Path(root);doc=json.loads((root/'freeze.json').read_text());sp=root/'selection_E03_stats.json';selection=json.loads(sp.read_text())
    validate_implementation(doc)
    if selection['finance_freeze_sha256']!=digest(root/'freeze.json'):raise ValueError('PREDICTION_STATISTICS_SELECTION')
    cap={'cpu_seconds':9000};budget=phase_budget(root/'report_E03_stats.budget',cap);budget.admit('report','work',{'cpu_seconds':8999})
    values,counts,bindings,dates=prediction_collection(root,'test',doc['seeds']);length=selection['block']['length']
    contrasts=[('local_message_vs_validation_classical','classical_validation_graph'),('directional_vs_self','selected_self'),
               ('directional_vs_reverse','selected_reverse'),('directional_vs_random','selected_random')]
    primary=[]
    for name,reference in contrasts:
        a=values['selected_directional'];b=values[reference]
        r=dict(comparison=name,reference=reference,**paired_inference(a['mae'],b['mae'],length,replicates=2000,seed=BOOTSTRAP_SEED))
        r['mse_secondary']=paired_inference(a['mse'],b['mse'],length,replicates=2000,seed=BOOTSTRAP_SEED)
        r['rmse_secondary']=paired_inference(a['rmse'],b['rmse'],length,replicates=2000,seed=BOOTSTRAP_SEED)
        r['block_sensitivity']=[paired_inference(a['mae'],b['mae'],l,replicates=2000,seed=BOOTSTRAP_SEED)
                                for l in selection['adjacent_block_lengths'] if l!=length]
        primary.append(r)
    holm(primary)
    endpoints={name:{metric:{'mean':float(array.mean()),'per_seed':array.mean(axis=0).tolist()} for metric,array in row.items()}
               for name,row in values.items()}
    series_sha=save_npz(root/'E03_prediction_error_paths.npz',date_indices=dates,common_asset_counts=counts,
                       **{name+'__'+metric:array for name,row in values.items() for metric,array in row.items()})
    finite_root=Path(doc['stock_root'])/'finite_shots';finite_path=finite_root/'result.json'
    finite=json.loads(finite_path.read_text())
    for name,key in [('freeze.json','freeze_sha256'),('unit_results.json','unit_results_sha256'),('resource_ledger.json','resource_ledger_sha256')]:
        if digest(finite_root/name)!=finite[key]:raise ValueError('FINITE_SHOT_RESULT_LINEAGE')
    finite_freeze=json.loads((finite_root/'freeze.json').read_text())
    if finite_freeze['stock_freeze_sha256']!=doc['stock_freeze_sha256'] or finite['shots']!=2064384 or finite['new_quantum_evolutions']!=0:
        raise ValueError('FINITE_SHOT_CONTRACT')
    budget.finish('report',{})
    out={'schema_version':'qf.stock-prediction-report.v1','experiment':'E03','finance_freeze_sha256':digest(root/'freeze.json'),
        'selection_sha256':digest(sp),'primary_comparisons':primary,'endpoints':endpoints,'prediction_bindings':bindings,
        'series_sha256':series_sha,'date_indices':dates.tolist(),'common_asset_counts':counts.tolist(),
        'resource_ledger_sha256':digest(budget.path),'data_epoch':doc['data_epoch'],'task_id':doc['task_id'],
        'finite_readout_package':{'path':str(finite_path),'sha256':digest(finite_path),'sampling_mode':finite_freeze['sampling_mode'],
            'units':finite['units'],'shots':finite['shots'],'summaries':finite['summaries'],
            'readout_classical_identity':finite['readout_classical_identity'],'inference_scope':finite['inference_scope']},
        'evaluation_mask':'intersection of signal feature validity and observed one-session labels across all paired arrays',
        'seed_aggregation':'date statistic for each seed, temporal endpoint per seed, arithmetic seed mean',
        'scientific_status':'LOCAL_VERIFIED','execution_mode':'PAIRED_STATISTICS_OF_LOCAL_CLASSICAL_AND_SDK_PREDICTIONS','hardware_calls':0}
    write_json(root/'report_E03_stats.json',out)
    return {'sha256':digest(root/'report_E03_stats.json'),'primary_comparisons':primary}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['select-finance','report-finance','select-prediction','report-prediction'])
    p.add_argument('--finance-root',type=Path,required=True);p.add_argument('--group',choices=['E04','E07'])
    a=p.parse_args()
    if a.mode in ['select-finance','report-finance'] and a.group is None:p.error('--group is required for finance modes')
    if a.mode=='select-finance':r=select_finance(a.finance_root,a.group)
    elif a.mode=='report-finance':r=report_finance(a.finance_root,a.group)
    elif a.mode=='select-prediction':r=select_prediction(a.finance_root)
    else:r=report_prediction(a.finance_root)
    print(json.dumps(r,ensure_ascii=False))


if __name__=='__main__':main()
