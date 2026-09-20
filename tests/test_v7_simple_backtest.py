"""Economic/causal invariants, not model-training tests."""
from dataclasses import replace
import gzip
import json
import pytest
from research.backtest.data import SAFETY, decision, snapshot
from research.backtest.run import Parameters,Tape,replay,metrics,evaluate,split,grid,validate,final_test,lead_lag


def opportunity(**changes):
    value=dict(id='a',market='m',asset='BTC',horizon='M5',ts=1000,end=111000,tte=110,p=.8,
        signal_age_ms=20,fee_rate=.07,fee_exponent=1,sides={'YES':dict(token='yes',ask=.5,bid=.49,minimum=5,tick=.01)},
        settlement=dict(tokens={'yes':1},ts=120000),external_return_bp=1,strong_signal=True)
    return dict(value,**changes)


def book(ts=1100,ask=.5,qty=10,**changes):
    return dict(dict(market='m',token='yes',ts=ts,bid=.49,ask=ask,qty=qty,valid=True),**changes)


def test_never_fill_predecision_or_predelay_book():
    r=replay([opportunity()],Tape([book(999),book(1099)]),Parameters(),100)[0]
    assert r['status']=='UNAVAILABLE' and r['pnl'] is None


def test_deterioration_causes_real_nonfill_beyond_bounded_chase_limit():
    r=replay([opportunity()],Tape([book(1100,.53)]),Parameters(),100)[0]
    assert r['status']=='NO_FILL' and r['pnl']==0 and r['deterioration']==pytest.approx(.03)


def test_partial_fill_known_fee_actual_cheaper_price_and_settlement():
    r=replay([opportunity()],Tape([book(1100,.49,2,bid=.48)]),Parameters(),100)[0]
    assert r['status']=='PARTIAL_FILL' and r['filled']==2
    assert r['fees']==pytest.approx(.03499) and r['pnl']==pytest.approx(2-.98-.03499)


def test_missing_settlement_never_loss_or_zero():
    r=replay([opportunity(settlement=None)],Tape([book()]),Parameters(),100)[0]
    assert r['filled']==5 and r['pnl'] is None
    m=metrics([r],1);assert m['net_pnl'] is None and m['pending_settlements']==1


def test_unavailable_first_attempt_cannot_be_replaced_by_later_lucky_fill():
    r=replay([opportunity(),opportunity(id='b',ts=2000)],Tape([book(2100)]),Parameters(),100)
    assert len(r)==1 and r[0]['status']=='UNAVAILABLE'


def test_conflicting_or_invalid_first_post_delay_snapshot_is_unavailable():
    for books in ([book(),book(1100,.51)],[book(valid=False),book(1101)]):
        assert replay([opportunity()],Tape(books),Parameters(),100)[0]['status']=='UNAVAILABLE'


def test_late_snapshot_and_expired_market_unavailable():
    assert replay([opportunity()],Tape([book(1151)]),Parameters(),100)[0]['status']=='UNAVAILABLE'
    assert replay([opportunity(end=1050)],Tape([book()]),Parameters(),100)[0]['reason']=='market_expired'


def test_missing_model_is_not_zero_pnl_evidence():
    m=evaluate([opportunity(p=None)],Tape([book()]),Parameters(),100)
    assert m['trades']==0 and m['net_pnl'] is None and m['observed_net_pnl'] is None
    assert m['status']=='MODEL_FORECASTS_UNAVAILABLE'


def test_sizing_caps_fee_reserve_and_minimum():
    r=replay([opportunity()],Tape([book(qty=100)]),replace(Parameters(),shares=20),100)[0]
    assert 5<=r['requested']<7.5 and r['turnover']+r['fees']<=3.75


def test_split_whole_markets_and_purge_overlap():
    rows=[opportunity(id=str(i),market=str(i),ts=1000+i*200000,end=120000+i*200000) for i in range(12)]
    rows.append(opportunity(id='late',market='0',ts=1001,end=99999999))
    parts,meta=split(rows)
    assert [len(parts[k]) for k in parts]==[6,3,3] and meta['purged_opportunities']==1
    assert max(r['end'] for r in parts['validation'])<=min(r['ts'] for r in parts['test'])


def test_lead_lag_down_token_is_already_directional():
    o=opportunity(sides={'NO':dict(token='no',ask=.5,bid=.49,minimum=5)},external_return_bp=-1)
    t=Tape([book(1100,.55,token='no',bid=.54)])
    r=lead_lag([o],t)[0]
    assert r['samples']==1 and r['mean_directional_pm_move']==pytest.approx(.05)


def test_small_grid_baseline_and_age_windows():
    g=grid();assert len(g)<40 and Parameters() in g
    assert {p.signal_age_ms for p in g}=={50,100,250,500}
    assert {p.tte_min for p in g}=={30,60,90,105}


def test_validation_freezes_and_test_reuses_without_retuning(tmp_path):
    obs=[opportunity(id=str(i),market=str(i),ts=i*200000+1000,end=i*200000+111000,p=None) for i in range(12)]
    data=dict(**SAFETY,opportunities=obs,books=[],model_hash=None,model_description='missing',exclusions={})
    path=tmp_path/'data.gz'
    with gzip.open(path,'wt') as f:json.dump(data,f)
    out=tmp_path/'result';validate(path,out)
    with pytest.raises(ValueError,match='already_frozen'):validate(path,out)
    final_test(path,out)
    assert final_test(path,out)['status']=='EXISTING_FINAL_TEST_REUSED'
    frozen=json.loads((out/'freeze.json').read_text())
    assert frozen['selection_status']=='INSUFFICIENT_VALIDATION_SUPPORT_BASELINE_FROZEN'


def test_epoch_change_never_reuses_another_connection():
    r=replay([opportunity(model_input={'connection_epoch':1})],Tape([book(epoch=2)]),Parameters(),100)[0]
    assert r['reason']=='capture_epoch_changed' and r['pnl'] is None


def test_native_adapter_rejects_future_features_and_uses_signal_age():
    from test_v7_cumulative_learning import native
    r=native(signal_valid=True,confirmed_non_opposing=True,minimum_order_microunits=5000000,
        fee_rate=.07,fee_exponent=1,paper_terms_sha256='terms',fee_source='GAMMA',connection_epoch=1)
    o=decision(r);assert o['p'] is None and o['signal_age_ms']==5 and o['asset']=='BTC'
    with pytest.raises(ValueError,match='causal_clock'):
        decision(dict(r,receive_monotonic_ns=r['decision_monotonic_ns']+1))


def test_settlement_preserves_actual_retrieval_and_reported_resolution():
    pytest.importorskip('sklearn')
    from research.backtest.fit import settlement
    raw=dict(id='m',closed=True,umaResolutionStatus='resolved',clobTokenIds=['yes','no'],
        outcomePrices=['1','0'],endDate='2026-09-20T17:00:00Z',closedTime='2026-09-20 17:00:54+00',
        umaEndDate='2026-09-20T17:00:54Z')
    observed=1789930000000000000
    value=settlement(raw,observed)
    assert value['retrieved_ns']==observed and value['information_ns']<observed
    assert 'RETROSPECTIVE' in value['availability_assumption']
    assert settlement(dict(raw,closed=False),observed) is None
    assert settlement(dict(raw,closedTime=None),observed) is None


def test_fit_cutoff_preserves_native_nanoseconds_at_epoch_scale():
    pytest.importorskip('sklearn')
    from research.backtest.fit import exact_boundaries
    stamp=1789925400000000001
    rows=[dict(market='v',model_input={'decision_wall_ns':stamp}),
          dict(market='v',model_input={'decision_wall_ns':stamp+100})]
    assert int(stamp/1e6*1e6)!=stamp
    assert exact_boundaries(rows,{'markets':{'validation':['v']}})['validation']==stamp
