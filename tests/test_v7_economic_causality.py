"""Regression evidence for the September economic-causality repair."""
from pathlib import Path
import copy
import sys
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import v7_canonical_economics as econ
from v7_market_execution_terms import snapshot, persist
from test_v7_native_settlement_projection import fill, final, SHA


def report_for(monkeypatch, fills, end):
    events = []
    for f in fills:
        f.update(event_id='event', exchange_ts_ms=100, receive_ts_ms=101, fee_source='test')
        f['metadata']['native_settlement_receipt'].update(paper_only=True)
        events.append(econ.ledger.LedgerEvent(event_type='ORDER_SUBMITTED',
            strategy=f['strategy'], model_sha=SHA, order_id=f['order_id'],
            token_id=f['token_id'], market_id='m', event_id='event',
            exchange_ts_ms=100, receive_ts_ms=101, decision_ts_ms=102,
            intended_action='TAKE', side=f['side'], intended_size=f['filled_size'],
            limit_price=f['fill_price'], book_snapshot_id='book', metadata=copy.deepcopy(f['metadata'])))
        events.append(econ.ledger.LedgerEvent(**f))
    end.update(event_id='event', fee=0., slippage=0., unwind_loss=0., capital_cost=0., latency_cost=0.)
    end['metadata'].update(realized=True, cost_vector_complete=True, unwind_accounted=True)
    events.append(econ.ledger.LedgerEvent(**end))
    monkeypatch.setattr(econ.ledger, 'iter_events', lambda *a, **k: iter(events))
    return econ.assess(Path('fixture-not-a-live-ledger'), expected_model_sha=SHA)


def test_aggregate_final_allocates_identity_decomposition_and_fees_once(monkeypatch):
    fs = [fill(), fill('f2', 'N', 3, .3)]
    end = final(fs); end.update(order_id=fs[0]['order_id'], fill_id=fs[0]['fill_id'])
    end['metadata']['pnl_decomposition'] = {'trading_pnl': end['final_pnl']}
    out = report_for(monkeypatch, fs, end)
    assert out['economic_units'] == out['mature_terminal_units'] == 2
    assert out['net_pnl'] == pytest.approx(end['final_pnl'])
    assert out['pnl_decomposition']['trading_pnl'] == pytest.approx(end['final_pnl'])
    assert out['costs']['components']['fee'] == pytest.approx(.02)
    assert not any('identity_conflict' in r for r in out['reason_codes'])
    assert 'native_paper_arrival_parity_unverified' in out['reason_codes']
    assert not out['economic_evidence_ready']


def test_cross_component_sale_keeps_additive_accounting(monkeypatch):
    fs = [fill(), fill('f2', qty=2, price=.6, side='SELL', component='professional_maker')]
    end = final(fs)
    out = report_for(monkeypatch, fs, end)
    assert out['economic_units'] == 2
    assert out['net_pnl'] == pytest.approx(end['final_pnl'])
    assert out['costs']['components']['fee'] == pytest.approx(.02)


def test_invalid_aggregate_is_not_reported_as_earned_pnl(monkeypatch):
    fs = [fill()]; end = final(fs); end['final_pnl'] += 100
    out = report_for(monkeypatch, fs, end)
    assert out['net_pnl'] is None
    assert any('native_projection:settlement_pnl:mismatch' in r for r in out['reason_codes'])


@pytest.mark.parametrize('itode,delay', [(True,250_000_000),(False,0)])
def test_market_terms_bind_tokens_and_persist_immutably(tmp_path, itode, delay):
    market = {'market_id':'m', 'condition_id':'0x'+'a'*64, 'clob_token_ids':['Y','N']}
    out = snapshot(market, lambda url: {'itode':itode, 't':[{'t':'Y'},{'t':'N'}]}, now_ns=100)
    assert out['state'] == 'VERIFIED_SNAPSHOT'
    assert out['mandatory_taker_delay_ns'] == delay
    path = persist(tmp_path, out)
    assert persist(tmp_path, out) == path
    bad = copy.deepcopy(out); bad['mandatory_taker_delay_ns'] = 123
    with pytest.raises(ValueError, match='hash_mismatch'): persist(tmp_path, bad)


@pytest.mark.parametrize('raw', [{}, {'itode':None}, {'itode':'false'}, {'itode':0},
    {'itode':False,'t':[{'t':'other'},{'t':'N'}]},
    {'itode':True,'t':[{'t':'Y'},{'t':'Y'}]}])
def test_unknown_or_mismatched_itode_is_never_zero_delay(raw):
    market = {'market_id':'m','condition_id':'0x'+'a'*64,'clob_token_ids':['Y','N']}
    out = snapshot(market, lambda url:raw, now_ns=100)
    assert out['state'] == 'UNKNOWN' and out['mandatory_taker_delay_ns'] is None


def test_invalid_condition_never_makes_an_http_request():
    def forbidden(url): raise AssertionError('unexpected network request')
    out = snapshot({'condition_id':'../../orders'}, forbidden, now_ns=100)
    assert out['state'] == 'UNKNOWN' and out['source'] is None
