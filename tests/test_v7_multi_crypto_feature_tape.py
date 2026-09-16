#!/usr/bin/env python3
from __future__ import annotations
import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT=Path('/Users/enrico/polymarket-multi-crypto-v7'); sys.path.insert(0,str(ROOT/'scripts'))
from v7_multi_crypto_feature_tape import Collector, SegmentedWriter, validate_snapshot
SHA='a'*40; H='b'*64; P='c'*64; NOW=2_000_000_000_000_000_000


def market(mid='m1', active=True, source='d'*64):
    return {
      'asset':'ETH','horizon':'M5','market_id':mid,'event_id':'e','yes_token':'y','no_token':'n',
      'start_timestamp':'x','end_timestamp':'y','active_now':active,'source_identity_hash':source,
      'feature_schema_hash':H,'feature_schema_version':'v','available_at_ns':NOW-1,'signal_eligible':False,
      'source_versions':{'external_state_version':1},'blockers':['UNCALIBRATED_SHADOW'],
      'tte_seconds':50.0,'pm_book_valid':True,'pm_yes_mid':.51,'pm_no_mid':.49,'pm_complete_set_gap':0.0,
      'pm_yes_spread':.02,'pm_yes_imbalance':.1,'oracle_fresh':True,'oracle_price':100.0,
      'reference_valid':True,'reference_price':99.0,'distance_to_reference_bp':101.0,'spot_minus_oracle_bp':None,
      'external':{'fresh':True,'composite_price':100.1,'return_50ms_bp':None,'return_100ms_bp':1.0,
                  'return_250ms_bp':2.0,'return_1s_bp':3.0,'dispersion_bps':1.0,'aggregate_ofi':.2,
                  'aggregate_trade_imbalance':-.1,'fresh_venue_count':3,
                  'shock':{'shock_z_unfloored':None,'sigma_100ms_bp_prior':None,'observations':10}},
      'leader_features':{},'derivatives':[]}


def snapshot(rows=None):
    return {'schema':'polymarket_v7_multi_crypto_feature_snapshot_v1','model_sha':SHA,'timestamp_ns':NOW,
            'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'real_capital_at_risk':False,
            'execution_authority':False,'research_only':True,'policy_mode':'SHADOW_COLLECTION_ONLY_UNCALIBRATED',
            'policy_hash':P,'feature_schema_hash':H,'markets':rows or [market()]}


def test_active_only_and_missing_stays_null():
    c=Collector(model_sha=SHA,minimum_interval_ms=100)
    rows=c.collect(snapshot([market(),market('future',False,'e'*64)]),now_ns=NOW+10)
    assert len(rows)==1 and rows[0]['market_id']=='m1'
    assert rows[0]['features']['external']['return_50ms_bp'] is None
    assert rows[0]['features']['spot_minus_oracle_bp'] is None
    assert rows[0]['execution_authority'] is False
    assert len(rows[0]['record_hash'])==64 and c.inactive_skips==1


def test_dedup_and_interval_gate_are_per_market():
    c=Collector(model_sha=SHA,minimum_interval_ms=100)
    assert len(c.collect(snapshot(),now_ns=NOW+10))==1
    assert c.collect(snapshot(),now_ns=NOW+20)==[] and c.duplicate_skips==1
    changed=snapshot([market(source='f'*64)])
    assert c.collect(changed,now_ns=NOW+50_000_000)==[] and c.interval_skips==1
    assert len(c.collect(changed,now_ns=NOW+110_000_000))==1


def test_executable_or_future_snapshot_is_rejected():
    c=Collector(model_sha=SHA,minimum_interval_ms=100)
    bad=snapshot(); bad['markets'][0]['signal_eligible']=True
    assert c.collect(bad,now_ns=NOW+1)==[] and c.invalid_snapshots==1
    future=snapshot(); future['markets'][0]['available_at_ns']=NOW+1
    try: validate_snapshot(future,SHA)
    except ValueError as exc: assert 'future_or_unknown' in str(exc)
    else: raise AssertionError('future availability accepted')


def test_wrong_sha_or_schema_hash_fails():
    bad=snapshot(); bad['model_sha']='0'*40
    try: validate_snapshot(bad,SHA)
    except ValueError: pass
    else: raise AssertionError('wrong SHA accepted')
    bad=snapshot(); bad['markets'][0]['feature_schema_hash']='1'*64
    try: validate_snapshot(bad,SHA)
    except ValueError as exc: assert 'schema mismatch' in str(exc)
    else: raise AssertionError('row schema mismatch accepted')


def test_segment_rotation_is_bounded_and_durable_close():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'features.jsonl'; w=SegmentedWriter(path,300)
        base={'x':'z'*250}
        w.append(base); w.append(base); w.close()
        sealed=list(Path(tmp).glob('features.segment-*.jsonl'))
        assert sealed and path.exists()


if __name__=='__main__':
    tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
    for _,f in tests:f()
    print(f'{len(tests)} function tests passed')
