from __future__ import annotations
import copy
import json
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'monitoring'))
from v7_native_settlement_projection import allocate_final, iter_position_economics, ProjectionError
from v7_multi_crypto_performance import summarize_multi_crypto
SHA = 'a' * 40


def fill(fid='f1', token='Y', qty=5, price=.4, fee=.01, side='BUY', component='crypto_informed_taker'):
    return {'event_type':'FILL','record_id':fid,'strategy':'CRYPTO_SETTLEMENT_ENGINE',
        'model_sha':SHA,'market_id':'m','paper_only':True,'authenticated_execution':False,
        'fill_id':fid,'order_id':'o'+fid,'position_id':'native-position:m:'+token,
        'token_id':token,'filled_size':qty,'fill_price':price,'fee':fee,'side':side,
        'metadata':{'run_id':'r','component':component,'model_family':component,
            'crypto_context':{'asset':'BTC','horizon':'M5'},
            'native_settlement_receipt':{'owner':'V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE',
                'model_sha':SHA,'single_owner':True}}}


def final(fills, winner='Y'):
    pnl = sum((1 if f['side']=='BUY' else -1)*f['filled_size']*((1 if f['token_id']==winner else 0)-f['fill_price'])-f['fee'] for f in fills)
    payout = sum((1 if f['side']=='BUY' else -1)*f['filled_size'] for f in fills if f['token_id']==winner)
    return {'event_type':'FINAL','record_id':'end','strategy':'CRYPTO_SETTLEMENT_ENGINE',
        'model_sha':SHA,'market_id':'m','paper_only':True,'authenticated_execution':False,
        'position_id':'native-market:m','final_pnl':pnl,'realized_cashflow':payout,
        'metadata':{'run_id':'r','native_market_settlement_id':'native-settlement:m',
            'component':'native_market_settlement','model_family':'native-paper-engine',
            'winning_token_id':winner,'included_fill_ids':[f['fill_id'] for f in fills]}}


@pytest.mark.parametrize('winner',['Y','N'])
def test_winning_and_losing_positions_close_and_reconcile(tmp_path, winner):
    fills=[fill('f1'),fill('f2','N',3,.3)]
    end=final(fills,winner)
    views=allocate_final(end,fills)
    assert sum(v['final_pnl'] for v in views)==pytest.approx(end['final_pnl'])
    assert {v['position_id'] for v in views}=={f['position_id'] for f in fills}
    (tmp_path/'ledger').mkdir()
    (tmp_path/'ledger/execution.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in [*fills,end]))
    out=summarize_multi_crypto(tmp_path,expected_sha=SHA,portfolio={},canonical={'strategy_net_pnl':{'CRYPTO_SETTLEMENT_ENGINE':end['final_pnl']}},global_coordinator={},crypto_registry={},crypto_model_registry={},ledger_valid=True)
    btc=next(x for x in out['lanes'] if (x['asset'],x['horizon'])==('BTC','M5'))
    assert btc['open_positions']==0 and btc['open_cost_at_risk']==0
    assert out['attribution']['reconciled']
    assert out['attribution']['unattributed_final_rows']==0


def test_cross_component_inventory_sale_is_additive():
    fills=[fill(),fill('f2',qty=2,price=.6,side='SELL',component='professional_maker')]
    end=final(fills)
    views=allocate_final(end,fills)
    assert len(views)==2
    assert sum(v['final_pnl'] for v in views)==pytest.approx(end['final_pnl'])
    assert sum(v['realized_cashflow'] for v in views)==3


def test_partial_fills_group_once_and_never_mutate_canonical():
    fills=[fill('f1',qty=2),fill('f2',qty=3)]
    end=final(fills);original=copy.deepcopy(end)
    views=allocate_final(end,fills)
    assert len(views)==1 and views[0]['position_id']==fills[0]['position_id']
    assert end==original
    assert views[0]['metadata']['projection_only'] is True


@pytest.mark.parametrize('bad',['missing_fill','duplicate_fill','wrong_pnl','wrong_payout','wrong_market','wrong_run','missing_winner','unknown_context','naked_sell'])
def test_bad_evidence_fails_closed(bad):
    fills=[fill()];end=final(fills)
    if bad=='missing_fill': end['metadata']['included_fill_ids']=[]
    if bad=='duplicate_fill': fills.append(copy.deepcopy(fills[0]))
    if bad=='wrong_pnl': end['final_pnl']+=1
    if bad=='wrong_payout': end['realized_cashflow']+=1
    if bad=='wrong_market': fills[0]['market_id']='another'
    if bad=='wrong_run': fills[0]['metadata']['run_id']='another'
    if bad=='missing_winner': end['metadata'].pop('winning_token_id')
    if bad=='unknown_context': fills[0]['metadata']={**fills[0]['metadata'],'model_family':'professional_maker','crypto_context':{}}
    if bad=='naked_sell': fills[0]['side']='SELL'
    with pytest.raises(ProjectionError): allocate_final(end,fills)


def test_duplicate_settlement_not_double_counted():
    fills=[fill()];end=final(fills);errors=[]
    rows=list(iter_position_economics([*fills,end,end],errors))
    assert len([x for x in rows if x['event_type']=='FINAL'])==1
    assert errors==['duplicate_native_final:m']


def test_fill_after_final_invalidates_projection():
    fills=[fill()];end=final(fills);errors=[]
    list(iter_position_economics([*fills,end,fill('late')],errors))
    assert errors==['fill_after_final:m']


def test_rollover_keeps_cost_basis_and_fee_until_verified_final():
    from v7_native_risk_policy import unsettled_exposure,remaining_capital_lease
    fills=[fill()]
    pending=unsettled_exposure(fills,SHA)
    assert pending['total_unsettled_microdollars']==2_010_000
    receipt={'limits':{'sleeve_budget_microdollars':10_000_000,'max_total_exposure_microdollars':10_000_000,
        'max_market_exposure_microdollars':10_000_000,'max_single_order_microdollars':10_000_000}}
    lease=remaining_capital_lease(receipt,pending)
    assert lease['limits']['sleeve_budget_microdollars']==7_990_000
    assert receipt['limits']['sleeve_budget_microdollars']==10_000_000
    after=unsettled_exposure([*fills,final(fills)],SHA)
    assert after['total_unsettled_microdollars']==0
    assert remaining_capital_lease(receipt,after)['limits']['sleeve_budget_microdollars']==10_000_000


def test_bad_final_cannot_release_unsettled_claim():
    from v7_native_risk_policy import unsettled_exposure
    fills=[fill()];end=final(fills);end['final_pnl']+=100
    with pytest.raises(ValueError):unsettled_exposure([*fills,end],SHA)


def test_unsettled_claims_can_exhaust_allowance_without_reset():
    from v7_native_risk_policy import remaining_capital_lease
    receipt={'limits':{'sleeve_budget_microdollars':1,'max_total_exposure_microdollars':1,
        'max_market_exposure_microdollars':1,'max_single_order_microdollars':1}}
    result=remaining_capital_lease(receipt,{'total_unsettled_microdollars':2})
    assert result['limits'] is None and not result['lease_available']


def test_market_commit_barrier_waits_for_canonical_ledger(tmp_path, monkeypatch):
    import types
    import v7_native_crypto_engine_manager as manager
    control=tmp_path/'control';control.mkdir();(tmp_path/'ledger/spool').mkdir(parents=True)
    status={'model_sha':SHA,'run_id':'r','market_id':'m','healthy':True,'dropped':0,'published':1,'written':1}
    (control/'native_evidence_status.json').write_text(json.dumps(status))
    monkeypatch.setattr(manager,'iter_events',lambda *a,**k:iter([]))
    monkeypatch.setattr(manager,'open_native_orders',lambda *a:{})
    assert not manager.native_commit_barrier(tmp_path,SHA,'r','m',timeout=.01)
    row=types.SimpleNamespace(record_id='r:m:native:00000000000000000001')
    monkeypatch.setattr(manager,'iter_events',lambda *a,**k:iter([row]))
    assert manager.native_commit_barrier(tmp_path,SHA,'r','m',timeout=.01)
    (tmp_path/'ledger/spool/r:m:native:00000000000000000001.json').write_text('{}')
    assert not manager.native_commit_barrier(tmp_path,SHA,'r','m',timeout=.01)
