from __future__ import annotations
import importlib.util
import json
import types
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0,str(ROOT/"scripts"))
SPEC=importlib.util.spec_from_file_location("v7_native_crypto_engine_manager",ROOT/"scripts/v7_native_crypto_engine_manager.py")
assert SPEC and SPEC.loader
manager=importlib.util.module_from_spec(SPEC);sys.modules[SPEC.name]=manager;SPEC.loader.exec_module(manager)

def test_all_thirty_contexts_are_explicit_and_asset_horizon_specific():
    policy=manager._signal_policy(ROOT/"config/v7_crypto_signal_policy.json")
    raw=json.loads((ROOT/"config/v7_crypto_signal_policy.json").read_text())
    assert raw["require_pm_book_pre_signal"] is True
    assert len(policy)==30
    assert set(policy)=={f"{a}:{h}" for a in ("BTC","ETH","SOL","XRP","DOGE","BNB") for h in ("M5","M15","H1","H4","D1")}
    assert policy["DOGE:M5"]["minimum_binance_return_bp"] > policy["BTC:M5"]["minimum_binance_return_bp"]
    assert policy["SOL:M5"]["maximum_signal_age_ns"] != policy["SOL:M15"]["maximum_signal_age_ns"]
    assert policy["XRP:M5"]["maximum_signal_age_ns"] != policy["XRP:M15"]["maximum_signal_age_ns"]
    assert policy["BNB:M5"]["confirmation_venue"]=="BYBIT"
    assert all(row["minimum_confirmation_return_bp"]>0 for row in policy.values())

def test_invalid_or_partial_policy_fails_closed(tmp_path):
    value=json.loads((ROOT/"config/v7_crypto_signal_policy.json").read_text())
    value["contexts"].pop("BNB:M5")
    p=tmp_path/"bad.json";p.write_text(json.dumps(value))
    try: manager._signal_policy(p)
    except RuntimeError as exc: assert str(exc)=="crypto_signal_policy_invalid"
    else: raise AssertionError("partial policy accepted")

def test_bnb_launch_uses_bybit_without_aliasing_coinbase(tmp_path,monkeypatch):
    policy=manager._signal_policy(ROOT/"config/v7_crypto_signal_policy.json")
    m=object.__new__(manager.Manager)
    m.run_root=tmp_path
    m.signal_policy=policy
    m.signal_policy_sha256="d"*64
    m.base_risk_receipt={"risk_policy_sha256":"a"*64,"limits":{
        "max_market_exposure_microdollars":10_000_000,
        "max_single_order_microdollars":5_000_000}}
    m.args=types.SimpleNamespace(
        min_order_microunits=5_000_000,target_quantity_microunits=5_000_000,
        maximum_entry_price_e4=7500,minimum_tte_ns=105_000_000_000,
        maximum_tte_ns=120_000_000_000,maker_share_cap_microunits=1_000_000,
        model_sha="b"*40,run_id="run",server_id="server",engine=Path("/tmp/engine"),
        probability_model=None,capture_native_full_context=[],
        capture_native_observations=False,capture_native_decisions=True)
    monkeypatch.setattr(manager,"tick_size_e4",lambda token:100)
    monkeypatch.setattr(manager,"venue_minimum_microunits",lambda token:5_000_000)
    monkeypatch.setattr(manager,"fee_parameters",lambda market:(.07,1.0,"TEST"))
    monkeypatch.setattr(manager,"_close_unix",lambda market:9_999_999_999)
    terms={"schema":"terms","state":"VERIFIED_SNAPSHOT","reason":"","market_id":"1",
        "snapshot_sha256":"c"*64,"observed_at_ns":1,"mandatory_taker_delay_ns":250_000_000}
    monkeypatch.setattr(manager,"execution_terms_snapshot",lambda market,fetch:terms)
    monkeypatch.setattr(manager,"persist_execution_terms",lambda root,value:tmp_path/"terms.json")
    market={"asset":"BNB","horizon":"M5","market_id":"1",
        "clob_token_ids":["yes","no"],"outcomes":["YES","NO"],"event_ids":["e"],
        "external_symbols":{"binance_spot":"BNBUSDT","coinbase_spot":None,"bybit_spot":"BNBUSDT"}}
    command=m._launch_command(market,10_000_000)
    def arg(flag):
        i=command.index(flag);return command[i+1]
    assert arg("--coinbase-symbol")=="NONE"
    assert arg("--bybit-symbol")=="BNBUSDT"
    assert arg("--confirmation-venue")=="BYBIT"
    assert arg("--signal-policy-sha256")=="d"*64
    assert "--strict-signal-policy" in command
    assert float(arg("--minimum-absolute-binance-return-bp"))==policy["BNB:M5"]["minimum_binance_return_bp"]

def test_launcher_wires_policy():
    launcher=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text()
    assert 'CRYPTO_SIGNAL_POLICY="${PM_V7_CRYPTO_SIGNAL_POLICY:-$ROOT/config/v7_crypto_signal_policy.json}"' in launcher
    assert '--signal-policy "$CRYPTO_SIGNAL_POLICY"' in launcher
    manifest=json.loads((ROOT/'config/v7_process_manifest.json').read_text())
    native=next(p for p in manifest['processes'] if p['id']=='native_engine_manager')
    assert '--signal-policy' in native['arguments']
    assert '${ROOT}/config/v7_crypto_signal_policy.json' in native['arguments']
    assert 'PROBABILITY_MODEL="${PM_V7_PROBABILITY_MODEL:-}"' in launcher
    assert '"${PROBABILITY_MODEL_ARGS[@]}"' in launcher
    assert 'PM_V7_PROBABILITY_EVALUATION_SECONDS:-7200' in launcher
    assert 'PROBABILITY_EVALUATION_SECONDS == 7200' in launcher
    assert '--probability-evaluation-end-wall-ns' in launcher
    assert '"${PROBABILITY_EVALUATION_ARGS[@]}"' in launcher
    assert 'PM_V7_PROBABILITY_EVALUATION_SECONDS:-7200' in launcher
    assert 'PROBABILITY_EVALUATION_SECONDS == 7200' in launcher
    assert '--probability-evaluation-end-wall-ns' in launcher
    assert '"${PROBABILITY_EVALUATION_ARGS[@]}"' in launcher

def test_native_candidate_wires_bybit_as_explicit_confirmation_source():
    source=(ROOT/'src/v7_crypto_settlement_engine.cpp').read_text()
    manager_source=(ROOT/'scripts/v7_native_crypto_engine_manager.py').read_text()
    assert 'VenueId::BybitSpot' in source
    assert 'orderbook.1.' in source and 'publicTrade.' in source
    assert '--confirmation-venue' in source
    assert '--bybit-symbol' in source
    assert 'symbols.get("bybit_spot")' in manager_source
    assert 'signal_policy["confirmation_venue"] == "BYBIT"' in manager_source
    # It must remain separately identified; no alias from Bybit into Coinbase.
    assert 'coinbase_symbol = bybit_symbol' not in manager_source

def test_confirmation_evidence_does_not_fabricate_unobserved_provider_returns():
    source=(ROOT/'src/v7_native_runtime_evidence.cpp').read_text()
    assert '{"coinbase_return_100ms_bp", event.confirmation_venue != external_fair::VenueId::BybitSpot' in source
    assert '{"confirmation_return_100ms_bp", event.confirmation_venue != external_fair::VenueId::Unknown' in source

def test_strict_signal_policy_does_not_silently_activate_probability_ev():
    native=(ROOT/'src/v7_crypto_settlement_engine.cpp').read_text()
    assert 'decision_policy.probability_ev_enabled = probability_model.loaded ? 1 : 0' in native
    assert '(probability_model.loaded || options.strict_signal_policy) ? 1 : 0' not in native
    # Context-specific signal gates remain strict even when no private
    # probability artifact is installed; the zero-artifact PAPER lane may then
    # emit a direction-based proposal sized by the native capital contract.
    assert 'decision_policy.require_pm_book_pre_signal = options.strict_signal_policy ? 1 : 0' in native
