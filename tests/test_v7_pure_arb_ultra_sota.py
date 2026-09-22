from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))

import v7_combo_collateral_return_shadow as collateral
import v7_combo_rfq_shadow as rfq
import v7_exact_relation_discovery as relations

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
    return {
        "schema":"polymarket_v7_combo_market_source_v1",
        "paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"model_sha":SHA,
        "position_index":{
            "A_Y":{"market_id":"a","condition_id":"ca","outcome":"YES","complement_position_id":"A_N"},
            "B_Y":{"market_id":"b","condition_id":"cb","outcome":"YES","complement_position_id":"B_N"},
        },
    }


def test_combo_rfq_exact_hedge_bounds(monkeypatch):
    books={
        "A_Y":{"bid":0.39,"bid_q":10.0,"ask":0.40,"ask_q":10.0},
        "A_N":{"bid":0.59,"bid_q":10.0,"ask":0.60,"ask_q":10.0},
        "B_Y":{"bid":0.29,"bid_q":10.0,"ask":0.30,"ask_q":10.0},
        "B_N":{"bid":0.69,"bid_q":10.0,"ask":0.70,"ask_q":10.0},
    }
    monkeypatch.setattr(rfq,"batch_bbos",lambda base,tokens,timeout:{token:books[token] for token in tokens})
    monkeypatch.setattr(rfq,"fee_rate",lambda base,token,timeout:0.0)
    args=SimpleNamespace(clob_url="x",timeout_seconds=1.0,reference_shares=5.0,reserve_per_share=0.001)
    common={"rfq_id":"r","leg_position_ids":["A_Y","B_Y"],
            "submission_deadline":10_000,"receive_wall_ms":9_700}
    sell_yes=rfq.evaluate({**common,"direction":"BUY","side":"YES"},_catalog(),args)
    assert sell_yes["state"]=="PRICED_EXACT_BOUND"
    assert math.isclose(sell_yes["reference_quote_bound"],0.30)
    assert sell_yes["quote_budget_ms"]==300
    sell_no=rfq.evaluate({**common,"direction":"BUY","side":"NO"},_catalog(),args)
    assert math.isclose(sell_no["reference_quote_bound"],1.30)
    buy_yes=rfq.evaluate({**common,"direction":"SELL","side":"YES"},_catalog(),args)
    assert math.isclose(buy_yes["reference_quote_bound"],-0.30)
    buy_no=rfq.evaluate({**common,"direction":"SELL","side":"NO"},_catalog(),args)
    assert math.isclose(buy_no["reference_quote_bound"],0.70)


def test_collateral_return_is_never_credited_without_verified_capture():
    assert collateral.summarize({},SHA)["released_pusd"]==0.0
    bad={"model_sha":SHA,"paper_only":True,"authenticated_execution":False,
         "real_order_submission":False,"verified_capture":False,"released_pusd":100}
    assert collateral.summarize(bad,SHA)["released_pusd"]==0.0
    good={"model_sha":SHA,"paper_only":True,"authenticated_execution":False,
          "real_order_submission":False,"verified_capture":True,"released_pusd":12.5,
          "operations":[{"kind":"merge","condition_id":"x","amount":"12500000"}]}
    out=collateral.summarize(good,SHA)
    assert out["state"]=="VERIFIED_PLAN_ONLY"
    assert out["released_pusd"]==12.5


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
            "combo_collateral_return_shadow","clock_guard","pure_arb_maker_self_fill_calibration","multi_az_fencing_supervisor","combo_rfq_gateway_readonly"}<=ids
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
