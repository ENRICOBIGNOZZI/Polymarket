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

from v7_pure_arb_economics import tail_jsonl, tail_risk_summary

SCHEMA="polymarket_v7_pure_arb_capital_allocator_v1"


def load(path:Path|None)->dict[str,Any]:
    if path is None:return {}
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}


def rows(
    path:Path|None,policy:dict[str,Any]|None=None
)->list[dict[str,Any]]:
    policy=policy or {}
    maximum=max(1,int(policy.get("maximum_observations_per_source") or 50_000))
    byte_cap=max(1_048_576,int(policy.get("maximum_jsonl_scan_bytes") or 67_108_864))
    return tail_jsonl(path,max_rows=maximum,max_bytes=byte_cap)


def conservative_mean(values:list[float],z:float)->float|None:
    xs=[x for x in values if math.isfinite(x)]
    if not xs:return None
    mean=sum(xs)/len(xs)
    if len(xs)<2:return mean
    sd=statistics.stdev(xs)
    return mean-z*sd/math.sqrt(len(xs))


def merge_evidence_policy(
    policy:dict[str,Any],path:Path|None,model_sha:str,now_ms:int
)->dict[str,Any]:
    out=dict(policy)
    evidence=load(path)
    if (
        evidence.get("schema")=="polymarket_v7_complete_set_merge_evidence_v1"
        and evidence.get("model_sha")==model_sha
        and evidence.get("paper_only") is True
        and evidence.get("authenticated_execution") is False
        and evidence.get("real_order_submission") is False
        and evidence.get("verified") is True
    ):
        try:
            latency=float(evidence.get("confirmed_latency_ms"))
            fixed=float(evidence.get("fixed_cost_pusd") or 0.0)
            bps=float(evidence.get("variable_cost_bps") or 0.0)
            expires=int(evidence.get("expires_at_ms") or 0)
        except (TypeError,ValueError,OverflowError):
            latency,fixed,bps,expires=math.nan,math.nan,math.nan,0
        if (math.isfinite(latency) and latency>0 and math.isfinite(fixed) and fixed>=0
            and math.isfinite(bps) and bps>=0 and expires>=now_ms):
            out["complete_set_merge_verified"]=True
            out["complete_set_merge_latency_ms"]=latency
            out["complete_set_merge_fixed_cost_pusd"]=fixed
            out["complete_set_merge_variable_cost_bps"]=bps
            out["complete_set_merge_evidence_source"]=str(evidence.get("source") or "")
    return out


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


def verified_maker_reward_rates(path:Path|None,model_sha:str,now_ms:int)->dict[str,float]:
    v=load(path)
    if (
        v.get("schema")!="polymarket_v7_fee_reward_registry_v1"
        or v.get("model_sha")!=model_sha
        or v.get("paper_only") is not True
        or v.get("authenticated_execution") is not False
        or v.get("real_order_submission") is not False
        or v.get("unknown_reward_policy")!="ZERO_EXPECTED_VALUE"
    ):
        return {}
    out={}
    for row in v.get("markets") or []:
        if not isinstance(row,dict):
            continue
        reward=row.get("reward") if isinstance(row.get("reward"),dict) else {}
        if reward.get("verified") is not True:
            continue
        if reward.get("source")!="verified_realized_maker_reward_rate":
            continue
        try:
            rate=float(reward.get("realized_pnl_pusd_per_capital_second"))
            expires=int(reward.get("expires_at_ms") or 0)
        except (TypeError,ValueError,OverflowError):
            continue
        mid=str(row.get("market_id") or "")
        if mid and math.isfinite(rate) and rate>=0.0 and expires>=now_ms:
            out[mid]=rate
    return out


def taker_observations(path:Path|None,policy:dict[str,Any])->list[dict[str,Any]]:
    transport=int(policy["taker_transport_delay_ms_for_allocation"])
    skew=int(policy["taker_inter_leg_skew_ms_for_allocation"])
    minimum=float(policy["minimum_lock_seconds"])
    allocation_mode=str(policy.get("taker_execution_mode_for_allocation") or "WORST_ACROSS_MODES").upper()
    groups=defaultdict(list)
    for r in rows(path,policy):
        if r.get("schema")!="polymarket_v7_pure_arb_exchange_execution_cycle_v1":continue
        if int(r.get("transport_delay_ms") or -1)!=transport:continue
        mode=str(r.get("execution_mode") or "SEQUENTIAL").upper()
        if mode!="BATCH" and int(r.get("inter_leg_skew_ms") or -1)!=skew:continue
        if allocation_mode not in {"WORST_ACROSS_MODES","BEST_VERIFIED_MODE"} and mode!=allocation_mode:continue
        key=(str(r.get("market_id") or ""),str(r.get("kind") or ""),
             int(r.get("revalidation_wall_ms") or r.get("detected_wall_ms") or 0))
        groups[key].append(r)
    out=[]
    for key,rr in groups.items():
        if allocation_mode=="WORST_ACROSS_MODES":
            modes={str(x.get("execution_mode") or "SEQUENTIAL").upper() for x in rr}
            required={str(x).upper() for x in policy.get(
                "taker_required_execution_modes",["SEQUENTIAL","PARALLEL","BATCH"])}
            if not required.issubset(modes):continue
        usable=[x for x in rr if isinstance(x.get("execution_pnl_after_reserve"),(int,float))]
        if not usable:continue
        valid=True
        for mode in {str(x.get("execution_mode") or "SEQUENTIAL").upper() for x in rr}:
            if mode=="BATCH":continue
            orderings={str(x.get("leg_order")) for x in rr
                       if str(x.get("execution_mode") or "SEQUENTIAL").upper()==mode}
            if len(orderings)<2:valid=False
        if not valid:continue
        worst=min(usable,key=lambda x:float(x["execution_pnl_after_reserve"]))
        if allocation_mode=="BEST_VERIFIED_MODE":
            by_mode={}
            for row in usable:
                mode=str(row.get("execution_mode") or "SEQUENTIAL").upper()
                by_mode.setdefault(mode,[]).append(row)
            mode_worst=[min(group,key=lambda x:float(x["execution_pnl_after_reserve"]))
                        for group in by_mode.values()]
            worst=max(mode_worst,key=lambda x:float(x["execution_pnl_after_reserve"]))
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
        base_pnl=float(worst["execution_pnl_after_reserve"])-(
            complete_set_merge_cost(policy,capital) if kind=="BUY_COMPLETE_SET" else 0.0)
        ancillary=max(0.0,float(worst.get("verified_ancillary_taker_rebate_pusd") or 0.0))
        pnl=base_pnl+ancillary
        if capital>0 and lock>0:
            out.append({"strategy":"TAKER_COMPLETE_SET","market_id":key[0],
                        "episode_id":"|".join(map(str,key)),
                        "pnl":pnl,"base_pnl":base_pnl,
                        "verified_ancillary_taker_rebate_pnl":ancillary,
                        "execution_mode":str(worst.get("execution_mode") or ""),
                        "capital":capital,"lock_seconds":lock,
                        "merge_credit_applied":(
                            kind=="BUY_COMPLETE_SET"
                            and policy.get("complete_set_merge_verified") is True)})
    return out

def maker_observations(path:Path|None,policy:dict[str,Any],
                       reward_rates:dict[str,float]|None=None)->list[dict[str,Any]]:
    multiplier=float(policy["maker_queue_multiplier_for_allocation"])
    cancel_relief=float(policy.get("maker_cancel_relief_fraction_for_allocation",0.0))
    minimum=float(policy["minimum_lock_seconds"])
    reward_rates=reward_rates or {}
    out=[]
    for r in rows(path,policy):
        if r.get("schema")!="polymarket_v7_two_sided_complete_set_cycle_v2":continue
        scenarios={
            (round(float(x.get("multiplier") or 0.0),6),
             round(float(x.get("cancel_relief_fraction") or 0.0),6)):x
            for x in r.get("queue_scenarios") or [] if isinstance(x,dict)
        }
        x=scenarios.get((round(multiplier,6),round(cancel_relief,6)))
        if not isinstance(x,dict) or not isinstance(x.get("total_shadow_pnl"),(int,float)):continue
        target=float(r.get("target_shares") or 0)
        capital=target*(float(r.get("yes_price") or 0)+float(r.get("no_price") or 0))
        event_ms=int(r.get("origin_ms") or 0)
        paired=str(x.get("state")) in {"BOTH_FULL","BOTH_PARTIAL"}
        # Current maker shadow deliberately holds a filled leg through TTL to
        # measure legging. It does not execute an immediate on-chain merge.
        # Therefore verified taker merge latency cannot be credited here.
        end_ms=event_ms+int(r.get("ttl_ms") or 0)
        lock=capital_lock_seconds(end_ms=end_ms,event_ms=event_ms,minimum=minimum)
        base_pnl=float(x["total_shadow_pnl"])
        market_id=str(r.get("market_id") or "")
        reward_rate=max(0.0,float(reward_rates.get(market_id,0.0)))
        ancillary_reward_pnl=reward_rate*capital*lock if capital>0 and lock>0 else 0.0
        pnl=base_pnl+ancillary_reward_pnl
        if capital>0 and lock>0:
            out.append({"strategy":"MAKER_COMPLETE_SET","market_id":market_id,
                        "episode_id":str(r.get("cycle_id") or r.get("origin_ms") or ""),
                        "pnl":pnl,"base_pnl":base_pnl,
                        "verified_ancillary_reward_pnl":ancillary_reward_pnl,
                        "verified_reward_rate_pusd_per_capital_second":reward_rate,
                        "capital":capital,"lock_seconds":lock,
                        "merge_credit_applied":False})
    return out


def postfix_observations(path:Path|None,policy:dict[str,Any])->list[dict[str,Any]]:
    lag=max(float(policy["minimum_lock_seconds"]),float(policy["post_fix_redemption_lag_seconds"]))
    out=[]
    for r in rows(path,policy):
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
                        "episode_id":str(r.get("cycle_key") or r.get("outcome_determined_wall_ms") or ""),
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
                        "episode_id":str(r.get("relation_id") or r.get("market_a") or ""),
                        "pnl":pnl,"capital":capital,"lock_seconds":lock})
    return out


def _return_rate(x:dict[str,Any])->float|None:
    try:
        capital=float(x["capital"]);lock=float(x["lock_seconds"]);pnl=float(x["pnl"])
    except (KeyError,TypeError,ValueError,OverflowError):return None
    if not(capital>0 and lock>0 and math.isfinite(pnl)):return None
    return pnl/(capital*lock)


def summarize(obs:list[dict[str,Any]],policy:dict[str,Any])->dict[str,Any]:
    z=float(policy["confidence_z"]);minimum=int(policy["minimum_samples"])
    min_clusters=int(policy.get("minimum_independent_clusters",5))
    block_size=int(policy.get("bootstrap_block_size",5))
    draws=int(policy.get("bootstrap_draws",1000))
    alpha=float(policy.get("bootstrap_alpha",0.05))
    grouped=defaultdict(list)
    for x in obs:grouped[str(x["strategy"])].append(x)
    result={}
    for strategy,rr in sorted(grouped.items()):
        values=[v for v in (_return_rate(x) for x in rr) if v is not None]
        clusters=defaultdict(list)
        for x in rr:
            value=_return_rate(x)
            if value is not None:clusters[str(x.get("market_id") or "UNKNOWN")].append(value)
        cluster_means=[statistics.fmean(v) for v in clusters.values() if v]
        sem_lower=conservative_mean(cluster_means,z)
        tail=tail_risk_summary(cluster_means,block_size=block_size,draws=draws,alpha=alpha)
        bootstrap=tail.get("block_bootstrap_lower_95")
        candidates=[x for x in (sem_lower,bootstrap) if isinstance(x,(int,float)) and math.isfinite(float(x))]
        lower=min(candidates) if candidates else None
        raw_tail=tail_risk_summary(values,block_size=block_size,draws=draws,alpha=alpha)
        result[strategy]={
            "samples":len(values),"independent_market_clusters":len(cluster_means),
            "mean_pnl_per_capital_second":statistics.fmean(values) if values else None,
            "cluster_mean_pnl_per_capital_second":statistics.fmean(cluster_means) if cluster_means else None,
            "conservative_pnl_per_capital_second":lower,
            "cluster_sem_lower":sem_lower,
            "block_bootstrap_lower_95":bootstrap,
            "p05_pnl_per_capital_second":raw_tail.get("p05"),
            "p01_pnl_per_capital_second":raw_tail.get("p01"),
            "expected_shortfall_05":raw_tail.get("expected_shortfall_05"),
            "worst_pnl_per_capital_second":raw_tail.get("worst"),
            "eligible_for_allocation":(
                len(values)>=minimum and len(cluster_means)>=min_clusters
                and lower is not None and lower>0),
            "total_observed_pnl":sum(float(x["pnl"]) for x in rr),
            "total_capital_seconds":sum(float(x["capital"])*float(x["lock_seconds"]) for x in rr),
            "inference_semantics":"MARKET_CLUSTERED_MOVING_BLOCK_BOOTSTRAP_AND_TAIL_RISK",
        }
    return result


def summarize_by_market(obs:list[dict[str,Any]],policy:dict[str,Any])->dict[str,Any]:
    z=float(policy["confidence_z"]);minimum=int(policy["minimum_samples"])
    block_size=int(policy.get("bootstrap_block_size",5))
    draws=int(policy.get("bootstrap_draws",1000))
    alpha=float(policy.get("bootstrap_alpha",0.05))
    grouped=defaultdict(list)
    for x in obs:
        grouped[(str(x["strategy"]),str(x.get("market_id") or "UNKNOWN"))].append(x)
    out={}
    for (strategy,market),rr in sorted(grouped.items()):
        values=[v for v in (_return_rate(x) for x in rr) if v is not None]
        sem_lower=conservative_mean(values,z)
        tail=tail_risk_summary(values,block_size=block_size,draws=draws,alpha=alpha)
        bootstrap=tail.get("block_bootstrap_lower_95")
        candidates=[x for x in (sem_lower,bootstrap) if isinstance(x,(int,float)) and math.isfinite(float(x))]
        lower=min(candidates) if candidates else None
        key=f"{strategy}|{market}"
        out[key]={
            "strategy":strategy,"market_id":market,"samples":len(values),
            "mean_pnl_per_capital_second":statistics.fmean(values) if values else None,
            "conservative_pnl_per_capital_second":lower,
            "block_bootstrap_lower_95":bootstrap,
            "p05_pnl_per_capital_second":tail.get("p05"),
            "expected_shortfall_05":tail.get("expected_shortfall_05"),
            "worst_pnl_per_capital_second":tail.get("worst"),
            "eligible_for_allocation":len(values)>=minimum and lower is not None and lower>0,
        }
    return out

def allocate_market(stats:dict[str,Any],policy:dict[str,Any])->dict[str,float]:
    budget=float(policy["paper_budget_pusd"])
    strategy_cap=budget*float(policy["maximum_strategy_fraction"])
    market_cap=budget*float(policy["maximum_market_fraction"])
    weights={k:max(0.0,float(v["conservative_pnl_per_capital_second"]))
             for k,v in stats.items() if v.get("eligible_for_allocation")}
    allocation={k:0.0 for k in weights}
    strategy_used=defaultdict(float);market_used=defaultdict(float)
    remaining=budget
    for _ in range(32):
        active=[]
        for key,w in weights.items():
            row=stats[key];strategy=str(row["strategy"]);market=str(row["market_id"])
            room=min(strategy_cap-strategy_used[strategy],market_cap-market_used[market])
            if w>0 and room>1e-9:active.append((key,w,room))
        if not active or remaining<=1e-9:break
        denom=sum(w for _,w,_ in active)
        used=0.0
        for key,w,room in active:
            add=min(room,remaining*w/denom)
            if add<=0:continue
            allocation[key]+=add
            strategy=str(stats[key]["strategy"]);market=str(stats[key]["market_id"])
            strategy_used[strategy]+=add;market_used[market]+=add;used+=add
        if used<=1e-9:break
        remaining-=used
    return {k:v for k,v in allocation.items() if v>1e-9}


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
    ap.add_argument("--fee-reward-registry",type=Path)
    ap.add_argument("--merge-evidence",type=Path)
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
        effective_policy=merge_evidence_policy(policy,args.merge_evidence,args.model_sha,now_ms)
        reward_rates=verified_maker_reward_rates(
            args.fee_reward_registry,args.model_sha,now_ms)
        obs=(taker_observations(args.taker_cycles,effective_policy)
             +maker_observations(args.maker_cycles,effective_policy,reward_rates)
             +postfix_observations(args.postfix_cycles,effective_policy)
             +cross_observations(args.cross_status,effective_policy,now_ms))
        stats=summarize(obs,effective_policy)
        market_stats=summarize_by_market(obs,effective_policy)
        market_alloc=allocate_market(market_stats,effective_policy)
        strategy_alloc=defaultdict(float)
        for key,value in market_alloc.items():
            strategy_alloc[str(market_stats[key]["strategy"])]+=value
        result={
            "schema":SCHEMA,"version":1,"model_sha":args.model_sha,
            "timestamp_ms":now_ms,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,
            "real_capital_at_risk":False,
            "execution_authority":"ZERO_AUTHORITY_CAPITAL_RECOMMENDATION_ONLY",
            "automatic_promotion":False,
            "score_semantics":policy["score"],
            "maker_rebate_policy":policy["maker_rebate_policy"],
            "complete_set_merge_verified":effective_policy.get("complete_set_merge_verified") is True,
            "complete_set_merge_latency_ms":effective_policy.get("complete_set_merge_latency_ms"),
            "inference_semantics":"MARKET_CLUSTERED_BLOCK_BOOTSTRAP_WITH_EXPECTED_SHORTFALL",
            "verified_maker_reward_markets":len(reward_rates),
            "strategy_statistics":stats,
            "market_statistics":market_stats,
            "recommended_market_budget_pusd":market_alloc,
            "recommended_paper_budget_pusd":dict(strategy_alloc),
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
