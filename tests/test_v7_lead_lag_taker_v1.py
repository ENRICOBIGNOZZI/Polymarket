from __future__ import annotations
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_lead_lag_taker_runtime import signal_candidate, validate_config

SHA='a'*40
NOW=10_000_000_000

def cfg():
    return validate_config(json.loads((ROOT/'config/v7_lead_lag_taker_v1.json').read_text()))

def signal(direction='UP', age_ms=100, bp=0.4):
    return {
        'schema':'polymarket_v7_btc_m5_external_cancel_live_signal_v2','code_sha':SHA,
        'rule_id':'btc-m5-external-cancel-v1','rule_sha256':cfg()['source_rule_sha256'],
        'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
        'real_money_authority':False,'research_only':True,'shock_source':'BINANCE_SPOT_TRADES',
        'confirmation_source':'COINBASE_SPOT_TOP_OF_BOOK','confirmation':'NON_OPPOSING',
        'confirmed_non_opposing':True,'signal_version':7,
        'trigger_receive_wall_ns':NOW-age_ms*1_000_000,
        'binance_return_100ms_bp':bp,'coinbase_return_100ms_bp':bp,'direction':direction,
    }

def status(tte=110.0):
    return {
        'code_sha':SHA,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
        'market':{'market_id':'m1','event_id':'e1','yes_token':'yes','no_token':'no',
                  'accepting_orders':True,'closed':False},
        'contract':{'verified':True,'rules_hash_recognized':True},
        'settlement_reference':{'valid':True},
        'causal_observation':{'cut':{'observed_tte_seconds':tte,'observed_wall_ns':NOW}},
    }

def test_frozen_rule_maps_up_to_yes_and_down_to_no():
    row,reason=signal_candidate(cfg(),signal('UP'),status(),model_sha=SHA,current_ns=NOW)
    assert reason=='ELIGIBLE' and row['outcome']=='YES' and row['token_id']=='yes'
    row,reason=signal_candidate(cfg(),signal('DOWN'),status(),model_sha=SHA,current_ns=NOW)
    assert reason=='ELIGIBLE' and row['outcome']=='NO' and row['token_id']=='no'

def test_rule_fails_closed_outside_frozen_time_and_signal_age():
    row,reason=signal_candidate(cfg(),signal('UP',age_ms=5001),status(),model_sha=SHA,current_ns=NOW)
    assert row is None and reason=='SIGNAL_TOO_OLD'
    row,reason=signal_candidate(cfg(),signal(),status(104.99),model_sha=SHA,current_ns=NOW)
    assert row is None and reason=='TTE_OUTSIDE_FROZEN_WINDOW'
    row,reason=signal_candidate(cfg(),signal(),status(120.01),model_sha=SHA,current_ns=NOW)
    assert row is None and reason=='TTE_OUTSIDE_FROZEN_WINDOW'

def test_rule_requires_original_hard_signal_contract():
    bad=signal(); bad['confirmed_non_opposing']=False
    row,reason=signal_candidate(cfg(),bad,status(),model_sha=SHA,current_ns=NOW)
    assert row is None and reason=='SIGNAL_CONTRACT_INVALID'
    weak=signal(bp=.299)
    row,reason=signal_candidate(cfg(),weak,status(),model_sha=SHA,current_ns=NOW)
    assert row is None and reason=='SIGNAL_TOO_WEAK'

import tempfile, time
from unittest import mock
from v7_lead_lag_taker_runtime import LeadLagRuntime
from v7_ledger_spool import drain_spool
from v7_execution_ledger import canonical_ledger_path, iter_events

def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')

def _live_status(sha: str, now_ns: int, tte: float = 110.0) -> dict:
    return {
        'code_sha':sha,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
        'market':{'market_id':'m-live','event_id':'e-live','yes_token':'yes','no_token':'no',
                  'accepting_orders':True,'closed':False,
                  'fee_schedule':{'rate':0.07,'exponent':1,'takerOnly':True}},
        'contract':{'verified':True,'rules_hash_recognized':True},
        'settlement_reference':{'valid':True},
        'causal_observation':{'cut':{'observed_tte_seconds':tte,'observed_wall_ns':now_ns}},
    }

def _live_signal(sha: str, now_ns: int, direction: str='UP') -> dict:
    return {
        'schema':'polymarket_v7_btc_m5_external_cancel_live_signal_v2','code_sha':sha,
        'rule_id':'btc-m5-external-cancel-v1','rule_sha256':cfg()['source_rule_sha256'],
        'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
        'real_money_authority':False,'research_only':True,'shock_source':'BINANCE_SPOT_TRADES',
        'confirmation_source':'COINBASE_SPOT_TOP_OF_BOOK','confirmation':'NON_OPPOSING',
        'confirmed_non_opposing':True,'signal_version':17,
        'trigger_receive_wall_ns':now_ns-100_000_000,
        'binance_return_100ms_bp':0.7,'coinbase_return_100ms_bp':0.2,'direction':direction,
    }

def _books(now_ms: int, yes_ask: float=.40, no_ask: float=.61):
    return [
        {'asset_id':'yes','timestamp':str(now_ms),'hash':f'y-{yes_ask}','tick_size':'0.01','min_order_size':'5',
         'bids':[{'price':str(round(yes_ask-.01,2)),'size':'100'}],
         'asks':[{'price':str(yes_ask),'size':'100'}]},
        {'asset_id':'no','timestamp':str(now_ms),'hash':f'n-{no_ask}','tick_size':'0.01','min_order_size':'5',
         'bids':[{'price':str(round(no_ask-.01,2)),'size':'100'}],
         'asks':[{'price':str(no_ask),'size':'100'}]},
    ]

def _receipt(replay: str) -> dict:
    return {'schema':'polymarket_v7_global_opportunity_decision_v1','owner':'V7_GLOBAL_PORTFOLIO_COORDINATOR',
            'engine_id':'CRYPTO_SETTLEMENT_ENGINE','action':'TAKE','selected_replay_key':replay,
            'new_risk_authorized':False,'paper_exploration_authorized':True,
            'paper_exploration_probe_authorized':False,'paper_forward_test_authorized':True,
            'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
            'real_capital_at_risk':False,
            'crypto_context':{'asset':'BTC','horizon':'M5','authority':'PAPER_EXPLORATION'}}

def _runtime_case(root: Path, *, arrival_yes_ask: float=.40):
    sha='a'*40; now_ns=time.time_ns()
    _write(root/'control/runtime_status.json', {'schema':'polymarket_v7_runtime_status_v3','model_sha':sha,
           'config_hash':'b'*40,'policy_hash':'c'*40,'run_id':'r1','paper_only':True,
           'authenticated_execution':False,'real_order_submission':False})
    _write(root/'external_fair/status.json', _live_status(sha,now_ns))
    _write(root/'external_fair/external_cancel_signal.json', _live_signal(sha,now_ns))
    r=LeadLagRuntime(root,sha,ROOT/'config/v7_lead_lag_taker_v1.json','https://clob.invalid','https://gamma.invalid')
    stamp=time.time_ns()//1_000_000
    r.clob.request_books=mock.Mock(side_effect=[_books(stamp,.40,.61),_books(stamp,arrival_yes_ask,.61)])
    def receipt(key): return _receipt(key)
    r.wait_receipt=mock.Mock(side_effect=receipt)
    return r

def test_forward_runtime_fill_and_settlement_are_canonical_paper_events():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d); r=_runtime_case(root)
        r.candidate_step()
        rows=[json.loads(p.read_text()) for p in sorted((root/'ledger/spool').glob('*.json'))]
        assert [x['event_type'] for x in rows]==['ORDER_SUBMITTED','FILL']
        order,fill=rows
        capacity_book=order['metadata']['capacity_book']
        assert capacity_book['schema']=='polymarket_v7_lead_lag_capacity_book_v1'
        assert capacity_book['ask_levels']==[{'price':0.4,'size':100.0}]
        assert capacity_book['fee_schedule']['rate']==0.07
        assert fill['filled_size']==5.0 and fill['fill_price']==0.40
        assert fill['metadata']['paper_forward_test'] is True
        assert fill['metadata']['hold_to_settlement'] is True
        assert fill['metadata']['entry_uses_absolute_fair'] is False
        assert fill['metadata']['coordinator_receipt']['paper_forward_test_authorized'] is True
        assert r.state['entries']==1 and r.state['traded_markets']==['m-live']
        pos=next(iter(r.state['positions'].values())); pos['resolution_due_ms']=0
        settled={'closed':True,'outcomes':'["Up","Down"]','clobTokenIds':'["yes","no"]','outcomePrices':'["1","0"]'}
        with mock.patch('v7_lead_lag_taker_runtime.request_json',return_value=settled):
            r.settle_positions()
        rows=[json.loads(p.read_text()) for p in sorted((root/'ledger/spool').glob('*.json'))]
        final=next(x for x in rows if x['event_type']=='FINAL')
        assert final['final_pnl']>0 and final['metadata']['won'] is True
        assert final['metadata']['coordinator_receipt']['paper_forward_test_authorized'] is True
        result=drain_spool(root,model_sha='a'*40)
        assert result['appended']==3 and result['quarantined']==0
        canonical=list(iter_events(canonical_ledger_path(root),expected_model_sha='a'*40))
        assert [x.event_type for x in canonical]==['ORDER_SUBMITTED','FILL','FINAL']
        assert canonical[-1].metadata['paper_forward_test'] is True
        assert canonical[-1].metadata['coordinator_receipt']['selected_replay_key']
        assert r.state['settled']==1 and r.state['wins']==1

def test_forward_runtime_disk_pressure_blocks_before_book_or_receipt() -> None:
    with tempfile.TemporaryDirectory() as d:
        root=Path(d); r=_runtime_case(root)
        (root/'control/DISK_PRESSURE').write_text('{}')
        r.clob.request_books.reset_mock(); r.wait_receipt.reset_mock()
        r.candidate_step()
        assert r.clob.request_books.call_count==0 and r.wait_receipt.call_count==0
        assert r.state['entries']==0
        assert r.state['skip_reasons']['DISK_PRESSURE']==1
        assert not (root/'ledger/spool').exists()


def test_forward_runtime_never_chases_a_worse_arrival_ask():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d); r=_runtime_case(root,arrival_yes_ask=.41)
        r.candidate_step()
        rows=[json.loads(p.read_text()) for p in sorted((root/'ledger/spool').glob('*.json'))] if (root/'ledger/spool').exists() else []
        assert rows==[]
        assert r.state['entries']==0 and r.state['arrival_rejections']==1
        assert r.state['skip_reasons']['ARRIVAL_NO_CHASE_OR_DEPTH']==1


if __name__=='__main__':
    test_frozen_rule_maps_up_to_yes_and_down_to_no()
    test_rule_fails_closed_outside_frozen_time_and_signal_age()
    test_rule_requires_original_hard_signal_contract()
    test_forward_runtime_fill_and_settlement_are_canonical_paper_events()
    test_forward_runtime_disk_pressure_blocks_before_book_or_receipt()
    test_forward_runtime_never_chases_a_worse_arrival_ask()
    print('6 lead-lag taker tests passed')
