from __future__ import annotations
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'research/economic'))
from native_ev_report import evaluate

def row(i:int)->dict:
    y=i%2;signal=1.0 if y else -1.0
    features={
      "signal_strength_bp":abs(signal),"confirmation_aligned_bp":.2,"tte_seconds":60.0,
      "signal_age_ms":10.0,"spread":.01,"depth_imbalance":.1,"direction_up":float(y),
      "technical_signal_valid":1.0,"confirmed_non_opposing":1.0,
    }
    for a in ("BTC","ETH","SOL","XRP","DOGE","BNB"):features[f"asset_{a}"]=1.0 if a=="BTC" else 0.0
    for h in ("M5","M15","H1","H4","D1"):features[f"horizon_{h}"]=1.0 if h=="M5" else 0.0
    decision=(i+1)*10_000_000_000
    return {"market":str(i),"decision_ns":decision,"label_observed_ns":decision+1_000_000_000,
            "outcome":y,"pm_probability":.5,"executable_ask":.51,"fee_per_share":.017493,
            "ex_post_net_per_share":y-.51-.017493,"features":features,"complete":True}

def test_small_sample_stays_fail_closed():
    ds={"schema":"polymarket_v7_native_ev_dataset_v1","dataset_sha256":"a"*64,"rows":[row(i) for i in range(50)]}
    report=evaluate(ds)
    assert report["state"]=="INSUFFICIENT_EVIDENCE" and report["automatic_promotion"] is False

def test_causal_split_purges_labels_unavailable_at_cutoff():
    rows=[row(i) for i in range(220)]
    rows[0]["label_observed_ns"]=10**18
    ds={"schema":"polymarket_v7_native_ev_dataset_v1","dataset_sha256":"b"*64,"rows":rows}
    report=evaluate(ds)
    assert report["split"]["purged"]>=1
    assert report["state"]=="HELDOUT_EVALUATED_NO_AUTO_PROMOTION"
    assert report["automatic_promotion"] is False
    assert len(report["test"]["gates"])==5
