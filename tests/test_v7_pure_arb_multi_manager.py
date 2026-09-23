from __future__ import annotations

import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))

import v7_pure_arb_multi_manager as manager


def selection() -> dict:
    rows=[]
    index=0
    for asset in ("BTC","ETH","SOL","XRP","DOGE","BNB"):
        for horizon in ("M5","M15","H1","H4","D1"):
            rows.append({
                "asset":asset,"horizon":horizon,"role":"CURRENT",
                "market_id":f"market-{index}","event_id":f"event-{index}",
                "yes_token":f"yes-{index}","no_token":f"no-{index}",
                "start_timestamp_ms":1_700_000_000_000,
                "end_timestamp_ms":1_900_000_000_000,
                "fee_schedule":None,"fees_enabled":False,
                "fees_enabled_explicit":True,
            })
            index+=1
    return {
        "schema":"polymarket_v7_multi_crypto_book_selection_v1",
        "version":2,"model_sha":"a"*40,
        "paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"execution_authority":False,
        "selection_only":True,"generation_sha256":"c"*64,
        "markets":rows,
    }


def risk() -> dict:
    return {
        "risk_policy_sha256":"b"*64,
        "canonical_engine_budget_microdollars":10_000_000_000,
        "limits":{
            "sleeve_budget_microdollars":10_000_000_000,
            "max_total_exposure_microdollars":10_000_000_000,
            "max_market_exposure_microdollars":333_333_333,
            "max_single_order_microdollars":100_000_000,
        },
    }


def test_build_config_is_one_runtime_for_exactly_30_current_contexts(tmp_path,monkeypatch):
    monkeypatch.setattr(manager,"venue_metadata",lambda token:(100,5_000_000))
    inventory={("market-0","yes-0"):(7_000_000,2_800_000)}
    config,contexts=manager.build_config(
        selection(),model_sha="a"*40,risk=risk(),
        latency_tape=tmp_path/"control/pure_arb_latency.bin",
        inventory=inventory)
    assert len(contexts)==30
    assert len(config["markets"])==30
    assert config["capital_limits"]["sleeve_budget_microdollars"]==10_000_000_000
    assert config["markets"][0]["yes_inventory_microunits"]==7_000_000
    assert config["markets"][0]["yes_collateral_basis_microdollars"]==2_800_000
    assert config["markets"][0]["no_inventory_microunits"]==0
    handles=set()
    for row in config["markets"]:
        for key in ("market_handle","event_handle","yes_instrument_handle","no_instrument_handle"):
            assert row[key]>0
            assert row[key] not in handles
            handles.add(row[key])
        assert row["fee_rate"]==0.0
        assert row["minimum_order_microunits"]==5_000_000


def test_empty_ledger_has_zero_carryover(tmp_path):
    exposure,inventory,unsettled=manager.ledger_state(tmp_path,"a"*40)
    assert exposure["total_unsettled_microdollars"]==0
    assert inventory=={}
    assert unsettled==set()


def test_manager_contract_is_single_process_and_cold_settlement_only():
    source=(ROOT/"scripts/v7_pure_arb_multi_manager.py").read_text()
    launcher=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text()
    manifest=__import__("json").loads((ROOT/"config/v7_process_manifest.json").read_text())
    assert '"partitioned_native_workers": False' in source
    assert '"worker_process_count": 1 if alive else 0' in source
    assert "launch_worker" not in source
    assert source.count("self.child = subprocess.Popen")==1
    assert "start_settlements" in source
    assert "remaining_capital_lease" in source
    assert "ledger_state" in source
    assert "v7_native_crypto_engine_manager.py" not in launcher
    assert launcher.count("v7_pure_arb_multi_manager.py")==1
    manager_row=next(x for x in manifest["processes"] if x["id"]=="native_engine_manager")
    engine_row=next(x for x in manifest["processes"] if x["id"]=="crypto_settlement_engine")
    assert manager_row["executable"]=="scripts/v7_pure_arb_multi_manager.py"
    assert engine_row["executable"]=="${PURE_ARB_MULTI_RUNTIME}"


if __name__=="__main__":
    test_manager_contract_is_single_process_and_cold_settlement_only()
    print("pure_arb_multi_manager_static=PASS")
