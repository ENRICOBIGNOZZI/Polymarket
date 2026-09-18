from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'research/economic'))
from native_ev_labels import collect,settlement_label

def test_binary_and_fifty_fifty_labels():
    binary=settlement_label('m',{'closed':True,'clobTokenIds':['y','n'],'outcomePrices':['1','0']},123)
    assert binary['token_payouts']=={'y':1.0,'n':0.0} and binary['observed_ns']==123
    tie=settlement_label('m',{'closed':True,'clobTokenIds':['y','n'],'outcomePrices':['0.5','0.5']},123)
    assert tie['token_payouts']=={'y':.5,'n':.5}

def test_unresolved_is_missing_not_zero(tmp_path):
    p=tmp_path/'obs.jsonl'
    p.write_text(json.dumps({'schema':'polymarket_v7_native_observation_v1','paper_only':True,'execution_authority':False,'market_id':'m1'})+'\n')
    result=collect([p],fetcher=lambda _:{'closed':False},now_ns=lambda:999)
    assert result['labels']=={} and result['unresolved_markets']==['m1']
