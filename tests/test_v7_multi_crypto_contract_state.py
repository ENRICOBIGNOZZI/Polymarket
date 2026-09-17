#!/usr/bin/env python3
from __future__ import annotations
import json, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path('/Users/enrico/polymarket-multi-crypto-v7'); sys.path.insert(0,str(ROOT/'scripts'))
from v7_multi_crypto_contract_state import build
SHA='a'*40; H='b'*64; R='c'*64; NOW=2_000_000_000_000_000_000

def iso(ns:int)->str:return datetime.fromtimestamp(ns/1e9,tz=timezone.utc).isoformat().replace('+00:00','Z')
def selection():
    return {'schema':'polymarket_v7_multi_crypto_book_selection_v1','model_sha':SHA,'paper_only':True,
      'authenticated_execution':False,'real_order_submission':False,'real_capital_at_risk':False,
      'execution_authority':False,'selection_only':True,'generation_sha256':'d'*64,'generated_at_ms':NOW//1_000_000-100,
      'markets':[{'asset':'ETH','horizon':'M5','market_id':'m1','event_id':'e1','yes_token':'y','no_token':'n',
        'start_timestamp':iso(NOW-10_000_000_000),'end_timestamp':iso(NOW+100_000_000_000),
        'normalized_rules_hash':H,'rule_snapshot_sha256':R}]}
def oracle():
    return {'model_sha':SHA,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'execution_authority':False,
      'assets':{'ETH':{'fresh':True,'receive_age_ms':10.0,'price':100.0,'version':3,'receive_wall_ns':NOW-10_000_000}},
      'settlement_references':{'m1':{'valid':True,'asset':'ETH','horizon':'M5','market_id':'m1','normalized_rules_hash':H,
        'price':99.0,'source_timestamp_ms':(NOW-10_500_000_000)//1_000_000,'captured_at_ms':(NOW-9_000_000_000)//1_000_000}}}
def write_books(root:Path,age_ms:int=5):
    for token,bid,ask in [('y',.49,.51),('n',.48,.52)]:
        (root/f'{token}.json').write_text(json.dumps({'model_sha':SHA,'market_id':'m1','token_id':token,'paper_only':True,
          'authenticated_execution':False,'real_order_submission':False,'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY',
          'valid':True,'lineage_continuous':True,'receive_wall_ms':NOW//1_000_000-age_ms,'state_version':4,'connection_epoch':2,
          'best_bid':bid,'best_ask':ask}))

def run(sel=None,ora=None,age_ms=5):
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);write_books(root,age_ms)
        return build(sel or selection(),ora or oracle(),book_dir=root,model_sha=SHA,now_ns=NOW,maximum_book_age_ms=1000,maximum_oracle_age_ms=3000)

def test_active_contract_is_ready_only_when_all_causal_sources_match():
    out=run();row=out['markets'][0]
    assert out['active_markets']==1 and out['active_ready_markets']==1 and out['all_active_ready'] is True
    assert row['state']=='ACTIVE_READY_SHADOW' and row['entry_authority'] is False and row['reference_valid'] is True and row['book_valid'] is True

def test_stale_book_blocks_without_imputation():
    out=run(age_ms=2000);row=out['markets'][0]
    assert row['state']=='ACTIVE_BLOCKED' and 'PM_BOOK_NOT_READY' in row['blockers'] and row['yes_book']['best_bid'] is None

def test_reference_hash_mismatch_blocks():
    o=oracle();o['settlement_references']['m1']['normalized_rules_hash']='e'*64
    out=run(ora=o);assert out['markets'][0]['reference_valid'] is False and 'REFERENCE_NOT_CAUSAL_OR_MISMATCHED' in out['markets'][0]['blockers']

def test_authoritative_or_wrong_sha_source_is_rejected():
    o=oracle();o['model_sha']='f'*40
    try:run(ora=o)
    except ValueError as exc:assert 'oracle_identity' in str(exc)
    else:raise AssertionError('wrong-sha oracle accepted')

if __name__=='__main__':
    tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
    for _,f in tests:f()
    print(f'{len(tests)} function tests passed')
