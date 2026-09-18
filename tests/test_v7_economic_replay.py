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


def test_grouped_settlement_model_gives_equal_total_weight_per_market():
    from models import fit_settlement_residual_grouped,predict
    rows=[]
    for i in range(20):
        outcome=i%2
        for j in range(1 if i else 25):
            rows.append({'market':str(i),'decision_ns':10+i+j,'label_observed_ns':200+i,
                'complete':True,'outcome':outcome,'pm_probability':.5,
                'features':{'return':(-1 if outcome==0 else 1)}})
    model=fit_settlement_residual_grouped(rows,feature_names=['return'],train_end_ns=500,
        dataset_sha256='b'*64,minimum_markets=20)
    assert model['training_unique_markets']==20
    assert model['training_rows']==44
    assert model['market_weighting']=='EQUAL_TOTAL_WEIGHT_PER_MARKET'
    future=[{**rows[-1],'decision_ns':600}]
    assert predict(model,future)[0]>.5


def test_settlement_dataset_joins_native_decision_to_final_without_future_leakage(tmp_path):
    from settlement_dataset import build,FEATURE_NAMES
    source=tmp_path/'source';(source/'ledger').mkdir(parents=True)
    obsdir=source/'research/native_observations/run';obsdir.mkdir(parents=True)
    final={'event_type':'FINAL','market_id':'m1','recorded_ts_ms':2000,
        'metadata':{'crypto_context':{'asset':'SOL','horizon':'M5'},
                    'settlement_payouts':{'YES':1.0,'NO':0.0}}}
    (source/'ledger/execution.jsonl').write_text(json.dumps(final)+'\n')
    observation={'schema':'polymarket_v7_native_observation_v1','kind':2,
        'market_id':'m1','token_id':'YES','asset':'SOL','horizon':'M5',
        'signal_version':7,'decision_wall_ms':1000,'book_valid':True,
        'bid_e4':4000,'ask_e4':4100,'bid_quantity':3_000_000,'ask_quantity':2_000_000,
        'binance_return_100ms_bp':.6,'coinbase_return_100ms_bp':.2,'direction':1,
        'confirmed_non_opposing':True,'signal_age_ns':20_000_000,'tte_ns':60_000_000_000,
        'fee_rate':.02,'fee_exponent':1.0,'accepted':True,'reason':1}
    (obsdir/'m1.jsonl').write_text(json.dumps({**observation,'sequence':1})+'\n')
    (obsdir/'m1.jsonl.closed.json').write_text(json.dumps({
        'schema':'polymarket_v7_native_capture_closed_v1','healthy':True,'closed':True,
        'capture_mode':'DECISIONS','last_sequence':1})+'\n')
    dataset=tmp_path/'dataset'
    freeze(source,dataset,['ledger/execution.jsonl','research/native_observations/run/m1.jsonl',
        'research/native_observations/run/m1.jsonl.closed.json'],identity={'sha':'a'*40})
    result=build(dataset)
    assert result['diagnostics']['unique_markets']==1
    row=result['rows'][0]
    assert row['outcome']==1 and row['pm_probability']==pytest.approx(.405)
    assert row['entry_price']==pytest.approx(.41)
    assert row['break_even_probability']>.41
    assert row['features']['signal_abs_bp']==pytest.approx(.6)
    assert row['features']['asset_SOL']==1
    assert set(result['feature_names'])==set(FEATURE_NAMES)


def test_ev_policy_replays_later_signal_when_first_is_not_price_worthy(monkeypatch):
    import ev_policy
    base={'market':'m1','asset':'SOL','horizon':'M5','complete':True,'outcome':1,
        'pm_probability':.5,'fee_per_share':.001,'confirmed_non_opposing':True,
        'binance_return_100ms_bp':.6,'signal_age_ns':20_000_000,'tte_ns':60_000_000_000,
        'minimum_order_microunits':5_000_000,'ask_quantity':30_000_000,
        'tick_e4':100,'features':{}}
    rows=[
        {**base,'signal_version':1,'decision_ns':600,'bid_e4':9800,'ask_e4':9900,'entry_price':.99},
        {**base,'signal_version':2,'decision_ns':700,'bid_e4':3900,'ask_e4':4000,'entry_price':.40},
    ]
    monkeypatch.setattr(ev_policy,'predict',lambda _m,_r:[.95,.70])
    report=ev_policy.evaluate({'train_end_ns':500},rows,edge_buffer=.01)
    assert report['baseline_first_structural']['trades']==1
    assert report['ev_first_positive']['trades']==1
    assert report['candidate_trades'][0]['entry_price']==pytest.approx(.40)
    assert report['incremental_net_pnl']>0


def test_ev_policy_structural_gate_rejects_insufficient_depth():
    from ev_policy import structural_eligible
    row={'entry_price':.5,'confirmed_non_opposing':True,'binance_return_100ms_bp':.6,
        'signal_age_ns':1,'tte_ns':60_000_000_000,'minimum_order_microunits':5_000_000,
        'ask_quantity':19_000_000,'ask_e4':5000,'tick_e4':100}
    assert not structural_eligible(row,target_shares=20)


def test_price_aware_kelly_sizing_requires_robust_positive_edge():
    from price_aware import price_aware_fractional_kelly_size
    rejected=price_aware_fractional_kelly_size(
        probability=.96,executable_ask=.96,fee_per_share=.002688,
        uncertainty_buffer=.005,available_capital_usd=333.0)
    assert rejected['shares']==0 and rejected['reason']=='NONPOSITIVE_ROBUST_EDGE'
    accepted=price_aware_fractional_kelly_size(
        probability=.80,executable_ask=.60,fee_per_share=.01,
        uncertainty_buffer=.02,available_capital_usd=333.0,
        max_loss_usd=5,max_shares=20,visible_depth_shares=100)
    assert 0<accepted['shares']<=20
    assert accepted['cash_at_risk_usd']<=5+1e-12
    assert accepted['net_edge_per_share']>0


def test_price_aware_kelly_sizing_respects_depth_and_minimum():
    from price_aware import price_aware_fractional_kelly_size
    shallow=price_aware_fractional_kelly_size(
        probability=.9,executable_ask=.5,fee_per_share=0,
        uncertainty_buffer=0,available_capital_usd=1000,
        max_loss_usd=100,max_shares=20,visible_depth_shares=3)
    assert shallow['shares']==3
    too_small=price_aware_fractional_kelly_size(
        probability=.55,executable_ask=.5,fee_per_share=0,
        uncertainty_buffer=.01,available_capital_usd=10,
        max_loss_usd=1,max_shares=20,visible_depth_shares=20,
        minimum_shares=5)
    assert too_small['shares']==0 and too_small['reason']=='BELOW_MINIMUM_SHARES'
