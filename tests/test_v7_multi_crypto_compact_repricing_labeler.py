#!/usr/bin/env python3
from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path('/Users/enrico/polymarket-multi-crypto-v7'); sys.path.insert(0,str(ROOT/'scripts'))
from v7_multi_crypto_repricing_labeler import canonical_hash,validate_policy
from v7_multi_crypto_compact_pm_tape import build_timelines
from v7_multi_crypto_compact_repricing_labeler import label_rows
SHA='a'*40; FH='b'*64; PH='c'*64; BASE=1_000_000_000_000_000_000; BASE_MS=BASE//1_000_000

def policy(): return validate_policy(json.loads((ROOT/'config/v7_multi_crypto_repricing_label_policy.json').read_text()))

def origin(ms:int,mid:float=.5):
    v={'schema':'polymarket_v7_multi_crypto_feature_tape_v1','recorded_wall_ns':BASE+ms*1_000_000+1,'decision_wall_ns':BASE+ms*1_000_000,'available_at_ns':BASE+ms*1_000_000-1,
       'model_sha':SHA,'policy_hash':PH,'feature_schema_hash':FH,'feature_schema_version':'v','source_identity_hash':canonical_hash([ms,mid]),'record_hash':'x'*64,
       'asset':'ETH','horizon':'M5','market_id':'m1','event_id':'e1','yes_token':'y','no_token':'n','start_timestamp':'2000-01-01T00:00:00Z','end_timestamp':'2035-01-01T00:00:00Z',
       'features':{'pm_book_valid':True,'pm_yes_mid':mid},'source_versions':{},'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'execution_authority':False}
    return v

def token(outcome:str,ms:int,mid:float,seq:int):
    spread=.01; bid=mid-spread/2; ask=mid+spread/2
    return {'observer_sequence':seq,'instrument_handle':1 if outcome=='YES' else 2,'state_version':seq,'connection_epoch':1,
            'receive_wall_ms':BASE_MS+ms,'receive_monotonic_ns':ms*1_000_000,'best_bid':bid,'best_ask':ask,'tick_size':.01,
            'valid':True,'lineage_continuous':True,'event_kind':1,'market_id':'m1','event_id':'e1','token_id':outcome.lower(),'outcome':outcome}

def session(name:str,points:list[tuple[int,float]]):
    rows=[]; seq=0
    for ms,p in points:
        seq+=1; rows.append(token('YES',ms,p,seq)); seq+=1; rows.append(token('NO',ms,1-p,seq))
    return {'session_id':name,'records':len(rows),'rows':rows,'timelines':build_timelines(rows),
            'first_receive_wall_ms':min(r['receive_wall_ms'] for r in rows),'last_receive_wall_ms':max(r['receive_wall_ms'] for r in rows)}

def test_exact_compact_targets_label_50_100_250_without_interpolation():
    s=session('s1',[(0,.50),(50,.51),(100,.52),(200,.53),(500,.54)])
    out,report=label_rows([origin(0,.50)],[s],policy()); labels=out[0]['labels']
    assert labels['50']['status']=='LABELED' and abs(labels['50']['delta_pm_yes']-.01)<1e-12
    assert labels['100']['status']=='LABELED' and abs(labels['100']['delta_pm_yes']-.02)<1e-12
    assert labels['250']['status']=='LABELED' and abs(labels['250']['delta_pm_yes']-.03)<1e-12
    assert labels['250']['asof_gap_ms']==50.0 and report['labeled_counts']['50']==1
    assert report['coverage']['50']==1.0

def test_restart_boundary_is_never_bridged():
    first=session('s1',[(0,.50),(50,.51),(100,.52)]); second=session('s2',[(200,.70),(250,.80),(500,.90)])
    out,_=label_rows([origin(0,.50)],[first,second],policy()); labels=out[0]['labels']
    assert labels['250']['status']=='SESSION_END_BEFORE_TARGET'
    assert labels['250']['delta_pm_yes'] is None

def test_origin_mismatch_is_explicit():
    s=session('s1',[(0,.60),(50,.61)])
    out,report=label_rows([origin(0,.50)],[s],policy())
    assert out[0]['origin_compact_status']=='ORIGIN_TAPE_MISMATCH'
    assert report['origin_status_counts']['ORIGIN_TAPE_MISMATCH']==1

def test_asof_gap_policy_censors_old_state_not_future_fill():
    s=session('s1',[(0,.50),(10,.51),(100,.52)])
    out,_=label_rows([origin(0,.50)],[s],policy())
    assert out[0]['labels']['50']['status']=='ASOF_GAP_TOO_LARGE'
    assert out[0]['labels']['50']['asof_gap_ms']==40.0

if __name__=='__main__':
    tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
    for _,f in tests:f()
    print(f'{len(tests)} function tests passed')
