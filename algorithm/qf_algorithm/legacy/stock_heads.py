"""Development-only GCN selection and frozen full-axis mean prediction jobs."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import numpy as np
from .research_common import digest,identity,write_json,utc
from .stock_task import GRAPHS
from .graph_head import GraphHead


def verified_inputs(root,fold_id,family,graph,indices):
    cache_arm='selected' if family=='classical' else family
    cache=root/'caches'/f'{fold_id}_{cache_arm}_{graph}'
    manifest=json.loads((cache/'manifest.json').read_text())
    graph_manifest=json.loads((root/'graph_manifest.json').read_text())
    by_index={r['index']:r for r in graph_manifest['records']}
    cache_index={r['index']:r for r in manifest['records']}
    values,graphs,labels,masks,assets=[],[],[],[],[]
    for index in indices:
        gr=by_index[index];ca=cache_index[index]
        gp=root/gr['path'];cp=cache/ca['path']
        if digest(gp)!=gr['sha256'] or digest(cp)!=ca['sha256'] or ca['parent_day_sha256']!=gr['sha256']:
            raise ValueError('PREDICTION_INPUT_LINEAGE')
        with np.load(gp,allow_pickle=False) as day,np.load(cp,allow_pickle=False) as obs:
            np.testing.assert_array_equal(day['assets'],obs['assets'])
            values.append(obs['classical_readout' if family=='classical' else 'readout'])
            graphs.append(day['graphs'][GRAPHS.index(graph)].astype(float))
            labels.append(day['labels']);masks.append(day['feature_valid']&day['label_valid'])
            assets.append(day['assets'])
    return np.stack(values),np.stack(graphs),np.stack(labels),np.stack(masks),np.stack(assets)


def verify_complete_caches(root):
    freeze=json.loads((root/'freeze.json').read_text());total=0;bindings=[]
    for job in freeze['frontend_jobs']:
        path=root/'caches'/job['job_id']/'manifest.json'
        obj=json.loads(path.read_text())
        if [r['index'] for r in obj['records']]!=job['date_indices'] or obj['actual_sdk_evolutions']!=job['analytic_calls_cap']:
            raise ValueError('COMPLETE_CACHE_COVERAGE')
        total+=obj['actual_sdk_evolutions'];bindings.append({'job_id':job['job_id'],'sha256':digest(path)})
    if total!=freeze['analytic_call_reservation'] or total>10000000:raise ValueError('E03_TOTAL_ANALYTIC_CALLS')
    return bindings


def date_mae(prediction,labels,mask):
    error=np.where(mask,np.abs(prediction-labels),0)
    count=mask.sum(axis=1)
    if (count==0).any():raise ValueError('EMPTY_COMMON_DATE_LABELS')
    return error.sum(axis=1)/count


def select(root,family):
    start=time.process_time();freeze=json.loads((root/'freeze.json').read_text());bindings=verify_complete_caches(root)
    out=root/'heads'/('selection_'+family+'.json')
    if out.exists():raise FileExistsError('HEAD_SELECTION_COMPLETE')
    candidates=freeze['head_candidates'];folds=[f for f in freeze['folds'] if f['role']=='DEVELOPMENT'];scores=[]
    input_cache={}
    def evaluate(graph,config,stage):
        values=[]
        for fold in folds:
            key=(fold['id'],graph)
            if key not in input_cache:
                input_cache[key]=(verified_inputs(root,fold['id'],family,graph,fold['train']),verified_inputs(root,fold['id'],family,graph,fold['evaluation']))
            tr,ev=input_cache[key]
            model=GraphHead(tr[0].shape[-1],config['width'],config['layers'],config['l2'],1601)
            history=model.fit(*tr[:4],epochs=config['epochs'])
            prediction=model.predict(ev[0],ev[1]);values.append(float(date_mae(prediction,ev[2],ev[3]).mean()))
            if time.process_time()-start>8000:raise ValueError('HEAD_SELECTION_CPU_CAP')
        row={'stage':stage,'graph':graph,'config':config,'fold_mae':values,'mean_mae':float(np.mean(values))}
        scores.append(row);print(json.dumps({'family':family,**row}),flush=True)
        return row
    graph='directional'
    if family=='classical':
        initial=[evaluate(g,candidates[0],'GRAPH_SELECTION') for g in GRAPHS]
        graph=min(initial,key=lambda r:(r['mean_mae'],r['graph']))['graph']
        # Keep one graph's arrays in memory when refining its head.
        input_cache={key:value for key,value in input_cache.items() if key[1]==graph}
    heads=[evaluate(graph,c,'HEAD_SELECTION') for c in candidates]
    chosen=min(heads,key=lambda r:(r['mean_mae'],identity(r['config'])))
    write_json(out,{'family':family,'graph':graph,'config':chosen['config'],'development_scores':scores,
        'complete_cache_bindings':bindings,'stock_freeze_sha256':digest(root/'freeze.json'),
        'cpu_seconds':time.process_time()-start,'created_at':utc(),'selection_scope':'DEVELOPMENT_FOLDS_ONLY',
        'implementation_sha256':digest(__file__),'graph_head_sha256':digest(Path(__file__).with_name('graph_head.py'))})


def predict(root,family,graph,seed):
    start=time.process_time();freeze=json.loads((root/'freeze.json').read_text())
    selection_path=root/'heads'/('selection_'+family+'.json');selected=json.loads(selection_path.read_text())
    if selected['stock_freeze_sha256']!=digest(root/'freeze.json'):raise ValueError('HEAD_FREEZE_HASH')
    if family=='selected':
        if graph not in GRAPHS:raise ValueError('GRAPH_ABLATION')
    elif graph!=selected['graph']:raise ValueError('SELECTED_GRAPH_IDENTITY')
    if seed not in freeze['final_seeds']:raise ValueError('CLASSICAL_SEED_FREEZE')
    fold=next(f for f in freeze['folds'] if f['id']=='final')
    indices=fold['validation']+fold['evaluation']
    output=root/'predictions'/f'{family}_{graph}_seed{seed}';output.mkdir(parents=True,exist_ok=True)
    if (output/'manifest.json').exists():raise FileExistsError('PREDICTION_COMPLETE')
    train=verified_inputs(root,'final',family,graph,fold['train'])
    config=selected['config'];model=GraphHead(11,config['width'],config['layers'],config['l2'],seed)
    history=model.fit(*train[:4],epochs=config['epochs'])
    del train
    evaluation=verified_inputs(root,'final',family,graph,indices)
    mu=model.predict(evaluation[0],evaluation[1])
    if not np.isfinite(mu).all():raise ValueError('NONFINITE_MEAN_PREDICTION')
    np.savez_compressed(output/'prediction.npz',mu=mu,date_indices=np.array(indices),assets=evaluation[4],
                        labels=evaluation[2],prediction_mask=evaluation[3])
    np.savez_compressed(output/'model.npz',**model.export())
    cpu=time.process_time()-start
    if cpu>900:raise ValueError('FINAL_HEAD_CPU_CAP')
    write_json(output/'manifest.json',{'family':family,'graph':graph,'seed':seed,'selection_sha256':digest(selection_path),
        'prediction_sha256':digest(output/'prediction.npz'),'model_sha256':digest(output/'model.npz'),
        'date_indices':indices,'asset_dates':int(mu.size),'validation_date_count':len(fold['validation']),
        'test_date_count':len(fold['evaluation']),'horizon_sessions':1,'mean_unit':'SIMPLE_RETURN',
        'training_loss_by_epoch':history,'cpu_seconds':cpu,'created_at':utc(),'scientific_status':'LOCAL_VERIFIED',
        'data_epoch':freeze['data_epoch'],'new_quantum_evolutions':0,'cache_producer':'E03',
        'implementation_sha256':digest(__file__),'graph_head_sha256':digest(Path(__file__).with_name('graph_head.py'))})
    print(json.dumps({'state':'PREDICTION_COMPLETE','family':family,'graph':graph,'seed':seed,'cpu_seconds':cpu}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['select','predict']);p.add_argument('--stock-root',type=Path,required=True)
    p.add_argument('--family',choices=['selected','fixed','classical'],required=True);p.add_argument('--graph',default='directional');p.add_argument('--seed',type=int,default=1701)
    a=p.parse_args()
    if a.mode=='select':select(a.stock_root,a.family)
    else:predict(a.stock_root,a.family,a.graph,a.seed)
