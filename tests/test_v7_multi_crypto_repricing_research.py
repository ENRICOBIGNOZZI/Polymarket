#!/usr/bin/env python3
from __future__ import annotations
import json,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
from v7_multi_crypto_repricing_research import evaluate,split_rows,validate_policy,Transformer
BASE=1_800_000_000_000_000_000

def policy(min_clusters=20):
    v=json.loads((ROOT/'config/v7_multi_crypto_repricing_research.json').read_text()); v['minimum_time_clusters']=min_clusters; return validate_policy(v)

def row(cluster:int,x:float,asset='ETH',horizon='M5',offset_s=60):
    d=BASE+cluster*300_000_000_000+offset_s*1_000_000_000
    features={'pm_yes_mid':.5+.01*(x%3),'pm_yes_spread':.02,'pm_yes_imbalance':.1,'pm_complete_set_gap':0.0,'tte_seconds':120.0,
              'distance_to_reference_bp':2*x,'spot_minus_oracle_bp':.5*x,
              'external':{'return_50ms_bp':.5*x,'return_100ms_bp':x,'return_250ms_bp':1.2*x,'return_1s_bp':1.5*x,'dispersion_bps':1.0,'aggregate_ofi':.2*x,'aggregate_trade_imbalance':.1*x,'fresh_venue_count':3,'shock':{'shock_z_unfloored':.3*x}},
              'derivatives':[], 'leader_features':{}}
    return {'decision_wall_ns':d,'asset':asset,'horizon':horizon,'features':features,'_target':.002*x}

def synthetic(nclusters=30):
    rows=[]
    for c in range(nclusters):
        for j,a in enumerate(('BTC','ETH','SOL','XRP','DOGE','BNB')): rows.append(row(c,((c*6+j)%17)-8,a,'M5' if j%2==0 else 'M15'))
    return rows

def test_chronological_cluster_split_is_disjoint_and_purged():
    rows=synthetic(); s=split_rows(rows,policy())
    assert set(s['train_clusters']).isdisjoint(s['validation_clusters']) and set(s['validation_clusters']).isdisjoint(s['test_clusters'])
    cluster_ns=300_000_000_000
    val_start=min(s['validation_clusters'])*cluster_ns
    assert all(int(r['decision_wall_ns'])+10_000_000_000<val_start for r in s['train'])

def test_fixed_ridge_recovers_synthetic_signal_oos():
    report=evaluate(synthetic(),policy(),{'test':'synthetic'})
    assert report['status']=='RESEARCH_OOS_AVAILABLE'
    zero=report['models']['ZERO_REPRICING']['metrics']['test']['mse']; own=report['models']['OWN_SHOCK']['metrics']['test']['mse']
    assert own is not None and zero is not None and own < zero*.1

def test_scaler_is_training_only_not_test_outlier_driven():
    rows=synthetic(); s=split_rows(rows,policy()); t=Transformer(('ext_return_100ms_bp',)); t.fit(s['train']); training_mean=t.stats['ext_return_100ms_bp'][1]
    test=dict(s['test'][0]); test['features']=json.loads(json.dumps(test['features'])); test['features']['external']['return_100ms_bp']=1e9
    _=t.transform(test); assert abs(training_mean)<100

def test_insufficient_clusters_fail_closed_without_model_ranking():
    report=evaluate(synthetic(3),policy(),{'test':'small'})
    assert report['status']=='INSUFFICIENT_EVIDENCE' and report['models']=={}
    assert 'INSUFFICIENT_INDEPENDENT_TIME_CLUSTERS' in report['blockers']

def test_policy_rejects_execution_authority_and_tuning():
    p=json.loads((ROOT/'config/v7_multi_crypto_repricing_research.json').read_text()); p['execution_authority']=True
    try:validate_policy(p)
    except ValueError:pass
    else:raise AssertionError('authority accepted')
    p=json.loads((ROOT/'config/v7_multi_crypto_repricing_research.json').read_text()); p['ridge_tuning']='GRID_SEARCH'
    try:validate_policy(p)
    except ValueError:pass
    else:raise AssertionError('forward tuning accepted')

if __name__=='__main__':
    tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
    for _,f in tests:f()
    print(f'{len(tests)} function tests passed')
