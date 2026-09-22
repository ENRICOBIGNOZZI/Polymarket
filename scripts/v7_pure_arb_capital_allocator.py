#!/usr/bin/env python3
"""Zero-authority capital-time allocator for deterministic PAPER arbitrage.

Ranks strategy sleeves by a conservative estimate of:
    expected PnL / (capital * seconds locked)

It never submits orders, mutates the canonical ledger, or promotes a strategy.
Unverified economics are excluded, not guessed.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any

SCHEMA="polymarket_v7_pure_arb_capital_allocator_v1"


def load(path:Path|None)->dict[str,Any]:
    if path is None:return {}
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}


def rows(path:Path|None)->list[dict[str,Any]]:
    if path is None:return []
    out=[]
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:v=json.loads(raw)
            except json.JSONDecodeError:continue
            if isinstance(v,dict):out.append(v)
    except OSError:pass
    return out


def conservative_mean(values:list[float],z:float)->float|None:
    xs=[x for x in values if math.isfinite(x)]
    if not xs:return None
    mean=sum(xs)/len(xs)
    if len(xs)<2:return mean
    sd=statistics.stdev(xs)
    return mean-z*sd/math.sqrt(len(xs))


def capital_lock_seconds(*,end_ms:int,event_ms:int,minimum:float)->float:
    if end_ms>event_ms>0:return max(minimum,(end_ms-event_ms)/1000.0)
    return minimum


def complete_set_lock_seconds(policy:dict[str,Any],*,end_ms:int,event_ms:int,minimum:float)->float:
    if policy.get("complete_set_merge_verified") is True:
        raw=policy.get("complete_set_merge_latency_ms")
        if isinstance(raw,(int,float)) and math.isfinite(float(raw)) and float(raw)>0:
            return max(minimum,float(raw)/1000.0)
    return capital_lock_seconds(end_ms=end_ms,event_ms=event_ms,minimum=minimum)


def complete_set_merge_cost(policy:dict[str,Any],capital:float)->float:
    if policy.get("complete_set_merge_verified") is not True:
        return 0.0
    fixed=max(0.0,float(policy.get("complete_set_merge_fixed_cost_pusd") or 0.0))
    bps=max(0.0,float(policy.get("complete_set_merge_variable_cost_bps") or 0.0))
    return fixed+capital*bps/10_000.0


def taker_observations(path:Path|None,policy:dict[str,Any])->list[dict[str,Any]]:
    transport=int(policy["taker_transport_delay_ms_for_allocation"])
    skew=int(policy["taker_inter_leg_skew_ms_for_allocation"])
    minimum=float(policy["minimum_lock_seconds"])
    groups=defaultdict(list)
    for r in rows(path):
        if r.get("schema")!="polymarket_v7_pure_arb_exchange_execution_cycle_v1":continue
        if int(r.get("transport_delay_ms") or -1)!=transport or int(r.get("inter_leg_skew_ms") or -1)!=skew:continue
        key=(str(r.get("market_id") or ""),str(r.get("kind") or ""),
             int(r.get("revalidation_wall_ms") or r.get("detected_wall_ms") or 0))
        groups[key].append(r)
    out=[]
    for key,rr in groups.items():
        # Both orderings must be survivable; use worst outcome/PnL.
        if len({str(x.get("leg_order")) for x in rr})<2:continue
        usable=[x for x in rr if isinstance(x.get("execution_pnl_after_reserve"),(int,float))]
        if len(usable)<2:continue
        worst=min(usable,key=lambda x:float(x["execution_pnl_after_reserve"]))
        q=float(worst.get("target_shares") or 0)
        yp=float(worst.get("revalidation_yes_price") or 0)
        np=float(worst.get("revalidation_no_price") or 0)
        kind=str(worst.get("kind") or "")
        capital=q*(yp+np) if kind=="BUY_COMPLETE_SET" else q
        event_ms=int(worst.get("revalidation_wall_ms") or 0)
        end_ms=int(worst.get("market_end_ms") or 0)
        lock=(complete_set_lock_seconds(policy,end_ms=end_ms,event_ms=event_ms,minimum=minimum)
              if kind=="BUY_COMPLETE_SET"
              else capital_lock_seconds(end_ms=end_ms,event_ms=event_ms,minimum=minimum))
        pnl=float(worst["execution_pnl_after_reserve"])-(
            complete_set_merge_cost(policy,capital) if kind=="BUY_COMPLETE_SET" else 0.0)
        if capital>0 and lock>0:
            out.append({"strategy":"TAKER_COMPLETE_SET","market_id":key[0],
                        "pnl":pnl,
                        "capital":capital,"lock_seconds":lock})
    return out


def maker_observations(path:Path|None,policy:dict[str,Any])->list[dict[str,Any]]:
    arm=f'{float(policy["maker_queue_multiplier_for_allocation"]):.3f}'
    minimum=float(policy["minimum_lock_seconds"])
    out=[]
    for r in rows(path):
        if r.get("schema")!="polymarket_v7_two_sided_complete_set_cycle_v2":continue
        scenarios={f'{float(x.get("multiplier") or 0):.3f}':x
                   for x in r.get("queue_scenarios") or [] if isinstance(x,dict)}
        x=scenarios.get(arm)
        if not isinstance(x,dict) or not isinstance(x.get("total_shadow_pnl"),(int,float)):continue
        target=float(r.get("target_shares") or 0)
        capital=target*(float(r.get("yes_price") or 0)+float(r.get("no_price") or 0))
        event_ms=int(r.get("origin_ms") or 0)
        paired=str(x.get("state")) in {"BOTH_FULL","BOTH_PARTIAL"}
        end_ms=int(r.get("market_end_ms") or 0) if paired else event_ms+int(r.get("ttl_ms") or 0)
        lock=(complete_set_lock_seconds(policy,end_ms=end_ms,event_ms=event_ms,minimum=minimum)
              if paired else capital_lock_seconds(end_ms=end_ms,event_ms=event_ms,minimum=minimum))
        pnl=float(x["total_shadow_pnl"])-(complete_set_merge_cost(policy,capital) if paired else 0.0)
        if capital>0 and lock>0:
            out.append({"strategy":"MAKER_COMPLETE_SET","market_id":str(r.get("market_id") or ""),
                        "pnl":pnl,"capital":capital,
                        "lock_seconds":lock})
    return out


def postfix_observations(path:Path|None,policy:dict[str,Any])->list[dict[str,Any]]:
    lag=max(float(policy["minimum_lock_seconds"]),float(policy["post_fix_redemption_lag_seconds"]))
    out=[]
    for r in rows(path):
        if r.get("schema")!="polymarket_v7_settlement_source_arb_cycle_v2":continue
        if r.get("state")!="PAPER_LOCKED_ARBITRAGE" or not isinstance(r.get("locked_pnl"),(int,float)):continue
        q=float(r.get("filled_shares") or 0)
        kind=str(r.get("kind") or "")
        if kind=="BUY_WINNER":
            capital=q*float(r.get("entry_ask") or 0)
        elif kind=="SELL_LOSER":
            capital=q
        else:continue
        if capital>0:
            out.append({"strategy":"POST_FIX","market_id":str(r.get("market_id") or ""),
                        "pnl":float(r["locked_pnl"]),"capital":capital,"lock_seconds":lag})
    return out


def cross_observations(status_path:Path|None,policy:dict[str,Any],now_ms:int)->list[dict[str,Any]]:
    v=load(status_path)
    minimum=float(policy["minimum_lock_seconds"])
    out=[]
    if v.get("schema")!="polymarket_v7_cross_market_exact_arb_status_v1":return out
    for r in v.get("opportunities") or []:
        if not isinstance(r,dict):continue
        pnl=float(r.get("locked_pnl_capacity") or 0)
        if r.get("kind")=="EXPLICIT_EXACT_PAYOUT_BASKET":
            units=float(r.get("executable_basket_units") or 0)
            capital=units*float(r.get("basket_cost_after_fees") or 0)
            end_ms=int(r.get("market_end_ms") or 0)
        else:
            q=float(r.get("executable_shares") or 0)
            capital=q*(float(r.get("ask_1") or 0)+float(r.get("ask_2") or 0))
            end_ms=1000*int((r.get("identity") or {}).get("close_timestamp_unix") or 0)
        lock=capital_lock_seconds(end_ms=end_ms,event_ms=now_ms,minimum=minimum)
        if pnl>0 and capital>0:
            out.append({"strategy":"CROSS_MARKET_EXACT",
                        "market_id":str(r.get("market_a") or r.get("relation_id") or ""),
                        "pnl":pnl,"capital":capital,"lock_seconds":lock})
    return out


def summarize(obs:list[dict[str,Any]],policy:dict[str,Any])->dict[str,Any]:
    z=float(policy["confidence_z"]);minimum=int(policy["minimum_samples"])
    grouped=defaultdict(list)
    for x in obs:grouped[str(x["strategy"])].append(x)
    result={}
    for strategy,rr in sorted(grouped.items()):
        returns=[float(x["pnl"])/(float(x["capital"])*float(x["lock_seconds"])) for x in rr
                 if x["capital"]>0 and x["lock_seconds"]>0]
        lower=conservative_mean(returns,z)
        result[strategy]={
            "samples":len(returns),
            "mean_pnl_per_capital_second":sum(returns)/len(returns) if returns else None,
            "conservative_pnl_per_capital_second":lower,
            "eligible_for_allocation":len(returns)>=minimum and lower is not None and lower>0,
            "total_observed_pnl":sum(float(x["pnl"]) for x in rr),
            "total_capital_seconds":sum(float(x["capital"])*float(x["lock_seconds"]) for x in rr),
        }
    return result


def allocate(stats:dict[str,Any],policy:dict[str,Any])->dict[str,float]:
    budget=float(policy["paper_budget_pusd"])
    cap=budget*float(policy["maximum_strategy_fraction"])
    weights={k:max(0.0,float(v["conservative_pnl_per_capital_second"]))
             for k,v in stats.items() if v.get("eligible_for_allocation")}
    total=sum(weights.values())
    if total<=0:return {}
    raw={k:budget*w/total for k,w in weights.items()}
    clipped={k:min(cap,v) for k,v in raw.items()}
    remaining=budget-sum(clipped.values())
    # Redistribute only to sleeves still below their cap.
    for _ in range(8):
        if remaining<=1e-9:break
        room={k:cap-clipped[k] for k in clipped if cap-clipped[k]>1e-9}
        if not room:break
        denom=sum(weights[k] for k in room)
        if denom<=0:break
        used=0.0
        for k in room:
            add=min(room[k],remaining*weights[k]/denom)
            clipped[k]+=add;used+=add
        remaining-=used
    return clipped


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--policy",type=Path,required=True)
    ap.add_argument("--taker-cycles",type=Path)
    ap.add_argument("--maker-cycles",type=Path)
    ap.add_argument("--postfix-cycles",type=Path)
    ap.add_argument("--cross-status",type=Path)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--interval-seconds",type=float,default=5.0)
    args=ap.parse_args()
    policy=load(args.policy)
    if policy.get("schema")!="polymarket_v7_pure_arb_capital_policy_v1":
        raise SystemExit("invalid policy")
    if len(args.model_sha)!=40:raise SystemExit("invalid sha")
    while True:
        now_ms=time.time_ns()//1_000_000
        obs=(taker_observations(args.taker_cycles,policy)
             +maker_observations(args.maker_cycles,policy)
             +postfix_observations(args.postfix_cycles,policy)
             +cross_observations(args.cross_status,policy,now_ms))
        stats=summarize(obs,policy)
        result={
            "schema":SCHEMA,"version":1,"model_sha":args.model_sha,
            "timestamp_ms":now_ms,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,
            "real_capital_at_risk":False,
            "execution_authority":"ZERO_AUTHORITY_CAPITAL_RECOMMENDATION_ONLY",
            "automatic_promotion":False,
            "score_semantics":policy["score"],
            "maker_rebate_policy":policy["maker_rebate_policy"],
            "strategy_statistics":stats,
            "recommended_paper_budget_pusd":allocate(stats,policy),
            "unallocated_is_cash":True,
            "observations":len(obs),
        }
        args.output.parent.mkdir(parents=True,exist_ok=True)
        tmp=args.output.with_name(args.output.name+".tmp")
        tmp.write_text(json.dumps(result,sort_keys=True,indent=2)+"\n",encoding="utf-8")
        tmp.replace(args.output)
        time.sleep(max(.5,args.interval_seconds))


if __name__=="__main__":
    raise SystemExit(main())
