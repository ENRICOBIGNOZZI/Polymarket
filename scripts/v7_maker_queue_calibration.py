#!/usr/bin/env python3
"""Calibrate PAPER maker queue arms against independently verified own fills.

No authenticated connection is opened here.  The input fill receipt tape must
come from a separately authorized capture and every receipt must be explicitly
verified.  Recommendations are research output only and are never promoted
automatically.
"""
from __future__ import annotations
import argparse,json,math,time
from pathlib import Path
from typing import Any

from v7_pure_arb_economics import tail_jsonl

SCHEMA="polymarket_v7_maker_queue_calibration_v1"
RECEIPT_SCHEMA="polymarket_v7_verified_maker_fill_receipt_v1"
CYCLE_SCHEMA="polymarket_v7_two_sided_complete_set_cycle_v2"

def _arm_key(row:dict[str,Any])->tuple[float,float]|None:
    try:
        m=float(row.get("multiplier"));r=float(row.get("cancel_relief_fraction"))
    except (TypeError,ValueError,OverflowError):return None
    return (m,r) if math.isfinite(m) and m>0 and math.isfinite(r) and 0<=r<=1 else None

def calibrate(
    cycles_path:Path,receipts_path:Path,model_sha:str,*,max_rows:int=50_000,
    minimum_samples:int=20,
)->dict[str,Any]:
    cycles={}
    for row in tail_jsonl(cycles_path,max_rows=max_rows,max_bytes=64*1024*1024):
        if (row.get("schema")==CYCLE_SCHEMA and row.get("model_sha")==model_sha
            and row.get("paper_only") is True):
            cid=str(row.get("cycle_id") or "")
            if cid:cycles[cid]=row
    receipts=[]
    for row in tail_jsonl(receipts_path,max_rows=max_rows,max_bytes=64*1024*1024):
        if (row.get("schema")==RECEIPT_SCHEMA and row.get("model_sha")==model_sha
            and row.get("verified") is True and row.get("paper_only") is True
            and row.get("authenticated_execution") is False
            and row.get("real_order_submission") is False):
            receipts.append(row)
    scores={}
    matched=0
    for receipt in receipts:
        cid=str(receipt.get("cycle_id") or "")
        cycle=cycles.get(cid)
        if cycle is None:continue
        try:
            actual_yes=max(0.0,float(receipt.get("yes_filled_shares") or 0.0))
            actual_no=max(0.0,float(receipt.get("no_filled_shares") or 0.0))
            target=max(1e-12,float(cycle.get("target_shares") or 0.0))
        except (TypeError,ValueError,OverflowError):continue
        matched+=1
        actual_pair=min(1.0,min(actual_yes,actual_no)/target)
        for arm in cycle.get("queue_scenarios") or []:
            if not isinstance(arm,dict):continue
            key=_arm_key(arm)
            if key is None:continue
            try:
                py=max(0.0,float(arm.get("yes_filled_shares") or 0.0))
                pn=max(0.0,float(arm.get("no_filled_shares") or 0.0))
            except (TypeError,ValueError,OverflowError):continue
            pred_pair=min(1.0,min(py,pn)/target)
            pred_yes=min(1.0,py/target);pred_no=min(1.0,pn/target)
            act_yes=min(1.0,actual_yes/target);act_no=min(1.0,actual_no/target)
            bucket=scores.setdefault(key,{"n":0,"loss":0.0,"pair_loss":0.0})
            bucket["n"]+=1
            bucket["loss"]+=(pred_yes-act_yes)**2+(pred_no-act_no)**2
            bucket["pair_loss"]+=(pred_pair-actual_pair)**2
    ranked=[]
    for (mult,relief),v in scores.items():
        n=int(v["n"])
        ranked.append({
            "queue_ahead_multiplier":mult,"cancel_relief_fraction":relief,"samples":n,
            "mean_leg_brier":v["loss"]/(2*n) if n else None,
            "mean_pair_brier":v["pair_loss"]/n if n else None,
        })
    ranked.sort(key=lambda x:(
        x["mean_pair_brier"] if x["mean_pair_brier"] is not None else math.inf,
        x["mean_leg_brier"] if x["mean_leg_brier"] is not None else math.inf,
        x["queue_ahead_multiplier"],x["cancel_relief_fraction"]))
    eligible=[x for x in ranked if x["samples"]>=minimum_samples]
    state="CALIBRATED_RESEARCH_ONLY" if eligible else "NO_VERIFIED_FILL_RECEIPTS"
    if receipts and matched and not eligible:state="INSUFFICIENT_VERIFIED_SAMPLES"
    return {
        "schema":SCHEMA,"paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"automatic_promotion":False,
        "execution_authority":"ZERO_AUTHORITY_CALIBRATION",
        "model_sha":model_sha,"timestamp_ms":time.time_ns()//1_000_000,
        "state":state,"verified_receipts":len(receipts),"matched_cycles":matched,
        "minimum_samples":minimum_samples,"recommended":eligible[0] if eligible else None,
        "arms":ranked,
    }

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles",type=Path,required=True);ap.add_argument("--receipts",type=Path,required=True)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--minimum-samples",type=int,default=20);ap.add_argument("--interval-seconds",type=float,default=10)
    args=ap.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    while True:
        value=calibrate(args.cycles,args.receipts,args.model_sha,minimum_samples=args.minimum_samples)
        tmp=args.output.with_suffix(args.output.suffix+".tmp")
        tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(args.output)
        time.sleep(args.interval_seconds)

if __name__=="__main__":raise SystemExit(main())
