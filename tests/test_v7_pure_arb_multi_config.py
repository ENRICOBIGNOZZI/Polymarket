#!/usr/bin/env python3
from __future__ import annotations
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from v7_pure_arb_multi_config import build_config

SHA="a"*40

def snapshot():
    return {
      "schema":"polymarket_v7_crypto_universe_snapshot_v1",
      "paper_only":True,
      "authenticated_execution":False,
      "real_order_submission":False,
      "execution_authority":False,
      "model_sha":SHA,
      "discovery_exhaustive":True,
      "markets":[{
        "market_id":"123","event_ids":["456"],"active":True,"closed":False,
        "accepting_orders":True,"research_only":False,
        "asset":"BTC","horizon":"M5","window_start_unix":1000,
        "close_timestamp_unix":1300,"horizon_seconds":300,
        "clob_token_ids":["yes-token","no-token"],"outcomes":["Up","Down"],
        "external_symbols":{"binance_spot":"BTCUSDT"},
        "fees_enabled":True,
        "fee_schedule":{"rate":0.02,"exponent":1.0},
      }]
    }

def allocation():
    return {
      "paper_only":True,
      "starting_capital":10_000.0,
      "max_gross_fraction":1.0,
      "max_market_fraction":1.0,
      "max_trade_usd":300.0,
      "v7":{"authenticated_execution":False,"real_order_submission":False},
      "capital_scope":{
        "scope_class":"ENGINE_ENVELOPE","engine_id":"CRYPTO_SETTLEMENT_ENGINE",
        "independent_capital_authority":False,
        "independent_risk_authority":False,
        "independent_oms_authority":False,
      }
    }

def terms(row,yes,no):
    assert yes=="yes-token" and no=="no-token"
    return {
      "yes_tick_size_e4":100,"no_tick_size_e4":100,
      "minimum_order_microunits":5_000_000,
      "fee_rate":0.02,"fee_exponent":1.0,"fee_source":"TEST",
    }

def test_builds_one_owner_config():
    v=build_config(snapshot(),allocation(),SHA,"/tmp/latency.bin",terms_resolver=terms,now_s=1100)
    assert v["paper_only"] is True
    assert v["authenticated_execution"] is False
    assert v["real_order_submission"] is False
    assert v["capital_limits"]["sleeve_budget_microdollars"]==10_000_000_000
    assert v["capital_limits"]["max_single_order_microdollars"]==300_000_000
    assert len(v["markets"])==1
    m=v["markets"][0]
    assert m["minimum_order_microunits"]==5_000_000
    assert m["yes_inventory_microunits"]==0
    assert m["no_inventory_microunits"]==0
    assert m["fee_verified"] is True
    assert all(m[k]>0 for k in (
      "market_handle","event_handle","yes_instrument_handle","no_instrument_handle"))

def test_wrong_authority_fails():
    a=allocation(); a["capital_scope"]["independent_oms_authority"]=True
    try: build_config(snapshot(),a,SHA,"/tmp/x",terms_resolver=terms,now_s=1100)
    except ValueError as e: assert "capital_scope_invalid" in str(e)
    else: raise AssertionError("expected fail closed")

if __name__=="__main__":
    tests=[v for k,v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests: fn()
    print(f"pure_arb_multi_config_tests={len(tests)}")
