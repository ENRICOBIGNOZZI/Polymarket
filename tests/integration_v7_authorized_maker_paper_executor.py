#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
from v7_global_portfolio_coordinator import process_cut
from test_v7_opportunity import envelope
SHA='a'*40

def write(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value),encoding='utf-8')

def maker_envelope(now:int)->dict:
    v=envelope(action='MAKE',ev=.20,key='authorized-maker-e2e',authority='PAPER_EXPLORATION')
    v.update(model_sha=SHA,market_id='market-1',event_id='event-1',contract_id='yes-token',decision_receive_timestamp_ns=now-20_000_000,source_event_timestamps_ns=[now-30_000_000],expires_at_ns=now+5_000_000_000,inventory_delta=5.,portfolio_exposure_delta=2.5)
    v['execution_plan']['timeout_ms']=2000
    v['execution_plan']['legs'][0].update(token_id='yes-token',contract_id='yes-token',market_id='market-1',target_quantity=5.,limit_price=.50)
    v['execution_alpha']={'schema':'polymarket_v7_execution_alpha_packet_v1','action':'MAKE','outcome':'YES','evidence_status':'MATURE','fill_probability':{'lower':.4,'point':.5,'upper':.6},'queue_ahead_shares':1.,'action_ev':{'conservative':.20,'point':.25},'attribution':{'settlement_alpha':.20,'spread_capture':0.,'rebate':0.,'fees':0.,'slippage':0.,'adverse_selection':0.,'latency':0.,'inventory':0.,'unwind':0.,'cancel':0.,'capital':0.}}
    return v

def setup_arrival(root:Path)->Path:
    write(root/'external_fair/paper_router_status.json',{'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'code_sha':SHA,'live_market':{'valid':True,'market_id':'market-1','receive_ts_ms':time.time_ns()//1_000_000,'execution_alpha_books':{'YES':{'token_id':'yes-token','best_bid':.50,'best_ask':.52,'best_bid_size':1.,'best_ask_size':10.,'tick_size':.01,'min_order_size':1.},'NO':{'token_id':'no-token','best_bid':.48,'best_ask':.50,'best_bid_size':1.,'best_ask_size':10.,'tick_size':.01,'min_order_size':1.}}}})
    d=root/'crypto_execution_alpha/fillability'; d.mkdir(parents=True,exist_ok=True); tape=d/'fillability_ws.jsonl'; tape.touch()
    write(d/'fillability_ws_status.json',{'schema':'polymarket_v7_maker_fillability_ws_status_v1','timestamp_ms':time.time_ns()//1_000_000,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'model_sha':SHA,'state':'running','evidence_complete':True,'last_exchange_event_ns':time.time_ns()-100_000_000})
    return tape

def spool_rows(root:Path)->list[dict]:
    d=root/'ledger/spool'
    return [json.loads(p.read_text()) for p in d.glob('*.json')] if d.exists() else []

def wait_event(root:Path,kind:str,timeout:float=1.5)->dict:
    end=time.time()+timeout
    while time.time()<end:
        row=next((r for r in spool_rows(root) if r.get('event_type')==kind),None)
        if row is not None:return row
        time.sleep(.005)
    raise AssertionError(f'missing {kind}')

def test_fill(binary:Path)->None:
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); now=time.time_ns()
        write(root/'opportunities/inbox/make.json',maker_envelope(now))
        decision=process_cut(root,now_ns=now)['last_decision']
        assert decision['action']=='MAKE' and decision['make_authorization_published'] is True
        tape=setup_arrival(root)
        proc=subprocess.Popen([str(binary),'--run-root',str(root),'--model-sha',SHA])
        try:
            submitted=wait_event(root,'ORDER_SUBMITTED')
            trade={'schema':'polymarket_v7_maker_fillability_ws_trade_v1','model_sha':SHA,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'observer_sequence':1,'market_id':'market-1','event_id':'event-1','token_id':'yes-token','instrument_handle':1,'state_version':1,'connection_epoch':1,'exchange_event_ns':int(submitted['paper_arrival_exchange_event_ns'])+10_000_000,'receive_wall_ms':time.time_ns()//1_000_000,'receive_monotonic_ns':int(submitted['paper_arrival_receive_monotonic_ns'])+10_000_000,'aggressor_side':'SELL','price':.50,'size':100.,'lineage_continuous':True}
            with tape.open('a',encoding='utf-8') as out: out.write(json.dumps(trade)+'\n'); out.flush()
            wait_event(root,'FILL')
            status=json.loads((root/'micro_maker/authorized_make_executor_status.json').read_text())
            assert status['trade_causally_pre_arrival']==0
            assert status['trade_wrong_aggressor_side']==0
            assert status['trade_price_not_crossing']==0
            assert status['trade_eligible_orders']==1
            assert status['trade_queue_not_depleted']==0
            assert status['trade_operational_fill_microunits']==5_000_000
        finally:
            proc.terminate()
            try: proc.wait(timeout=1)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()

def test_bootstrap_defers(binary:Path)->None:
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); now=time.time_ns()
        write(root/'opportunities/inbox/make.json',maker_envelope(now))
        assert process_cut(root,now_ns=now)['last_decision']['action']=='MAKE'
        setup_arrival(root)
        (root/'crypto_execution_alpha/fillability/fillability_ws_status.json').unlink()
        subprocess.run([str(binary),'--run-root',str(root),'--model-sha',SHA,'--once'],check=True)
        status=json.loads((root/'micro_maker/authorized_make_executor_status.json').read_text())
        assert status['submitted_orders']==0
        assert status['rejected_authorizations']==0
        assert status['deferred_authorizations']==1
        assert len(list((root/'micro_maker/authorized_make').glob('*.json')))==1


def cancel_envelope(now:int, order_id:str, target_replay_key:str, remaining:float)->dict:
    v=envelope(action='CANCEL',ev=.10,key=f'cancel-{order_id}',authority='PAPER_EXPLORATION')
    v.update(model_sha=SHA,market_id='market-1',event_id='event-1',contract_id='yes-token',decision_receive_timestamp_ns=now-20_000_000,source_event_timestamps_ns=[now-30_000_000],expires_at_ns=now+5_000_000_000,inventory_delta=0.,portfolio_exposure_delta=0.)
    v['side']='NONE'
    v['execution_plan']={'atomic_unit_id':target_replay_key,'execution_style':'SINGLE_LEG','legs':[{'leg_id':order_id,'market_id':'market-1','contract_id':'yes-token','token_id':'yes-token','side':'BUY','target_quantity':remaining,'limit_price':.50,'fee_authority':'CONSERVATIVE_ZERO'}],'partial_fill_plan':'CANCEL_REMAINDER','timeout_ms':100,'unwind_plan':'CANCEL_ONLY'}
    v['execution_alpha']={'schema':'polymarket_v7_execution_alpha_packet_v1','action':'CANCEL','outcome':'NONE','evidence_status':'MATURE','fill_probability':{'lower':0.,'point':0.,'upper':0.},'queue_ahead_shares':0.,'action_ev':{'conservative':.10,'point':.10},'attribution':{'settlement_alpha':0.,'spread_capture':0.,'rebate':0.,'fees':0.,'slippage':0.,'adverse_selection':.10,'latency':0.,'inventory':0.,'unwind':0.,'cancel':0.,'capital':0.}}
    v['cost_vector']={k:0. for k in ('fee','slippage','unwind_loss','capital_cost','latency_cost','adverse_markout','rebate')}
    v['cost_authority']={k:'CONSERVATIVE_ZERO' for k in v['cost_vector']}
    v['calibration_status']='NOT_APPLICABLE';v['uncertainty']={'lower_bound':0.,'upper_bound':0.,'status':'MATURE'}
    return v

def wait_status(root:Path,predicate,timeout:float=1.5)->dict:
    path=root/'micro_maker/authorized_make_executor_status.json';end=time.time()+timeout
    while time.time()<end:
        try: status=json.loads(path.read_text())
        except (OSError,json.JSONDecodeError): status={}
        if predicate(status): return status
        time.sleep(.005)
    raise AssertionError(f'status predicate not reached: {status}')

def test_cancel_latency(binary:Path)->None:
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); now=time.time_ns();tape=setup_arrival(root)
        write(root/'opportunities/inbox/make.json',maker_envelope(now))
        assert process_cut(root,now_ns=now)['last_decision']['action']=='MAKE'
        proc=subprocess.Popen([str(binary),'--run-root',str(root),'--model-sha',SHA])
        try:
            submitted=wait_event(root,'ORDER_SUBMITTED')
            status=wait_status(root,lambda x:x.get('active_orders')==1)
            active=status['active_order_details'][0]
            now2=time.time_ns();write(root/'opportunities/inbox/cancel.json',cancel_envelope(now2,active['order_id'],active['replay_key'],active['remaining_shares']))
            decision=process_cut(root,now_ns=now2)['last_decision']
            assert decision['action']=='CANCEL' and decision['cancel_authorization_published'] is True
            status=wait_status(root,lambda x:(x.get('active_order_details') or [{}])[0].get('cancel_requested') is True)
            active=status['active_order_details'][0];cancel_ns=int(active['cancel_requested_monotonic_ns']);arrival_ex=int(active['arrival_exchange_event_ns'])
            trades=[
                {'schema':'polymarket_v7_maker_fillability_ws_trade_v1','model_sha':SHA,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'observer_sequence':11,'market_id':'market-1','event_id':'event-1','token_id':'yes-token','instrument_handle':1,'state_version':11,'connection_epoch':1,'exchange_event_ns':arrival_ex+10_000_000,'receive_wall_ms':time.time_ns()//1_000_000,'receive_monotonic_ns':cancel_ns+50_000_000,'aggressor_side':'SELL','price':.50,'size':2.,'lineage_continuous':True},
                {'schema':'polymarket_v7_maker_fillability_ws_trade_v1','model_sha':SHA,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'observer_sequence':12,'market_id':'market-1','event_id':'event-1','token_id':'yes-token','instrument_handle':1,'state_version':12,'connection_epoch':1,'exchange_event_ns':arrival_ex+20_000_000,'receive_wall_ms':time.time_ns()//1_000_000,'receive_monotonic_ns':cancel_ns+150_000_000,'aggressor_side':'SELL','price':.50,'size':100.,'lineage_continuous':True},
            ]
            with tape.open('a',encoding='utf-8') as out:
                for trade in trades: out.write(json.dumps(trade)+'\n')
                out.flush()
            wait_event(root,'FILL');wait_event(root,'ORDER_STATE')
            status=wait_status(root,lambda x:x.get('trade_rows_consumed',0)>=2 and x.get('terminal_orders',0)>=1)
            assert status['trade_cancel_effective_before']>=1
            assert status['trade_operational_fill_microunits']==500_000
            fills=[r for r in spool_rows(root) if r.get('event_type')=='FILL']
            assert len(fills)==1 and abs(float(fills[0]['filled_size'])-.5)<1e-12
        finally:
            proc.terminate()
            try: proc.wait(timeout=1)
            except subprocess.TimeoutExpired: proc.kill();proc.wait()

def main()->None:
    parser=argparse.ArgumentParser();parser.add_argument('--binary',type=Path,required=True);args=parser.parse_args()
    binary=args.binary.resolve();test_fill(binary);test_bootstrap_defers(binary);test_cancel_latency(binary)
    print('authorized maker PAPER integration passed')

if __name__=='__main__': main()
