from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import v7_native_repricing_dataset as m

def row(kind=2,h=None,pair=(4000,4200,5800,6000),observed=1_000_000_000):
    yb,ya,nb,na=pair
    return {'schema':'polymarket_v7_native_observation_v1','paper_only':True,'execution_authority':False,
      'run_id':'r','market_id':'m','code_sha':'a'*40,'asset':'XRP','horizon':'M5','kind':kind,
      'reason':15,'repricing_origin_signal_version':7,'repricing_horizon_ms':h,
      'repricing_pair_valid':True,'yes_bid_e4':yb,'yes_ask_e4':ya,'no_bid_e4':nb,'no_ask_e4':na,
      'tick_e4':100,'decision_monotonic_ns':1_000_000_000,'observed_monotonic_ns':observed,
      'decision_wall_ns':2_000_000_000,'binance_return_100ms_bp':1.2,
      'confirmation_return_100ms_bp':.2,'confirmation_venue':'COINBASE','signal_age_ns':10_000_000,'tte_ns':100_000_000_000}

def test_complete_pair_builds_causal_delta(tmp_path):
    p=tmp_path/'x.jsonl'; rows=[row()]
    rows += [row(6,h,(4100,4300,5700,5900),1_000_000_000+h*1_000_000) for h in (100,250,500,1000)]
    p.write_text(''.join(json.dumps(x)+'\n' for x in rows))
    out,s=m.build([p]);assert len(out)==4 and s['censored_horizons']==0
    assert {x['repricing_horizon_ms'] for x in out}=={100,250,500,1000}
    assert all(x['delta_logit']>0 for x in out)

def test_missing_or_broken_pair_is_censored_not_zero(tmp_path):
    p=tmp_path/'x.jsonl'; bad=row(6,100);bad['repricing_pair_valid']=False
    p.write_text(json.dumps(row())+'\n'+json.dumps(bad)+'\n')
    out,s=m.build([p]);assert out==[] and s['censored_horizons']==4

def test_conflicting_identity_fails(tmp_path):
    p=tmp_path/'x.jsonl'; a=row();b=row();b['yes_bid_e4']=3900
    p.write_text(json.dumps(a)+'\n'+json.dumps(b)+'\n')
    try:m.build([p])
    except ValueError as e:assert 'conflicting' in str(e)
    else:raise AssertionError('conflict accepted')
