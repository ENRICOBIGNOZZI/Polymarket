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
from v7_unified_exact_arb_graph import SAFETY, GraphError, frac, fstr, levels, fee_per_share, round_fee, safe, sha, prove
from v7_unified_exact_arb_graph_shadow import atomic
from v7_exact_arb_execution_timing import timing_plan, normalize_delay_profile, duration, candidate_delay_profile

SCHEMA = "polymarket_v7_unified_exact_arb_graph_execution_status_v1"
EXECUTION_MODEL = "CAUSAL_LIMITED_ORDER_V4_VENUE_TIMING"


def time_value(value):
    """Preserve native submillisecond times without float rounding or truncation."""
    value = frac(value)
    return value.numerator if value.denominator == 1 else fstr(value)


def checked_depth(book, side, timestamp, maximum_age_ms):
    if book is None or not book.get("lineage_continuous"): raise GraphError("arrival_lineage")
    if not isinstance(book.get("lineage_id"),str) or not book["lineage_id"]: raise GraphError("arrival_lineage_identity")
    if book.get("depth_truncated") is not False: raise GraphError("arrival_truncated")
    if not 0 <= timestamp-frac(book["timestamp_ms"]) <= maximum_age_ms: raise GraphError("arrival_stale")
    if not frac(book["timestamp_ms"]) <= frac(book.get("observation_ms", book["timestamp_ms"])) <= timestamp:
        raise GraphError("arrival_lookahead")
    raw = book.get(side)
    if not isinstance(raw, list): raise GraphError("arrival_depth_missing")
    depth = levels(raw, descending=side=="bids")
    if raw and not depth: raise GraphError("arrival_depth_invalid")
    opposite = "bids" if side=="asks" else "asks"
    other_raw = book.get(opposite)
    if not isinstance(other_raw,list): raise GraphError("arrival_depth_missing")
    other = levels(other_raw,descending=opposite=="bids")
    if other_raw and not other: raise GraphError("arrival_depth_invalid")
    if depth and other:
        bid,ask = (depth[0][0],other[0][0]) if side=="bids" else (other[0][0],depth[0][0])
        if bid>=ask: raise GraphError("arrival_crossed_book")
    return depth


def order_limit(leg, book, requested, direction, now, maximum_age_ms, require_capacity=True):
    """Pin a marketable limit using ONLY the book known at submission decision."""
    side = "asks" if direction == "BUY" else "bids"
    depth = checked_depth(book, side, now, maximum_age_ms)
    if leg.get("tick_size") is None: raise GraphError("execution_tick_unknown")
    tick = frac(leg["tick_size"])
    if not 0 < tick < 1: raise GraphError("execution_tick_unknown")
    total = Fraction(0)
    for price, size in depth:
        if (price/tick).denominator != 1: raise GraphError("execution_off_tick")
        total += size
        if total >= requested: return price
    if require_capacity: raise GraphError("decision_insufficient_depth")
    return depth[-1][0] if depth else None


def fill(leg, book, requested, direction, arrival, maximum_age_ms, depletion, limit, order_type="FAK"):
    if direction not in {"BUY", "SELL"} or order_type not in {"FAK", "FOK"}:
        raise GraphError("execution_order_type")
    if requested <= 0 or limit is None or not 0 < limit < 1: raise GraphError("execution_order_terms")
    side = "asks" if direction == "BUY" else "bids"
    depth_levels = checked_depth(book, side, arrival, maximum_age_ms)
    rate, exponent = frac(leg.get("fee_rate")), frac(leg.get("fee_exponent"))
    if not 0 <= rate <= 1 or exponent.denominator != 1 or not 0 <= exponent <= 16: raise GraphError("arrival_fee")
    if book.get("fee_rate") is not None and frac(book["fee_rate"])!=rate:raise GraphError("arrival_fee_changed")
    if leg.get("tick_size") and book.get("tick_size") and frac(leg["tick_size"])!=frac(book["tick_size"]):
        raise GraphError("arrival_tick_changed")
    if book.get("fee_exponent") is not None and frac(book["fee_exponent"]) != exponent:
        raise GraphError("arrival_fee_exponent_changed")
    increment = frac(leg["fee_rounding_increment"]) if leg.get("fee_rounding_increment") else None
    rounding = leg.get("fee_rounding_mode")
    if rate and (increment != Fraction(1, 100000) or rounding != "VENUE_5DP"):
        raise GraphError("arrival_fee_rounding_unknown")
    tick = frac(leg.get("tick_size"))
    if not 0 < tick < 1 or (limit/tick).denominator != 1:
        raise GraphError("execution_off_tick")
    if any((price/tick).denominator != 1 for price, _ in depth_levels):
        raise GraphError("arrival_off_tick")
    token = leg["token_id"]
    filled = cash = fees = Fraction(0)
    prices = []
    # A heartbeat/repeated observation of the SAME state is not fresh liquidity.
    version = (book.get("lineage_id"), book.get("state_version", book.get("observation_ms")))
    depth_levels = [(p, q) for p, q in depth_levels if p <= limit] if direction == "BUY" else [
        (p, q) for p, q in depth_levels if p >= limit]
    total_available=sum((max(Fraction(0),depth-depletion[(token,side,version,str(price))])
                         for price,depth in depth_levels),Fraction(0))
    # FOK is atomic only for this single hypothetical order, never the basket.
    for price, depth in (() if order_type == "FOK" and total_available < requested else depth_levels):
        key = (token, side, version, str(price))
        available = max(Fraction(0), depth-depletion[key])
        take = min(requested-filled, available)
        if take <= 0: continue
        fee = round_fee(take*fee_per_share(price, rate, int(exponent)), increment, rounding or "EXACT")
        depletion[key] += take
        filled += take; cash += take*price; fees += fee
        prices.append([str(price),str(take)])
        if filled == requested: break
    return {"token_id":token,"arrival_timestamp_ms":time_value(arrival),"book_timestamp_ms":time_value(book["timestamp_ms"]),
            "book_observation_ms":book.get("observation_ms"),"lineage_id":book.get("lineage_id"),
            "book_age_ms":fstr(frac(arrival)-frac(book["timestamp_ms"])),
            "limit_price":fstr(limit),"order_type":order_type,
            "requested_size":fstr(requested),"filled_size":fstr(filled),"remaining_quantity":fstr(requested-filled),
            "available_size":fstr(total_available),
            "fill_price":fstr(cash/filled) if filled else None,"fee":fstr(fees),
            "notional":fstr(cash),"fills":prices}


def residual_payoffs(relation, exposure):
    """Token quantities, not relation coefficients, price residual payoff risk."""
    states = relation.get("states")
    if not isinstance(states, list) or not states: return None
    try:
        totals = [Fraction(0) for _ in states]
        for leg in relation["legs"]:
            size = frac(exposure.get(leg["token_id"], "0"))
            if not size: continue
            vector = leg.get("payout_vector")
            if not isinstance(vector, list) or len(vector) != len(states): return None
            totals = [v+size*frac(p) for v,p in zip(totals, vector)]
        return {"states":states, "payoffs":[fstr(v) for v in totals], "worst_payoff":fstr(min(totals))}
    except (GraphError, ValueError, TypeError): return None


def execution_steps(opportunity, history, mode, delay_ms, skew_ms, unwind_delay_ms=2, maximum_age_ms=100, maximum_skew_ms=100,
                    order_type="FAK", order_share_quantum=Fraction(1,100), batch_max_orders=15, depletion=None,
                    venue_delay_ms_by_token=None, ack_delay_ms=0):
    """Yield each arrival BEFORE reading its book or mutating shared liquidity.

    A driver may interleave these steps across episodes. No later-leg book or
    terminal watermark is consulted to decide an earlier counterfactual fill.
    """
    relation, result = opportunity["relation"], opportunity["result"]
    detected = frac(opportunity["timestamp_ms"])
    quantity = frac(result["quantity"])
    direction = result["direction"]
    delay_ms,skew_ms,unwind_delay_ms,maximum_age_ms,maximum_skew_ms = map(frac,
        (delay_ms,skew_ms,unwind_delay_ms,maximum_age_ms,maximum_skew_ms))
    if (mode not in {"SEQUENTIAL","PARALLEL","BATCH"} or direction not in {"BUY","SELL"}
        or order_type not in {"FAK","FOK"}): raise GraphError("execution_mode")
    order_share_quantum = frac(order_share_quantum)
    if (min(delay_ms,skew_ms,unwind_delay_ms) < 0 or quantity <= 0 or order_share_quantum <= 0
        or maximum_age_ms <= 0 or maximum_skew_ms < 0 or batch_max_orders <= 0): raise GraphError("execution_terms")
    if not relation.get("enabled") or relation.get("relation_type","CONSTANT_PAYOUT_EQUALITY") != "CONSTANT_PAYOUT_EQUALITY":
        raise GraphError("execution_relation")
    legs = relation["legs"]
    if not 1 <= len(legs) <= 16: raise GraphError("execution_cardinality")
    if direction == "SELL" and opportunity.get("inventory_reserved") is not True: raise GraphError("execution_inventory")
    profile = normalize_delay_profile(venue_delay_ms_by_token)
    ack_delay_ms = duration(ack_delay_ms)
    out = {"schema":"polymarket_v7_unified_exact_arb_graph_execution_cycle_v1", **SAFETY,
           "execution_model":EXECUTION_MODEL,
           "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY", "model_sha":opportunity["model_sha"],
           "graph_generation":opportunity["graph_generation"], "relation_id":relation["relation_id"],
           "relation_family":relation.get("relation_family"),"opportunity_id":opportunity["opportunity_id"],
           "mode":mode,"transport_delay_ms":time_value(delay_ms),"inter_leg_skew_ms":time_value(skew_ms),"atomic_batch":False,
           "order_type":order_type,"order_share_quantum":fstr(order_share_quantum),
           "unwind_delay_ms":time_value(unwind_delay_ms),"maximum_book_age_ms":time_value(maximum_age_ms),
           "maximum_leg_skew_ms":time_value(maximum_skew_ms),"batch_max_orders":batch_max_orders,
           "order_limit_policy":"FROZEN_AT_DETECTION", "venue_execution_verified":False,
           "economic_admission":"NON_EXECUTABLE_UNVERIFIED_VENUE_TERMS",
           "submission_policy":"ALL_PLANNED_LEGS_WITHOUT_ASSUMED_CANCEL_SUCCESS",
           "mandatory_venue_delay_verified":False,"sequential_ack_latency_verified":False,
           "venue_delay_ms_by_token":profile,"ack_delay_ms":time_value(ack_delay_ms),
           "venue_delay_policy":"EXPLICIT_PER_TOKEN_SCENARIO" if profile is not None else "ZERO_HOLD_UPPER_BOUND_UNVERIFIED",
           "time_basis":opportunity.get("time_basis","MILLISECOND_REPLAY_NOT_NATIVE_SUBMILLISECOND_ATTESTATION"),
           "timestamp_encoding":"EXACT_RATIONAL_MILLISECONDS",
           "fee_granularity_assumption":"L2_PRICE_LEVEL_AS_MATCH_NOT_VERIFIED_FILL_FRAGMENTATION",
           "evidence_scope":"TRANSPORT_AND_VENUE_HOLD_SCENARIO_NOT_VERIFIED_EXECUTION",
           "timestamp_ms":time_value(detected),"scheduled_unwind_horizon_ms":None, "state":"CENSORED", "legs":[],
           "theoretical_locked_pnl":result.get("net_locked_pnl"),
           "realized_counterfactual_pnl":None, "unwind_cost":None}
    execution_inputs = [opportunity["model_sha"],opportunity["graph_generation"],
        relation,result,opportunity.get("decision_books_sha256"),fstr(detected),out["time_basis"],
        opportunity.get("require_venue_terms"),opportunity.get("venue_terms"),opportunity.get("selection_receipt_sha256")]
    if hasattr(history,"evidence_identity"):
        out["arrival_evidence"] = history.evidence_identity
        execution_inputs.append(out["arrival_evidence"])
    out["execution_input_sha256"] = sha(execution_inputs)
    out["cycle_id"] = sha([EXECUTION_MODEL,out["execution_input_sha256"],out["opportunity_id"],mode,
                           time_value(delay_ms),time_value(skew_ms),time_value(unwind_delay_ms),
                           time_value(maximum_age_ms),time_value(maximum_skew_ms),order_type,fstr(order_share_quantum),batch_max_orders,
                           profile,time_value(ack_delay_ms)])
    depletion = defaultdict(Fraction) if depletion is None else depletion
    try:
        profile=candidate_delay_profile(opportunity,profile)
        out["venue_delay_ms_by_token"]=profile
        if opportunity.get("require_venue_terms") is True:
            out["venue_delay_policy"]="RECORDED_PUBLIC_METADATA_AT_SELECTION_ADMISSION"
            out["venue_terms"]=opportunity["venue_terms"]
        plan = timing_plan([leg["token_id"] for leg in legs],detected,mode,delay_ms,skew_ms,unwind_delay_ms,profile,ack_delay_ms)
        arrivals = [row["match_ms"] for row in plan["legs"]]
        finish = plan["finish_ms"]
        out["scheduled_unwind_horizon_ms"] = time_value(finish)
        prove(relation)
        guarantee = frac(relation["guaranteed_payout"])
        if guarantee <= 0 or frac(relation.get("reserve_per_unit")) < guarantee/2000:
            raise GraphError("execution_reserve_below_baseline")
        anchors = opportunity.get("decision_books")
        if not isinstance(anchors,dict) or opportunity.get("decision_books_sha256") != sha(anchors):
            raise GraphError("decision_books_missing_or_digest")
        if set(anchors) != {leg["token_id"] for leg in legs}: raise GraphError("decision_books_membership")
        if mode == "BATCH" and len(legs) > batch_max_orders: raise GraphError("batch_order_capacity")
        if len({leg["token_id"] for leg in legs}) != len(legs): raise GraphError("execution_duplicate_token")
        if relation.get("settlement_close_ms",0) and max(arrivals)>=frac(relation["settlement_close_ms"]):
            raise GraphError("arrival_market_closed")
        limits = []
        for leg in legs:
            requested = quantity*frac(leg["coefficient"])
            if requested <= 0 or (requested/order_share_quantum).denominator != 1:
                raise GraphError("execution_share_precision")
            if leg.get("minimum_order") is None or not 0 <= frac(leg["minimum_order"]) <= requested:
                raise GraphError("execution_minimum")
            anchor = anchors[leg["token_id"]]
            limits.append(order_limit(leg, anchor, requested, direction, detected, maximum_age_ms))
        stamps = []
        # The candidate anchors lineage; a reset between detection and arrival
        # censors the counterfactual even when the new snapshot looks healthy.
        for timing in sorted(plan["legs"],key=lambda row:(row["match_ms"],row["leg_index"])):
            index = timing["leg_index"]
            leg,t,limit = legs[index],timing["match_ms"],limits[index]
            out["timestamp_ms"]=time_value(t)
            yield {"kind":"ENTRY_MATCH","timestamp_ms":time_value(t),"leg_index":index,"token_id":leg["token_id"]}
            if history.watermark < t: raise GraphError("observation_watermark")
            book=history.at(leg["token_id"],t)
            anchor = anchors[leg["token_id"]]
            if anchor is None or book is None or anchor.get("lineage_id") != book.get("lineage_id"):
                raise GraphError("arrival_lineage_reset")
            stamps.append(frac(book["timestamp_ms"]))
            if max(stamps)-min(stamps)>maximum_skew_ms: raise GraphError("arrival_skew")
            requested = quantity*frac(leg["coefficient"])
            if requested < frac(leg.get("minimum_order",0)): raise GraphError("execution_minimum")
            executed = fill(leg,book,requested,direction,t,maximum_age_ms,depletion,limit,order_type)
            executed.update(leg_index=index, submission_timestamp_ms=time_value(timing["submission_ms"]),
                wire_arrival_timestamp_ms=time_value(timing["wire_arrival_ms"]),
                match_timestamp_ms=time_value(t),result_timestamp_ms=time_value(timing["result_ms"]),
                mandatory_venue_delay_ms=time_value(timing["venue_delay_ms"]),
                arrival_timestamp_ms=time_value(timing["wire_arrival_ms"]))
            out["legs"].append(executed)
        out["entry_completion_timestamp_ms"]=time_value(max(arrivals))
        out["entry_results_timestamp_ms"]=time_value(plan["entry_result_ms"])
        if ack_delay_ms:
            out["timestamp_ms"]=time_value(plan["entry_result_ms"])
            yield {"kind":"ENTRY_RESULTS","timestamp_ms":out["timestamp_ms"],"leg_index":len(legs),"token_id":None}
            if history.watermark < plan["entry_result_ms"]: raise GraphError("observation_watermark")
        complete = all(frac(row["remaining_quantity"])==0 for row in out["legs"])
        out.update(all_legs_filled=complete, partial_fill=any(frac(r["filled_size"])>0 for r in out["legs"]) and not complete,
                   number_of_filled_legs=sum(frac(r["filled_size"])>0 for r in out["legs"]),
                   unwind_required=not complete)
        notional=sum((frac(r["notional"]) for r in out["legs"]),Fraction(0))
        fees=sum((frac(r["fee"]) for r in out["legs"]),Fraction(0))
        reserve=quantity*frac(relation["reserve_per_unit"])
        payout=quantity*frac(relation["guaranteed_payout"])
        if not any(frac(r["filled_size"]) for r in out["legs"]):
            out.update(state="NO_LEGS_FILLED",unhedged_exposure={},unwind_required=False,
                       realized_counterfactual_pnl="0",unwind_cost="0",reserve_drag="0",fee_drag="0")
            return out
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
                    or frac(transform["capacity"])<quantity or frac(transform["expires_at_ms"])<finish
                    or int(transform["latency_ms"])<=0 or int(transform["capital_lock_time_ms"])<=0):
                    raise GraphError("transformation_ineligible")
                cost=frac(transform["fixed_cost"])+quantity*frac(transform["variable_cost_per_unit"])
                if cost < 0: raise GraphError("transformation_cost")
                out["transformation"]={"kind":transform["kind"],"state":"COUNTERFACTUAL_ELIGIBLE",
                    "completion_timestamp_ms":time_value(max(arrivals)+int(transform["latency_ms"])),
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
        unwind_orders=[]
        out["pre_unwind_exposure"]={r["token_id"]:fstr(frac(r["filled_size"])*(1 if direction=="BUY" else -1))
                                    for r in out["legs"] if frac(r["filled_size"])}
        out["pre_unwind_state_payoffs"]=residual_payoffs(relation,out["pre_unwind_exposure"])
        for row in out["legs"]:
            index = row["leg_index"]
            leg = legs[index]
            timing = plan["legs"][index]
            size=frac(row["filled_size"])
            if size == 0: continue
            opposite="SELL" if direction=="BUY" else "BUY"
            submit = timing["unwind_submission_ms"]
            unwind_anchor = history.at(leg["token_id"], submit)
            if unwind_anchor is None or unwind_anchor.get("lineage_id") != row["lineage_id"]:
                raise GraphError("unwind_lineage_reset")
            units = size/order_share_quantum
            unwind_size = (units.numerator//units.denominator)*order_share_quantum
            if unwind_size <= 0 or unwind_size < frac(leg["minimum_order"]):
                exposures[leg["token_id"]]=fstr(size if direction=="BUY" else -size)
                unwind_rows.append({"token_id":leg["token_id"],"state":"NON_EXECUTABLE_DUST",
                                    "requested_size":fstr(size),"filled_size":"0"})
                continue
            limit = order_limit(leg,unwind_anchor,unwind_size,opposite,submit,maximum_age_ms,False)
            if limit is None:
                exposures[leg["token_id"]]=fstr(size if direction=="BUY" else -size)
                unwind_rows.append({"token_id":leg["token_id"],"state":"NO_UNWIND_DEPTH",
                                    "requested_size":fstr(unwind_size),"filled_size":"0"})
                continue
            # Freeze EVERY unwind order at submission, before yielding any
            # later arrival. A preceding unwind cannot inform another's limit.
            unwind_orders.append((timing,leg,row,size,opposite,unwind_size,limit,submit))
        for timing,leg,row,size,opposite,unwind_size,limit,submit in sorted(
                unwind_orders,key=lambda order:(order[0]["unwind_match_ms"],order[0]["leg_index"])):
            match = timing["unwind_match_ms"]
            out["timestamp_ms"]=time_value(match)
            yield {"kind":"UNWIND_MATCH","timestamp_ms":time_value(match),"leg_index":timing["leg_index"],"token_id":leg["token_id"]}
            if history.watermark < match: raise GraphError("observation_watermark")
            unwind_book=history.at(leg["token_id"],match)
            if unwind_book is None or unwind_book.get("lineage_id")!=row["lineage_id"]:
                raise GraphError("unwind_lineage_reset")
            u=fill(leg,unwind_book,unwind_size,opposite,match,maximum_age_ms,depletion,limit,"FAK")
            u.update(leg_index=timing["leg_index"],submission_timestamp_ms=time_value(submit),
                wire_arrival_timestamp_ms=time_value(timing["unwind_wire_arrival_ms"]),
                arrival_timestamp_ms=time_value(timing["unwind_wire_arrival_ms"]),
                match_timestamp_ms=time_value(match),result_timestamp_ms=time_value(timing["unwind_result_ms"]),
                mandatory_venue_delay_ms=time_value(timing["venue_delay_ms"]))
            unwind_rows.append(u)
            remaining=size-frac(u["filled_size"])
            if remaining: exposures[leg["token_id"]]=fstr(remaining if direction=="BUY" else -remaining)
            unwound_cash+=frac(u["notional"]); unwind_fees+=frac(u["fee"])
        if ack_delay_ms and unwind_orders:
            result_time = max(order[0]["unwind_result_ms"] for order in unwind_orders)
            out["timestamp_ms"]=time_value(result_time)
            yield {"kind":"UNWIND_RESULTS","timestamp_ms":out["timestamp_ms"],"leg_index":len(legs),"token_id":None}
            if history.watermark < result_time: raise GraphError("observation_watermark")
        pnl=(unwound_cash-notional if direction=="BUY" else notional-unwound_cash)-fees-unwind_fees-reserve
        out.update(state="EXPOSURE_REMAINS" if exposures else "PARTIAL_UNWOUND",unhedged_exposure=exposures,
                   unwind_legs=unwind_rows,unwind_cost=fstr(max(Fraction(0),-pnl)),
                   residual_state_payoffs=residual_payoffs(relation,exposures),
                   fee_drag=fstr(fees),unwind_fee_drag=fstr(unwind_fees),reserve_drag=fstr(reserve),
                   realized_counterfactual_pnl=fstr(pnl) if not exposures else None)
    except (GraphError,KeyError,ValueError,TypeError) as exc:
        out.update(state="CENSORED",reason=str(exc),net_locked_pnl=None,realized_counterfactual_pnl=None)
        # Preserve known fills even if a later leg/unwind observation is missing.
        # Other scheduled orders can have unknown fills; this is NOT a complete
        # residual portfolio or a guarantee of zero exposure.
        out["known_entry_fills"]={r["token_id"]:r["filled_size"] for r in out["legs"] if frac(r["filled_size"])}
        out["known_unwind_fills"]={r["token_id"]:r["filled_size"] for r in locals().get("unwind_rows",[])
                                  if r.get("filled_size") is not None and frac(r["filled_size"])}
        out["residual_exposure_verified"]=False
    return out


def simulate(opportunity, history, mode, delay_ms, skew_ms, unwind_delay_ms=2, maximum_age_ms=100, maximum_skew_ms=100,
             order_type="FAK", order_share_quantum=Fraction(1,100), batch_max_orders=15,
             venue_delay_ms_by_token=None, ack_delay_ms=0):
    """Single independent scenario driven through the same event-order kernel."""
    steps=execution_steps(opportunity,history,mode,delay_ms,skew_ms,unwind_delay_ms,maximum_age_ms,maximum_skew_ms,
                          order_type,order_share_quantum,batch_max_orders,
                          venue_delay_ms_by_token=venue_delay_ms_by_token,ack_delay_ms=ack_delay_ms)
    while True:
        try: next(steps)
        except StopIteration as done: return done.value


def record_summary(row, modes, arms):
    """Alternative latency/order scenarios must never be added as business PnL."""
    mode = modes[row["mode"]]
    mode["scenarios"] += 1
    mode["paired"] += row.get("all_legs_filled") is True
    mode["one_leg_unwound"] += row["state"] == "PARTIAL_UNWOUND"
    mode["sum_pnl_after_reserve"] = None
    parameters = {name:row[name] for name in ("mode","order_type","transport_delay_ms","inter_leg_skew_ms",
        "unwind_delay_ms","maximum_book_age_ms","maximum_leg_skew_ms","batch_max_orders","order_share_quantum",
        "venue_delay_ms_by_token","ack_delay_ms","venue_delay_policy")}
    key = sha(parameters)
    arm = arms.setdefault(key, {"parameters":parameters,"scenarios":0,"all_leg_fills":0,
        "censored":0,"closed_pnl_observations":0,"sum_closed_counterfactual_pnl":None,
        "locked_pnl_observations":0,"sum_conditional_locked_pnl":None, "economic_value_verified":False})
    arm["scenarios"] += 1
    arm["all_leg_fills"] += row.get("all_legs_filled") is True
    arm["censored"] += row["state"] == "CENSORED"
    for field, count, total in (("realized_counterfactual_pnl","closed_pnl_observations","sum_closed_counterfactual_pnl"),
                               ("net_locked_pnl","locked_pnl_observations","sum_conditional_locked_pnl")):
        if row.get(field) is not None and row["state"] != "CENSORED":
            arm[count] += 1
            arm[total] = fstr(frac(arm[total] if arm[total] is not None else "0")+frac(row[field]))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("candidates","book-tape","output","status"): p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--model-sha",required=True)
    p.add_argument("--delta-tape",type=Path)
    p.add_argument("--transport-delay-ms",default="1,2,5,10")
    p.add_argument("--inter-leg-skew-ms",default="0,1,2,5,10")
    p.add_argument("--transport-modes",default="SEQUENTIAL,PARALLEL,BATCH")
    p.add_argument("--order-type",choices=("FAK","FOK"),default="FAK")
    p.add_argument("--unwind-delay-ms",type=int,default=2)
    p.add_argument("--venue-delay-profile",type=Path,help="Frozen JSON token -> hold ms scenario; not a live venue attestation")
    p.add_argument("--ack-delay-ms",default="0",help="Assumed post-match response travel, not measured ACK latency")
    p.add_argument("--maximum-book-age-ms",type=int,default=100)
    p.add_argument("--maximum-leg-skew-ms",type=int,default=100)
    p.add_argument("--interval-ms",type=int,default=5)
    a=p.parse_args()
    profile=normalize_delay_profile(json.loads(a.venue_delay_profile.read_text()) if a.venue_delay_profile else None)
    ack=duration(a.ack_delay_ms)
    candidates,books=JsonlCursor(a.candidates),JsonlCursor(a.book_tape)
    deltas=JsonlCursor(a.delta_tape,segmented=True) if a.delta_tape else None
    reconstruction=ReconstructedDepth(a.model_sha)
    history=CausalBooks(); pending={}; seen=set(); counts=Counter(); modes=defaultdict(Counter); arms={}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    if a.output.exists():
        for line in a.output.open():
            row=json.loads(line)
            if row.get("model_sha") != a.model_sha: raise SystemExit("execution output model mismatch")
            if row.get("execution_model") != EXECUTION_MODEL:
                counts["historical_execution_model_excluded"] += 1
                continue
            if row["cycle_id"] in seen: continue
            seen.add(row["cycle_id"])
            counts[row["state"]]+=1
            record_summary(row,modes,arms)
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
            try:
                horizon=max(timing_plan([leg["token_id"] for leg in row["relation"]["legs"]],row["timestamp_ms"],
                    mode,delay,skew,a.unwind_delay_ms,profile,ack)["finish_ms"]
                    for mode in a.transport_modes.split(",") for delay in delays for skew in skews)
            except GraphError as error:
                if str(error)!="execution_mandatory_delay_unknown": raise
                horizon=frac(row["timestamp_ms"])
            if history.watermark<horizon: continue
            for mode in a.transport_modes.split(","):
                for delay in delays:
                    for skew in skews:
                        result=simulate(row,history,mode,delay,skew,a.unwind_delay_ms,a.maximum_book_age_ms,a.maximum_leg_skew_ms,
                                        order_type=a.order_type,venue_delay_ms_by_token=profile,ack_delay_ms=ack)
                        if result["cycle_id"] in seen: continue
                        with a.output.open("a") as f: f.write(json.dumps(result,sort_keys=True)+"\n")
                        seen.add(result["cycle_id"]); counts[result["state"]]+=1
                        record_summary(result,modes,arms)
            del pending[key]
        atomic(a.status,{"schema":SCHEMA,**SAFETY,"model_sha":a.model_sha,"state":"COLLECTING",
            "execution_model":EXECUTION_MODEL,"economic_pnl":None,"by_scenario_arm":arms,
            "evidence_scope":"TRANSPORT_AND_VENUE_HOLD_SCENARIO_NOT_VERIFIED_EXECUTION",
            "execution_authority":"ZERO_AUTHORITY_EXCHANGE_EXECUTION_SHADOW","timestamp_ms":time.time_ns()//1000000,
            "evaluated":len(seen),"states":dict(counts),"by_execution_mode":{k:dict(v) for k,v in modes.items()},
            "pending":len(pending),"observation_watermark_ms":history.watermark})
        time.sleep(max(.001,a.interval_ms/1000))


if __name__=="__main__": main()
