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
    original=cross.fetch_book
    cross.fetch_book=lambda _base,token,_timeout: {
        "bid":0.39,"bid_q":10.0,
        "ask":0.40 if token=="ay" else 0.50,"ask_q":10.0,
    }
    try:
        args=SimpleNamespace(clob_url="unused",timeout_seconds=1.0,
                             maximum_shares=100.0,minimum_shares=1.0,
                             reserve_per_share=0.001,
                             minimum_locked_edge_per_share=0.001)
        rows,checked,invalid=cross.explicit_relation_opportunities(
            [relation],markets,{},args)
        assert checked==1 and invalid==0 and len(rows)==1
        assert rows[0]["kind"]=="EXPLICIT_EXACT_PAYOUT_BASKET"
        assert rows[0]["locked_pnl_capacity"]>0
        bad=json.loads(json.dumps(relation))
        bad["legs"][1]["payout_vector"]=[1.0,0.0]
        rows,checked,invalid=cross.explicit_relation_opportunities(
            [bad],markets,{},args)
        assert checked==1 and invalid==1 and rows==[]
    finally:
        cross.fetch_book=original


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
    ]
    for worker in workers:
        assert loop.count(worker)==1,worker
    assert "--pure-arb-prefunded-complete-set-shares 1000" in loop
    assert "v7_assert_registered_child_count 15" in loop


def test_arrival_shadow_defines_delay_and_reserve_counterfactuals():
    source=(ROOT/"scripts/v7_pure_arb_arrival_survival_shadow.py").read_text(encoding="utf-8")
    assert "1,2,5,10,25,50" in source
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


if __name__=="__main__":
    tests=[v for k,v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:test()
    print(f"pure_arb_frequency_tests={len(tests)}")
