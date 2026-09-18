from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'research/economic'))
from ev_gate_report import analyze, policy_metrics

FEATURES = {
    "signal_return_bp": 1.0, "confirmation_return_bp": .2,
    "abs_signal_return_bp": 1.0, "aligned_signal_return_bp": 1.0,
    "aligned_confirmation_return_bp": .2, "tte_seconds": 60.0,
    "signal_age_ms": 10.0, "spread": .01, "bid_depth_shares": 20.0,
    "ask_depth_shares": 20.0, "depth_imbalance": 0.0,
    "confirmed_non_opposing": 1.0,
    "asset_BTC":1.0,"asset_ETH":0.0,"asset_SOL":0.0,"asset_XRP":0.0,"asset_DOGE":0.0,"asset_BNB":0.0,
    "horizon_M5":1.0,"horizon_M15":0.0,"horizon_H1":0.0,"horizon_H4":0.0,"horizon_D1":0.0,
}

def synthetic(n=180):
    rows=[]
    base=1_000_000_000_000
    for i in range(n):
        y=i%2
        features=dict(FEATURES)
        features["signal_return_bp"] = 2.0 if y else -2.0
        features["aligned_signal_return_bp"] = 2.0 if y else -2.0
        rows.append({
            "market":str(i),"asset":"BTC","horizon":"M5",
            "decision_ns":base+i*1_000_000_000,
            "label_observed_ns":base+i*1_000_000_000+100_000_000,
            "complete":True,"outcome":y,"pm_probability":.5,
            "executable_ask":.51,"fee_per_share":.001,
            "features":features,
        })
    payload=json.dumps(rows,sort_keys=True,separators=(",",":")).encode()
    return {"schema":"polymarket_v7_native_ev_dataset_v1","rows":rows,
            "dataset_sha256":hashlib.sha256(payload).hexdigest()}

def test_policy_gate_respects_price_and_fee():
    rows=[
        {"outcome":1,"executable_ask":.99,"fee_per_share":.001},
        {"outcome":1,"executable_ask":.50,"fee_per_share":.001},
    ]
    result=policy_metrics(rows,[.995,.55],buffer=.01)
    assert result["trades"]==1
    assert result["net_pnl_per_share_total"]==.499

def test_insufficient_dataset_fails_closed():
    report=analyze(synthetic(50),minimum_training_markets=30,minimum_validation_markets=20,minimum_test_markets=20)
    assert report["state"]=="INSUFFICIENT_EVIDENCE"
    assert report["automatic_promotion"] is False

def test_oos_report_keeps_test_threshold_fixed():
    report=analyze(synthetic(),minimum_training_markets=80,minimum_validation_markets=20,minimum_test_markets=20)
    assert report["state"]=="HELDOUT_EVALUATED_RESEARCH_ONLY"
    assert report["primary_buffer_per_share"]==.01
    assert set(report["validation_buffer_diagnostics_no_selection"])=={"0.0","0.005","0.01","0.02"}
    assert report["test"]["model_brier"] < report["test"]["pm_brier"]
    assert report["test"]["model_ev_gate"]["trades"] > 0
    assert report["automatic_promotion"] is False
    assert report["real_money_authorized"] is False
