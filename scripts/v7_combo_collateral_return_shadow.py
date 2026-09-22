#!/usr/bin/env python3
"""Zero-authority verifier for captured Combo collateral-return plans.

The script never requests or executes a plan.  It validates an externally
captured plan receipt and reports the pUSD capital-release amount/operations for
research attribution.  Unknown/truncated/unconfirmed plans remain fail-closed.
"""
from __future__ import annotations
import argparse,json,time
from pathlib import Path
from typing import Any

SCHEMA="polymarket_v7_combo_collateral_return_shadow_v1"
ALLOWED={"split","merge","redeem","split_on_condition","merge_on_condition"}

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
        normalized.append({"kind":kind,"condition_id":str(op.get("condition_id") or ""),
                           "amount":str(op.get("amount") or "")})
    if plan.get("truncated") is True:
        base.update(state="TRUNCATED_REPLAN_REQUIRED",operations=normalized);return base
    try:released=float(plan.get("released_pusd") or plan.get("collateral_returned") or 0.0)
    except (TypeError,ValueError):released=0.0
    if released<0:base["state"]="INVALID_PLAN";return base
    base.update(state="VERIFIED_PLAN_ONLY",released_pusd=released,operations=normalized,
                operation_count=len(normalized),
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
