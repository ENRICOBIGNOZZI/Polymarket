#!/usr/bin/env python3
from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from v7_multi_crypto_shock_calibration import calibrate,dedup_shocks,validate_policy
BASE=1_800_000_000_000_000_000

def policy(min_clusters=20):
 v=json.loads((ROOT/'config/v7_multi_crypto_shock_calibration.json').read_text());v['minimum_time_clusters']=min_clusters;return validate_policy(v)
def row(asset,cluster,state,z,horizon='M5'):
 return {'asset':asset,'horizon':horizon,'decision_wall_ns':BASE+cluster*300_000_000_000+1_000_000_000,'source_versions':{'external_state_version':state},'features':{'external':{'shock':{'shock_z_unfloored':z,'sigma_100ms_bp_prior':.2+abs(z)*.01,'return_100ms_bp':z*.2,'observations':250}}}}
def synthetic(n=30,outlier=False):
 rows=[];state=0
 for c in range(n):
  for j,a in enumerate(('BTC','ETH','SOL','XRP','DOGE','BNB')):
   state+=1;z=((c*6+j)%21-10)/3
   if outlier and c==n-1 and a=='BTC':z=1e6
   rows += [row(a,c,state,z,'M5'),row(a,c,state,z,'M15')]
 return rows

def test_dedup_removes_horizon_copy_of_same_external_state():
 rows=synthetic(2);unique=dedup_shocks(rows,policy(1));assert len(unique)==12 and len(rows)==24

def test_insufficient_clusters_fail_closed_with_no_selected_threshold():
 r=calibrate(synthetic(3),policy());assert r['status']=='INSUFFICIENT_EVIDENCE' and r['selected_threshold'] is None and r['candidate_quantiles']=={}

def test_training_only_quantiles_ignore_late_forward_outlier():
 a=calibrate(synthetic(30,False),policy());b=calibrate(synthetic(30,True),policy())
 assert a['status']=='TRAINING_DISTRIBUTION_CALIBRATED' and b['status']==a['status']
 assert a['candidate_quantiles']==b['candidate_quantiles'] and a['sigma_floor_candidates_bp']==b['sigma_floor_candidates_bp']

def test_calibration_emits_common_and_per_asset_candidates_only():
 r=calibrate(synthetic(30),policy());assert r['selected_threshold'] is None
 assert set(r['candidate_quantiles'])=={'COMMON','BTC','ETH','SOL','XRP','DOGE','BNB'}
 assert all(r['sigma_floor_candidates_bp'][a] is not None for a in ('BTC','ETH','SOL','XRP','DOGE','BNB'))

def test_policy_rejects_forward_threshold_selection():
 p=json.loads((ROOT/'config/v7_multi_crypto_shock_calibration.json').read_text());p['selected_threshold']=2.5
 try:validate_policy(p)
 except ValueError:pass
 else:raise AssertionError('selected threshold accepted')

if __name__=='__main__':
 tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
 for _,f in tests:f()
 print(f'{len(tests)} function tests passed')
