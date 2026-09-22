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

SHA="b"*40


def write(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value)+"\n",encoding="utf-8")


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
    assert ok
    assert ok["levels_used"]==2
    no=execution.fok_fill(
        point,side="BUY",limit=.40,quantity=5,target_ms=1000,maximum_book_age_ms=100)
    assert not no


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
            "target_shares":12.5,"revalidation_yes_price":0.4,
            "revalidation_no_price":0.5,
        }
        out=owner.evaluate(row)
        assert out["state"]=="MERGE_ECONOMICS_UNVERIFIED"
        assert out["operation_atomic"] is True
        assert out["partition"]==[1,2]
        assert out["prepared_amount_base_units"]==12_500_000
        assert out["real_order_submission"] is False


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
    assert '--merge-evidence "$RUN_ROOT/control/verified_complete_set_merge_evidence.json"' in runtime
    assert '--taker-tier-snapshot "$RUN_ROOT/control/verified_taker_tier.json"' in runtime
    assert '--account-source "$RUN_ROOT/control/account_execution_mode.json"' in runtime
    assert "--paper-account-counterfactual OPEN" in runtime


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


def test_cross_market_registry_remains_empty_until_proof_exists():
    value=json.loads((ROOT/"config/v7_exact_arb_relations.json").read_text())
    assert value["relations"]==[]
    assert value["automatic_promotion"] is False
