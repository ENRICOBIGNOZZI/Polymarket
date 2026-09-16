#!/usr/bin/env python3
from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path('/Users/enrico/polymarket-multi-crypto-v7');sys.path.insert(0,str(ROOT/'scripts'))
from v7_multi_crypto_compact_pm_tape import build_indexed_timelines,build_timelines
from v7_multi_crypto_latency_capacity_replay import arrival,mechanics_candidates,replay,scenario,validate_candidate,validate_policy
SHA='a'*40;BASE_MS=1_800_000_000_000

def policy():return validate_policy(json.loads((ROOT/'config/v7_multi_crypto_latency_capacity.json').read_text()))
def book(outcome,ms,bid,ask,depth,seq,market='m'):
 return {'observer_sequence':seq,'instrument_handle':1 if outcome=='YES' else 2,'state_version':seq,'connection_epoch':1,'receive_wall_ms':BASE_MS+ms,'receive_monotonic_ns':ms*1_000_000,'best_bid':bid,'best_ask':ask,'tick_size':.01,'bid_depth_l1':depth,'ask_depth_l1':depth,'valid':True,'lineage_continuous':True,'event_kind':1,'market_id':market,'event_id':'e','token_id':outcome.lower(),'outcome':outcome}
def session(points):
 rows=[];seq=0
 for ms,yes_ask,yes_depth in points:
  seq+=1;rows.append(book('YES',ms,yes_ask-.02,yes_ask,yes_depth,seq));seq+=1;no_ask=1-(yes_ask-.02);rows.append(book('NO',ms,no_ask-.02,no_ask,100,seq))
 return {'session_id':'s1','rows':rows,'records':len(rows),'timelines':build_timelines(rows),'indexed_timelines':build_indexed_timelines(rows),'first_receive_wall_ms':min(r['receive_wall_ms'] for r in rows),'last_receive_wall_ms':max(r['receive_wall_ms'] for r in rows)}
def candidate(limit=.50,group='g',decision_ms=0):
 return {'schema':'polymarket_v7_multi_crypto_research_candidate_v1','candidate_mode':'TEST','candidate_id':'c'+group,'competition_group_id':group,'model_sha':SHA,'asset':'ETH','horizon':'M5','market_id':'m','event_id':'e','outcome':'YES','decision_wall_ns':(BASE_MS+decision_ms)*1_000_000,'original_limit_price':limit,'source_record_hash':'x','paper_only':True,'authenticated_execution':False,'real_order_submission':False,'execution_authority':False,'economic_signal':False}

def test_partial_fak_and_no_chase_at_modeled_arrival():
 s=session([(0,.50,7),(50,.52,20)])
 r0=scenario([candidate(.50)], [s],0,10);assert r0['filled_shares']==7 and r0['partial_fill_count']==1
 r50=scenario([candidate(.50)], [s],50,10);assert r50['filled_shares']==0 and r50['no_fill_worse_price_count']==1

def test_better_ask_fills_at_arrival_price_not_original_limit():
 s=session([(0,.50,20),(25,.48,20)]);r=scenario([candidate(.50)],[s],25,10)
 assert r['full_fill_count']==1 and abs(r['average_fill_price']-.48)<1e-12 and abs(r['average_price_improvement_vs_limit']-.02)<1e-12

def test_simultaneous_competition_consumes_visible_depth_once():
 s=session([(0,.50,7)]);a=candidate(.50,'same');b=dict(a);b['candidate_id']='c2'
 r=scenario([a,b],[s],0,5);assert r['filled_shares']==7 and r['full_fill_count']==1 and r['partial_fill_count']==1

def test_session_end_is_unobserved_not_zero_fill():
 s=session([(0,.50,100),(50,.50,100)]);r=scenario([candidate(.50)],[s],100,10)
 assert r['observed_arrival_count']==0 and r['unobserved_count']==1 and r['filled_shares']==0

def test_mechanics_probe_creates_two_zero_authority_outcomes():
 s=session([(0,.50,100),(100,.50,100)])
 feature={'decision_wall_ns':BASE_MS*1_000_000,'model_sha':SHA,'asset':'ETH','horizon':'M5','market_id':'m','event_id':'e','record_hash':'r1','features':{'pm_book_valid':True,'pm_yes_mid':.5}}
 rows=mechanics_candidates([feature],[s],policy());assert len(rows)==2 and {r['outcome'] for r in rows}=={'YES','NO'}
 assert all(r['execution_authority'] is False and r['economic_signal'] is False for r in rows)

def test_replay_is_mechanics_only_and_common_sample_explicit():
 s=session([(0,.50,100),(1000,.50,100)]);c=[candidate(.50)]
 p=policy();r=replay(c,[s],p,candidate_mode='MECHANICS_PROBE',lineage={'x':'y'})
 assert r['economic_evidence'] is False and r['status']=='RESEARCH_EXECUTION_MECHANICS_ONLY'
 assert r['common_observable_candidate_count']==1 and r['venue_matching_delay_ms'] is None

def test_candidate_and_policy_fail_closed_on_authority_or_matching_delay():
 c=candidate(.5);validate_candidate(c,SHA);bad=dict(c);bad['execution_authority']=True
 try:validate_candidate(bad,SHA)
 except ValueError:pass
 else:raise AssertionError('authoritative candidate accepted')
 p=json.loads((ROOT/'config/v7_multi_crypto_latency_capacity.json').read_text());p['venue_matching_delay_ms']=250
 try:validate_policy(p)
 except ValueError:pass
 else:raise AssertionError('unbound matching delay accepted')

if __name__=='__main__':
 tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
 for _,f in tests:f()
 print(f'{len(tests)} function tests passed')
