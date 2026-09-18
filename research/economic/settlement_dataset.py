#!/usr/bin/env python3
"""Build causal multi-asset settlement rows from a verified frozen dataset."""
from __future__ import annotations
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys
from typing import Any

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from evidence import verify

ASSETS=("BTC","ETH","SOL","XRP","DOGE","BNB")
HORIZONS=("M5","M15","H1","H4","D1")
FEATURE_NAMES=(
    "signal_abs_bp","coinbase_aligned_bp","signal_age_ms","tte_seconds",
    "spread","log_bid_depth","log_ask_depth","book_imbalance","confirmed",
    "asset_ETH","asset_SOL","asset_XRP","asset_DOGE","asset_BNB",
    "horizon_M15","horizon_H1","horizon_H4","horizon_D1",
)

def _finite(value:Any)->float:
    out=float(value)
    if not math.isfinite(out): raise ValueError("non-finite value")
    return out
def _read_ledger(root:Path,manifest:dict)->list[dict]:
    rows=[]
    for file in manifest["files"]:
        if file["path"].endswith("ledger/execution.jsonl"):
            with (root/file["path"]).open() as handle:
                rows.extend(json.loads(line) for line in handle if line.strip())
    return rows

def _labels(ledger:list[dict])->dict[str,dict]:
    labels={}
    for row in ledger:
        if row.get("event_type")!="FINAL" or not row.get("market_id"): continue
        md=row.get("metadata") if isinstance(row.get("metadata"),dict) else {}
        payouts=md.get("settlement_payouts")
        if not isinstance(payouts,dict): continue
        context=md.get("crypto_context") if isinstance(md.get("crypto_context"),dict) else {}
        receipt=md.get("native_settlement_receipt") if isinstance(md.get("native_settlement_receipt"),dict) else {}
        asset=str(context.get("asset") or receipt.get("asset") or md.get("asset") or "").upper()
        horizon=str(context.get("horizon") or receipt.get("horizon") or md.get("horizon") or "").upper()
        if asset not in ASSETS or horizon not in HORIZONS: continue
        observed=int(row.get("recorded_ts_ms") or 0)*1_000_000
        if observed<=0: continue
        labels[str(row["market_id"])]={"payouts":payouts,"asset":asset,"horizon":horizon,
                                      "label_observed_ns":observed}
    return labels

def _decision_files(manifest:dict)->list[str]:
    names={r["path"] for r in manifest["files"]}
    files=[name for name in names if name.endswith(".jsonl") and "native_observations" in name]
    for name in files:
        if name+".closed.json" not in names: raise ValueError("native decision capture is not closed")
    return sorted(files)


def _closed_capture(root:Path,name:str)->dict:
    value=json.loads((root/(name+".closed.json")).read_text())
    if (value.get("schema")!="polymarket_v7_native_capture_closed_v1"
            or value.get("healthy") is not True or value.get("closed") is not True
            or value.get("capture_mode") not in ("DECISIONS","FULL")):
        raise ValueError("native decision capture closure invalid")
    return value
def _features(row:dict,asset:str,horizon:str)->dict[str,float]:
    bid=int(row.get("bid_e4") or 0);ask=int(row.get("ask_e4") or 0)
    bq=max(0,int(row.get("bid_quantity") or 0))/1_000_000.0
    aq=max(0,int(row.get("ask_quantity") or 0))/1_000_000.0
    direction=int(row.get("direction") or 0)
    binance=_finite(row.get("binance_return_100ms_bp",row.get("signal_return_bp",0.0)))
    coinbase=_finite(row.get("coinbase_return_100ms_bp",0.0))
    age=max(0,int(row.get("signal_age_ns") or 0))/1e6
    tte=max(0,int(row.get("tte_ns") or 0))/1e9
    total=bq+aq
    out={
        "signal_abs_bp":abs(binance),
        "coinbase_aligned_bp":direction*coinbase,
        "signal_age_ms":age,"tte_seconds":tte,
        "spread":max(0,ask-bid)/10_000.0,
        "log_bid_depth":math.log1p(bq),"log_ask_depth":math.log1p(aq),
        "book_imbalance":(bq-aq)/total if total>0 else 0.0,
        "confirmed":1.0 if row.get("confirmed_non_opposing") is True else 0.0,
    }
    for name in ASSETS[1:]: out["asset_"+name]=1.0 if asset==name else 0.0
    for name in HORIZONS[1:]: out["horizon_"+name]=1.0 if horizon==name else 0.0
    return out

def _row(observation:dict,label:dict)->dict|None:
    if observation.get("kind")!=2 or not observation.get("token_id"): return None
    asset=str(observation.get("asset") or label["asset"]).upper()
    horizon=str(observation.get("horizon") or label["horizon"]).upper()
    if asset!=label["asset"] or horizon!=label["horizon"]: return None
    bid=int(observation.get("bid_e4") or 0);ask=int(observation.get("ask_e4") or 0)
    if not (0<bid<ask<10_000) or observation.get("book_valid") is not True: return None
    decision_wall_ms=int(observation.get("decision_wall_ms") or 0)
    if decision_wall_ms<=0:return None
    token=str(observation["token_id"])
    try:payout=_finite(label["payouts"][token])
    except (KeyError,TypeError,ValueError):return None
    if payout not in (0.0,1.0):return None
    prior=((bid+ask)/2)/10_000.0
    price=ask/10_000.0
    fee_rate=_finite(observation.get("fee_rate") or 0.0)
    fee_exponent=_finite(observation.get("fee_exponent") or 1.0)
    fee_per_share=fee_rate*((price*(1-price))**fee_exponent)
    return {
        "market":str(observation["market_id"]),"asset":asset,"horizon":horizon,
        "signal_version":int(observation.get("signal_version") or 0),
        "decision_ns":decision_wall_ms*1_000_000,
        "label_observed_ns":label["label_observed_ns"],
        "pm_probability":prior,"entry_price":price,"fee_per_share":fee_per_share,
        "break_even_probability":price+fee_per_share,
        "bid_e4":bid,"ask_e4":ask,"tick_e4":int(observation.get("tick_e4") or 0),
        "bid_quantity":int(observation.get("bid_quantity") or 0),
        "ask_quantity":int(observation.get("ask_quantity") or 0),
        "minimum_order_microunits":int(observation.get("minimum_order_microunits") or 0),
        "signal_age_ns":int(observation.get("signal_age_ns") or 0),
        "tte_ns":int(observation.get("tte_ns") or 0),
        "confirmed_non_opposing":observation.get("confirmed_non_opposing") is True,
        "binance_return_100ms_bp":_finite(observation.get("binance_return_100ms_bp",0.0)),
        "outcome":int(payout),"complete":True,
        "accepted":observation.get("accepted") is True,
        "reason":int(observation.get("reason") or 0),
        "features":_features(observation,asset,horizon),
    }

def build(root:Path)->dict:
    manifest=verify(root);ledger=_read_ledger(root,manifest);labels=_labels(ledger)
    chosen={}
    raw_decisions=0
    for name in _decision_files(manifest):
        closed=_closed_capture(root,name);last_sequence=0
        with (root/name).open() as handle:
            for line in handle:
                obs=json.loads(line)
                sequence=int(obs.get("sequence") or 0)
                if sequence!=last_sequence+1:raise ValueError("native decision sequence gap")
                last_sequence=sequence
                if obs.get("schema")!="polymarket_v7_native_observation_v1" or obs.get("kind")!=2:continue
                raw_decisions+=1
                label=labels.get(str(obs.get("market_id") or ""))
                if label is None:continue
                row=_row(obs,label)
                if row is None:continue
                key=(row["market"],row["signal_version"],str(obs.get("token_id")))
                old=chosen.get(key)
                if old is None or row["decision_ns"]<old["decision_ns"]:chosen[key]=row
        if last_sequence!=int(closed.get("last_sequence") or -1):
            raise ValueError("native decision closure count mismatch")
    rows=sorted(chosen.values(),key=lambda x:(x["decision_ns"],x["market"],x["signal_version"]))
    return {
        "schema":"polymarket_settlement_training_rows_v1",
        "dataset_sha256":manifest["dataset_sha256"],"feature_names":list(FEATURE_NAMES),
        "rows":rows,"diagnostics":{
            "raw_decision_rows":raw_decisions,"labelled_distinct_signal_rows":len(rows),
            "unique_markets":len({r["market"] for r in rows}),
            "accepted_rows":sum(r["accepted"] for r in rows),
            "asset_counts":dict(Counter(r["asset"] for r in rows)),
            "horizon_counts":dict(Counter(r["horizon"] for r in rows)),
        },
    }

def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset",required=True,type=Path);p.add_argument("--output",required=True,type=Path)
    a=p.parse_args()
    if a.output.exists():raise SystemExit("output exists")
    result=build(a.dataset);a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    print(json.dumps(result["diagnostics"],indent=2))
    return 0

if __name__=="__main__":raise SystemExit(main())
