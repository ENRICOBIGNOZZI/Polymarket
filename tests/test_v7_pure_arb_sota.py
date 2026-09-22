from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))

import v7_complete_set_merge_shadow as merge_shadow
import v7_fee_reward_registry as registry
import v7_pure_arb_capital_allocator as allocator
import v7_pure_arb_economics as econ
import v7_pure_arb_exchange_execution_shadow as execution
import v7_pure_arb_venue_mode as venue

SHA="b"*40


def write(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value)+"\n",encoding="utf-8")


def test_bounded_jsonl_tail_never_scans_full_history():
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/"evidence.jsonl"
        path.write_text("".join(json.dumps({"i":i})+"\n" for i in range(100)),encoding="utf-8")
        rows=econ.tail_jsonl(path,max_rows=5,max_bytes=1_000_000)
        assert [row["i"] for row in rows]==[95,96,97,98,99]
        tiny=econ.tail_jsonl(path,max_rows=100,max_bytes=64)
        assert tiny
        assert tiny[-1]["i"]==99
        assert len(tiny)<100


def test_order_size_precision_and_market_minimum_are_exchange_faithful():
    assert execution.quantize_order_shares(5.019)==5.01
    assert execution.quantize_order_shares(5.009)==5.0
    assert execution.quantize_order_shares(0.009)==0.0
    observer=(ROOT/"src/v7_maker_fillability_observer.cpp").read_text()
    api=(ROOT/"src/api.cpp").read_text()
    assert 'o.find("min_order_size")' in api
    assert "minimum_order_shares" in observer
    assert "yes->second.min_order_size" in observer
    assert "no->second.min_order_size" in observer
    semantics=json.loads((ROOT/"config/v7_exchange_semantics.json").read_text())
    constraints=semantics["order_constraints"]
    assert constraints["share_precision_decimals"]==2
    assert constraints["share_rounding"]=="FLOOR"
    assert constraints["unknown_minimum_order_size_policy"]=="NON_EXECUTABLE"


def test_fee_precision_matches_venue_contract():
    # Below 1e-5 USDC the venue charges zero.
    assert econ.rounded_fee_usdc(0.001,0.5,0.02,1.0)==0.0
    charged=econ.rounded_fee_usdc(10.0,0.5,0.02,1.0)
    assert charged==0.05
    assert math.isclose(econ.effective_fee_per_share(10.0,0.5,0.02,1.0),0.005)


def test_multilevel_fok_is_all_or_zero_and_respects_limit():
    levels=[(0.40,2.0),(0.41,3.0),(0.42,10.0)]
    filled=econ.fok_sweep(levels,5.0,"BUY",0.41)
    assert filled["filled"] is True
    assert filled["levels_used"]==2
    assert math.isclose(filled["vwap"],0.406,abs_tol=1e-12)
    assert math.isclose(filled["worst_price"],0.41)
    rejected=econ.fok_sweep(levels,6.0,"BUY",0.41)
    assert rejected["filled"] is False
    assert rejected["quantity"]==0.0


def test_execution_fok_preserves_boolean_contract_with_detail():
    point={"ts":1000,"levels":[(0.40,2.0),(0.41,3.0)]}
    ok=execution.fok_fill(
        point,side="BUY",limit=.41,quantity=5,target_ms=1000,maximum_book_age_ms=100)
    assert ok["filled"] is True
    assert ok["levels_used"]==2
    no=execution.fok_fill(
        point,side="BUY",limit=.40,quantity=5,target_ms=1000,maximum_book_age_ms=100)
    assert no["filled"] is False


def test_transport_modes_have_distinct_arrival_semantics():
    seq,order=execution.leg_arrival_times("SEQUENTIAL","YES_FIRST",1000,5,2)
    par,_=execution.leg_arrival_times("PARALLEL","YES_FIRST",1000,5,2)
    bat,_=execution.leg_arrival_times("BATCH","YES_FIRST",1000,5,2)
    assert order==("YES","NO")
    assert seq=={"YES":1000,"NO":1007}
    assert par=={"YES":1000,"NO":1002}
    assert bat=={"YES":1000,"NO":1000}


def test_deep_replay_uses_latest_same_episode_snapshot():
    deep=execution.DeepReplayTimeline(Path("/nonexistent"),SHA)
    common={
        "schema":"polymarket_v7_pure_arb_deep_book_snapshot_v1",
        "model_sha":SHA,"paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,
        "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
        "observer_session_id":"s","connection_epoch":1,
        "capture_origin_wall_ms":1_000,"market_id":"m",
        "yes_token":"y","no_token":"n",
        "yes_bid_levels":[{"price":0.39,"size":10}],
        "no_bid_levels":[{"price":0.49,"size":10}],
        "no_ask_levels":[{"price":0.50,"size":10}],
    }
    deep.ingest({**common,"receive_wall_ms":1_000,
                 "yes_ask_levels":[{"price":0.40,"size":10}]})
    deep.ingest({**common,"receive_wall_ms":1_100,
                 "yes_ask_levels":[{"price":0.43,"size":10}]})
    class Book:
        session="s";epoch=1
        def between(self,*_args):return []
    levels=deep.levels_at("m","y",1_000,1_150,"BUY",Book())
    assert levels is not None and levels[0]==(0.43,10.0)


def test_deep_replay_fails_closed_across_unreanchored_reset():
    deep=execution.DeepReplayTimeline(Path("/nonexistent"),SHA)
    deep.ingest({
        "schema":"polymarket_v7_pure_arb_deep_book_snapshot_v1",
        "model_sha":SHA,"paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,
        "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
        "observer_session_id":"s","connection_epoch":1,
        "capture_origin_wall_ms":1_000,"receive_wall_ms":1_000,"market_id":"m",
        "yes_token":"y","no_token":"n",
        "yes_bid_levels":[{"price":0.39,"size":10}],
        "yes_ask_levels":[{"price":0.40,"size":10}],
        "no_bid_levels":[{"price":0.49,"size":10}],
        "no_ask_levels":[{"price":0.50,"size":10}],
    })
    class Book:
        session="s";epoch=1
        def between(self,*_args):
            return [{
                "observer_session_id":"s","connection_epoch":1,
                "valid":True,"lineage_continuous":True,
                "deep_replay_reset":True,"book_change":None,
            }]
    assert deep.levels_at("m","y",1_000,1_100,"BUY",Book()) is None


def test_taker_tier_schedule_and_unknown_is_zero():
    assert econ.taker_tier(1_999)["rebate_fraction"]==0.0
    assert econ.taker_tier(2_000)["rebate_fraction"]==0.03
    assert econ.taker_tier(20_000)["rebate_fraction"]==0.08
    assert econ.taker_tier(10_000_000)["rebate_fraction"]==0.50
    assert econ.ancillary_taker_rebate(10.0,0.50,verified=False)==0.0
    assert econ.ancillary_taker_rebate(10.0,0.50,verified=True)==5.0


def test_fee_registry_accepts_only_fresh_exact_taker_tier_evidence():
    universe={
        "schema":"polymarket_v7_crypto_universe_snapshot_v1",
        "model_sha":SHA,"paper_only":True,
        "authenticated_execution":False,"real_order_submission":False,
        "markets":[{
            "market_id":"m","condition_id":"c","clob_token_ids":["y","n"],
            "active":True,"closed":False,"accepting_orders":True,
            "fee_schedule":{"rate":0.02,"exponent":1.0,"takerOnly":True},
        }],
    }
    snapshot={
        "schema":"polymarket_v7_verified_taker_tier_snapshot_v1",
        "model_sha":SHA,"paper_only":True,
        "authenticated_execution":False,"real_order_submission":False,
        "weighted_volume_30d":20_000,"tier":"SILVER","rebate_fraction":0.08,
        "observed_at_ms":900,"expires_at_ms":2_000,
    }
    out=registry.build(
        universe,{},model_sha=SHA,now_ms=1_000,
        exchange_semantics=json.loads((ROOT/"config/v7_exchange_semantics.json").read_text()),
        taker_tier_snapshot=snapshot)
    assert out["taker_rebate"]["verified"] is True
    assert out["taker_rebate"]["rebate_fraction"]==0.08
    bad=dict(snapshot);bad["model_sha"]="c"*40
    rejected=registry.build(
        universe,{},model_sha=SHA,now_ms=1_000,
        exchange_semantics=json.loads((ROOT/"config/v7_exchange_semantics.json").read_text()),
        taker_tier_snapshot=bad)
    assert rejected["taker_rebate"]["verified"] is False
    assert rejected["taker_rebate"]["rebate_fraction"]==0.0


def test_merge_evidence_is_fail_closed_until_independently_verified():
    policy=json.loads((ROOT/"config/v7_pure_arb_capital_policy.json").read_text())
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/"merge.json"
        p=allocator.merge_evidence_policy(policy,path,SHA,1_000)
        assert p["complete_set_merge_verified"] is False
        write(path,{
            "schema":"polymarket_v7_complete_set_merge_evidence_v1",
            "model_sha":SHA,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,
            "verified":True,"confirmed_latency_ms":350.0,
            "fixed_cost_pusd":0.01,"variable_cost_bps":0.5,
            "expires_at_ms":2_000,"source":"INDEPENDENT_TEST",
        })
        p=allocator.merge_evidence_policy(policy,path,SHA,1_000)
        assert p["complete_set_merge_verified"] is True
        assert p["complete_set_merge_latency_ms"]==350.0


def test_merge_shadow_prepares_exact_atomic_operation_without_authority():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        selection={
            "schema":"polymarket_v7_multi_crypto_book_selection_v1",
            "model_sha":SHA,"paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"execution_authority":False,
            "markets":[{
                "market_id":"m1","condition_id":"0x"+"1"*64,
                "yes_token":"y","no_token":"n","neg_risk":False,
            }],
        }
        write(root/"selection.json",selection)
        owner=merge_shadow.Shadow.__new__(merge_shadow.Shadow)
        owner.args=SimpleNamespace(
            model_sha=SHA,selection=root/"selection.json",evidence=root/"none.json")
        owner.seen=set()
        row={
            "scenario_id":"s","market_id":"m1","state":"COMPLETE_PAIRED",
            "kind":"BUY_COMPLETE_SET",
            "target_shares":12.5,"revalidation_yes_price":0.4,
            "revalidation_no_price":0.5,
        }
        out=owner.evaluate(row)
        assert out["state"]=="MERGE_ECONOMICS_UNVERIFIED"
        assert out["operation_atomic"] is True
        assert out["partition"]==[1,2]
        assert out["prepared_amount_base_units"]==12_500_000
        assert out["real_order_submission"] is False


def test_taker_allocator_rejects_censored_required_mode():
    policy=json.loads((ROOT/"config/v7_pure_arb_capital_policy.json").read_text())
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/"taker.jsonl"
        rows=[]
        common={
            "schema":"polymarket_v7_pure_arb_exchange_execution_cycle_v1",
            "market_id":"m","kind":"BUY_COMPLETE_SET",
            "transport_delay_ms":5,"inter_leg_skew_ms":5,
            "revalidation_wall_ms":1_000,"target_shares":10,
            "revalidation_yes_price":0.4,"revalidation_no_price":0.5,
            "market_end_ms":10_000,
        }
        for mode in ("SEQUENTIAL","PARALLEL"):
            for order in ("YES_FIRST","NO_FIRST"):
                rows.append({**common,"execution_mode":mode,"leg_order":order,
                             "execution_pnl_after_reserve":0.1})
        # Batch exists but is censored: presence alone must not satisfy the gate.
        rows.append({**common,"execution_mode":"BATCH","leg_order":"BATCH",
                     "state":"CENSORED_DEEP_REPLAY_UNAVAILABLE"})
        path.write_text("".join(json.dumps(row)+"\n" for row in rows),encoding="utf-8")
        assert allocator.taker_observations(path,policy)==[]
        rows[-1]["execution_pnl_after_reserve"]=0.05
        path.write_text("".join(json.dumps(row)+"\n" for row in rows),encoding="utf-8")
        assert len(allocator.taker_observations(path,policy))==1


def test_sell_pair_is_not_mergeable_and_maker_gets_no_early_merge_credit():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        selection={
            "schema":"polymarket_v7_multi_crypto_book_selection_v1",
            "model_sha":SHA,"paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"execution_authority":False,
            "markets":[{"market_id":"m1","condition_id":"0x"+"1"*64,
                        "yes_token":"y","no_token":"n","neg_risk":False}],
        }
        write(root/"selection.json",selection)
        owner=merge_shadow.Shadow.__new__(merge_shadow.Shadow)
        owner.args=SimpleNamespace(
            model_sha=SHA,selection=root/"selection.json",evidence=root/"none.json")
        owner.seen=set()
        sell={
            "scenario_id":"sell","market_id":"m1","state":"COMPLETE_PAIRED",
            "kind":"SELL_COMPLETE_SET","target_shares":5,
        }
        assert owner.evaluate(sell) is None

        maker_path=root/"maker.jsonl"
        maker_path.write_text(json.dumps({
            "schema":"polymarket_v7_two_sided_complete_set_cycle_v2",
            "market_id":"m1","cycle_id":"c1","origin_ms":1_000,
            "ttl_ms":1_000,"market_end_ms":100_000,"target_shares":10,
            "yes_price":0.4,"no_price":0.5,
            "queue_scenarios":[{
                "multiplier":1.5,"cancel_relief_fraction":0.0,
                "state":"BOTH_PARTIAL","yes_filled_shares":4,
                "no_filled_shares":3,"matched_shares":3,
                "total_shadow_pnl":0.25,
            }],
        })+"\n",encoding="utf-8")
        policy=json.loads((ROOT/"config/v7_pure_arb_capital_policy.json").read_text())
        policy={**policy,"complete_set_merge_verified":True,
                "complete_set_merge_latency_ms":100.0}
        obs=allocator.maker_observations(maker_path,policy,{})
        assert len(obs)==1
        assert math.isclose(obs[0]["lock_seconds"],1.0)
        assert obs[0]["merge_credit_applied"] is False


def test_capital_statistics_include_tail_risk_and_market_clustering():
    policy=json.loads((ROOT/"config/v7_pure_arb_capital_policy.json").read_text())
    policy={**policy,"minimum_samples":4,"minimum_independent_clusters":2,
            "bootstrap_draws":200,"bootstrap_block_size":2}
    obs=[]
    for market,pnls in (("a",[1.0,1.2]),("b",[0.8,-2.0])):
        for i,pnl in enumerate(pnls):
            obs.append({"strategy":"S","market_id":market,"episode_id":f"{market}-{i}",
                        "pnl":pnl,"capital":100.0,"lock_seconds":1.0})
    stats=allocator.summarize(obs,policy)["S"]
    assert stats["independent_market_clusters"]==2
    assert stats["expected_shortfall_05"] is not None
    assert stats["worst_pnl_per_capital_second"]<0
    assert stats["block_bootstrap_lower_95"] is not None
    assert stats["inference_semantics"].startswith("MARKET_CLUSTERED")


def test_exchange_semantics_contains_batch_merge_rebate_heartbeat_and_restrictions():
    value=json.loads((ROOT/"config/v7_exchange_semantics.json").read_text())
    assert value["order_submission"]["batch_max_orders"]==15
    assert value["order_submission"]["batch_atomic"] is False
    assert value["taker_rebates"]["use_in_entry_gate"] is False
    assert value["position_operations"]["complete_set_merge"]["supported"] is True
    assert value["matching_engine_restrictions"]["restart_http_status"]==425
    assert value["matching_engine_restrictions"]["restricted_http_status"]==503
    assert value["order_heartbeat"]["send_interval_seconds"]==5
    assert value["order_heartbeat"]["maker_promotion_requires_native_heartbeat"] is True
    assert "CLOSED_ONLY" in value["account_execution_modes"]


def test_causal_observer_persists_l10_ladders_and_runtime_wires_all_modes():
    observer=(ROOT/"src/v7_maker_fillability_observer.cpp").read_text()
    runtime=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text()
    assert '"bid_levels_l10"' in observer
    assert '"ask_levels_l10"' in observer
    assert "--transport-modes SEQUENTIAL,PARALLEL,BATCH" in runtime
    assert '--deep-book-snapshots "$PURE_ARB_DIR/pure_arb_deep_book_snapshots.jsonl"' in runtime
    assert "kPureArbDeepEvidenceArmsMs" in observer
    assert "5000, 5500" in observer
    assert "capture_origin_wall_ms" in observer
    assert runtime.count("v7_complete_set_merge_shadow.py")==1
    assert "v7_assert_registered_child_count 29" in runtime
    assert '--merge-evidence "$RUN_ROOT/control/verified_complete_set_merge_evidence.json"' in runtime
    assert '--taker-tier-snapshot "$RUN_ROOT/control/verified_taker_tier.json"' in runtime
    assert '--account-source "$RUN_ROOT/control/account_execution_mode.json"' in runtime
    assert "--paper-account-counterfactual OPEN" in runtime


def test_closed_only_observed_mode_stays_separate_from_paper_open_counterfactual():
    now=1_000
    out=venue.build(
        {"mode":"NORMAL","timestamp_ms":now},
        model_sha=SHA,now_ms=now,maximum_age_ms=5_000,
        account_source={"mode":"CLOSED_ONLY","timestamp_ms":now},
        paper_counterfactual_mode="NORMAL",
        paper_account_counterfactual="OPEN",
    )
    assert out["observed_account_mode"]=="CLOSED_ONLY"
    assert out["observed_policy"]["new_taker"] is False
    assert out["simulation_account_mode"]=="OPEN"
    assert out["simulation_policy"]["new_taker"] is True
    assert out["paper_account_counterfactual"] is True


def test_native_lane_classifies_restricted_http_responses_without_blind_retry():
    header=(ROOT/"include/pm/v7_native_clob_order_lane.hpp").read_text()
    source=(ROOT/"src/v7_native_clob_order_lane.cpp").read_text()
    parser=(ROOT/"src/v7_clob_http1_response.cpp").read_text()
    for token in ("MatchingEngineRestart","RestrictedTradingMode","RateLimited"):
        assert token in header and token in source
    assert "out.http_status == 425" in source
    assert "out.http_status == 503" in source
    assert "out.http_status == 429" in source
    assert "never performs" in source and "automatic retry" in source
    assert 'iequals(name, "Retry-After")' in parser


HTTP_PROGRAM=r"""
#include "pm/v7_clob_http1_response.hpp"
#include <cassert>
#include <cstring>
#include <string>
using namespace pm::v7::clob_transport;
int main() {
    FixedHttp1Response response;
    const std::string raw =
        "HTTP/1.1 503 Service Unavailable\r\n"
        "Content-Length: 0\r\n"
        "Retry-After: 7\r\n"
        "Connection: keep-alive\r\n\r\n";
    auto dst=response.writable();
    std::memcpy(dst.data(),raw.data(),raw.size());
    assert(response.commit(raw.size())==Http1ResponseState::Complete);
    assert(response.status_code()==503);
    assert(response.retry_after_seconds()==7);
    return 0;
}
"""


def test_http_parser_exposes_retry_after():
    compiler=shutil.which("c++")
    assert compiler
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        main=root/"main.cpp";binary=root/"test"
        main.write_text(HTTP_PROGRAM)
        subprocess.run([
            compiler,"-std=c++20","-Wall","-Wextra","-Wpedantic",
            f"-I{ROOT/'include'}",str(ROOT/"src/v7_clob_http1_response.cpp"),
            str(main),"-o",str(binary),
        ],check=True,capture_output=True,text=True)
        subprocess.run([str(binary)],check=True,capture_output=True,text=True)


def test_cpp_detector_uses_venue_fee_precision():
    source=(ROOT/"src/v7_maker_fillability_observer.cpp").read_text()
    assert "pure_arb_fee_usdc" in source
    assert "raw < 0.00001" in source
    assert "std::round(raw * 100000.0) / 100000.0" in source


def test_cross_market_registry_remains_empty_until_proof_exists():
    value=json.loads((ROOT/"config/v7_exact_arb_relations.json").read_text())
    assert value["relations"]==[]
    assert value["automatic_promotion"] is False


if __name__=="__main__":
    tests=[value for name,value in sorted(globals().items())
           if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"pure_arb_sota_tests={len(tests)}")
