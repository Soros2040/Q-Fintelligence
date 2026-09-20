"""Read-only paired hardware/reference analysis of the frozen 1000 circuits."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import numpy as np

WS = Path(__file__).resolve().parents[3]
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def read(path, digest=None):
    if digest is not None:
        assert sha(path) == digest, (str(path), 'SHA')
    return json.loads(path.read_text())

def put(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')

def counts_vector(counts, shots=1024):
    """Raw key leftmost bit is q0; numeric vector bit zero is q0."""
    assert sum(counts.values()) == shots
    assert all(len(k) == 6 and set(k) <= {'0','1'} and
               type(v) is int and v >= 0 for k,v in counts.items())
    a = np.zeros(64)
    for key, count in counts.items():
        a[int(key[::-1], 2)] = count / shots
    return a

def probability(a):
    a=np.asarray(a, dtype=float)
    assert a.shape == (64,) and np.isfinite(a).all()
    assert a.min() >= -1e-12 and abs(a.sum()-1) < 1e-9
    return a

def metrics(a,b):
    return {'tv': float(np.abs(a-b).sum()/2),
            'h2': float(np.square(np.sqrt(a)-np.sqrt(b)).sum()/2)}

def depth(qcis):
    clocks=[0]*6
    gates=0; two=0
    for line in qcis.splitlines():
        if line.startswith('M '): continue
        qs=[int(x) for x in re.findall(r'Q(\d+)', line)]
        assert qs
        t=max(clocks[q] for q in qs)+1
        for q in qs: clocks[q]=t
        gates+=1;two+=len(qs)==2
    return gates,two,max(clocks)

def intervals(rows, keys, rng, resamples):
    blocks={}
    for row in rows:
        blocks.setdefault(row['hardware_batch_id'], []).append(row)
    assert all(len(b)==5 for b in blocks.values())
    data=np.array([[sum(r[k] for r in group)/5 for k in keys]
                   for group in blocks.values()])
    sampled=rng.integers(0,len(data),size=(resamples,len(data)))
    draws=data[sampled].mean(axis=1)
    ci=np.quantile(draws,[.025,.975],axis=0)
    return {k:{'mean':float(data[:,i].mean()),
               'conditional_ci95':[float(ci[0,i]),float(ci[1,i])]}
            for i,k in enumerate(keys)}


def load_cloud_results(cloud_root, cloud_audit, local_manifest_sha, expected_ids):
    """Load only the exact complete snapshot certified by the independent audit."""
    cloud_root = cloud_root.resolve()
    audit = read(cloud_audit)
    assert audit['status'] == 'PASS' and audit['circuit_count'] == audit['unique_query_ids'] == 1000
    assert audit['valid_shots'] == 1024000
    assert audit['local_reference_manifest_sha256'] == local_manifest_sha
    assert sha(cloud_root / 'retrieval_manifest.json') == audit['retrieval_manifest_sha256'], 'Cloud snapshot identity'
    records = audit['records']
    assert len(records) == len(expected_ids) == 1000
    assert {r['circuit_id'] for r in records} == set(expected_ids), 'Cloud circuit coverage'
    assert len({r['query_id'] for r in records}) == 1000, 'Cloud Query ID uniqueness'
    cloud = {}
    for lineage in records:
        relative = Path(lineage['result_path'])
        assert not relative.is_absolute() and '..' not in relative.parts, 'Cloud result path'
        path = WS / relative
        assert path.resolve().is_relative_to(cloud_root), 'Cloud result outside audited snapshot'
        result = read(path, lineage['result_sha256'])
        assert result['circuit_id'] == lineage['circuit_id'] and result['query_id'] == lineage['query_id']
        assert result['circuit_id'] not in cloud
        assert result['request_sha256'] == lineage['request_sha256']
        assert result['shots'] == lineage['shots'] == 1024 and result['target'] == 'tianyan_swn'
        assert result['scientific_status'] == 'REAL_CLOUD_SIMULATOR_RESULT'
        assert result['measurement_order'] == result['bitstring_columns_left_to_right'] == list(range(6))
        assert result['provider_probability_columns_left_to_right'] == list(reversed(range(6)))
        counts_vector(result['counts'], result['shots'])
        cloud[result['circuit_id']] = (path, result, lineage)
    assert len(cloud) == 1000
    return cloud, audit


def analyze(run, output, cloud_root=None, cloud_audit=None):
    ref=run/'local_reference'
    manifest=read(ref/'manifest.json'); summary=read(ref/'summary.json')
    plan=read(run/'analysis_plan.json')
    assert summary['state']=='COMPLETED' and summary['circuit_count']==1000
    assert summary['manifest_sha256']==sha(ref/'manifest.json')
    summaries={r['circuit_id']:r for r in summary['results']}
    assert (cloud_root is None) == (cloud_audit is None), 'Cloud root and independent audit must be supplied together'
    cloud = {}; audit = None
    if cloud_root:
        cloud, audit = load_cloud_results(cloud_root, cloud_audit, sha(ref/'manifest.json'),
                                         {x['circuit_id'] for x in manifest['inputs']})
    parents=[]; rows=[]; ids=set(); qids=set(); calibration=set()
    feasible=np.array([i.bit_count()==3 for i in range(64)])
    for item in manifest['inputs']:
        ip=ref/item['path']; x=read(ip,item['sha256'])
        rp=ref/summaries[x['circuit_id']]['result_path']
        r=read(rp,summaries[x['circuit_id']]['result_sha256'])
        hp=WS/x['hardware_result_path']; h=read(hp,x['hardware_result_sha256'])
        freeze=read(WS/x['hardware_freeze_path'],x['hardware_freeze_sha256'])
        assert r['input_sha256']==sha(ip) and r['manifest_sha256']==sha(ref/'manifest.json')
        assert r['state']=='COMPLETED' and h['scientific_status']=='REAL_TIANYAN287_RESULT'
        assert h['circuit_id']==r['circuit_id']==x['circuit_id']
        assert h['query_id']==r['hardware_query_id']==x['hardware_query_id']
        assert h['request_sha256']==x['hardware_request_sha256']
        assert h['parent_freeze_sha256']==x['hardware_freeze_sha256']
        assert h['calibration_sha256']==x['hardware_calibration_sha256']
        assert h['measurement_order']==h['bitstring_columns_left_to_right']==[1,2,8,9,15,16]
        assert r['counts_columns_left_to_right']==[0,1,2,3,4,5]
        assert r['probability_columns_left_to_right']==[5,4,3,2,1,0]
        assert h['circuit_id'] not in ids and h['query_id'] not in qids
        ids.add(h['circuit_id']);qids.add(h['query_id']);calibration.add(h['calibration_sha256'])
        distributions={'hardware':counts_vector(h['counts'],h['shots']),
                       'finite':counts_vector(r['finite_shot_counts'],r['finite_shots']),
                       'ideal':probability(r['ideal_probabilities']),
                       'local_noise':probability(r['noisy_probabilities'])}
        gates,two,d=depth(x['qcis_no_terminal_newline'])
        assert gates==x['native_unitary_count'] and two==x['two_qubit_unitary_count']
        row={'circuit_id':h['circuit_id'],'family': 'qaoa' if 'qaoa' in h['circuit_id'] else 'message',
             'hardware_query_id':h['query_id'],'hardware_batch_id':x['hardware_batch_id'],
             'compiled_gate_count':gates,'compiled_two_qubit_count':two,'compiled_depth':d,
             'input_sha256':sha(ip),'local_result_sha256':sha(rp),'hardware_result_sha256':sha(hp)}
        if cloud_root:
            cp,c,lineage=cloud[h['circuit_id']]
            assert lineage['canonical_qcis_sha256']==x['canonical_qcis_sha256']
            assert lineage['request_sha256']==c['request_sha256']
            assert c['target']=='tianyan_swn' and c['shots']==1024
            assert c['measurement_order']==[0,1,2,3,4,5]
            distributions['cloud_noise']=counts_vector(c['counts'])
            row.update(cloud_query_id=c['query_id'],cloud_result_sha256=sha(cp))
            parents.append({'path':str(cp.relative_to(WS)),'sha256':sha(cp)})
        pairs=plan['comparison_pairs'] if cloud_root else plan['comparison_pairs'][:-1]
        for pair in pairs:
            a,b=pair.split('_vs_')
            for k,v in metrics(distributions[a],distributions[b]).items():
                row[pair+'_'+k]=v
        row['hardware_noise_minus_ideal_tv']=row['hardware_vs_local_noise_tv']-row['hardware_vs_ideal_tv']
        row['hardware_excess_over_finite_tv']=row['hardware_vs_ideal_tv']-row['finite_vs_ideal_tv']
        if row['family']=='qaoa':
            for name,p in distributions.items():
                row[name+'_feasible_k3']=float(p[feasible].sum())
        rows.append(row)
        parents.extend([{'path':str(p.relative_to(WS)),'sha256':sha(p)} for p in (ip,rp,hp)])
    assert len(rows)==1000 and len(calibration)==1
    out={'schema_version':'qf.hardware-matched-descriptive-analysis.v1',
         'state':'COMPLETED','scope':'HARDWARE_LOCAL_AND_CLOUD' if cloud_root else 'HARDWARE_AND_LOCAL_ONLY',
         'created_at':datetime.now(timezone.utc).isoformat(),'experiment_id':'E07',
         'run_id':run.name,'source_commit':manifest['source_commit'],
         'analysis_plan_sha256':sha(run/'analysis_plan.json'),'manifest_sha256':sha(ref/'manifest.json'),
         'implementation_sha256':sha(Path(__file__)),'local_summary_sha256':sha(ref/'summary.json'),
         'independent_cloud_audit_sha256':sha(cloud_audit) if cloud_root else None,
         'circuit_count':len(rows),'observed_calibration_snapshots':len(calibration),
         'calibration_sha256':sorted(calibration),'statistics':plan['statistics'],
         'hardware_shots':1024000,'local_finite_shots':1024000,'cloud_shots':1024000 if cloud_root else 0,
         'family_results':{},'parent_artifacts':parents,'records':rows}
    if cloud_root:
        out.update(cloud_production_revision=audit.get('production_revision', 'v1'),
                   cloud_data_epoch=audit['data_epoch'], cloud_run_id=audit['run_id'],
                   cloud_snapshot_manifest_sha256=audit['retrieval_manifest_sha256'],
                   cloud_production_manifest_sha256=audit['production_manifest_sha256'])
        parents.extend({'path':str(p.relative_to(WS)), 'sha256':sha(p)}
                       for p in (cloud_audit, cloud_root/'retrieval_manifest.json'))
    rng=np.random.default_rng(plan['statistics']['seed'])
    for family in ('message','qaoa'):
        group=[r for r in rows if r['family']==family]
        keys=[k for k,v in group[0].items() if isinstance(v,(int,float))]
        out['family_results'][family]={'circuits':len(group),'submission_blocks':len({r['hardware_batch_id'] for r in group}),
                'metrics':intervals(group,keys,rng,plan['statistics']['paired_bootstrap_resamples'])}
    put(output/'analysis.json',out)
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with (output/'per_circuit.csv').open('x',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    put(output/'artifact_manifest.json',{'analysis_sha256':sha(output/'analysis.json'),
        'table_sha256':sha(output/'per_circuit.csv'),'analysis_code_sha256':sha(Path(__file__)),
        'state':'PASS','raw_evidence_preserved':True})
    print(json.dumps({k:v for k,v in out.items() if k in ['state','scope','circuit_count','family_results']},ensure_ascii=False))
    return out

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--cloud-root',type=Path)
    p.add_argument('--cloud-audit',type=Path)
    a=p.parse_args();analyze(a.run.resolve(),a.output.resolve(),a.cloud_root.resolve() if a.cloud_root else None,
                          a.cloud_audit.resolve() if a.cloud_audit else None)
