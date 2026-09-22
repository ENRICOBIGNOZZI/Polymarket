#!/usr/bin/env python3
"""Zero-authority EV gate for the complete-set maker PAPER strategy.

Consumes:
- empirical maker shadow status,
- capital-time allocator recommendations,
- venue mode policy.

It never submits orders. It only emits per-market PAPER recommendations when:
1) the empirical state bucket is mature and conservative PnL is positive;
2) a mature TTL/queue/cancel-relief arm has positive conservative PnL;
3) capital-time allocator assigns positive market budget;
4) venue mode permits maker activity.

Observed venue mode and PAPER counterfactual mode are reported separately.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time
from typing import Any

MAKER_SCHEMA="polymarket_v7_two_sided_complete_set_shadow_status_v2"
CAPITAL_SCHEMA="polymarket_v7_pure_arb_capital_allocator_v1"
VENUE_SCHEMA="polymarket_v7_pure_arb_venue_mode_v1"
SCHEMA="polymarket_v7_pure_arb_maker_policy_v1"


def load(path:Path)->dict[str,Any]:
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}


def atomic_json(path:Path,value:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    os.replace(tmp,path)


def valid_common(v:dict[str,Any],schema:str,sha:str)->bool:
    return (
        v.get("schema")==schema
        and v.get("model_sha")==sha
        and v.get("paper_only") is True
        and v.get("authenticated_execution") is False
        and v.get("real_order_submission") is False
    )


def fresh(v:dict[str,Any],now_ms:int,maximum_age_ms:int)->bool:
    try:ts=int(v.get("timestamp_ms") or 0)
    except (TypeError,ValueError,OverflowError):return False
    return 0<=now_ms-ts<=maximum_age_ms


def best_arm(maker:dict[str,Any])->dict[str,Any]|None:
    rows=[]
    for key,row in (maker.get("policy_matrix") or {}).items():
        if not isinstance(row,dict) or row.get("deployment_candidate") is not True:
            continue
        try:
            lower=float(row["conservative_mean_total_shadow_pnl_lower_90"])
            ttl=int(row["ttl_ms"])
            q=float(row["queue_ahead_multiplier"])
            relief=float(row.get("cancel_relief_fraction") or 0.0)
        except (KeyError,TypeError,ValueError,OverflowError):
            continue
        if not(math.isfinite(lower) and lower>0 and ttl>0 and math.isfinite(q) and q>=0
               and math.isfinite(relief) and 0<=relief<=1):
            continue
        rows.append({
            "arm_key":str(key),"ttl_ms":ttl,"queue_ahead_multiplier":q,
            "cancel_relief_fraction":relief,
            "conservative_mean_total_shadow_pnl_lower_90":lower,
            "conservative_pnl_rate_per_second":lower/(ttl/1000.0),
        })
    return max(rows,key=lambda x:(x["conservative_pnl_rate_per_second"],
                                  x["conservative_mean_total_shadow_pnl_lower_90"])) if rows else None


def market_budget(capital:dict[str,Any],market_id:str)->float:
    total=0.0
    for key,value in (capital.get("recommended_market_budget_pusd") or {}).items():
        if not str(key).startswith("MAKER_COMPLETE_SET|"):
            continue
        if str(key).split("|",1)[1]!=market_id:
            continue
        try:x=float(value)
        except (TypeError,ValueError):continue
        if math.isfinite(x) and x>0:total+=x
    return total


def build(maker:dict[str,Any],capital:dict[str,Any],venue:dict[str,Any], *,
          model_sha:str,now_ms:int,maximum_age_ms:int=5000)->dict[str,Any]:
    maker_ok=(valid_common(maker,MAKER_SCHEMA,model_sha)
              and maker.get("research_mature") is True
              and fresh(maker,now_ms,maximum_age_ms))
    capital_ok=(valid_common(capital,CAPITAL_SCHEMA,model_sha)
                and fresh(capital,now_ms,maximum_age_ms))
    venue_ok=(valid_common(venue,VENUE_SCHEMA,model_sha)
              and fresh(venue,now_ms,maximum_age_ms))
    arm=best_arm(maker) if maker_ok else None

    observed_policy=(venue.get("observed_policy") or {}) if venue_ok else {}
    simulation_policy=(venue.get("simulation_policy") or {}) if venue_ok else {}
    observed_maker_allowed=observed_policy.get("new_maker") is True
    paper_maker_allowed=simulation_policy.get("new_maker") is True

    by_state=(maker.get("by_state") or {}) if maker_ok else {}
    recommendations=[]
    blockers=[]
    if not maker_ok:blockers.append("MAKER_STATUS_INVALID")
    if not capital_ok:blockers.append("CAPITAL_STATUS_INVALID")
    if not venue_ok:blockers.append("VENUE_STATUS_INVALID")
    if arm is None:blockers.append("NO_MATURE_POSITIVE_ARM")

    if maker_ok and capital_ok and venue_ok and arm is not None:
        for row in maker.get("active_states") or []:
            if not isinstance(row,dict):continue
            market_id=str(row.get("market_id") or "")
            state_bucket=str(row.get("state_bucket") or "UNKNOWN")
            state=by_state.get(state_bucket) if isinstance(by_state,dict) else None
            budget=market_budget(capital,market_id)
            try:
                yes=float(row.get("yes_price") or 0)
                no=float(row.get("no_price") or 0)
                target=float(row.get("target_shares") or 0)
                edge=float(row.get("locked_edge_per_share") or 0)
                expires=int(row.get("expires_ms") or 0)
            except (TypeError,ValueError,OverflowError):
                continue
            state_admitted=bool(
                isinstance(state,dict)
                and state.get("deployment_candidate") is True
                and state.get("mature") is True
            )
            price_sum=yes+no
            shares_from_budget=budget/price_sum if budget>0 and price_sum>0 else 0.0
            recommended=max(0.0,min(target,shares_from_budget))
            economic_admitted=bool(
                state_admitted and budget>0 and recommended>0 and edge>0 and expires>now_ms
            )
            recommendations.append({
                "market_id":market_id,
                "cycle_id":str(row.get("cycle_id") or ""),
                "state_bucket":state_bucket,
                "state_mature_positive":state_admitted,
                "budget_pusd":budget,
                "target_shares":target,
                "recommended_shares":recommended,
                "locked_edge_per_share":edge,
                "ttl_ms":arm["ttl_ms"],
                "queue_ahead_multiplier":arm["queue_ahead_multiplier"],
                "cancel_relief_fraction":arm["cancel_relief_fraction"],
                "arm_key":arm["arm_key"],
                "arm_conservative_pnl_rate_per_second":
                    arm["conservative_pnl_rate_per_second"],
                "economic_admitted":economic_admitted,
                "observed_venue_admitted":bool(economic_admitted and observed_maker_allowed),
                "paper_simulation_admitted":bool(economic_admitted and paper_maker_allowed),
                "execution_authority":False,
            })

    return {
        "schema":SCHEMA,"version":1,"model_sha":model_sha,"timestamp_ms":now_ms,
        "paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"real_capital_at_risk":False,
        "execution_authority":"ZERO_AUTHORITY_RECOMMENDATION_ONLY",
        "automatic_promotion":False,
        "maker_status_valid":maker_ok,"capital_status_valid":capital_ok,
        "venue_status_valid":venue_ok,
        "observed_maker_allowed":observed_maker_allowed,
        "paper_simulation_maker_allowed":paper_maker_allowed,
        "selected_global_arm":arm,
        "recommendations":recommendations,
        "observed_admitted_count":sum(r["observed_venue_admitted"] for r in recommendations),
        "paper_admitted_count":sum(r["paper_simulation_admitted"] for r in recommendations),
        "blockers":blockers,
    }


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--maker-status",type=Path,required=True)
    ap.add_argument("--capital-status",type=Path,required=True)
    ap.add_argument("--venue-mode",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--maximum-age-ms",type=int,default=5000)
    ap.add_argument("--interval-ms",type=int,default=1000)
    args=ap.parse_args()
    if len(args.model_sha)!=40 or any(c not in "0123456789abcdef" for c in args.model_sha):
        raise SystemExit("invalid sha")
    if not(100<=args.interval_ms<=60_000 and 100<=args.maximum_age_ms<=600_000):
        raise SystemExit("invalid timing")
    while True:
        atomic_json(args.output,build(
            load(args.maker_status),load(args.capital_status),load(args.venue_mode),
            model_sha=args.model_sha,now_ms=time.time_ns()//1_000_000,
            maximum_age_ms=args.maximum_age_ms))
        time.sleep(args.interval_ms/1000.0)


if __name__=="__main__":
    raise SystemExit(main())
