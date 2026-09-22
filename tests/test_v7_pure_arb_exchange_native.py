from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))

import v7_market_execution_terms as terms
import v7_pure_arb_capital_allocator as allocator
import v7_pure_arb_exchange_execution_shadow as execution
import v7_pure_arb_venue_mode as venue
import v7_polymarket_status_source as public_status
import v7_pure_arb_maker_policy as maker_policy

SHA="a"*40


def write(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value)+"\n",encoding="utf-8")


def selection_market(**overrides):
    row={
        "market_id":"m1","event_id":"e1","yes_token":"yes","no_token":"no",
        "start_timestamp_ms":1_000_000,"end_timestamp_ms":2_000_000,
        "normalized_rules_hash":"r"*64,"rule_snapshot_sha256":"s"*64,
        "fee_schedule":{"rate":0.02,"exponent":1.0,"takerOnly":True},
        "fees_enabled":True,"fees_enabled_explicit":True,
    }
    row.update(overrides);return row


def selection(row):
    return {
        "schema":"polymarket_v7_multi_crypto_book_selection_v1",
        "model_sha":SHA,"paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"execution_authority":False,
        "markets":[row],
    }


def test_market_terms_itode_is_exact_250ms_delay():
    market={"market_id":"m1","condition_id":"0x"+"1"*64,
            "clob_token_ids":["yes","no"]}
    snap=terms.snapshot(
        market,
        lambda _url:{"itode":True,"t":[{"t":"yes"},{"t":"no"}]},
        now_ns=123,
    )
    assert snap["state"]=="VERIFIED_SNAPSHOT"
    assert snap["mandatory_taker_delay_ns"]==250_000_000


def test_venue_modes_fail_closed():
    assert venue.policy("NORMAL")=={"new_taker":True,"new_maker":True,"cancel":True}
    assert venue.policy("POST_ONLY")=={"new_taker":False,"new_maker":True,"cancel":True}
    assert venue.policy("CANCEL_ONLY")=={"new_taker":False,"new_maker":False,"cancel":True}
    assert venue.policy("PAUSED")["new_taker"] is False
    assert venue.policy("DEGRADED")["new_maker"] is False
    mode,reason=venue.resolve_mode({},now_ms=1000,maximum_age_ms=100)
    assert (mode,reason)==("DEGRADED","MODE_UNKNOWN") or (mode,reason)==("DEGRADED","SOURCE_MISSING")


def test_fok_revalidation_rejects_price_move_depth_and_stale():
    p={"ts":1000,"price":0.40,"depth":5.0,"unwind":0.39,"unwind_depth":5.0}
    assert execution.fok_fill(p,side="BUY",limit=.40,quantity=5,target_ms=1000,maximum_book_age_ms=100)
    assert not execution.fok_fill({**p,"price":.41},side="BUY",limit=.40,quantity=5,target_ms=1000,maximum_book_age_ms=100)
    assert not execution.fok_fill({**p,"depth":4.99},side="BUY",limit=.40,quantity=5,target_ms=1000,maximum_book_age_ms=100)
    assert not execution.fok_fill(p,side="BUY",limit=.40,quantity=5,target_ms=1200,maximum_book_age_ms=100)


class FakeBook:
    def __init__(self,points):self.points=points
    def asof(self,mid,token,at_ms):
        return self.points.get((token,at_ms))


def bookrow(ts,bid,ask,bidq=10,askq=10):
    return {"receive_wall_ms":ts,"best_bid":bid,"best_ask":ask,
            "bid_depth_l1":bidq,"ask_depth_l1":askq}


def exchange_owner(tmp:Path,points):
    row=selection_market()
    write(tmp/"selection.json",selection(row))
    owner=execution.Shadow.__new__(execution.Shadow)
    owner.args=SimpleNamespace(
        selection=tmp/"selection.json",model_sha=SHA,maximum_shares=1000.0,
        minimum_shares=1.0,maximum_book_age_ms=100,maximum_leg_skew_ms=100,
        reserve_per_share=.0005,unwind_delay_ms=2,
    )
    owner.book=FakeBook(points)
    owner.semantics=json.loads((ROOT/"config/v7_exchange_semantics.json").read_text())
    return owner,row


def base_item(row,*,allowed=True,skew=2,order="YES_FIRST",target=1250):
    candidate={
        "market_id":"m1","kind":"BUY_COMPLETE_SET","receive_wall_ms":1000,
        "asset":"BTC","horizon":"M5","executable_shares_local_deep":5.0,
    }
    return {
        "scenario_id":"s","candidate":candidate,"market":row,
        "fingerprint":execution.fingerprint(row),
        "terms":{"mandatory_taker_delay_ns":250_000_000},
        "venue_taker_allowed":allowed,"transport_ms":0,"skew_ms":skew,
        "order":order,"total_delay_ms":250,"target_ms":target,
    }


def test_one_leg_fill_is_unwound_not_counted_as_arb():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        # At revalidation both books show edge. YES has depth; NO loses depth
        # before its 2ms-later FOK. YES is then unwound at t=1254.
        points={
            ("yes",1250):bookrow(1250,.39,.40,bidq=5,askq=5),
            ("no",1250):bookrow(1250,.49,.50,bidq=5,askq=5),
            ("no",1252):bookrow(1252,.49,.50,bidq=5,askq=0),
            ("yes",1254):bookrow(1254,.39,.40,bidq=5,askq=5),
        }
        owner,row=exchange_owner(root,points)
        result=owner.evaluate(base_item(row))
        assert result["state"]=="ONE_LEG_UNWOUND"
        assert result["paired_execution"] is False
        assert result["execution_pnl_after_reserve"]<0
        assert "COMPLETE_WITH_LEGGING" in result["lifecycle"]


def test_semantic_change_invalidates_pending_taker():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        owner,row=exchange_owner(root,{})
        changed=selection_market(normalized_rules_hash="x"*64)
        write(root/"selection.json",selection(changed))
        result=owner.evaluate(base_item(row))
        assert result["state"]=="SEMANTIC_RESET"


def test_cancel_only_blocks_new_taker():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        owner,row=exchange_owner(root,{})
        result=owner.evaluate(base_item(row,allowed=False))
        assert result["state"]=="BLOCKED_VENUE_MODE"


def test_current_v2_semantics_reject_legacy_share_fee_mode():
    value=json.loads((ROOT/"config/v7_exchange_semantics.json").read_text())
    assert execution.validate_semantics(value)
    assert value["fee_semantics"]["settlement_asset"]=="USDC_VALUE"
    assert value["fee_semantics"]["legacy_buy_fee_in_shares_is_not_production_v2"] is True
    assert value["fee_semantics"]["maker_fee_rate"]==0.0


def test_buy_fee_collection_models_are_both_explicit_and_distinct():
    rate=0.07
    exp=1.0
    usdc=execution.buy_pair_edge_per_net_share(0.50,0.50,rate,exp,"USDC_VALUE")
    shares=execution.buy_pair_edge_per_net_share(0.50,0.50,rate,exp,"SHARES_ON_BUY")
    assert math.isfinite(usdc) and math.isfinite(shares)
    assert shares < usdc
    assert execution.gross_buy_quantity_for_net(
        10,0.50,rate,exp,"USDC_VALUE")==10
    gross=execution.gross_buy_quantity_for_net(
        10,0.50,rate,exp,"SHARES_ON_BUY")
    assert gross>10
    value=json.loads((ROOT/"config/v7_exchange_semantics.json").read_text())
    assert execution.validate_semantics(value)
    assert set(value["fee_semantics"]["supported_buy_collection_modes"])=={
        "USDC_VALUE","SHARES_ON_BUY"}
    assert value["safety"]["unverified_buy_collection_mode_policy"]=="NON_EXECUTABLE_TAKER"


def test_maker_entry_edge_is_fee_free_and_rebate_is_ancillary():
    source=(ROOT/"scripts/v7_two_sided_complete_set_shadow.py").read_text()
    assert "CLOB V2 makers are fee-free" in source
    assert '"maker_fee_per_share": 0.0' in source
    assert '"rebate_reference_used_in_entry_gate": False' in source
    assert "by_queue_arm" in source


def test_deep_local_book_is_decision_authority_not_rest():
    state=(ROOT/"include/pm/v7_market_state.hpp").read_text()
    ws=(ROOT/"src/v7_market_ws.cpp").read_text()
    observer=(ROOT/"src/v7_maker_fillability_observer.cpp").read_text()
    assert "kDeepDepthLevels = 1024" in state
    assert "BookDeepSnapshot" in state
    assert "deep_snapshot" in ws
    assert "LOCAL_DEEP_BOOK_POSITIVE_MARGINAL_EDGE" in observer
    assert "pure_arb_deep_queue_" in observer
    assert "bid_truncated" in observer and "ask_truncated" in observer


def test_capital_allocator_uses_capital_time_not_edge():
    policy=json.loads((ROOT/"config/v7_pure_arb_capital_policy.json").read_text())
    obs=[
        {"strategy":"A","market_id":"a","pnl":1.0,"capital":100.0,"lock_seconds":10.0},
        {"strategy":"A","market_id":"a","pnl":1.0,"capital":100.0,"lock_seconds":10.0},
        {"strategy":"B","market_id":"b","pnl":2.0,"capital":1000.0,"lock_seconds":100.0},
        {"strategy":"B","market_id":"b","pnl":2.0,"capital":1000.0,"lock_seconds":100.0},
    ]
    p=dict(policy);p["minimum_samples"]=2
    stats=allocator.summarize(obs,p)
    assert stats["A"]["mean_pnl_per_capital_second"]>stats["B"]["mean_pnl_per_capital_second"]



def test_verified_maker_reward_is_ancillary_and_unverified_is_zero():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        registry=root/"registry.json"
        now=1_000
        write(registry,{
            "schema":"polymarket_v7_fee_reward_registry_v1",
            "model_sha":SHA,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,
            "unknown_reward_policy":"ZERO_EXPECTED_VALUE",
            "markets":[
                {"market_id":"m1","reward":{
                    "verified":True,
                    "source":"verified_realized_maker_reward_rate",
                    "realized_pnl_pusd_per_capital_second":0.001,
                    "expires_at_ms":2_000,
                }},
                {"market_id":"m2","reward":{
                    "verified":False,
                    "source":"unknown_reward_forced_zero",
                    "realized_pnl_pusd_per_capital_second":999.0,
                    "expires_at_ms":2_000,
                }},
            ],
        })
        rates=allocator.verified_maker_reward_rates(registry,SHA,now)
        assert rates=={"m1":0.001}



def test_public_status_observer_is_fail_closed_and_detects_modes():
    normal=(
        "All systems operational "
        "Predictions Trading API (CLOB) - Operational "
        "Clob Websocket - Operational Recent notices historical post-only"
    )
    assert public_status.classify(normal)==(
        "NORMAL","PUBLIC_STATUS_EXPLICIT_OPERATIONAL")
    assert public_status.classify(
        "Scheduled maintenance Post-only Predictions Trading API (CLOB)") == (
            "POST_ONLY","PUBLIC_STATUS_POST_ONLY")
    assert public_status.classify("Predictions Trading API (CLOB) degraded performance")[0]=="DEGRADED"
    assert public_status.classify("unrecognized page")[0]=="DEGRADED"


def test_maker_policy_requires_mature_state_positive_arm_budget_and_venue():
    maker={
        "schema":"polymarket_v7_two_sided_complete_set_shadow_status_v2",
        "model_sha":SHA,"paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"research_mature":True,"timestamp_ms":1_000,
        "policy_matrix":{
            "ttl=250|queue=1.250|cancel_relief=0.000":{
                "deployment_candidate":True,"ttl_ms":250,
                "queue_ahead_multiplier":1.25,"cancel_relief_fraction":0.0,
                "conservative_mean_total_shadow_pnl_lower_90":0.02,
            }
        },
        "by_state":{
            "BTC:M5|tte=LTE_30S|flow=HEAVY|vol=FAST":{
                "mature":True,"deployment_candidate":True,
            }
        },
        "active_states":[{
            "market_id":"m1","cycle_id":"c1",
            "state_bucket":"BTC:M5|tte=LTE_30S|flow=HEAVY|vol=FAST",
            "yes_price":0.45,"no_price":0.50,"target_shares":10,
            "locked_edge_per_share":0.05,"expires_ms":10_000,
        }],
    }
    capital={
        "schema":"polymarket_v7_pure_arb_capital_allocator_v1",
        "model_sha":SHA,"paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"timestamp_ms":1_000,
        "recommended_market_budget_pusd":{"MAKER_COMPLETE_SET|m1":5.0},
    }
    venue_status={
        "schema":"polymarket_v7_pure_arb_venue_mode_v1",
        "model_sha":SHA,"paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"timestamp_ms":1_000,
        "observed_policy":{"new_maker":True},
        "simulation_policy":{"new_maker":True},
    }
    out=maker_policy.build(maker,capital,venue_status,model_sha=SHA,now_ms=1_000)
    assert out["paper_admitted_count"]==1
    assert out["observed_admitted_count"]==1
    row=out["recommendations"][0]
    assert 0 < row["recommended_shares"] <= 10
    assert row["state_mature_positive"] is True
    venue_status["observed_policy"]={"new_maker":False}
    blocked=maker_policy.build(maker,capital,venue_status,model_sha=SHA,now_ms=1_000)
    assert blocked["observed_admitted_count"]==0
    assert blocked["paper_admitted_count"]==1
    stale=maker_policy.build(maker,capital,venue_status,model_sha=SHA,now_ms=10_000,
                             maximum_age_ms=5_000)
    assert stale["paper_admitted_count"]==0
    assert "MAKER_STATUS_INVALID" in stale["blockers"]


def test_maker_shadow_is_state_conditioned():
    source=(ROOT/"scripts/v7_two_sided_complete_set_shadow.py").read_text()
    for token in (
        "tte_bucket","flow_regime","vol_regime","state_bucket",
        "by_state","maturity_min_cycles_per_state",
    ):
        assert token in source


def test_runtime_remains_zero_authority():
    loop=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text()
    for worker in (
        "v7_polymarket_status_source.py",
        "v7_pure_arb_venue_mode.py",
        "v7_pure_arb_exchange_execution_shadow.py",
        "v7_fee_reward_registry.py",
        "v7_pure_arb_capital_allocator.py",
        "v7_pure_arb_maker_policy.py",
    ):
        assert loop.count(worker)==1
    assert "v7_assert_registered_child_count 20" in loop
    assert '--fee-reward-registry "$PURE_ARB_DIR/fee_reward_registry.json"' in loop
    assert '"authenticated_execution":false' in loop.replace(" ", "")
    assert '"real_order_submission":false' in loop.replace(" ", "")


if __name__=="__main__":
    tests=[v for k,v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:test()
    print(f"exchange_native_arb_tests={len(tests)}")
