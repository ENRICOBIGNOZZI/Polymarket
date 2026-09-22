#!/usr/bin/env python3
"""Calibrate pure-arb maker queue arms against verified own-order fills.

No order submission and no automatic promotion.  Until authenticated self-fill
evidence exists, the only valid state is WAITING_FOR_VERIFIED_SELF_FILL_EVIDENCE.
"""
from __future__ import annotations
import argparse,json,math,time
from collections import defaultdict
from pathlib import Path
from typing import Any
from v7_pure_arb_economics import tail_jsonl

SCHEMA="polymarket_v7_maker_self_fill_calibration_v1"
EVIDENCE_SCHEMA="polymarket_v7_verified_self_fill_event_v1"
MAKER_SCHEMA="polymarket_v7_two_sided_complete_set_cycle_v2"

def verified_evidence(path:Path,sha:str,max_rows:int)->dict[str,dict[str,Any]]:
    out={}
    for row in tail_jsonl(path,max_rows=max_rows,max_bytes=67_108_864):
        if (row.get("schema")!=EVIDENCE_SCHEMA or row.get("model_sha")!=sha
            or row.get("verified") is not True or row.get("source")!="USER_WS_SELF_ORDER"):
            continue
        cid=str(row.get("cycle_id") or "")
        if not cid:continue
        try:y=float(row.get("yes_filled_shares") or 0);n=float(row.get("no_filled_shares") or 0)
        except (TypeError,ValueError):continue
        if y<0 or n<0:continue
        out[cid]={**row,"yes_filled_shares":y,"no_filled_shares":n}
    return out

def calibrate(cycles:Path,evidence:Path,sha:str,max_rows:int,minimum:int)->dict[str,Any]:
    actual=verified_evidence(evidence,sha,max_rows)
    base={"schema":SCHEMA,"paper_only":True,"authenticated_execution":False,
          "real_order_submission":False,"automatic_promotion":False,"model_sha":sha,
          "timestamp_ms":time.time_ns()//1_000_000,"verified_self_fill_events":len(actual)}
    if not actual:
        return {**base,"state":"WAITING_FOR_VERIFIED_SELF_FILL_EVIDENCE","arms":[]}
    arms=defaultdict(lambda:{"n":0,"errors":0,"paired_actual":0,"paired_predicted":0})
    matched=0
    for row in tail_jsonl(cycles,max_rows=max_rows,max_bytes=67_108_864):
        if row.get("schema")!=MAKER_SCHEMA or row.get("model_sha")!=sha:continue
        cid=str(row.get("cycle_id") or "")
        observed=actual.get(cid)
        if observed is None:continue
        matched+=1
        target=max(0.0,float(row.get("target_shares") or 0))
        actual_pair=min(observed["yes_filled_shares"],observed["no_filled_shares"])+1e-12>=target>0
        for scenario in row.get("queue_scenarios") or []:
            if not isinstance(scenario,dict):continue
            key=f"q={float(scenario.get('multiplier') or 0):.3f}|c={float(scenario.get('cancel_relief_fraction') or 0):.3f}"
            predicted=str(scenario.get("state") or "")=="BOTH_FULL"
            cell=arms[key];cell["n"]+=1;cell["errors"]+=predicted!=actual_pair
            cell["paired_actual"]+=actual_pair;cell["paired_predicted"]+=predicted
    rows=[]
    for key,cell in arms.items():
        n=cell["n"]
        rows.append({"arm":key,**cell,
                     "classification_error_rate":cell["errors"]/n if n else None,
                     "actual_paired_rate":cell["paired_actual"]/n if n else None,
                     "predicted_paired_rate":cell["paired_predicted"]/n if n else None})
    rows.sort(key=lambda x:(x["classification_error_rate"] if x["classification_error_rate"] is not None else 2,-x["n"],x["arm"]))
    enough=matched>=minimum and rows and rows[0]["n"]>=minimum
    return {**base,"state":"CALIBRATED_RESEARCH_ONLY" if enough else "INSUFFICIENT_VERIFIED_SELF_FILL_EVIDENCE",
            "matched_cycles":matched,"minimum_samples":minimum,
            "recommended_research_arm":rows[0]["arm"] if enough else None,
            "recommendation_has_execution_authority":False,"arms":rows}

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--maker-cycles",type=Path,required=True)
    ap.add_argument("--verified-self-fills",type=Path,required=True);ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--maximum-rows",type=int,default=50000);ap.add_argument("--minimum-samples",type=int,default=100)
    ap.add_argument("--interval-seconds",type=float,default=30.0)
    a=ap.parse_args();a.output.parent.mkdir(parents=True,exist_ok=True)
    while True:
        value=calibrate(a.maker_cycles,a.verified_self_fills,a.model_sha,a.maximum_rows,a.minimum_samples)
        tmp=a.output.with_suffix(a.output.suffix+".tmp")
        tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(a.output)
        time.sleep(a.interval_seconds)

if __name__=="__main__":raise SystemExit(main())
