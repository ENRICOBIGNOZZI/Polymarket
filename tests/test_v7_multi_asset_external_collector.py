from __future__ import annotations
import json
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
