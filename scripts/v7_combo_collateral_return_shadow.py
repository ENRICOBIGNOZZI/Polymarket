#!/usr/bin/env python3
"""Zero-authority verifier for captured Combo collateral-return plans.

The script never requests or executes a plan.  It validates an externally
captured plan receipt and reports the pUSD capital-release amount/operations for
research attribution.  Unknown/truncated/unconfirmed plans remain fail-closed.
"""
from __future__ import annotations
import argparse,json,time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCHEMA="polymarket_v7_combo_collateral_return_shadow_v1"
ALLOWED={
    "split","merge","redeem","split_on_condition","merge_on_condition",
    "split_on_event","merge_on_event","convert_on_event","extract","inject",
    "convert_to_yes_basket","merge_from_yes_basket","compress",
}

def load(path:Path)->dict[str,Any]:
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}

def summarize(plan:dict[str,Any],sha:str)->dict[str,Any]:
    base={"schema":SCHEMA,"paper_only":True,"authenticated_execution":False,
          "real_order_submission":False,"execution_authority":"ZERO_AUTHORITY_PLAN_AUDIT",
          "model_sha":sha,"timestamp_ms":time.time_ns()//1_000_000,"state":"NO_VERIFIED_PLAN",
          "released_pusd":0.0}
    if not plan:return base
    if (plan.get("model_sha")!=sha or plan.get("paper_only") is not True
        or plan.get("authenticated_execution") is not False
        or plan.get("real_order_submission") is not False
        or plan.get("verified_capture") is not True):
        base["state"]="UNVERIFIED_PLAN";return base
    operations=plan.get("operations")
    if not isinstance(operations,list):base["state"]="INVALID_PLAN";return base
    normalized=[]
    for op in operations:
        if not isinstance(op,dict):base["state"]="INVALID_PLAN";return base
        kind=str(op.get("kind") or op.get("type") or "").lower()
        if kind not in ALLOWED:base["state"]="UNKNOWN_OPERATION";return base
        normalized.append({
            "kind":kind,
            "condition_id":str(op.get("conditionId") or op.get("condition_id") or ""),
            "event_id":str(op.get("eventId") or op.get("event_id") or ""),
            "position_id":str(op.get("positionId") or op.get("position_id") or ""),
            "condition_index":op.get("conditionIndex",op.get("condition_index")),
            "amount":str(op.get("amount") or ""),
        })
    if plan.get("truncated") is True:
        base.update(state="TRUNCATED_REPLAN_REQUIRED",operations=normalized);return base
    raw_release=(
        plan.get("netPusdOut") if plan.get("netPusdOut") is not None
        else plan.get("released_pusd") if plan.get("released_pusd") is not None
        else plan.get("collateral_returned") if plan.get("collateral_returned") is not None
        else "0")
    try:released=Decimal(str(raw_release))
    except (InvalidOperation,ValueError):released=Decimal("-1")
    if released<0:base["state"]="INVALID_PLAN";return base
    try:declared_count=int(plan.get("operationCount",len(normalized)))
    except (TypeError,ValueError):declared_count=-1
    if declared_count!=len(normalized):
        base["state"]="OPERATION_COUNT_MISMATCH";return base
    base.update(
        state="VERIFIED_PLAN_ONLY",
        plan_hash=str(plan.get("planHash") or plan.get("plan_hash") or ""),
        chain_id=plan.get("chainId",plan.get("chain_id")),
        released_pusd_decimal=format(released,"f"),
        released_pusd=float(released),
        operations=normalized,operation_count=len(normalized),
        estimated_cost=plan.get("estimatedCost",plan.get("estimated_cost")),
        required_pusd_input=str(plan.get("requiredPusdInput") or plan.get("required_pusd_input") or ""),
        execution_policy="NEVER_EXECUTE_FROM_SHADOW")
    return base

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-sha",required=True);ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True);ap.add_argument("--interval-seconds",type=float,default=5.0)
    args=ap.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    while True:
        value=summarize(load(args.plan),args.model_sha)
        tmp=args.output.with_suffix(args.output.suffix+".tmp")
        tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8");tmp.replace(args.output)
        time.sleep(args.interval_seconds)

if __name__=="__main__":raise SystemExit(main())
