"""Calendar holdings, frozen risk-head ablation and budgeted stock portfolio paths.

Returns and quantities use the provider-adjusted total-return proxy; executable
orders use original open, daily volume and strict reported price limits.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import resource
import time

import numpy as np
from sklearn.linear_model import SGDRegressor
from sklearn.preprocessing import StandardScaler
from .bridge import solve, objective
from .research_common import Budget,digest,identity,write_json,utc,cvar95

METHODS={
 'E04':{'classical_factor':('classical','factor'),'selected_factor':('selected','factor'),
        'selected_shrinkage':('selected','shrinkage'),'selected_readout_risk':('selected','readout')},
 'E07':{'full':('selected','factor'),'classical':('classical','factor'),'fixed':('fixed','factor')}}


def save_npz(path,**arrays):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():raise FileExistsError('ARTIFACT_ALREADY_EXISTS')
    np.savez_compressed(path,**arrays)
    if path.stat().st_size>8388608:raise ValueError('NPZ_SHARD_LIMIT')
    return digest(path)


def phase_budget(root,cap,phase='work'):
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    budget=Budget(root,cap,{phase:cap})
    return budget


def validate_implementation(doc):
    for parent in doc['implementation']:
        if digest(Path(__file__).with_name(parent['name']))!=parent['sha256']:
            raise ValueError('FINANCIAL_IMPLEMENTATION_CHANGED:'+parent['name'])


def validate_finance_freeze(doc,stock_freeze,data_manifest):
    if doc['stock_freeze_sha256']!=digest(stock_freeze) or doc['data_manifest_sha256']!=digest(data_manifest):
        raise ValueError('FINANCIAL_PARENT_IDENTITY')
    validate_implementation(doc)


def load_finance_panels(root):
    """Verified execution fields only, with a bounded allocation on the union axis."""
    root=Path(root);manifest=json.loads((root/'manifest.json').read_text())
    shape=(len(manifest['dates']),len(manifest['assets']))
    fields=['open','close','adj_factor','vol','up_limit','down_limit']
    data={k:np.empty(shape,float) for k in fields};offset=0;seen=[]
    for shard in manifest['shards']:
        path=root/shard['path']
        if digest(path)!=shard['sha256']:raise ValueError('PANEL_HASH')
        with np.load(path,allow_pickle=False) as z:
            np.testing.assert_array_equal(z['assets'],manifest['assets'])
            dates=z['dates'];n=len(dates);seen.extend(dates.astype(str).tolist())
            for k in fields:data[k][offset:offset+n]=z[k]
            offset+=n
    if offset!=shape[0] or seen!=list(manifest['dates']):raise ValueError('PANEL_CALENDAR')
    data.update(dates=np.array(seen),assets=np.array(manifest['assets']),manifest=manifest)
    return data


def entry_eligibility(day,pending,minimum=60):
    """Signal-time qualification; observed future labels enter error reports only."""
    return (day['feature_valid']&(day['history_observations']>=minimum)
            &np.array([int(a) not in pending for a in day['asset_indices']]))


def freeze(stock_root,data_root,campaign_path,output):
    stock_root,data_root,output=map(Path,[stock_root,data_root,output])
    st=json.loads((stock_root/'freeze.json').read_text());meta=json.loads((data_root/'manifest.json').read_text())
    campaign=json.loads(Path(campaign_path).read_text())
    if st['data_manifest_sha256']!=digest(data_root/'manifest.json') or meta['universe_visibility']!='PREVIOUS_MONTH_SNAPSHOT':
        raise ValueError('STOCK_TASK_SCOPE')
    final=next(f for f in st['folds'] if f['id']=='final');jobs=[];groups={}
    for group in ['E04','E07']:
        contract=st['solver_'+group];cap=contract['cpu_seconds_cap_per_job']
        expected_cap=min(60,contract['reserved_cpu_seconds']//contract['total_jobs'])
        if type(cap) is not int or cap<1 or cap!=expected_cap:raise ValueError('BUDGET_INFEASIBLE')
        roles={'development':final['validation'],'test':final['evaluation']}
        allocations={'development':0,'test':0};group_jobs=[]
        for phase,indices in roles.items():
            if not indices or np.any(np.diff(indices)!=1):raise ValueError('CONTIGUOUS_FINANCIAL_CALENDAR')
            lambdas=contract['lambda_candidates'] if phase=='development' else ['VALIDATION_SELECTED']
            costs=[contract['primary_cost']] if phase=='development' else contract['cost_levels']
            for method in METHODS[group]:
                for seed in st['final_seeds']:
                    for lam in lambdas:
                        for cost in costs:
                            label=f'{lam:g}' if isinstance(lam,(float,int)) else 'selected'
                            job={'job_id':f'{group}_{phase}_{method}_s{seed}_l{label}_c{round(cost*1e6):06d}',
                                 'group':group,'phase':phase,'method':method,'seed':seed,'lambda':lam,'cost':cost,
                                 'signal_indices':indices,'solver_jobs':len(indices),'solver_cpu_cap':cap}
                            group_jobs.append(job);allocations[phase]+=len(indices)
        if allocations['development']!=contract['development_jobs'] or allocations['test']!=contract['final_jobs']:
            raise ValueError('SOLVER_JOB_ENUMERATION')
        overhead_total=28800 if group=='E04' else 100800
        overhead_per_path=overhead_total//len(group_jobs)
        for job in group_jobs:
            job['overhead_cpu_seconds']=overhead_per_path
            job['cpu_seconds_cap']=job['solver_jobs']*cap+overhead_per_path
            job['memory_mib_cap']=1024
        jobs.extend(group_jobs)
        groups[group]={'solver_contract':contract,'path_jobs':len(group_jobs),'solver_phase_counts':allocations,
            'reserved_solver_cpu_seconds':contract['reserved_cpu_seconds'],
            'overhead_cpu_seconds':overhead_per_path*len(group_jobs),'risk_head_cpu_seconds':12000 if group=='E04' else 0,
            'preparation_and_statistics_cpu_seconds':12000,'total_cpu_seconds_cap':172800}
        if sum(groups[group][k] for k in ['reserved_solver_cpu_seconds','overhead_cpu_seconds','risk_head_cpu_seconds','preparation_and_statistics_cpu_seconds'])>172800:
            raise ValueError('GROUP_COMPLETE_BUDGET')
    doc={'schema_version':'qf.stock-finance-freeze.v1','created_at':utc(),'financial_endpoints_evaluated':0,
         'campaign_id':campaign['campaign_id'],'source_commit':campaign['source_commit'],
         'stock_root':str(stock_root.resolve()),
         'data_epoch':st['data_epoch'],'task_id':'STOCK_MONTHLY_300_NEXT_MONTH_VISIBLE',
         'stock_freeze_sha256':digest(stock_root/'freeze.json'),'data_manifest_sha256':digest(data_root/'manifest.json'),
         'campaign_sha256':digest(campaign_path),'methods':METHODS,'jobs':jobs,'groups':groups,
         'training_indices':final['train'],'development_indices':final['validation'],'test_indices':final['evaluation'],
         'seeds':st['final_seeds'],'k':5,'history_observations_min':60,'primary_cost':.001,
         'trade_rule':'NET_REBALANCE_AT_OPEN_WITH_LOCKED_HOLDINGS_AND_CASH_SLOTS',
         'entry_gate':'signal feature_valid and >=60 historical open-return observations; submitted names execute only at positive raw open/volume strictly inside both price limits',
         'prior_holdings_rule':'actual units at signal close for Bridge; actual units at next open for net fills',
         'pending_exit_rule':'failed full exits remain occupied and excluded from new signals until a tradable open exits them',
         'slot_priority':'pending/blocked holdings first, then fixed signal marginal-objective rank with asset identity tie break',
         'weight_rule':'target post-cost NAV/K; retained blocked drift may reduce free allocation uniformly; remaining value cash',
         'quantity_model':'fractional adjusted-price units; original-share equivalent=units*provider adjustment factor',
         'cost_rule':'single-side cost rate times actual absolute net traded value; unchanged retained quantity has zero transaction cost',
         'mark_rule':'current adjusted open; missing opening mark carries latest previously observed close/open mark',
         'first_interval_cost':'first entry fee joins first holding interval; subsequent opening fees enter the interval ending at that opening',
         'terminal_rule':'common final planned exit open; blocked positions remain marked at that horizon; subsequent liquidation attempts form a separate tail',
         'risk_head':{'input':'E03 final_selected_directional frozen readout','target':'one-session factor-projected return innovation squared',
             'innovation_center':'preceding126 observed adjusted open returns ending at signal-day open',
             'factor_projection':'least squares using signal Lambda and assets with mature observed target; rank must equal3',
             'fit_indices':final['train'],'label_maturity_max':'20221231','epochs':40,'alpha':.01,'eta0':.001,
             'learning_rate':'invscaling','target_scale':'training mean squared residual','floor':1e-8,
             'covariance':'same signal Lambda @ F @ Lambda.T + diag(predicted d)',
             'cpu_seconds_per_seed':2400,'quantum_calls':0,'shots':0},
         'selection':{'lambda':'minimum mean of per-seed classical validation CVaR95 at primary cost; numeric lambda tie break',
             'block':'2023 selected-lambda classical net-loss seed-mean proxy; significant ACF grid 1/5/10/20/40/80',
             'bootstrap':'paired stationary time blocks,2000 replicates,endpoint per seed then mean',
             'primary':'CVaR95 net-loss difference; Holm within group primary comparison family',
             'mdd':'complete marked NAV path; bootstrap path sensitivity includes initial transaction cost separately'},
         'prediction_partition_rule':'producer partitions frozen predictions into validation/test artifacts without endpoint calculation; downstream test phase requires selection SHA',
         'E03_statistics_cpu_seconds':{'finite_readout_package':1000,'development_block_selection':2000,'prediction_report':9000},
         'execution_mode':'CLASSICAL_HOLDINGS_ON_CLASSICAL_OR_LOCAL_SDK_FROZEN_PREDICTIONS',
         'hardware_calls':0,'new_quantum_calls':0,
         'implementation':[{'name':name,'sha256':digest(Path(__file__).with_name(name))} for name in
            ['stock_finance.py','stock_report.py','stock_task.py','stock_heads.py','stock_cache.py','bridge.py','research_common.py']]}
    write_json(output/'freeze.json',doc)
    return {'path':str(output/'freeze.json'),'sha256':digest(output/'freeze.json'),'path_jobs':len(jobs)}


def partition_predictions(stock_root,finance_root):
    stock_root,finance_root=map(Path,[stock_root,finance_root])
    doc=json.loads((finance_root/'freeze.json').read_text());st=json.loads((stock_root/'freeze.json').read_text())
    validate_implementation(doc)
    if digest(stock_root/'freeze.json')!=doc['stock_freeze_sha256']:raise ValueError('PARTITION_STOCK_FREEZE')
    output=finance_root/'prediction_partitions';cap={'cpu_seconds':2000};budget=phase_budget(output,cap)
    budget.admit('partition','work',{'cpu_seconds':1999})
    rows=[]
    for family in ['selected','classical','fixed']:
        selection_path=stock_root/'heads'/('selection_'+family+'.json');selection=json.loads(selection_path.read_text())
        if selection['stock_freeze_sha256']!=doc['stock_freeze_sha256']:raise ValueError('HEAD_SELECTION_PARENT')
        graphs=st['graphs'] if family=='selected' else [selection['graph']]
        for graph in graphs:
            for seed in doc['seeds']:
                key=f'{family}_{graph}_seed{seed}';source=stock_root/'predictions'/key
                manifest=json.loads((source/'manifest.json').read_text());path=source/'prediction.npz'
                if (manifest['family'],manifest['graph'],manifest['seed'])!=(family,graph,seed):raise ValueError('PREDICTION_PRODUCER_IDENTITY')
                if digest(path)!=manifest['prediction_sha256'] or manifest['selection_sha256']!=digest(selection_path):raise ValueError('PREDICTION_SHA')
                with np.load(path,allow_pickle=False) as z:arrays={k:z[k] for k in z.files}
                for phase,indices in [('development',doc['development_indices']),('test',doc['test_indices'])]:
                    positions=np.flatnonzero(np.isin(arrays['date_indices'],indices))
                    if arrays['date_indices'][positions].tolist()!=indices:raise ValueError('PREDICTION_PHASE_COVERAGE')
                    target=output/key/(phase+'.npz')
                    sha=save_npz(target,**{k:v[positions] for k,v in arrays.items()})
                    rows.append({'family':family,'graph':graph,'seed':seed,'phase':phase,'path':str(target.relative_to(output)),
                        'sha256':sha,'source_manifest_sha256':digest(source/'manifest.json'),'source_prediction_sha256':digest(path),
                        'date_indices':indices,'mean_unit':manifest['mean_unit'],'horizon_sessions':manifest['horizon_sessions']})
    budget.finish('partition',{})
    write_json(output/'manifest.json',{'schema_version':'qf.stock-prediction-partitions.v1','finance_freeze_sha256':digest(finance_root/'freeze.json'),
        'records':rows,'resource_ledger_sha256':digest(budget.path),'operation':'ROW_PARTITION_WITHOUT_ENDPOINT_CALCULATION','created_at':utc()})
    return {'records':len(rows),'sha256':digest(output/'manifest.json')}


def read_prediction(finance_root,family,seed,phase,graph=None):
    root=Path(finance_root)/'prediction_partitions';manifest=json.loads((root/'manifest.json').read_text())
    if manifest['finance_freeze_sha256']!=digest(Path(finance_root)/'freeze.json'):raise ValueError('PARTITION_FREEZE_SHA')
    rows=[r for r in manifest['records'] if r['family']==family and r['seed']==seed and r['phase']==phase and (graph is None or r['graph']==graph)]
    if len(rows)!=1:raise ValueError('PREDICTION_FAMILY_IDENTITY')
    row=rows[0];path=root/row['path']
    if digest(path)!=row['sha256'] or row['mean_unit']!='SIMPLE_RETURN' or row['horizon_sessions']!=1:raise ValueError('PREDICTION_UNIT_OR_SHA')
    with np.load(path,allow_pickle=False) as z:arrays={k:z[k] for k in z.files}
    return arrays,row


def graph_lookup(stock_root):
    root=Path(stock_root);manifest=json.loads((root/'graph_manifest.json').read_text())
    if manifest['freeze_sha256']!=digest(root/'freeze.json'):raise ValueError('GRAPH_FREEZE_SHA')
    return {r['index']:r for r in manifest['records']}


def read_graph(stock_root,record):
    path=Path(stock_root)/record['path']
    if digest(path)!=record['sha256']:raise ValueError('GRAPH_DAY_SHA')
    with np.load(path,allow_pickle=False) as z:return {k:z[k] for k in z.files}


def read_observation(stock_root,cache_records,index,day_record):
    root=Path(stock_root)/'caches/final_selected_directional';record=cache_records[index];path=root/record['path']
    if digest(path)!=record['sha256'] or record['parent_day_sha256']!=day_record['sha256']:raise ValueError('RISK_CACHE_LINEAGE')
    with np.load(path,allow_pickle=False) as z:return z['assets'],z['readout']


def risk_head(stock_root,data_root,finance_root,seed):
    stock_root,data_root,finance_root=map(Path,[stock_root,data_root,finance_root])
    doc=json.loads((finance_root/'freeze.json').read_text());validate_finance_freeze(doc,stock_root/'freeze.json',data_root/'manifest.json')
    if seed not in doc['seeds']:raise ValueError('RISK_SEED')
    out=finance_root/'risk_heads'/f'seed{seed}'
    cap={'cpu_seconds':2400};budget=phase_budget(out,cap)
    budget.admit('fit','work',{'cpu_seconds':1399})
    data=load_finance_panels(data_root);days=graph_lookup(stock_root)
    cache_root=stock_root/'caches/final_selected_directional';cm=json.loads((cache_root/'manifest.json').read_text())
    cache_records={r['index']:r for r in cm['records']};features=[];targets=[];lineage=[]
    for index in doc['training_indices']:
        if str(data['dates'][index+2])>doc['risk_head']['label_maturity_max']:raise ValueError('RISK_LABEL_MATURITY')
        day=read_graph(stock_root,days[index]);assets,readout=read_observation(stock_root,cache_records,index,days[index])
        np.testing.assert_array_equal(assets,day['assets'])
        mask=day['feature_valid']&day['label_valid']&(day['history_observations']>=60)
        axis=day['asset_indices'];adjusted=data['open'][index-126:index+1][:,axis]*data['adj_factor'][index-126:index+1][:,axis]
        historical=adjusted[1:]/adjusted[:-1]-1
        center=np.nanmean(historical,axis=0)
        mask=mask&np.isfinite(center)&np.isfinite(day['labels'])
        loading=day['Lambda'][mask];innovation=day['labels'][mask]-center[mask]
        factor,_,rank,_=np.linalg.lstsq(loading,innovation,rcond=None)
        if rank!=3:raise ValueError('RISK_TARGET_FACTOR_RANK')
        residual=innovation-loading@factor
        features.append(readout[mask]);targets.append(residual**2)
        lineage.append({'index':index,'signal_date':str(data['dates'][index]),'label_maturity_date':str(data['dates'][index+2]),
                        'rows':int(mask.sum()),'day_sha256':days[index]['sha256'],'cache_sha256':cache_records[index]['sha256']})
    x,y=np.vstack(features),np.concatenate(targets);scaler=StandardScaler().fit(x);scale=max(float(y.mean()),1e-8)
    model=SGDRegressor(loss='squared_error',penalty='l2',alpha=.01,max_iter=40,tol=None,eta0=.001,
                       learning_rate='invscaling',power_t=.25,random_state=seed,shuffle=True)
    model.fit(scaler.transform(x),y/scale)
    if not np.isfinite(model.coef_).all():raise ValueError('RISK_HEAD_FINITE')
    model_sha=save_npz(out/'model.npz',coef=model.coef_,intercept=model.intercept_,xmean=scaler.mean_,xscale=scaler.scale_,target_scale=np.array(scale))
    budget.finish('fit',{})
    outputs=[]
    for phase,indices in [('development',doc['development_indices']),('test',doc['test_indices'])]:
        budget.admit(phase,'work',{'cpu_seconds':399})
        values=[];axes=[]
        for index in indices:
            assets,readout=read_observation(stock_root,cache_records,index,days[index])
            predicted=np.maximum(model.predict(scaler.transform(readout))*scale,1e-8)
            if not np.isfinite(predicted).all():raise ValueError('RISK_PREDICTION_FINITE')
            values.append(predicted);axes.append(assets)
        sha=save_npz(out/(phase+'.npz'),d=np.stack(values),assets=np.stack(axes),date_indices=np.array(indices))
        outputs.append({'phase':phase,'path':phase+'.npz','sha256':sha,'date_indices':indices})
        budget.finish(phase,{})
    write_json(out/'manifest.json',{'schema_version':'qf.frozen-readout-risk-head.v1','finance_freeze_sha256':digest(finance_root/'freeze.json'),
        'seed':seed,'data_epoch':doc['data_epoch'],'model_sha256':model_sha,'cache_manifest_sha256':digest(cache_root/'manifest.json'),
        'training':lineage,'training_rows':len(y),'target_mean':scale,'outputs':outputs,'new_quantum_calls':0,'shots':0,
        'risk_unit':'ONE_SESSION_SIMPLE_RETURN_VARIANCE','resource_ledger_sha256':digest(budget.path),'scientific_status':'LOCAL_VERIFIED'})
    return {'seed':seed,'sha256':digest(out/'manifest.json')}


def prepare_market(data):
    opening=np.asarray(data['open'],float)*np.asarray(data['adj_factor'],float)
    close=np.asarray(data['close'],float)*np.asarray(data['adj_factor'],float)
    opening=np.where(np.isfinite(opening)&(opening>0),opening,np.nan)
    close=np.where(np.isfinite(close)&(close>0),close,np.nan)
    marked=np.full_like(close,np.nan);open_marks=np.full_like(close,np.nan)
    close_age=np.full(close.shape,-1,int);open_age=np.full(close.shape,-1,int)
    previous=np.full(close.shape[1],np.nan);previous_age=np.full(close.shape[1],-1,int)
    for index in range(len(close)):
        ov=np.isfinite(opening[index]);cv=np.isfinite(close[index])
        open_marks[index]=np.where(ov,opening[index],previous)
        open_age[index]=np.where(ov,index,previous_age)
        available=cv|ov;previous=np.where(cv,close[index],np.where(ov,opening[index],previous))
        previous_age=np.where(available,index,previous_age)
        marked[index]=previous;close_age[index]=previous_age
    return dict(data,adjusted_open=opening,mark_open=open_marks,mark_close=marked,open_mark_index=open_age,close_mark_index=close_age)


def can_trade(market,index,asset):
    price=float(market['open'][index,asset]);factor=float(market['adj_factor'][index,asset])
    volume=float(market['vol'][index,asset]);upper=float(market['up_limit'][index,asset]);lower=float(market['down_limit'][index,asset])
    return bool(np.isfinite([price,factor,volume,upper,lower]).all() and price>0 and factor>0 and volume>0
                and lower>0 and upper>lower and lower<price<upper)


def nav_at(market,index,positions,cash,when):
    marks=market['mark_'+when][index]
    if any(not np.isfinite(marks[a]) or marks[a]<=0 for a in positions):raise ValueError('HELD_MARK_UNAVAILABLE')
    values={a:float(units*marks[a]) for a,units in positions.items()}
    nav=float(cash+sum(values.values()))
    if nav<=0 or not math.isfinite(nav):raise ValueError('POSITIVE_NAV')
    return nav,values


def execute_open(market,index,positions,cash,desired,pending,cost,k=5):
    """Net fills with fixed-point transaction costs and blocked-name slot ownership."""
    if not 0<=cost<=.01 or k<1:raise ValueError('EXECUTION_PARAMETERS')
    positions=dict(positions);pending=dict(pending);desired=list(dict.fromkeys(map(int,desired)))
    before,old=nav_at(market,index,positions,cash,'open')
    tradable={a:can_trade(market,index,a) for a in set(desired)|set(old)}
    locked={a for a in old if not tradable[a]}
    exits=[];blocked_entries=[]
    for a in old:
        if a not in desired and not tradable[a]:
            since=pending.setdefault(a,index)
            exits.append({'asset_index':a,'first_failed_exit_index':since,'attempt_index':index,'value':old[a]})
    # Full pending exits have first priority when tradability returns; their
    # names are excluded from subsequent signal plans until the position clears.
    desired=[a for a in desired if a not in pending]
    chosen=[]
    for a in desired:
        if a in locked:continue
        if not tradable[a]:blocked_entries.append(a);continue
        if len(chosen)>=k-len(locked):break
        chosen.append(a)
    locked_value=sum(old[a] for a in locked)
    traded_assets=sorted((set(old)|set(chosen))-locked)
    def target_values(nav):
        each=min(nav/k,max(0.,(nav-locked_value)/len(chosen))) if chosen else 0.
        return {a:(each if a in chosen else 0.) for a in traded_assets}
    lower,upper=0.,before
    for _ in range(70):
        middle=(lower+upper)/2;target=target_values(middle)
        fees=cost*sum(abs(target[a]-old.get(a,0.)) for a in traded_assets)
        if middle+fees>before:upper=middle
        else:lower=middle
    after_target=(lower+upper)/2;target=target_values(after_target)
    delta={a:target[a]-old.get(a,0.) for a in traded_assets};trades=[]
    for a in sorted(traded_assets,key=lambda a:(delta[a]>0,a)):
        amount=float(delta[a])
        if abs(amount)<=1e-13*before:continue
        raw=float(market['open'][index,a]);factor=float(market['adj_factor'][index,a]);price=raw*factor
        fee=cost*abs(amount);cash-=amount+fee
        units=positions.get(a,0.)+amount/price
        if units<=1e-12*max(1.,positions.get(a,0.)):positions.pop(a,None);pending.pop(a,None)
        else:positions[a]=units
        trades.append({'asset_index':a,'side':'BUY' if amount>0 else 'SELL','value':abs(amount),'cost':fee,
                       'adjusted_units':abs(amount)/price,'original_share_equivalent':abs(amount)/raw,
                       'raw_open':raw,'adjustment_factor':factor})
        if cash < -1e-10*before:raise ValueError('NEGATIVE_CASH_DURING_SETTLEMENT')
    cash=max(0.,float(cash));after,values=nav_at(market,index,positions,cash,'open')
    fees=sum(r['cost'] for r in trades);turnover=sum(r['value'] for r in trades)
    if abs(after-(before-fees))>1e-10*before or len(positions)>k or abs(cash+sum(values.values())-after)>1e-10*before:
        raise ValueError('CASH_COST_WEIGHT_CONSERVATION')
    row={'open_index':index,'date':str(market['dates'][index]),'nav_before':before,'nav_after':after,'cash':cash,
         'cash_weight':cash/after,'holdings':[{'asset_index':a,'asset':str(market['assets'][a]),'units':positions[a],
             'value':values[a],'weight':values[a]/after,'mark_source_index':int(market['open_mark_index'][index,a])} for a in sorted(positions)],
         'planned_priority':desired,'actual_new_target_names':chosen,'trades':trades,'cost':fees,'turnover':turnover,
         'delayed_exits':exits,'pending_exit_names':sorted(pending),'blocked_entry_names':blocked_entries,
         'locked_names':sorted(locked),'stale_mark_names':[a for a in positions if market['open_mark_index'][index,a]<index]}
    return positions,cash,pending,row


def simulate_path(market,signal_indices,planner,cost,k=5,tail=True):
    if not signal_indices or np.any(np.diff(signal_indices)!=1):raise ValueError('SIGNAL_CALENDAR_CONTINUITY')
    positions={};cash=1.;pending={};opening=[];signals=[]
    for index in signal_indices:
        nav,values=nav_at(market,index,positions,cash,'close')
        plan=planner(index,positions,pending,nav,values)
        signals.append(plan)
        positions,cash,pending,row=execute_open(market,index+1,positions,cash,plan['priority_global'],pending,cost,k)
        row['signal_index']=index;opening.append(row)
    terminal=signal_indices[-1]+2
    positions,cash,pending,row=execute_open(market,terminal,positions,cash,[],pending,cost,k)
    row['signal_index']=None;row['purpose']='COMMON_TERMINAL_EXIT';opening.append(row)
    intervals=[]
    for offset,index in enumerate(signal_indices):
        entry,end=opening[offset],opening[offset+1]
        denominator=1. if offset==0 else entry['nav_after']
        initial_cost=entry['cost'] if offset==0 else 0.
        fees=initial_cost+end['cost'];turnover=(entry['turnover'] if offset==0 else 0.)+end['turnover']
        net=end['nav_after']/denominator-1
        gross=(end['nav_before']-entry['nav_after'])/denominator
        if abs(net-(gross-fees/denominator))>1e-10:raise ValueError('INTERVAL_PROFIT_AND_COST')
        intervals.append({'signal_index':index,'signal_date':str(market['dates'][index]),
            'entry_date':entry['date'],'exit_date':end['date'],'net_return':net,'net_loss':-net,
            'gross_return':gross,'cost_fraction':fees/denominator,'cost_value':fees,
            'turnover_fraction':turnover/denominator,'turnover_value':turnover,
            'nav_denominator':denominator,'nav_end':end['nav_after'],
            'open_to_open_after_entry_return':end['nav_after']/entry['nav_after']-1})
    returns=np.array([r['net_return'] for r in intervals]);wealth=np.r_[1.,[r['nav_after'] for r in opening]]
    if abs(np.prod(1+returns)-wealth[-1])>1e-9:raise ValueError('COMPOUNDING_CONSERVATION')
    if abs(sum(r['cost_value'] for r in intervals)-sum(r['cost'] for r in opening))>1e-10:raise ValueError('SINGLE_COST_ALLOCATION')
    terminal_positions=[dict(h) for h in opening[-1]['holdings']];tail_rows=[]
    if tail:
        for index in range(terminal+1,len(market['dates'])):
            if not positions:break
            positions,cash,pending,row=execute_open(market,index,positions,cash,[],pending,cost,k)
            row['purpose']='POST_HORIZON_LIQUIDATION';tail_rows.append(row)
    return {'signal_records':signals,'opening_records':opening,'interval_records':intervals,'nav_path':wealth.tolist(),
            'terminal_positions_at_primary_horizon':terminal_positions,'liquidation_tail':tail_rows,
            'remaining_positions_after_observed_tail':[{'asset_index':a,'units':v} for a,v in positions.items()],
            'summary':{'cvar95_net_loss':float(cvar95(-returns)),'mean_net_return':float(returns.mean()),
                'maximum_drawdown':float(np.max(1-wealth/np.maximum.accumulate(wealth))),
                'terminal_nav':float(wealth[-1]),'primary_cost_value':sum(r['cost'] for r in opening),
                'primary_turnover_value':sum(r['turnover'] for r in opening),
                'delayed_exit_count':sum(len(r['delayed_exits']) for r in opening),
                'liquidation_tail_cost_value':sum(r['cost'] for r in tail_rows)}}


def run_path(stock_root,data_root,finance_root,job_id):
    stock_root,data_root,finance_root=map(Path,[stock_root,data_root,finance_root]);started=time.process_time()
    doc=json.loads((finance_root/'freeze.json').read_text());validate_finance_freeze(doc,stock_root/'freeze.json',data_root/'manifest.json')
    jobs=[j for j in doc['jobs'] if j['job_id']==job_id]
    if len(jobs)!=1:raise ValueError('FINANCIAL_JOB_ID')
    job=jobs[0];lam=job['lambda'];selection_sha=None
    if job['phase']=='test':
        sp=finance_root/('selection_'+job['group']+'.json');selection=json.loads(sp.read_text())
        if selection['finance_freeze_sha256']!=digest(finance_root/'freeze.json'):raise ValueError('FINANCIAL_SELECTION_PARENT')
        lam=selection['lambda'];selection_sha=digest(sp)
    out=finance_root/'paths'/job_id
    cap={'cpu_seconds':job['cpu_seconds_cap'],'solver_cpu_seconds':job['solver_jobs']*job['solver_cpu_cap'],'solver_jobs':job['solver_jobs']}
    budget=phase_budget(out,cap,'execution');budget.cpu_start=started
    family,risk=METHODS[job['group']][job['method']]
    predictions,prediction_record=read_prediction(finance_root,family,job['seed'],job['phase'],'directional' if family in ['selected','fixed'] else None)
    if predictions['date_indices'].tolist()!=job['signal_indices']:raise ValueError('FINANCIAL_PREDICTION_DATES')
    classic,classic_record=read_prediction(finance_root,'classical',job['seed'],job['phase'])
    data=load_finance_panels(data_root);market=prepare_market(data);days=graph_lookup(stock_root)
    predicted_d=None;risk_manifest_sha=None
    if risk=='readout':
        rr=finance_root/'risk_heads'/f"seed{job['seed']}";rm=json.loads((rr/'manifest.json').read_text())
        if rm['finance_freeze_sha256']!=digest(finance_root/'freeze.json'):raise ValueError('RISK_HEAD_FREEZE')
        rp=next(r for r in rm['outputs'] if r['phase']==job['phase'])
        if digest(rr/rp['path'])!=rp['sha256']:raise ValueError('RISK_HEAD_PREDICTION_SHA')
        with np.load(rr/rp['path'],allow_pickle=False) as z:predicted_d={k:z[k] for k in z.files}
        if predicted_d['date_indices'].tolist()!=job['signal_indices']:raise ValueError('RISK_HEAD_PREDICTION_DATES')
        risk_manifest_sha=digest(rr/'manifest.json')
    position_map={index:n for n,index in enumerate(job['signal_indices'])}
    write_json(out/'intent.json',{'job':job,'lambda':lam,'finance_freeze_sha256':digest(finance_root/'freeze.json'),
        'selection_sha256':selection_sha,'prediction_source':prediction_record,'risk_head_manifest_sha256':risk_manifest_sha,'created_at':utc()})
    def planner(index,positions,pending,nav,held_values):
        row=position_map[index];record=days[index];day=read_graph(stock_root,record)
        np.testing.assert_array_equal(day['assets'],predictions['assets'][row]);np.testing.assert_array_equal(day['assets'],classic['assets'][row])
        axis=day['asset_indices'];mu=predictions['mu'][row];factor_sigma=day['Sigma']
        sigma=factor_sigma if risk=='factor' else day['shrinkage_Sigma']
        if predicted_d is not None:
            np.testing.assert_array_equal(day['assets'],predicted_d['assets'][row])
            sigma=day['Lambda']@day['F']@day['Lambda'].T+np.diag(predicted_d['d'][row])
        if not np.isfinite(mu).all() or not np.isfinite(sigma).all():raise ValueError('FINANCIAL_MODEL_FINITE')
        if np.max(np.abs(sigma-sigma.T))>1e-8 or np.diag(sigma).min()<0:raise ValueError('FINANCIAL_RISK_AXIS')
        previous=np.array([held_values.get(int(a),0.)/nav for a in axis])
        eligible=entry_eligibility(day,pending,doc['history_observations_min'])
        atom='signal_'+str(index);worst={'cpu_seconds':job['solver_cpu_cap']+job['overhead_cpu_seconds']/job['solver_jobs']*.8,
            'solver_cpu_seconds':job['solver_cpu_cap'],'solver_jobs':1}
        budget.admit(atom,'execution',worst);start=time.process_time()
        x,solver=solve(mu,sigma,5,float(lam),job['cost'],previous,eligible,job['solver_cpu_cap'],4)
        elapsed=time.process_time()-start
        if elapsed>job['solver_cpu_cap']:raise ValueError('ATOMIC_SOLVER_CPU_CAP')
        marginal=-mu/5+float(lam)*np.diag(sigma)/25+job['cost']*(np.abs(.2-previous)-previous)
        selected=np.flatnonzero(x);priority=sorted(selected,key=lambda p:(float(marginal[p]),str(day['assets'][p])))
        outside=sum(v for a,v in held_values.items() if a not in set(map(int,axis)))/nav
        common_score=objective(x,classic['mu'][row],factor_sigma,5,float(lam),job['cost'],previous)+job['cost']*outside
        budget.finish(atom,{'solver_cpu_seconds':elapsed,'solver_jobs':1})
        return {'signal_index':index,'signal_date':str(data['dates'][index]),'priority_global':[int(axis[p]) for p in priority],
            'target_assets':[str(day['assets'][p]) for p in priority],'signal_nav':nav,'eligible_count':int(eligible.sum()),
            'previous_weights_signal':[{'asset_index':int(a),'weight':v/nav} for a,v in held_values.items()],
            'outside_active_axis_weight':outside,'solver':solver,'mu_sha256':identity(mu.tolist()),'Sigma_sha256':identity(sigma.tolist()),
            'day_sha256':record['sha256'],'common_coefficient_objective':common_score,
            'common_objective_scope':'classical mu and factor Sigma, method-specific actual previous holdings; bounded solver score'}
    result=simulate_path(market,job['signal_indices'],planner,job['cost'],5,tail=True)
    budget.save()
    if budget.used['cpu_seconds']>cap['cpu_seconds'] or resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024>job['memory_mib_cap']:
        raise ValueError('FINANCIAL_PATH_RESOURCE_CAP')
    path_sha=write_json(out/'ledger.json',result)
    records=result['interval_records']
    arrays={'date_indices':np.array(job['signal_indices']),'net_returns':np.array([r['net_return'] for r in records]),
            'net_losses':np.array([r['net_loss'] for r in records]),'cost_fractions':np.array([r['cost_fraction'] for r in records]),
            'turnover_fractions':np.array([r['turnover_fraction'] for r in records]),
            'mdd_open_returns':np.array([r['open_to_open_after_entry_return'] for r in records]),
            'initial_net_factor':np.array(result['opening_records'][0]['nav_after'])}
    series_sha=save_npz(out/'series.npz',**arrays)
    budget.save()
    if budget.used['cpu_seconds']>cap['cpu_seconds']:raise ValueError('FINANCIAL_OUTPUT_CPU_CAP')
    write_json(out/'result.json',{'schema_version':'qf.stock-finance-path.v1','job':job,'lambda':lam,'selection_sha256':selection_sha,
        'finance_freeze_sha256':digest(finance_root/'freeze.json'),'data_epoch':doc['data_epoch'],'task_id':doc['task_id'],
        'run_id':doc['campaign_id']+'/'+job_id,'campaign_id':doc['campaign_id'],'source_commit':doc['source_commit'],
        'ledger_sha256':path_sha,'series_sha256':series_sha,'resource_ledger_sha256':digest(budget.path),'summary':result['summary'],
        'prediction_source':prediction_record,'common_prediction_source':classic_record,'risk_head_manifest_sha256':risk_manifest_sha,
        'execution_mode':doc['execution_mode'],'scientific_status':'LOCAL_VERIFIED','hardware_calls':0,'new_quantum_calls':0,'status':'COMPLETE'})
    return {'job_id':job_id,'status':'COMPLETE','sha256':digest(out/'result.json')}


def main():
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='command',required=True)
    a=s.add_parser('freeze');a.add_argument('--stock-root',type=Path,required=True);a.add_argument('--data',type=Path,required=True)
    a.add_argument('--campaign',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    a=s.add_parser('partition');a.add_argument('--stock-root',type=Path,required=True);a.add_argument('--finance-root',type=Path,required=True)
    for command in ['risk-head','run-path']:
        a=s.add_parser(command);a.add_argument('--stock-root',type=Path,required=True);a.add_argument('--data',type=Path,required=True)
        a.add_argument('--finance-root',type=Path,required=True)
        a.add_argument('--seed',type=int,required=True) if command=='risk-head' else a.add_argument('--job-id',required=True)
    a=p.parse_args()
    if a.command=='freeze':r=freeze(a.stock_root,a.data,a.campaign,a.output)
    elif a.command=='partition':r=partition_predictions(a.stock_root,a.finance_root)
    elif a.command=='risk-head':r=risk_head(a.stock_root,a.data,a.finance_root,a.seed)
    else:r=run_path(a.stock_root,a.data,a.finance_root,a.job_id)
    print(json.dumps(r,ensure_ascii=False))


if __name__=='__main__':main()
