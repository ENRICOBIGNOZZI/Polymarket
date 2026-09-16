#!/usr/bin/env python3
from __future__ import annotations
import copy, json, sys
from pathlib import Path
ROOT=Path('/Users/enrico/polymarket-multi-crypto-v7'); sys.path.insert(0,str(ROOT/'scripts'))
from v7_multi_crypto_repricing_labeler import canonical_hash,label_rows,validate_policy,validate_tape_record
SHA='a'*40; FH='b'*64; PH='c'*64; BASE=1_000_000_000_000_000_000

def policy(): return validate_policy(json.loads((ROOT/'config/v7_multi_crypto_repricing_label_policy.json').read_text()))

def row(ms:int,mid:float, *, end='2035-01-01T00:00:00Z'):
    v={'schema':'polymarket_v7_multi_crypto_feature_tape_v1','recorded_wall_ns':BASE+ms*1_000_000+1,'decision_wall_ns':BASE+ms*1_000_000,'available_at_ns':BASE+ms*1_000_000-1,
       'model_sha':SHA,'policy_hash':PH,'feature_schema_hash':FH,'feature_schema_version':'v','source_identity_hash':canonical_hash([ms,mid]),
       'asset':'ETH','horizon':'M5','market_id':'m1','event_id':'e1','yes_token':'y','no_token':'n','start_timestamp':'2020-01-01T00:00:00Z','end_timestamp':end,'active_now':True,
       'source_versions':{'x':ms},'blockers':['UNCALIBRATED_SHADOW'],'features':{'pm_book_valid':True,'pm_yes_mid':mid,'return_50ms_bp':None},
       'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'execution_authority':False}
    v['record_hash']=canonical_hash(v); return v

def test_exact_and_asof_labels_never_use_future_after_target():
    rows=[row(0,.50),row(50,.51),row(100,.52),row(200,.53),row(500,.54),row(1000,.55)]
    out,report=label_rows(rows,policy()); origin=out[0]
    assert origin['labels']['50']['status']=='LABELED' and abs(origin['labels']['50']['delta_pm_yes']-.01)<1e-12
    assert origin['labels']['100']['status']=='LABELED' and abs(origin['labels']['100']['delta_pm_yes']-.02)<1e-12
    # 250ms uses the last state at 200ms (50ms as-of gap), never the 500ms future state.
    assert origin['labels']['250']['status']=='LABELED' and abs(origin['labels']['250']['delta_pm_yes']-.03)<1e-12
    assert origin['labels']['250']['asof_gap_ms']==50.0
    assert report['labeled_counts']['50']>0

def test_missing_50ms_sample_is_not_interpolated_from_100ms_future():
    out,_=label_rows([row(0,.50),row(100,.60),row(200,.70)],policy())
    assert out[0]['labels']['50']['status']=='NO_LATER_ASOF_SAMPLE'
    assert out[0]['labels']['50']['delta_pm_yes'] is None

def test_invalid_target_book_and_expiry_are_explicit():
    rows=[row(0,.5),row(100,.6)]
    rows[1]['features']['pm_book_valid']=False; rows[1]['record_hash']=canonical_hash({k:v for k,v in rows[1].items() if k!='record_hash'})
    out,_=label_rows(rows,policy()); assert out[0]['labels']['100']['status']=='TARGET_BOOK_INVALID'
    exp=row(0,.5,end='2000-01-01T00:00:00Z'); out,_=label_rows([exp,row(100,.6,end='2000-01-01T00:00:00Z')],policy())
    assert out[0]['labels']['100']['status']=='CONTRACT_EXPIRES_BEFORE_HORIZON'

def test_tape_hash_and_availability_are_verified():
    good=row(0,.5); validate_tape_record(good,SHA)
    bad=copy.deepcopy(good); bad['features']['pm_yes_mid']=.9
    try: validate_tape_record(bad,SHA)
    except ValueError as exc: assert 'hash' in str(exc)
    else: raise AssertionError('mutated tape accepted')
    bad=row(0,.5); bad['available_at_ns']=bad['decision_wall_ns']+1; base=dict(bad); base.pop('record_hash'); bad['record_hash']=canonical_hash(base)
    try: validate_tape_record(bad,SHA)
    except ValueError as exc: assert 'availability' in str(exc)
    else: raise AssertionError('future input accepted')

def test_policy_forbids_future_and_interpolation():
    p=json.loads((ROOT/'config/v7_multi_crypto_repricing_label_policy.json').read_text()); p['interpolation']='LINEAR'
    try: validate_policy(p)
    except ValueError: pass
    else: raise AssertionError('interpolation accepted')

if __name__=='__main__':
    tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
    for _,f in tests:f()
    print(f'{len(tests)} function tests passed')
