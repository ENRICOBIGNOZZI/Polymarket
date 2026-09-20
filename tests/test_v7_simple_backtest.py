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


def test_deterioration_causes_real_nonfill_at_native_limit():
    r=replay([opportunity()],Tape([book(1100,.52)]),Parameters(),100)[0]
    assert r['status']=='NO_FILL' and r['pnl']==0 and r['deterioration']==pytest.approx(.02)


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
