#!/usr/bin/env python3
"""Causal N-leg execution counterfactuals. No order or authentication interface."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from fractions import Fraction
import json
from pathlib import Path
import time
from v7_exact_arb_causal import CausalBooks, ReconstructedDepth, JsonlCursor, decode_snapshot
from v7_unified_exact_arb_graph import SAFETY, GraphError, frac, fstr, levels, fee_per_share, round_fee, safe, sha
from v7_unified_exact_arb_graph_shadow import atomic

SCHEMA = "polymarket_v7_unified_exact_arb_graph_execution_status_v1"


def fill(leg, book, requested, direction, arrival, maximum_age_ms, depletion):
    if book is None or not book.get("lineage_continuous"): raise GraphError("arrival_lineage")
    if book.get("depth_truncated") is not False: raise GraphError("arrival_truncated")
    if not 0 <= arrival-int(book["timestamp_ms"]) <= maximum_age_ms: raise GraphError("arrival_stale")
    rate, exponent = frac(leg.get("fee_rate")), frac(leg.get("fee_exponent"))
    if not 0 <= rate <= 1 or exponent.denominator != 1 or not 0 <= exponent <= 16: raise GraphError("arrival_fee")
    if book.get("fee_rate") is not None and frac(book["fee_rate"])!=rate:raise GraphError("arrival_fee_changed")
    if leg.get("tick_size") and book.get("tick_size") and frac(leg["tick_size"])!=frac(book["tick_size"]):
        raise GraphError("arrival_tick_changed")
    side = "asks" if direction == "BUY" else "bids"
    token = leg["token_id"]
    filled = cash = fees = Fraction(0)
    prices = []
    depth_levels=levels(book.get(side), descending=direction=="SELL")
    total_available=sum((max(Fraction(0),depth-depletion[(token,side,book.get("observation_ms"),str(price))])
                         for price,depth in depth_levels),Fraction(0))
    for price, depth in depth_levels:
        key = (token, side, book.get("observation_ms"), str(price))
        available = max(Fraction(0), depth-depletion[key])
        take = min(requested-filled, available)
        if take <= 0: continue
        increment = frac(leg["fee_rounding_increment"]) if leg.get("fee_rounding_increment") else None
        fee = round_fee(take*fee_per_share(price, rate, int(exponent)), increment, leg.get("fee_rounding_mode", "EXACT"))
        depletion[key] += take
        filled += take; cash += take*price; fees += fee
        prices.append([str(price),str(take)])
        if filled == requested: break
    return {"token_id":token,"arrival_timestamp_ms":arrival,"book_timestamp_ms":book["timestamp_ms"],
            "book_observation_ms":book.get("observation_ms"),"lineage_id":book.get("lineage_id"),
            "requested_size":fstr(requested),"filled_size":fstr(filled),"remaining_quantity":fstr(requested-filled),
            "available_size":fstr(total_available),
            "fill_price":fstr(cash/filled) if filled else None,"fee":fstr(fees),
            "notional":fstr(cash),"fills":prices}


def simulate(opportunity, history, mode, delay_ms, skew_ms, unwind_delay_ms=2, maximum_age_ms=100, maximum_skew_ms=100):
    relation, result = opportunity["relation"], opportunity["result"]
    detected = int(opportunity["timestamp_ms"])
    quantity = frac(result["quantity"])
    direction = result["direction"]
    if mode not in {"SEQUENTIAL","PARALLEL","BATCH"} or direction not in {"BUY","SELL"}: raise GraphError("execution_mode")
    if min(delay_ms,skew_ms,unwind_delay_ms) < 0 or quantity <= 0: raise GraphError("execution_terms")
    if not relation.get("enabled") or relation.get("relation_type","CONSTANT_PAYOUT_EQUALITY") != "CONSTANT_PAYOUT_EQUALITY":
        raise GraphError("execution_relation")
    legs = relation["legs"]
    if not 1 <= len(legs) <= 16: raise GraphError("execution_cardinality")
    if direction == "SELL" and opportunity.get("inventory_reserved") is not True: raise GraphError("execution_inventory")
    arrivals = [detected+delay_ms+(i*(delay_ms+skew_ms) if mode=="SEQUENTIAL" else i*skew_ms if mode=="PARALLEL" else 0)
                for i in range(len(legs))]
    finish = max(arrivals)+unwind_delay_ms
    out = {"schema":"polymarket_v7_unified_exact_arb_graph_execution_cycle_v1", **SAFETY,
           "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY", "model_sha":opportunity["model_sha"],
           "graph_generation":opportunity["graph_generation"], "relation_id":relation["relation_id"],
           "relation_family":relation.get("relation_family"),"opportunity_id":opportunity["opportunity_id"],
           "mode":mode,"transport_delay_ms":delay_ms,"inter_leg_skew_ms":skew_ms,"atomic_batch":False,
           "evidence_scope":"TRANSPORT_SCENARIO_NOT_VERIFIED_VENUE_EXECUTION",
           "timestamp_ms":finish, "state":"CENSORED", "legs":[],
           "realized_counterfactual_pnl":None, "unwind_cost":None}
    out["cycle_id"] = sha([out["opportunity_id"],mode,delay_ms,skew_ms,unwind_delay_ms])
    if history.watermark < finish:
        out["reason"]="observation_watermark"; return out
    depletion = defaultdict(Fraction)
    try:
        if relation.get("settlement_close_ms",0) and max(arrivals)>=relation["settlement_close_ms"]:
            raise GraphError("arrival_market_closed")
        available_books = [history.at(leg["token_id"], t) for leg,t in zip(legs,arrivals)]
        stamps = [b["timestamp_ms"] for b in available_books if b is not None]
        if len(stamps) != len(legs) or max(stamps)-min(stamps)>maximum_skew_ms: raise GraphError("arrival_skew")
        # The candidate anchors lineage; a reset between detection and arrival
        # censors the counterfactual even when the new snapshot looks healthy.
        for leg,book,t in zip(legs,available_books,arrivals):
            anchor = history.at(leg["token_id"], detected)
            if anchor is None or book is None or anchor.get("lineage_id") != book.get("lineage_id"):
                raise GraphError("arrival_lineage_reset")
            requested = quantity*frac(leg["coefficient"])
            if requested < frac(leg.get("minimum_order",0)): raise GraphError("execution_minimum")
            executed = fill(leg,book,requested,direction,t,maximum_age_ms,depletion)
            executed["submission_timestamp_ms"] = t-delay_ms
            out["legs"].append(executed)
        complete = all(frac(row["remaining_quantity"])==0 for row in out["legs"])
        out.update(all_legs_filled=complete, partial_fill=any(frac(r["filled_size"])>0 for r in out["legs"]) and not complete,
                   number_of_filled_legs=sum(frac(r["filled_size"])>0 for r in out["legs"]),
                   unwind_required=not complete)
        notional=sum((frac(r["notional"]) for r in out["legs"]),Fraction(0))
        fees=sum((frac(r["fee"]) for r in out["legs"]),Fraction(0))
        reserve=quantity*frac(relation.get("reserve_per_unit",0))
        payout=quantity*frac(relation["guaranteed_payout"])
        if complete:
            raw=payout-notional if direction=="BUY" else notional-payout
            locked=raw-fees-reserve
            out.update(state="ALL_LEGS_FILLED",unhedged_exposure={},gross_pnl=fstr(raw),
                       fee_drag=fstr(fees),reserve_drag=fstr(reserve),net_locked_pnl=fstr(locked),
                       execution_drag=fstr(frac(result["net_locked_pnl"])-locked),unwind_cost="0",
                       capital_lock_time_ms=result.get("capital_lock_time_ms"))
            transform=relation.get("transformation")
            if transform is not None:
                required=("capacity","latency_ms","capital_lock_time_ms","fixed_cost","variable_cost_per_unit","expires_at_ms")
                if any(transform.get(k) is None for k in required): raise GraphError("transformation_terms_missing")
                if (transform.get("verification") not in {"AUTO_VERIFIED","EXPLICIT_VERIFIED"}
                    or frac(transform["capacity"])<quantity or int(transform["expires_at_ms"])<finish
                    or int(transform["latency_ms"])<=0 or int(transform["capital_lock_time_ms"])<=0):
                    raise GraphError("transformation_ineligible")
                cost=frac(transform["fixed_cost"])+quantity*frac(transform["variable_cost_per_unit"])
                if cost < 0: raise GraphError("transformation_cost")
                out["transformation"]={"kind":transform["kind"],"state":"COUNTERFACTUAL_ELIGIBLE",
                    "completion_timestamp_ms":max(arrivals)+int(transform["latency_ms"]),
                    "latency_ms":transform["latency_ms"],"capital_lock_time_ms":transform["capital_lock_time_ms"],
                    "cost":fstr(cost),"failure_state":"UNOBSERVED"}
                out["transformation_drag"]=fstr(cost)
                out["net_locked_pnl"]=fstr(locked-cost)
                # Eligibility is not evidence of conversion success.
                out["state"]="TRANSFORMATION_SUCCESS_UNOBSERVED"
            return out
        unwound_cash=unwind_fees=Fraction(0)
        exposures={}
        unwind_rows=[]
        for leg,row in zip(legs,out["legs"]):
            size=frac(row["filled_size"])
            if size == 0: continue
            opposite="SELL" if direction=="BUY" else "BUY"
            unwind_book=history.at(leg["token_id"],finish)
            if unwind_book is None or unwind_book.get("lineage_id")!=row["lineage_id"]:
                raise GraphError("unwind_lineage_reset")
            u=fill(leg,unwind_book,size,opposite,finish,maximum_age_ms,depletion)
            unwind_rows.append(u)
            remaining=size-frac(u["filled_size"])
            if remaining: exposures[leg["token_id"]]=fstr(remaining if direction=="BUY" else -remaining)
            unwound_cash+=frac(u["notional"]); unwind_fees+=frac(u["fee"])
        pnl=(unwound_cash-notional if direction=="BUY" else notional-unwound_cash)-fees-unwind_fees-reserve
        out.update(state="EXPOSURE_REMAINS" if exposures else "PARTIAL_UNWOUND",unhedged_exposure=exposures,
                   unwind_legs=unwind_rows,unwind_cost=fstr(max(Fraction(0),-pnl)),
                   realized_counterfactual_pnl=fstr(pnl) if not exposures else None)
    except (GraphError,KeyError,ValueError,TypeError) as exc:
        out.update(state="CENSORED",reason=str(exc))
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("candidates","book-tape","output","status"): p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--model-sha",required=True)
    p.add_argument("--delta-tape",type=Path)
    p.add_argument("--transport-delay-ms",default="1,2,5,10")
    p.add_argument("--inter-leg-skew-ms",default="0,1,2,5,10")
    p.add_argument("--transport-modes",default="SEQUENTIAL,PARALLEL,BATCH")
    p.add_argument("--unwind-delay-ms",type=int,default=2)
    p.add_argument("--maximum-book-age-ms",type=int,default=100)
    p.add_argument("--maximum-leg-skew-ms",type=int,default=100)
    p.add_argument("--interval-ms",type=int,default=5)
    a=p.parse_args()
    candidates,books=JsonlCursor(a.candidates),JsonlCursor(a.book_tape)
    deltas=JsonlCursor(a.delta_tape,segmented=True) if a.delta_tape else None
    reconstruction=ReconstructedDepth(a.model_sha)
    history=CausalBooks(); pending={}; seen=set(); counts=Counter(); modes=defaultdict(Counter)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    if a.output.exists():
        for line in a.output.open():
            row=json.loads(line)
            if row.get("model_sha") != a.model_sha: raise SystemExit("execution output model mismatch")
            seen.add(row["cycle_id"])
            counts[row["state"]]+=1
            summary=modes[row["mode"]]
            summary["scenarios"]+=1
            summary["paired"]+=row.get("all_legs_filled") is True
            summary["one_leg_unwound"]+=row["state"]=="PARTIAL_UNWOUND"
            if row.get("realized_counterfactual_pnl") is not None:
                summary["sum_pnl_after_reserve"]+=float(frac(row["realized_counterfactual_pnl"]))
    delays=[int(x) for x in a.transport_delay_ms.split(",")]
    skews=[int(x) for x in a.inter_leg_skew_ms.split(",")]
    while True:
        # Replay tapes incrementally with bounded history. Scenarios not covered
        # by retained history are censored, never reconstructed from future books.
        for row in books.poll():
            try:
                if deltas is not None:reconstruction.anchor(row)
                else:
                    now,decoded=decode_snapshot(row,a.model_sha); history.ingest(now,decoded)
            except (GraphError,ValueError,KeyError,TypeError): counts["book_rejected"]+=1
        if deltas is not None:
            for row in deltas.poll():
                try:
                    now,decoded=reconstruction.delta(row);history.ingest(now,decoded)
                except (GraphError,ValueError,KeyError,TypeError):counts["delta_rejected"]+=1
        for row in candidates.poll():
            if safe(row,a.model_sha) and row.get("relation") and row.get("opportunity_id"):
                pending[row["opportunity_id"]]=row
        for key,row in list(pending.items()):
            horizon=int(row["timestamp_ms"])+16*(max(delays)+max(skews))+a.unwind_delay_ms
            if history.watermark<horizon: continue
            for mode in a.transport_modes.split(","):
                for delay in delays:
                    for skew in skews:
                        result=simulate(row,history,mode,delay,skew,a.unwind_delay_ms,a.maximum_book_age_ms,a.maximum_leg_skew_ms)
                        if result["cycle_id"] in seen: continue
                        with a.output.open("a") as f: f.write(json.dumps(result,sort_keys=True)+"\n")
                        seen.add(result["cycle_id"]); counts[result["state"]]+=1
                        summary=modes[mode]
                        summary["scenarios"]+=1
                        summary["paired"]+=result.get("all_legs_filled") is True
                        summary["one_leg_unwound"]+=result["state"]=="PARTIAL_UNWOUND"
                        if result.get("realized_counterfactual_pnl") is not None:
                            summary["sum_pnl_after_reserve"]+=float(frac(result["realized_counterfactual_pnl"]))
            del pending[key]
        atomic(a.status,{"schema":SCHEMA,**SAFETY,"model_sha":a.model_sha,"state":"COLLECTING",
            "evidence_scope":"TRANSPORT_SCENARIO_NOT_VERIFIED_VENUE_EXECUTION",
            "execution_authority":"ZERO_AUTHORITY_EXCHANGE_EXECUTION_SHADOW","timestamp_ms":time.time_ns()//1000000,
            "evaluated":len(seen),"states":dict(counts),"by_execution_mode":{k:dict(v) for k,v in modes.items()},
            "pending":len(pending),"observation_watermark_ms":history.watermark})
        time.sleep(max(.001,a.interval_ms/1000))


if __name__=="__main__": main()
