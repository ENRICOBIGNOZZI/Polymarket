from pathlib import Path
import json
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'research/economic'))
from causal_replay import Book,BookTape,Order,CausalReplay,EvidenceError,RestingOrder,allocate_print
from inference import paired_block_report,chronological_split
from evidence import freeze,verify,restore_test


def book(t=1_000_000_000,ask=4100,qty=5_000_000,version=1):
    return Book('m','Y','capture',t,version,((ask-100,qty),),((ask,qty),),100)


def order(id='a',t=1_000_000_000,delay=0,quantity=5_000_000,full=True,side='BUY',limit=4100):
    return Order(id,'m','Y','capture',t,side,limit,quantity,1_000_000,delay,full)


def replay(books=None,gaps=None):
    return CausalReplay(BookTape.from_books(books or [book()],{'capture':2_000_000_000},gaps),rate=.07)


def test_future_book_never_used_early():
    sim=replay([book(),book(1_020_000_000,ask=4300,version=2)])
    first=sim.execute(order())
    assert first.status=='FILLED' and first.average_price==.41
    second=sim.execute(order('later',delay=25_000_000))
    assert second.status=='NONFILL'


def test_delay_arrival_outside_capture_is_missing_not_zero():
    sim=replay()
    result=sim.execute(order(delay=2_000_000_000))
    assert result.status=='CENSORED' and result.average_price is None


def test_same_liquidity_cannot_fill_twice_even_new_message_version():
    sim=replay([book(),book(1_001_000_000,version=2)])
    assert sim.execute(order()).status=='FILLED'
    assert sim.execute(order('b',t=1_001_000_000)).status=='NONFILL'


def test_additional_visible_depth_only_adds_incremental_capacity():
    sim=replay([book(),book(1_001_000_000,qty=7_000_000,version=2)])
    sim.execute(order())
    result=sim.execute(order('b',t=1_001_000_000,full=False))
    assert result.status=='PARTIAL' and result.filled_quantity==2_000_000


def test_observed_nonfill_different_from_gap():
    sim=replay(gaps={'capture':[(1_005_000_000,1_030_000_000)]})
    result=sim.execute(order(delay=10_000_000))
    assert result.status=='CENSORED' and result.reason=='capture_gap'


def test_sell_needs_inventory_and_uses_bid_not_mid():
    sim=replay()
    assert sim.execute(order(side='SELL',limit=4000)).status=='REJECTED'
    sim.execute(order('buy'))
    sold=sim.execute(order('sell',side='SELL',limit=4000))
    assert sold.average_price==.40
    assert sim.cash<-.05


def test_duplicate_order_and_reversed_time_rejected():
    sim=replay();sim.execute(order())
    with pytest.raises(EvidenceError):sim.execute(order())
    with pytest.raises(EvidenceError):sim.execute(order('old',t=999_999_999))


def test_settlement_only_once_zeroes_inventory():
    sim=replay();sim.execute(order());cash=sim.cash
    assert sim.settlement('m','Y')==5
    assert sim.settlement('m','Y')==0
    assert sim.cash==pytest.approx(cash+5)


def test_maker_prearrival_and_cancel_effective_boundary():
    o=RestingOrder('a','m','Y','BUY',4000,5,3,10,cancel_effective_ns=30)
    assert not allocate_print([o],market='m',token='Y',ts_ns=10,aggressor='SELL',price_e4=4000,quantity=100)
    assert allocate_print([o],market='m',token='Y',ts_ns=20,aggressor='SELL',price_e4=4000,quantity=5)=={'a':2}
    assert not allocate_print([o],market='m',token='Y',ts_ns=30,aggressor='SELL',price_e4=4000,quantity=100)
    assert o.remaining==3


def test_maker_print_conservation_and_token_binding():
    orders=[RestingOrder('a','m','Y','BUY',4000,5,0,10),RestingOrder('b','m','Y','BUY',4000,5,0,11),RestingOrder('x','m','N','BUY',4000,5,0,1)]
    result=allocate_print(orders,market='m',token='Y',ts_ns=20,aggressor='SELL',price_e4=4000,quantity=7)
    assert result=={'a':5,'b':2} and sum(result.values())==7


def test_inference_never_treats_missing_as_zero_or_messages_as_markets():
    rows=[{'block':'one_hour','market':str(i),'baseline':0,'candidate':1,'complete':True} for i in range(100)]
    result=paired_block_report(rows)
    assert result['state']=='INCONCLUSIVE' and result['observed_blocks']==1
    assert result['ci95_lower'] is None
    with pytest.raises(ValueError):paired_block_report([rows[0],rows[0]])
    rows[0]['complete']=False
    assert paired_block_report(rows)['missing_rows']==1


def test_block_inference_repeatable_and_no_automatic_promotion():
    rows=[{'block':f'{i:03}','market':str(i),'baseline':0,'candidate':1,'complete':True} for i in range(30)]
    a=paired_block_report(rows,repetitions=200);b=paired_block_report(rows,repetitions=200)
    assert a==b and a['ci95_lower']==1
    assert a['automatic_promotion'] is False and a['real_money_authorized'] is False


def test_label_overlap_is_purged():
    rows=[{'market':'a','start_ns':10,'label_end_ns':15},{'market':'b','start_ns':19,'label_end_ns':25},{'market':'c','start_ns':23,'label_end_ns':28},{'market':'d','start_ns':33,'label_end_ns':40}]
    result=chronological_split(rows,20,30,2)
    assert [r['market'] for r in result['purged']]==['b']
    assert [r['market'] for r in result['train']]==['a']
    assert [r['market'] for r in result['validation']]==['c']
    assert [r['market'] for r in result['test']]==['d']


def test_freeze_hash_restore_and_no_source_delete(tmp_path):
    src=tmp_path/'source';src.mkdir();(src/'tape.jsonl').write_text('observed\n')
    out=tmp_path/'frozen';manifest=freeze(src,out,['tape.jsonl'],identity={'sha':'a'*40})
    assert verify(out)==manifest and restore_test(out)['passed']
    assert (src/'tape.jsonl').exists()
    (out/'tape.jsonl').write_text('corrupted')
    with pytest.raises(ValueError):verify(out)


def test_freeze_refuses_path_escape(tmp_path):
    src=tmp_path/'source';src.mkdir()
    with pytest.raises(ValueError):freeze(src,tmp_path/'dest',['../escape'],identity={})


def test_capital_remains_reserved_across_rollover():
    from risk_capacity import CapitalClaims
    claims=CapitalClaims(10,10,10)
    assert claims.reserve('a','market1',8);claims.fill('a',8)
    assert claims.available==2
    assert not claims.reserve('b','market2',5)
    claims.settle('market1',10)
    assert claims.available==12
    assert claims.reserve('b','market2',5)
    with pytest.raises(ValueError):claims.settle('market1',10)


def test_model_training_cannot_read_future_and_hash_is_checked():
    import copy
    from models import fit_settlement_residual,predict,settlement_edge
    rows=[{'market':str(i),'decision_ns':10+i,'label_observed_ns':200+i,
        'complete':True,'outcome':i%2,'pm_probability':.5,'features':{'return':(-1 if i%2==0 else 1)}} for i in range(20)]
    with pytest.raises(ValueError):fit_settlement_residual(rows,feature_names=['return'],train_end_ns=210,dataset_sha256='a'*64,minimum_markets=20)
    model=fit_settlement_residual(rows,feature_names=['return'],train_end_ns=300,dataset_sha256='a'*64,minimum_markets=20)
    future=[{**rows[1],'decision_ns':400}]
    assert predict(model,future)[0]>.5
    corrupt=copy.deepcopy(model);corrupt['coefficients'][0]+=1
    with pytest.raises(ValueError):predict(corrupt,future)
    assert model['heldout_validated'] is False
    assert settlement_edge(.96,.96,.002688,0)<0


def test_report_keeps_missing_native_data_inconclusive(tmp_path):
    import importlib.util
    spec=importlib.util.spec_from_file_location('economic_report',ROOT/'research/economic/run.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    src=tmp_path/'source';src.mkdir();(src/'ledger').mkdir();(src/'ledger/execution.jsonl').write_text('')
    dataset=tmp_path/'dataset';freeze(src,dataset,['ledger/execution.jsonl'],identity={'sha':'a'*40})
    report=mod.analyze(dataset,json.loads((ROOT/'research/economic/protocol.json').read_text()))
    assert report['state']=='INCONCLUSIVE' and not report['ledger']['profitability_proven']
    assert report['ledger']['observed_realized_paper_pnl'] is None
    assert set(report['tests'])=={'T1','T2','T3','T4','T5','T6','T7','T8'}


def test_capacity_sweep_does_not_bypass_existing_order_limit():
    sim=replay([book(ask=9600,qty=50_000_000)])
    result=sim.execute(order(quantity=20_000_000,limit=9600))
    assert result.status=='REJECTED' and result.reason=='capital_or_notional_cap'


def test_inference_reports_multiplicity_adjusted_interval():
    rows=[{'block':f'{i:03}','market':str(i),'baseline':0,'candidate':i%3,'complete':True} for i in range(30)]
    result=paired_block_report(rows,repetitions=200,family_size=8)
    assert result['interval_alpha']==.05/8
    assert result['ci95_lower'] is None
    assert result['confidence_lower'] is not None


def test_native_suballocation_requires_single_canonical_allocator():
    sys.path.insert(0,str(ROOT/'scripts'))
    from v7_native_risk_policy import load_native_limits
    policy=json.loads((ROOT/'config/v7_native_risk_policy.json').read_text())
    allocation={'schema':'polymarket_v7_capital_allocation_v3','paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'capital_authority_owner':'V7_CANONICAL_ALLOCATOR','capital_authority_owner_count':1,
        'account_starting_capital':14000,'reserve_budget':4000,'engine_budgets':{'CRYPTO_SETTLEMENT_ENGINE':10000}}
    result=load_native_limits(policy,allocation)
    assert result['canonical_engine_budget_microdollars']==10_000_000_000
    assert result['native_suballocation_microdollars']==10_000_000_000
    assert result['limits']['max_market_exposure_microdollars']==333_333_333
    assert result['limits_increased'] is False
    allocation['capital_authority_owner_count']=2
    with pytest.raises(ValueError):load_native_limits(policy,allocation)


def test_native_suballocation_never_exceeds_portfolio_allocation():
    from v7_native_risk_policy import load_native_limits
    policy=json.loads((ROOT/'config/v7_native_risk_policy.json').read_text())
    allocation={'schema':'polymarket_v7_capital_allocation_v3','paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'capital_authority_owner':'V7_CANONICAL_ALLOCATOR','capital_authority_owner_count':1,
        'account_starting_capital':500,'reserve_budget':0,'engine_budgets':{'CRYPTO_SETTLEMENT_ENGINE':500}}
    with pytest.raises(ValueError):load_native_limits(policy,allocation)


def test_price_aware_break_even_and_loss_asymmetry() -> None:
    from price_aware import normalize_trade
    row = normalize_trade({
        "market": "m1", "asset": "BTC", "horizon": "M5",
        "entry_price": 0.99, "shares": 20, "fee_usd": 0.01386,
        "outcome_payout": 1,
    })
    assert row["break_even_probability"] == pytest.approx(0.990693)
    assert row["win_gain_per_share"] == pytest.approx(0.009307)
    assert row["max_loss_per_share"] == pytest.approx(0.990693)
    assert row["loss_to_win_ratio"] > 100


def test_price_aware_max_loss_sizing_reduces_high_price_exposure() -> None:
    from price_aware import max_loss_capped_shares
    expensive = {
        "market": "m1", "asset": "BTC", "horizon": "M5",
        "entry_price": 0.99, "shares": 20, "fee_usd": 0.01386,
        "outcome_payout": 0,
    }
    cheap = {
        "market": "m2", "asset": "DOGE", "horizon": "M5",
        "entry_price": 0.03, "shares": 20, "fee_usd": 0.04074,
        "outcome_payout": 0,
    }
    assert max_loss_capped_shares(expensive, 5) < 5.1
    assert max_loss_capped_shares(cheap, 5) == pytest.approx(20)


def test_price_aware_summary_keeps_fees_separate_from_pre_fee_edge() -> None:
    from price_aware import summarize
    report = summarize([
        {"market": "m1", "asset": "BTC", "horizon": "M5",
         "entry_price": 0.96, "shares": 5, "fee_usd": 0.01344, "outcome_payout": 1},
        {"market": "m2", "asset": "BTC", "horizon": "M5",
         "entry_price": 0.95, "shares": 5, "fee_usd": 0.016625, "outcome_payout": 0},
    ])
    overall = report["overall"]
    assert overall["trades"] == 2
    assert overall["fees_usd"] == pytest.approx(0.030065)
    assert overall["pre_fee_pnl_usd"] == pytest.approx(-4.55)
    assert overall["net_pnl_usd"] == pytest.approx(-4.580065)
    assert report["extreme_price_diagnostics"]["price_ge_0_95"]["trades"] == 2


def test_price_aware_extracts_taker_market_from_canonical_ledger(tmp_path) -> None:
    from price_aware import trades_from_ledger
    ledger=tmp_path/'execution.jsonl'
    fill={
        'event_type':'FILL','strategy':'CRYPTO_SETTLEMENT_ENGINE','model_sha':'a'*40,
        'market_id':'m1','token_id':'yes','filled_size':20.0,'fill_price':.76,'fee':.25,
        'metadata':{'model_family':'crypto_informed_taker','asset':'BTC','horizon':'M5'}
    }
    final={
        'event_type':'FINAL','strategy':'CRYPTO_SETTLEMENT_ENGINE','model_sha':'a'*40,
        'market_id':'m1','metadata':{'native_market_settlement_id':'native-settlement:m1',
        'settlement_payouts':{'yes':1.0,'no':0.0}}
    }
    ledger.write_text(json.dumps(fill)+'\n'+json.dumps(final)+'\n')
    rows,stats=trades_from_ledger(ledger,expected_model_sha='a'*40)
    assert stats=={'taker_markets':1,'resolved_taker_markets':1,'missing_final':0,'invalid_markets':0}
    assert rows[0]['entry_price']==.76 and rows[0]['shares']==20
    assert rows[0]['outcome_payout']==1.0

def test_tte_window_diagnostics_changes_only_tte_and_stays_zero_authority():
    import importlib.util
    spec=importlib.util.spec_from_file_location('economic_report_tte',ROOT/'research/economic/run.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    protocol={
        'quantity_shares':[5],
        'tte_windows_seconds':[[90,105],[105,120]],
        'baseline_tte_window_seconds':[105,120],
    }
    common={
        'kind':2,'book_valid':True,'confirmed_non_opposing':True,
        'minimum_order_microunits':1_000_000,'fee_rate':.07,'fee_exponent':1.0,
        'asks':[[4000,10_000_000]],'ask_e4':4000,'ask_quantity':10_000_000,
        'signal_age_ns':50_000_000,'_capture':'c',
    }
    points=[
        {**common,'market_id':'m1','token_id':'Y','reason':7,'tte_ns':100_000_000_000,
         'decision_wall_ns':90,'decision_monotonic_ns':90},
        {**common,'market_id':'m1','token_id':'Y','reason':6,'tte_ns':100_000_000_000,
         'decision_wall_ns':100,'decision_monotonic_ns':100},
        {**common,'market_id':'m1','token_id':'N','reason':1,'tte_ns':110_000_000_000,
         'decision_wall_ns':110,'decision_monotonic_ns':110},
        {**common,'market_id':'m2','token_id':'Y','reason':6,'tte_ns':100_000_000_000,
         'decision_wall_ns':120,'decision_monotonic_ns':120},
    ]
    ledger=[{
        'event_type':'FINAL','market_id':'m1','final_pnl':-2.084,
        'metadata':{'winning_token_id':'Y'},
    }]
    report=mod.tte_window_diagnostics(points,ledger,protocol)
    assert report['state']=='HYPOTHESIS_GENERATING_SELECTION_CONDITIONED'
    assert report['settlement_label_coverage']==pytest.approx(.5)
    assert report['automatic_promotion'] is False
    assert report['real_money_authorized'] is False
    by_window={tuple(x['window_seconds']):x for x in report['scenarios']}
    assert by_window[(90.0,105.0)]['markets']==1
    assert by_window[(90.0,105.0)]['net_pnl_usd']==pytest.approx(2.916)
    assert by_window[(105.0,120.0)]['net_pnl_usd']==pytest.approx(-2.084)
    assert by_window[(90.0,105.0)]['trades'][0]['reason']==6


def test_tte_window_diagnostics_is_available_when_all_observed_markets_are_labeled():
    import importlib.util
    spec=importlib.util.spec_from_file_location('economic_report_tte_full',ROOT/'research/economic/run.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    protocol={'quantity_shares':[5],'tte_windows_seconds':[[105,120]],'baseline_tte_window_seconds':[105,120]}
    point={
        'kind':2,'book_valid':True,'confirmed_non_opposing':True,
        'minimum_order_microunits':1_000_000,'fee_rate':.07,'fee_exponent':1.0,
        'asks':[[4000,10_000_000]],'ask_e4':4000,'ask_quantity':10_000_000,
        'signal_age_ns':50_000_000,'_capture':'c','market_id':'m1','token_id':'Y',
        'reason':1,'tte_ns':110_000_000_000,'decision_wall_ns':100,'decision_monotonic_ns':100,
    }
    ledger=[{'event_type':'FINAL','market_id':'m1','final_pnl':2.916,'metadata':{'winning_token_id':'Y'}}]
    report=mod.tte_window_diagnostics([point],ledger,protocol)
    assert report['state']=='COUNTERFACTUAL_DIAGNOSTICS_AVAILABLE'
    assert report['settlement_label_coverage']==1
    assert report['baseline_accepted_decision_book_pnl_usd']==pytest.approx(2.916)
    assert report['baseline_accepted_canonical_pnl_usd']==pytest.approx(2.916)
