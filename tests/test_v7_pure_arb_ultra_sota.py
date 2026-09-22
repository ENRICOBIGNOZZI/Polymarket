from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))

import v7_combo_collateral_return_shadow as collateral
import v7_combo_rfq_shadow as rfq
import v7_exact_relation_discovery as relations
import v7_maker_self_fill_calibration as maker_cal

SHA="c"*40


def test_exact_relation_proof_and_duplicate_discovery():
    universe={
        "paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"model_sha":SHA,
        "markets":[
            {"market_id":"a","active":True,"closed":False,"asset":"BTC","horizon":"M5",
             "contract_family":"BTC_UP","settlement_semantic_hash":"1"*64,
             "window_start_unix":100,"close_timestamp_unix":400},
            {"market_id":"b","active":True,"closed":False,"asset":"BTC","horizon":"M5",
             "contract_family":"BTC_UP","settlement_semantic_hash":"1"*64,
             "window_start_unix":100,"close_timestamp_unix":400},
        ],
    }
    out=relations.build(universe,SHA)
    assert len(out["relations"])==2
    for row in out["relations"]:
        proof=relations.prove_relation(row)
        assert proof["constant_payout"]=="1"
        assert proof["state_totals"]==["1","1"]


def test_exact_relation_proof_rejects_nonconstant_basket():
    bad={
        "states":["A","B"],"guaranteed_payout":1,
        "legs":[
            {"selector":{},"outcome":"YES","coefficient":1,"payout_vector":[1,0]},
            {"selector":{},"outcome":"NO","coefficient":1,"payout_vector":[0,0]},
        ],
    }
    try:relations.prove_relation(bad)
    except ValueError:pass
    else:raise AssertionError("nonconstant relation accepted")


def _catalog():
    def row(mid,condition,outcome,comp):
        return {"market_id":mid,"condition_id":condition,"outcome":outcome,
                "complement_position_id":comp,"fee_rate":0.0,"fee_verified":True}
    return {
        "schema":"polymarket_v7_combo_market_source_v1",
        "paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"model_sha":SHA,
        "position_index":{
            "A_Y":row("a","ca","YES","A_N"),
            "A_N":row("a","ca","NO","A_Y"),
            "B_Y":row("b","cb","YES","B_N"),
            "B_N":row("b","cb","NO","B_Y"),
        },
    }

def test_combo_rfq_exact_hedge_bounds():
    books={
        "A_Y":{"bid":0.39,"bid_q":10.0,"ask":0.40,"ask_q":10.0},
        "A_N":{"bid":0.19,"bid_q":10.0,"ask":0.20,"ask_q":10.0},
        "B_Y":{"bid":0.29,"bid_q":10.0,"ask":0.30,"ask_q":10.0},
        "B_N":{"bid":0.24,"bid_q":10.0,"ask":0.25,"ask_q":10.0},
    }
    original_batch=rfq.batch_bbos
    rfq.batch_bbos=lambda base,tokens,timeout:{token:books[token] for token in tokens}
    args=SimpleNamespace(clob_url="x",timeout_seconds=1.0,reserve_per_share=0.001)
    try:
        common={"rfq_id":"r","leg_position_ids":["A_Y","B_Y"],
                "submission_deadline":10_000,"receive_wall_ms":9_700}

        buy=rfq.evaluate({
            **common,"direction":"BUY","side":"YES",
            "requested_size":{"unit":"notional","value_e6":"1000000"},
        },_catalog(),args)
        assert buy["state"]=="PRICED_EXACT_BOUND"
        assert math.isclose(buy["reference_quote_bound"],0.30)
        assert math.isclose(buy["actionable_bound"],0.301)
        assert buy["quote_budget_ms"]==300
        assert buy["sizing_semantics"]=="BUY_NOTIONAL_FLOOR_AT_QUOTE_PRICE"
        assert 3.32 < buy["quote_size_shares"] < 3.33

        sell=rfq.evaluate({
            **common,"rfq_id":"s","direction":"SELL","side":"YES",
            "requested_size":{"unit":"shares","value_e6":"2000000"},
        },_catalog(),args)
        assert sell["state"]=="PRICED_EXACT_BOUND"
        assert math.isclose(sell["reference_quote_bound"],0.55)
        assert math.isclose(sell["actionable_bound"],0.549)
        assert sell["quote_size_shares"]==2.0
        assert sell["sizing_semantics"]=="SELL_EXACT_SHARES"

        unsupported=rfq.evaluate({
            **common,"rfq_id":"x","direction":"BUY","side":"NO",
            "requested_size":{"unit":"notional","value_e6":"1000000"},
        },_catalog(),args)
        assert unsupported["state"]=="INVALID_OR_UNSUPPORTED_REQUEST"
    finally:
        rfq.batch_bbos=original_batch


def test_collateral_return_is_never_credited_without_verified_capture():
    assert collateral.summarize({},SHA)["released_pusd"]==0.0
    bad={"model_sha":SHA,"paper_only":True,"authenticated_execution":False,
         "real_order_submission":False,"verified_capture":False,"released_pusd":100}
    assert collateral.summarize(bad,SHA)["released_pusd"]==0.0
    good={"model_sha":SHA,"paper_only":True,"authenticated_execution":False,
          "real_order_submission":False,"verified_capture":True,
          "planHash":"0xabc","chainId":137,"netPusdOut":"12.500000",
          "operationCount":1,"truncated":False,
          "operations":[{"kind":"merge_on_condition","conditionId":"x",
                         "conditionIndex":0,"amount":"12.500000"}]}
    out=collateral.summarize(good,SHA)
    assert out["state"]=="VERIFIED_PLAN_ONLY"
    assert out["released_pusd"]==12.5


def test_self_fill_calibration_uses_verified_user_ws_fractional_fills():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        cycles=root/"cycles.jsonl";fills=root/"fills.jsonl"
        cycle={
            "schema":"polymarket_v7_two_sided_complete_set_cycle_v2",
            "model_sha":SHA,"paper_only":True,"cycle_id":"c","target_shares":10,
            "queue_scenarios":[
                {"multiplier":1.0,"cancel_relief_fraction":0.0,
                 "state":"BOTH_PARTIAL","yes_filled_shares":8,"no_filled_shares":6},
                {"multiplier":2.0,"cancel_relief_fraction":0.0,
                 "state":"BOTH_PARTIAL","yes_filled_shares":4,"no_filled_shares":3},
            ],
        }
        fill={
            "schema":"polymarket_v7_verified_self_fill_event_v1",
            "model_sha":SHA,"verified":True,"source":"USER_WS_SELF_ORDER",
            "cycle_id":"c","yes_filled_shares":7.5,"no_filled_shares":6.2,
        }
        cycles.write_text(json.dumps(cycle)+"\n",encoding="utf-8")
        fills.write_text(json.dumps(fill)+"\n",encoding="utf-8")
        out=maker_cal.calibrate(cycles,fills,SHA,100,1)
        assert out["state"]=="CALIBRATED_RESEARCH_ONLY"
        assert out["arms"][0]["arm"]=="q=1.000|c=0.000"
        assert out["arms"][0]["mean_pair_brier"] is not None


def test_rfq_gateway_has_no_quote_or_confirmation_send_path():
    source=(ROOT/"scripts/v7_combo_rfq_gateway_readonly.py").read_text()
    assert source.count("_send_frame(sock") == 2  # auth helper + websocket pong only
    assert "outbound_application_messages\":\"AUTH_ONLY" in source
    assert "signed_order" not in source


def test_fencing_supervisor_uses_only_canonical_aws_lease():
    source=(ROOT/"scripts/v7_multi_az_fencing_supervisor.py").read_text()
    assert "ops/v7_aws_fencing_lease.py" in source
    assert "v7_multi_az_fencing_lease.py" not in source
    assert "MULTI_AZ_FENCING_LEASE_LOST" in source


def test_ultra_sota_runtime_is_registered_fail_closed():
    loop=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text()
    for name in (
        "v7_exact_relation_discovery.py","v7_combo_market_source.py",
        "v7_combo_rfq_shadow.py","v7_combo_collateral_return_shadow.py",
        "v7_clock_guard.py","v7_combo_rfq_gateway_readonly.py",
        "v7_multi_az_fencing_supervisor.py",
    ):
        assert loop.count(name)==1
    assert "v7_assert_registered_child_count 30" in loop
    manifest=json.loads((ROOT/"config/v7_process_manifest.json").read_text())
    assert manifest["expected_process_count"]==31
    assert manifest["expected_launcher_child_count"]==30
    ids={p["id"] for p in manifest["processes"]}
    assert {"pure_arb_exact_relation_discovery","combo_market_source","combo_rfq_shadow",
            "combo_collateral_return_shadow","clock_guard","pure_arb_maker_self_fill_calibration","multi_az_fencing_supervisor",
            "combo_rfq_gateway_readonly"}<=ids
    bundle=json.loads((ROOT/"deploy/london/runtime_manifest.json").read_text())
    for name in (
        "scripts/v7_exact_relation_discovery.py","scripts/v7_combo_market_source.py",
        "scripts/v7_combo_rfq_shadow.py","scripts/v7_combo_collateral_return_shadow.py",
        "scripts/v7_clock_guard.py","scripts/v7_maker_self_fill_calibration.py",
        "scripts/v7_multi_az_fencing_guard.py","scripts/v7_multi_az_fencing_supervisor.py",
        "scripts/v7_combo_rfq_gateway_readonly.py",
    ):
        assert name in bundle["python_entrypoints"]


def test_rate_limiter_is_pre_wire_on_native_lane():
    source=(ROOT/"src/v7_native_clob_order_lane.cpp").read_text()
    wire=source.index("tls.write_all")
    budget=source.index("rate_limiter.try_acquire")
    signing=source.index("sign_prepared_poly1271_hex")
    assert budget < signing < wire
    assert "Poly-RateLimit" in (ROOT/"src/v7_clob_http1_response.cpp").read_text()


def test_research_collectors_never_regress_to_serial_book_rest():
    cross=(ROOT/"scripts/v7_cross_market_exact_arb_shadow.py").read_text()
    deep=(ROOT/"scripts/v7_pure_arb_deep_sizing_shadow.py").read_text()
    combo=(ROOT/"scripts/v7_combo_rfq_shadow.py").read_text()
    assert "fetch_books" in cross and "fetch_books" in deep and "fetch_books" in combo
    assert "def fetch_book(" not in cross
    assert "/book?" not in deep
    assert "/book?" not in combo
    assert "/fee-rate?" not in combo
    assert "CENSORED_FEE_METADATA" in combo


def test_native_prewire_benchmark_matches_production_prepared_path():
    source=(ROOT/"src/v7_native_clob_full_prewire_bench.cpp").read_text()
    assert "ExchangeV2PreparedOrderHasher" in source
    assert "PreparedHasher" in source
    assert "sign_prepared_poly1271_hex" in source
    assert "production_prepared_path" in source
    assert "sign_poly1271_hex(hasher" not in source


if __name__=="__main__":
    tests=[value for name,value in sorted(globals().items())
           if name.startswith("test_") and callable(value)]
    for test in tests:test()
    print(f"pure_arb_ultra_sota_tests={len(tests)}")
