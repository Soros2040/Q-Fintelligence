"""Three-fold frozen-map selection on the independent six-asset downside task."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import numpy as np
import pandas as pd
from sklearn.linear_model import SGDRegressor
from sklearn.preprocessing import StandardScaler
from .research_common import Budget, digest, identity, write_json, utc, paired_inference, holm, select_block_length
from .quantum_features import catalog, states, fidelity_kernel


def sample_dates(frame, count):
    dates = sorted(frame.date.unique())
    chosen = np.array(dates)[np.linspace(0, len(dates)-1, min(count, len(dates))).astype(int)]
    return frame.loc[frame.date.isin(chosen)].sort_values(['date', 'asset']).reset_index(drop=True)


def transform(train, other, features):
    scaler = StandardScaler().fit(train[features])
    return np.clip(scaler.transform(train[features]), -3, 3) * np.pi/3, np.clip(scaler.transform(other[features]), -3, 3) * np.pi/3, {
        'mean': scaler.mean_.tolist(), 'scale': scaler.scale_.tolist(), 'fit_rows': len(train), 'fit_dates': sorted(train.date.unique().tolist())}


def fit_predict(x, y, z, seed):
    scale = StandardScaler().fit(x)
    ym, ys = float(y.mean()), max(float(y.std()), 1e-8)
    model = SGDRegressor(loss='squared_error', penalty='l2', alpha=.01, max_iter=40, tol=None,
                         learning_rate='invscaling', eta0=.01, power_t=.25, random_state=seed, shuffle=True)
    model.fit(scale.transform(x), (y-ym)/ys)
    return model.predict(scale.transform(z))*ys+ym


def classical_kernel(x, a):
    distance = ((x[:,None,:] - a[None,:,:])**2).sum(axis=2)
    return np.exp(-distance/6)


def load_task(base):
    protocol = json.loads((base/'protocol.json').read_text())
    features = pd.read_parquet(base/'features.parquet')
    labels = pd.read_parquet(base/'labels.parquet')
    features = features.loc[features.graph_variant == 'full__fevd__0']
    frame = features.merge(labels, on=['anchor', 'date', 'asset'], validate='one_to_one').sort_values(['date', 'asset'])
    split = pd.read_csv(base/'splits.csv')
    final = split.loc[split.phase == 'final']
    frame = frame.merge(final[['anchor','role']], on='anchor', validate='many_to_one')
    yname = 'future5_downside_annualized_original_scale'
    return frame, protocol['featureOrder'], yname


def execute(base, root):
    frame, features, yname = load_task(base)
    final_train = sample_dates(frame.loc[frame.role == 'train'], 85)
    final_test = frame.loc[frame.role == 'test'].sort_values(['date','asset']).reset_index(drop=True)
    if len(final_train)>512 or len(final_test)>5000 or len(final_test)%6 or len(final_test)==0:
        raise ValueError('MAPPING_TASK_COVERAGE')
    fold_boundaries = [('2020-01-01','2020-07-01'), ('2021-01-01','2021-07-01'), ('2022-01-01','2022-07-01')]
    folds = []
    for start, end in fold_boundaries:
        train = sample_dates(frame.loc[(frame.label_end < start) & (frame.date < start)], 85)
        valid = sample_dates(frame.loc[(frame.date >= start) & (frame.label_end < end)], 21)
        folds.append((train, valid))
    source = [{'path': str(base/name), 'sha256': digest(base/name)} for name in ['features.parquet','labels.parquet','splits.csv','protocol.json']]
    dev_series=frame.loc[frame.date<'2022-01-01'].groupby('date')[yname].mean().to_numpy()
    block=select_block_length(dev_series,holding_sessions=5)
    parameters = {'experiment_id':'E02','data_epoch':'d02-core6-experiment-ready-20260906', 'created_at':utc(),
        'models_evaluated':0,'candidates':catalog(),'source_files':source,'feature_order':features,'target':yname,
        'mapping_role':'STRUCTURE_TRANSFER_FROM_INDEPENDENT_FIVE_SESSION_DOWNSIDE_TASK',
        'folds':[{'train_keys':t[['date','asset']].values.tolist(),'validation_keys':v[['date','asset']].values.tolist()} for t,v in folds],
        'final_train_keys':final_train[['date','asset']].values.tolist(),'final_test_keys':final_test[['date','asset']].values.tolist(),
        'head':{'class':'SGDRegressor','epochs':40,'alpha':.01,'selection_seed':1601,'final_seeds':[1701,1702,1703,1704,1705]},
        'anchors':32,'evaluation':'SDK_STATEVECTOR_FIDELITY_WITH_CLASSICALLY_REUSED_STATE_CACHE',
        'hardware_equivalent_kernel_pair_budget':1640848,'actual_sdk_evolution_count_rule':'ONE_PER_UNIQUE_INPUT_CANDIDATE_FOLD',
        'maximum_final_frontends':2,'finite_units':384,'finite_shots_levels':[256,1024,4096],
        'block_length_freeze':block,'implementation_sha256':digest(__file__),
        'quantum_features_sha256':digest(Path(__file__).with_name('quantum_features.py'))}
    freeze_sha = write_json(root/'freeze.json', parameters)
    calls_dev = sum((len(t)+len(v))*12 for t,v in folds)
    calls_final = 2*(len(final_train)+len(final_test))
    budget = Budget(root, {'cpu_seconds':172800,'analytic_calls':2000000,'shots':2097152},
        {'development':{'cpu_seconds':70000,'analytic_calls':calls_dev,'shots':0},
         'final':{'cpu_seconds':102000,'analytic_calls':calls_final,'shots':2064384}})
    candidate_scores = []
    for spec in catalog():
        losses = []
        for fold,(train,valid) in enumerate(folds):
            unit=f'{spec["candidate_id"]}_fold{fold}'
            budget.admit(unit,'development',{'cpu_seconds':1900,'analytic_calls':len(train)+len(valid),'shots':0})
            x,v,scaler=transform(train,valid,features)
            anchors=np.linspace(0,len(train)-1,32).astype(int)
            vectors=states(np.row_stack([x,v]),spec)
            kernels=fidelity_kernel(vectors,vectors[anchors])
            y=train[yname].to_numpy()
            prediction=fit_predict(kernels[:len(train)],y,kernels[len(train):],1601)
            losses.append(float(np.mean(np.abs(prediction-valid[yname].to_numpy()))))
            write_json(root/'development'/f'{unit}.json',{'spec':spec,'fold':fold,'scaler':scaler,
                'mae':losses[-1],'actual_evolutions':len(vectors),'anchor_indices':anchors.tolist(),
                'state_cache_sha256':identity({'real':vectors.real.tolist(),'imag':vectors.imag.tolist()})})
            budget.finish(unit,{'analytic_calls':len(vectors),'shots':0})
        candidate_scores.append({'candidate_id':spec['candidate_id'],'fold_mae':losses,'mean_validation_mae':float(np.mean(losses))})
        print(json.dumps(candidate_scores[-1]),flush=True)
    selected=min(candidate_scores,key=lambda row:(row['mean_validation_mae'],row['candidate_id']))['candidate_id']
    selected_spec=next(s for s in catalog() if s['candidate_id']==selected)
    selection={'selected':selected_spec,'fixed':catalog()[0],'candidate_scores':candidate_scores,'freeze_sha256':freeze_sha,
        'selected_using':'THREE_DEVELOPMENT_FOLDS_ONLY','created_at':utc()}
    selection_sha=write_json(root/'selection.json',selection)
    x,z,scaler=transform(final_train,final_test,features)
    anchors=np.linspace(0,len(final_train)-1,32).astype(int)
    seed_predictions={}
    finite=[]
    final_details=[]
    for arm,spec in [('selected',selected_spec),('fixed',catalog()[0]),('classical',None)]:
        unit='final_'+arm
        charge=len(x)+len(z) if spec else 0
        budget.admit(unit,'final',{'cpu_seconds':30000,'analytic_calls':charge,'shots':1032192 if spec else 0})
        if spec:
            vector=states(np.row_stack([x,z]),spec)
            kernel=fidelity_kernel(vector,vector[anchors])
            gram=fidelity_kernel(vector[anchors],vector[anchors])
            path=root/'cache'/f'{arm}.npz';path.parent.mkdir(exist_ok=True)
            np.savez_compressed(path,state_real=vector.real,state_imag=vector.imag,kernel=kernel,anchors=anchors)
            min_eigen=float(np.linalg.eigvalsh(gram).min())
            empirical_rng=np.random.default_rng(2026090803+(0 if arm=='selected' else 1))
            pairs=[(i,j) for i in np.linspace(len(x),len(kernel)-1,6).astype(int) for j in range(32)]
            for i,j in pairs:
                probability=float(kernel[i,j])
                for shots in [256,1024,4096]:
                    successes=int(empirical_rng.binomial(shots,probability))
                    finite.append({'arm':arm,'input_index':int(i),'anchor':j,'shots':shots,'successes':successes,
                        'exact_probability':probability,'sample_probability':successes/shots,
                        'mode':'LOCAL_BINOMIAL_FIDELITY_ESTIMATOR_FROM_SDK_EXACT_STATE'})
            final_details.append({'arm':arm,'cache_sha256':digest(path),'minimum_anchor_gram_eigenvalue':min_eigen,
                                  'psd_correction_norm':0,'actual_evolutions':charge})
        else:
            kernel=classical_kernel(np.row_stack([x,z]),x[anchors])
        prediction=np.column_stack([fit_predict(kernel[:len(x)],final_train[yname].to_numpy(),kernel[len(x):],seed)
                                    for seed in [1701,1702,1703,1704,1705]])
        seed_predictions[arm]=prediction
        budget.finish(unit,{'analytic_calls':charge,'shots':1032192 if spec else 0})
    rows=final_test[['date','asset',yname]].copy()
    truth=rows[yname].to_numpy()
    paths={}
    for arm,prediction in seed_predictions.items():
        for j,seed in enumerate([1701,1702,1703,1704,1705]):rows[f'{arm}_seed{seed}']=prediction[:,j]
        losses=pd.DataFrame(np.abs(prediction-truth[:,None]));losses['date']=rows.date.to_numpy()
        paths[arm]=losses.groupby('date').mean().to_numpy()
    rows.to_csv(root/'predictions.csv',index=False)
    budget.admit('statistics','final',{'cpu_seconds':2000,'analytic_calls':0,'shots':0})
    comparisons=holm([dict(comparison='selected_vs_'+arm,**paired_inference(paths['selected'],paths[arm],block['length'])) for arm in ['fixed','classical']])
    write_json(root/'finite_shots.json',{'units':384,'total_shots':sum(r['shots'] for r in finite),'records':finite})
    budget.finish('statistics',{'analytic_calls':0,'shots':0})
    result={'experiment_id':'E02','run_id':root.name,'data_epoch':parameters['data_epoch'],'scientific_status':'LOCAL_VERIFIED',
        'execution_modes':['CLASSICAL_SGD_RBF_ANCHOR','CQLIB_IDEAL_STATEVECTOR','LOCAL_FINITE_SHOTS'],
        'target':yname,'hold_sessions':5,'comparison_family':comparisons,'date_count':len(paths['selected']),
        'asset_date_count':len(rows),'selection_sha256':selection_sha,'freeze_sha256':freeze_sha,
        'block_length_development_freeze':block,'final_caches':final_details,'predictions_sha256':digest(root/'predictions.csv'),
        'resource_ledger_sha256':digest(root/'resource_ledger.json'),'created_at':utc(),'archive_status':'RESULT_GENERATED','release_status':'NOT_RELEASED'}
    write_json(root/'result.json',result)
    print(json.dumps({'E02':'COMPLETE','selected':selected,'dates':len(paths['selected']),'comparisons':comparisons}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();execute(args.data,args.output)
