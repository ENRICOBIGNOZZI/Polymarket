from __future__ import annotations
import importlib.util
import json
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import v7_nonfill_outcomes as tracker
import v7_crypto_cross_asset_audit as audit


def event(identity,kind,order='o1',**extra):
    return {'record_id':identity,'event_type':kind,'order_id':order,'market_id':'m1','token_id':'up',
      'paper_only':True,'authenticated_execution':False,'recorded_ts_ms':1000,
      'decision_ts_ms':500,'limit_price':.4,'intended_size':5.,'metadata':{},**extra}

def write(path,rows):path.write_text(''.join(json.dumps(r)+'\n' for r in rows))

def test_nonfill_resolution_is_not_booked_pnl_and_restart_idempotent(tmp_path):
    path=tmp_path/'ledger.jsonl';output=tmp_path/'outcomes.jsonl'
    write(path,[event('s','ORDER_SUBMITTED'),event('state','ORDER_STATE',metadata={'paper_execution_reason':4})])
    with tracker.connect(tmp_path/'state.sqlite') as db:
        assert tracker.ingest(db,path)==2
        assert tracker.ingest(db,path)==0
        tracker.export(db,output);r=json.loads(output.read_text())
        assert r['settlement_payoff'] is None and r['hypothetical_gross_at_limit'] is None
        with path.open('a') as f:
            f.write(json.dumps(event('final','FINAL','other-order',metadata={'settlement_payouts':{'up':1.,'down':0.}}))+'\n')
        assert tracker.ingest(db,path)==1
        stats=tracker.export(db,output);r=json.loads(output.read_text())
        assert stats['resolved_orders']==1 and not r['was_filled']
        assert r['hypothetical_gross_at_limit']==3.
        assert r['canonical_pnl'] is None and r['hypothetical_net_at_limit'] is None
        assert not r['counterfactual_execution_verified']
    with tracker.connect(tmp_path/'state.sqlite') as db:assert tracker.ingest(db,path)==0

def test_partial_line_is_not_consumed(tmp_path):
    path=tmp_path/'ledger.jsonl';row=json.dumps(event('s','ORDER_SUBMITTED'))
    path.write_text(row)
    with tracker.connect(tmp_path/'state.sqlite') as db:
        assert tracker.ingest(db,path)==0
        with path.open('a') as f:f.write('\n')
        assert tracker.ingest(db,path)==1

def test_conflicting_record_rolls_back(tmp_path):
    path=tmp_path/'ledger.jsonl';write(path,[event('s','ORDER_SUBMITTED')])
    with tracker.connect(tmp_path/'state.sqlite') as db:
        tracker.ingest(db,path)
        with path.open('a') as f:f.write(json.dumps(event('s','ORDER_SUBMITTED',limit_price=.5))+'\n')
        with pytest.raises(ValueError,match='conflicting'):tracker.ingest(db,path)
        assert db.execute('SELECT count(*) FROM orders').fetchone()[0]==1

def test_mirror_replacement_deduplicates(tmp_path):
    path=tmp_path/'ledger.jsonl';write(path,[event('s','ORDER_SUBMITTED')])
    with tracker.connect(tmp_path/'state.sqlite') as db:
        tracker.ingest(db,path)
        new=tmp_path/'new';write(new,[event('s','ORDER_SUBMITTED'),event('state','ORDER_STATE')]);new.replace(path)
        assert tracker.ingest(db,path)==1

def test_binary_seek_unique_feature_join(tmp_path):
    path=tmp_path/'market-capture.jsonl';base={'close_wall_ns':10_000_000_000,'close_monotonic_ns':5_000_000_000,
      'capture_mode':'DECISIONS','kind':2,'accepted':True,'token_id':'token','book_version':4}
    with path.open('w') as out:
        for i in range(1000):out.write(json.dumps({**base,'observed_monotonic_ns':1_000_000_000+i*20_000_000})+'\n')
    matches=audit.observation_near(path,6000,'token',4)
    assert len(matches)==1 and matches[0]['observed_monotonic_ns']==1_000_000_000

def test_frozen_audit_duplicate_identity_rejected(tmp_path):
    p=tmp_path/'ledger';write(p,[event('s','ORDER_SUBMITTED'),event('s','ORDER_SUBMITTED')])
    with pytest.raises(ValueError,match='duplicate'):list(audit.load_rows(p))

def test_long_unresolved_markets_do_not_starve_later_labels(tmp_path,monkeypatch):
    path=tmp_path/'ledger.jsonl'
    write(path,[event('s1','ORDER_SUBMITTED','o1',market_id='m1'),event('s2','ORDER_SUBMITTED','o2',market_id='m2')])
    seen=[]
    def resolution(market,cache):
        seen.append(market);return {'resolved':False}
    monkeypatch.setattr(tracker,'public_resolution',resolution)
    with tracker.connect(tmp_path/'state.sqlite') as db:
        tracker.ingest(db,path)
        tracker.label(db,tmp_path/'cache',maximum_markets=1)
        tracker.label(db,tmp_path/'cache',maximum_markets=1)
    assert seen==['m1','m2']

def test_two_hour_protocol_preserves_all_assets():
    p=json.loads((ROOT/'config/v7_probability_forward_2h.json').read_text())
    assert p['duration_seconds']==7200
    assert p['state']=='PREPARED_NOT_STARTED'
    assert set(p['assets'])=={'BTC','ETH','SOL','XRP','DOGE','BNB'}
    assert not p['excluded_assets'] and not p['asset_shadow_overrides']
    assert p['paper_only'] and not p['real_order_submission']
    assert not p['automatic_promotion']

def test_unknown_fee_terms_cannot_create_hypothetical_net_pnl(tmp_path):
    root=tmp_path/'run';obs=root/'research/native_observations/run1';obs.mkdir(parents=True)
    snapshot=tmp_path/'ledger.jsonl'
    submit=event('s','ORDER_SUBMITTED',market_id='123',decision_ts_ms=6000,
        book_snapshot_id='native-book:4',model_sha='a'*40,
        metadata={'asset':'DOGE','horizon':'M5','run_id':'run1'})
    state=event('state','ORDER_STATE',market_id='123',metadata={'paper_execution_reason':11})
    final=event('final','FINAL',order='other',market_id='123',final_pnl=0.,
        metadata={'included_order_ids':['other'],'settlement_payouts':{'up':1.,'down':0.}})
    write(snapshot,[submit,state,final])
    point={'observed_monotonic_ns':1_000_000_000,'close_wall_ns':10_000_000_000,
        'close_monotonic_ns':5_000_000_000,'kind':2,'accepted':True,'token_id':'up',
        'book_version':4,'fee_rate':.07,'fee_exponent':1.,'signal_age_ns':1.,
        'tte_ns':4_000_000_000,'bid_e4':3900,'ask_e4':4000,
        'binance_return_100ms_bp':1.,'coinbase_return_100ms_bp':0.,
        'capture_mode':'DECISIONS'}
    (obs/'123-capture.jsonl').write_text(json.dumps(point)+'\n')
    out=tmp_path/'audit';audit.build(snapshot,root,out,False)
    r=json.loads((out/'orders.json').read_text())[0]
    assert r['hypothetical_payoff_at_limit']==3.
    assert r['hypothetical_net_at_limit'] is None
