from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))

import v7_cross_market_exact_arb_shadow as cross
import v7_pure_arb_deep_sizing_shadow as deep
import v7_two_sided_complete_set_shadow as maker


def test_wilson_joint_fill_lower_bound_is_direct_and_conservative():
    assert maker.wilson_lower(0,100)==0.0
    full=maker.wilson_lower(100,100)
    assert full is not None and 0.95<full<1.0
    assert maker.wilson_lower(50,100) < 0.5
    assert maker.wilson_lower(60,100) > maker.wilson_lower(50,100)


def test_deep_sizing_solves_positive_marginal_edge_over_multiple_levels():
    yes=[(0.45,2.0),(0.46,10.0)]
    no=[(0.50,2.0),(0.51,10.0)]
    out=deep.sweep(yes,no,0.0,1.0,0.01,True,100.0)
    assert out["shares"]==12.0
    assert math.isclose(out["locked_pnl_after_reserve"],0.28,abs_tol=1e-12)
    assert out["marginal_edge_per_share"]>0


def test_sell_deep_sizing_respects_prefunded_capacity_via_maximum_size():
    yes=[(0.55,100.0)]
    no=[(0.50,100.0)]
    out=deep.sweep(yes,no,0.0,1.0,0.01,False,5.0)
    assert out["shares"]==5.0
    assert math.isclose(out["locked_pnl_after_reserve"],0.20,abs_tol=1e-12)


def test_explicit_cross_market_basket_requires_exact_statewise_proof(monkeypatch=None):
    markets=[
        {"market_id":"a","asset":"BTC","horizon":"M5","contract_family":"A",
         "settlement_semantic_hash":"a"*64,"normalized_rules_hash":"1"*64,
         "window_start_unix":1,"close_timestamp_unix":2,
         "clob_token_ids":["ay","an"],"outcomes":["YES","NO"],
         "fee_schedule":{"rate":0.0,"exponent":1.0}},
        {"market_id":"b","asset":"BTC","horizon":"M5","contract_family":"B",
         "settlement_semantic_hash":"b"*64,"normalized_rules_hash":"2"*64,
         "window_start_unix":1,"close_timestamp_unix":2,
         "clob_token_ids":["by","bn"],"outcomes":["YES","NO"],
         "fee_schedule":{"rate":0.0,"exponent":1.0}},
    ]
    relation={
        "id":"proof-1","enabled":True,"states":["UP","DOWN"],"guaranteed_payout":1.0,
        "legs":[
            {"selector":{"market_id":"a"},"outcome":"YES","coefficient":1.0,
             "payout_vector":[1.0,0.0]},
            {"selector":{"market_id":"b"},"outcome":"NO","coefficient":1.0,
             "payout_vector":[0.0,1.0]},
        ],
    }
    args=SimpleNamespace(clob_url="unused",timeout_seconds=1.0,
                         maximum_shares=100.0,minimum_shares=1.0,
                         reserve_per_share=0.001,
                         minimum_locked_edge_per_share=0.001)
    books={
        "ay":{"bid":0.39,"bid_q":10.0,"ask":0.40,"ask_q":10.0},
        "bn":{"bid":0.49,"bid_q":10.0,"ask":0.50,"ask_q":10.0},
    }
    rows,checked,invalid=cross.explicit_relation_opportunities(
        [relation],markets,books,args)
    assert checked==1 and invalid==0 and len(rows)==1
    assert rows[0]["kind"]=="EXPLICIT_EXACT_PAYOUT_BASKET"
    assert rows[0]["locked_pnl_capacity"]>0
    bad=json.loads(json.dumps(relation))
    bad["legs"][1]["payout_vector"]=[1.0,0.0]
    rows,checked,invalid=cross.explicit_relation_opportunities(
        [bad],markets,books,args)
    assert checked==1 and invalid==1 and rows==[]


def test_cpp_hot_path_contract_contains_new_frequency_guards():
    source=(ROOT/"src/v7_maker_fillability_observer.cpp").read_text(encoding="utf-8")
    required=[
        "--pure-arb-prefunded-complete-set-shares",
        "pure_arb_event_queue_",
        "hot_path_io",
        "receive_to_enqueue_ns",
        "queue_wait_ns",
        "kPureArbReserveArms",
        "buy_reserve_positive",
        "sell_reserve_positive",
        "pure_arb_receive_to_decision_limit_ns_",
        "last_executable_shares_l10",
        "last_executable_shares_deep",
        "LOCAL_DEEP_BOOK_POSITIVE_MARGINAL_EDGE",
        "pure_arb_deep_queue_",
        "buy_cycles_recorded",
        "sell_cycles_recorded",
    ]
    for token in required:
        assert token in source,token
    record=source[source.index("void record_pure_arb_cycle"):
                  source.index("void flush_pure_arb_events")]
    assert ".flush()" not in record
    assert "pure_arb_output_ <<" not in record


def test_runtime_wires_all_zero_authority_frequency_workers_once():
    loop=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    workers=[
        "v7_multi_crypto_oracle_hub.py",
        "v7_settlement_source_arb_shadow.py",
        "v7_two_sided_complete_set_shadow.py",
        "v7_cross_market_exact_arb_shadow.py",
        "v7_pure_arb_arrival_survival_shadow.py",
        "v7_pure_arb_deep_sizing_shadow.py",
        "v7_polymarket_status_source.py",
        "v7_pure_arb_venue_mode.py",
        "v7_pure_arb_exchange_execution_shadow.py",
        "v7_fee_reward_registry.py",
        "v7_complete_set_merge_shadow.py",
        "v7_pure_arb_capital_allocator.py",
        "v7_pure_arb_maker_policy.py",
    ]
    for worker in workers:
        assert loop.count(worker)==1,worker
    assert "--pure-arb-prefunded-complete-set-shares 1000" in loop
    assert "v7_assert_registered_child_count 30" in loop



def test_rollover_policy_preloads_slow_contexts_inside_final_five_minutes():
    universe=(ROOT/"scripts/v7_crypto_universe.py").read_text(encoding="utf-8")
    assert "near_rollover = 0 < earliest_start - now_s <= 300" in universe
    assert 'horizon in {"M5", "M15"}' in universe



def test_arrival_shadow_defines_delay_and_reserve_counterfactuals():
    source=(ROOT/"scripts/v7_pure_arb_arrival_survival_shadow.py").read_text(encoding="utf-8")
    assert "1,2,5,10,25,50,100,200,250,275,300,400,500,750,1000" in source
    assert "reserve_curve" in source
    assert "SURVIVED_EXECUTABLE" in source
    assert "captured_pnl_l1" in source


def test_exact_relation_registry_is_empty_until_verified():
    value=json.loads((ROOT/"config/v7_exact_arb_relations.json").read_text())
    assert value["paper_only"] is True
    assert value["authenticated_execution"] is False
    assert value["real_order_submission"] is False
    assert value["automatic_promotion"] is False
    assert value["relations"]==[]



def test_prefunded_sell_inventory_is_per_market_and_consumed():
    source=(ROOT/"src/v7_maker_fillability_observer.cpp").read_text(encoding="utf-8")
    assert "prefunded_complete_set_shares_remaining" in source
    assert "market.prefunded_complete_set_shares_remaining - sell_sweep.shares()" in source
    refresh=source[source.index("void refresh_pure_arb_metadata"):
                   source.index("void reset_pure_arb_state")]
    assert "if (new_market)" in refresh
    assert "prefunded_complete_set_shares_remaining <= 0.0" not in refresh


def test_london_runtime_bundle_contains_all_pure_arb_workers():
    manifest=json.loads((ROOT/"deploy/london/runtime_manifest.json").read_text())
    entries=set(manifest["python_entrypoints"])
    expected={
        "scripts/v7_multi_crypto_oracle_hub.py",
        "scripts/v7_settlement_source_arb_shadow.py",
        "scripts/v7_two_sided_complete_set_shadow.py",
        "scripts/v7_cross_market_exact_arb_shadow.py",
        "scripts/v7_pure_arb_arrival_survival_shadow.py",
        "scripts/v7_pure_arb_deep_sizing_shadow.py",
        "scripts/v7_polymarket_status_source.py",
        "scripts/v7_pure_arb_venue_mode.py",
        "scripts/v7_pure_arb_exchange_execution_shadow.py",
        "scripts/v7_fee_reward_registry.py",
        "scripts/v7_complete_set_merge_shadow.py",
        "scripts/v7_pure_arb_capital_allocator.py",
        "scripts/v7_pure_arb_maker_policy.py",
    }
    assert expected <= entries
    assert "v7_two_sided_complete_set_shadow.py" not in manifest["forbidden_path_fragments"]
    assert "config/v7_exact_arb_relations.json" in set(manifest["support_files"])


def test_ten_point_program_contract_is_complete():
    cpp=(ROOT/"src/v7_maker_fillability_observer.cpp").read_text(encoding="utf-8")
    maker_source=(ROOT/"scripts/v7_two_sided_complete_set_shadow.py").read_text(encoding="utf-8")
    post=(ROOT/"scripts/v7_settlement_source_arb_shadow.py").read_text(encoding="utf-8")
    cross_source=(ROOT/"scripts/v7_cross_market_exact_arb_shadow.py").read_text(encoding="utf-8")
    arrival=(ROOT/"scripts/v7_pure_arb_arrival_survival_shadow.py").read_text(encoding="utf-8")
    deep_source=(ROOT/"scripts/v7_pure_arb_deep_sizing_shadow.py").read_text(encoding="utf-8")
    universe=(ROOT/"scripts/v7_crypto_universe.py").read_text(encoding="utf-8")
    runtime=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")

    # 1 funnel / 7 reserve survival
    assert "buy_after_reserve_positive" in cpp and "stale_decision_rejections" in cpp
    assert "reserve_curve" in arrival and "SURVIVED_EXECUTABLE" in arrival
    # 2 maker complete-set direct joint fills
    assert "DIRECT_EMPIRICAL_CYCLE_STATES_NOT_PRODUCT_OF_MARGINALS" in maker_source
    assert "wilson_lower" in maker_source and "research_mature" in maker_source
    # 3 prefunded SELL inventory
    assert "prefunded_complete_set_shares_remaining" in cpp
    # 4 post-fixing deterministic paths
    assert '"BUY_WINNER"' in post and '"SELL_LOSER"' in post
    # 5 exact cross-market identities / proved baskets
    assert "EXPLICIT_EXACT_PAYOUT_BASKET" in cross_source
    assert "FINITE_STATE_PAYOUT_VECTOR" in cross_source
    # 6 full-depth q* audit
    assert "positive marginal edge" in deep_source.lower() or "marginal_edge_per_share" in deep_source
    assert "incremental_pnl_vs_l10" in deep_source
    # 8 latency tail segmentation and fail-closed
    for token in ("receive_to_enqueue_ns","queue_wait_ns","receive_to_decision_ns"):
        assert token in cpp
    assert "pure_arb_receive_to_decision_limit_ns_" in cpp
    # 9 zero-downtime current+next preload
    assert "CURRENT_PLUS_NEXT_M5_M15_AND_ALL_CONTEXTS_WITHIN_300S" in universe
    assert "defer_pure_arb_membership_reload" in cpp
    # 10 event-driven taker with hot-path IO removed
    assert "evaluate_pure_arb(row)" in cpp
    record=cpp[cpp.index("void record_pure_arb_cycle"):cpp.index("void flush_pure_arb_events")]
    assert "pure_arb_output_ <<" not in record
    assert "--pure-arb-max-receive-to-decision-ms 50" in runtime


if __name__=="__main__":
    tests=[v for k,v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:test()
    print(f"pure_arb_frequency_tests={len(tests)}")
