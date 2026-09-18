from __future__ import annotations
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import v7_multi_asset_external_collector as collector


def registry():
    return json.loads((ROOT/"config/v7_crypto_settlement_markets.json").read_text())


def ready_status(asset: str, sha: str, now_ns: int) -> dict:
    secondary = "bybit_spot" if asset == "BNB" else "coinbase_spot"
    return {
        "schema":"polymarket_v7_external_venue_runtime_v1",
        "asset":asset,"code_sha":sha,"state":"OPERATIONAL","valid":True,
        "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
        "timestamp_ns":now_ns-1_000_000,
        "raw_frame_tapes":{
            "binance_spot":{"enabled":True,"writer_healthy":True,"evidence_valid":True,"written":10,"dropped":0},
            secondary:{"enabled":True,"writer_healthy":True,"evidence_valid":True,"written":5,"dropped":0},
        },
    }


def test_registry_symbols_cover_exactly_six_assets_consistently():
    symbols=collector.canonical_symbols(registry())
    assert set(symbols)==set(collector.ASSETS)
    assert symbols["BTC"]["binance_spot"]=="BTCUSDT"
    assert symbols["BNB"]["binance_spot"]=="BNBUSDT"
    assert symbols["BNB"]["coinbase_spot"]==""
    assert all(row["binance_spot"] for row in symbols.values())


def test_data_ready_requires_primary_and_secondary_tapes_without_drops():
    sha="a"*40; now=10_000_000_000
    for asset in collector.ASSETS:
        ok,reason=collector.data_ready(ready_status(asset,sha,now),asset=asset,sha=sha,now_ns=now)
        assert ok and reason==""
    bad=ready_status("ETH",sha,now)
    bad["raw_frame_tapes"]["binance_spot"]["dropped"]=1
    assert collector.data_ready(bad,asset="ETH",sha=sha,now_ns=now)==(False,"BINANCE_TAPE_NOT_READY")


def test_data_ready_rejects_stale_or_wrong_asset():
    sha="b"*40; now=20_000_000_000
    stale=ready_status("SOL",sha,now)
    stale["timestamp_ns"]=1
    assert collector.data_ready(stale,asset="SOL",sha=sha,now_ns=now)==(False,"STALE")
    wrong=ready_status("XRP",sha,now)
    assert collector.data_ready(wrong,asset="DOGE",sha=sha,now_ns=now)==(False,"STATUS_CONTRACT")


if __name__=="__main__":
    tests=sorted((n,f) for n,f in globals().items() if n.startswith("test_") and callable(f))
    for _,test in tests:test()
    print(f"{len(tests)} function tests passed")


def test_tape_paths_are_restart_unique_for_supervisor_process():
    btc=collector.child_paths(Path("/tmp/run"),"BTC")
    eth=collector.child_paths(Path("/tmp/run"),"ETH")
    assert str(os.getpid()) in btc["tape"].name
    assert str(os.getpid()) in eth["tape"].name
    assert btc["tape"] != eth["tape"]
    assert btc["tape"].parent.name == "tapes"
    assert eth["tape"].parent.name == "tapes"


def test_degraded_composite_with_complete_primary_secondary_tapes_is_collection_ready():
    sha="c"*40; now=30_000_000_000
    row=ready_status("BNB",sha,now)
    row["state"]="WARMING_OR_DEGRADED"
    row["valid"]=False
    ok,reason=collector.data_ready(row,asset="BNB",sha=sha,now_ns=now)
    assert ok and reason==""


def test_degraded_composite_still_fails_when_secondary_tape_missing():
    sha="d"*40; now=40_000_000_000
    row=ready_status("BNB",sha,now)
    row["state"]="WARMING_OR_DEGRADED"
    row["valid"]=False
    row["raw_frame_tapes"]["bybit_spot"]["written"]=0
    assert collector.data_ready(row,asset="BNB",sha=sha,now_ns=now)==(
        False,"SECONDARY_SPOT_TAPE_NOT_READY")


def test_collector_state_degrades_without_process_exit():
    assert collector.collector_state(ready=6,total=6,elapsed=500,startup_timeout=60)=="OPERATIONAL"
    assert collector.collector_state(ready=5,total=6,elapsed=10,startup_timeout=60)=="WARMING"
    assert collector.collector_state(ready=5,total=6,elapsed=61,startup_timeout=60)=="DEGRADED"
    source=(ROOT/"scripts/v7_multi_asset_external_collector.py").read_text()
    assert 'state="BLOCKED_CHILD_EXIT"' not in source
    assert 'return 70' not in source
    assert 'return 77' not in source
    assert 'CHILD_RESTART_FAILED' in source


def test_tape_session_id_changes_across_restarts():
    first=collector.child_paths(Path("/tmp/run"),"BNB",session_id="session-a")
    second=collector.child_paths(Path("/tmp/run"),"BNB",session_id="session-b")
    assert first["tape"] != second["tape"]
    assert first["tape"].name.endswith("session-a.bin")
    assert second["tape"].name.endswith("session-b.bin")
