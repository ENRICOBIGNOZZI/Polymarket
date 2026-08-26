#!/usr/bin/env python3
from __future__ import annotations

import math
import os
from collections import Counter
from typing import Any

import hf_active_flow_maker_batched_probe as batched
import hf_active_flow_maker_core as core
import hf_active_flow_maker_seeded_probe as seeded
from hf_fill_conditioned_maker_router import (
    CandidateEvidence,
    RestingConfig,
    RestingEvidence,
    RouterConfig,
    evaluate,
    evaluate_resting,
)


# Frozen Micro Maker forward outcomes from completed Aug 25 windows (#338/#386).
# The prospective arm gives no positive markout credit to at-touch quotes because the
# small historical sample is mixed. It does carry forward the observed adverse inside-
# spread markout as a conservative prior, and tests the previously hypothesis-generating
# confidence/toxicity split strictly on later decisions.
PRIOR_CUTOFF_TS = 1787688060
PRIOR_SOURCE = "PR338/PR386 completed Micro Maker forward windows before 2026-08-26"
AT_TOUCH_MARKOUT_PRIOR = 0.0
INSIDE_MARKOUT_PRIOR = -0.017787054548749986
INSIDE_MIN_CONFIDENCE = 0.80
MAX_RECURRENT_SELL_SHARE = 0.775
RESTING_GRACE_SECONDS = 20.0
CANCEL_LATENCY_SECONDS = 1.0

_ORIGINAL_PROFILE = seeded.apply_zero_fill_research_profile
_ORIGINAL_RUN_BATCHED = batched.run_batched
_ORIGINAL_CONSUME = core.consume
_ACTIVE_ARGS: Any | None = None
_REVALIDATION_STATS: Counter[str] = Counter()


def apply_fill_conditioned_profile(args: Any) -> dict[str, Any]:
    diagnostics = _ORIGINAL_PROFILE(args)
    args.improve_ticks = 1
    # The previous exact-head experiment found zero intersection between executable
    # static edge and causal same-token contra-flow in only 120 evaluated books. Broaden
    # the recent-flow-seeded scan before changing economics or lowering the 0.5 bp floor.
    if hasattr(args, "activity_scan_markets"):
        args.activity_scan_markets = min(int(args.markets), max(int(args.activity_scan_markets), 300))
    os.environ["HF_SEED_MAX_CONDITIONS"] = str(max(100, int(os.environ.get("HF_SEED_MAX_CONDITIONS", "50"))))

    diagnostics = dict(diagnostics)
    effective = dict(diagnostics.get("effective") or {})
    effective["improve_ticks"] = 1
    if hasattr(args, "activity_scan_markets"):
        effective["activity_scan_markets"] = int(args.activity_scan_markets)
    diagnostics.update({
        "research_admission_profile": "prospective_fill_conditioned_router_v2_rolling_revalidation",
        "effective": effective,
        "fill_conditioned_router": True,
        "prior_cutoff_ts": PRIOR_CUTOFF_TS,
        "prior_source": PRIOR_SOURCE,
        "at_touch_markout_prior_per_share": AT_TOUCH_MARKOUT_PRIOR,
        "inside_markout_prior_per_share": INSIDE_MARKOUT_PRIOR,
        "inside_min_confidence": INSIDE_MIN_CONFIDENCE,
        "max_recurrent_sell_share": MAX_RECURRENT_SELL_SHARE,
        "positive_at_touch_markout_credit": False,
        "resting_revalidation": True,
        "resting_grace_seconds": RESTING_GRACE_SECONDS,
        "cancel_latency_seconds": CANCEL_LATENCY_SECONDS,
        "interpretation": "broaden recent-flow-seeded books; historical fill outcomes set prospective quote-shape/toxicity guards; current-window future markouts never enter admission; resting residuals are revalidated from rolling causal flow",
    })
    seeded._ADMISSION_DIAGNOSTICS = dict(diagnostics)
    return diagnostics


def _candidate_for_price(
    source: core.Candidate,
    book: core.Book,
    trades: list[core.Trade],
    decision_ts: int,
    args: Any,
    limit_price: float,
    improvement_ticks: int,
) -> tuple[core.Candidate | None, core.Flow, float, float, float]:
    shares = core.size_at_price(book, limit_price, args)
    queue = book.displayed_size(True, limit_price)
    flow = core.flow_stats(trades, source.token_id, decision_ts, args.recent_lookback_seconds, limit_price)
    toxicity = args.toxicity_mult * book.spread() * max(0.0, -flow.signed_imbalance)
    adjusted_edge = source.static_edge - improvement_ticks * source.tick_size - toxicity
    p_fill = core.fill_probability_proxy(flow, queue, shares) if shares > 0 else 0.0
    if shares <= 0 or adjusted_edge <= args.min_edge:
        return None, flow, adjusted_edge, p_fill, queue
    candidate = core.Candidate(
        "active_flow",
        source.market,
        source.side,
        source.token_id,
        source.tick_size,
        limit_price,
        shares,
        queue,
        source.static_edge,
        adjusted_edge,
        source.confidence,
        improvement_ticks,
        flow,
        p_fill,
        0.0,
    )
    return candidate, flow, adjusted_edge, p_fill, queue


def fill_conditioned_gate(
    candidates: list[core.Candidate],
    books: dict[str, core.Book],
    flows: dict[str, list[core.Trade]],
    decision_ts: int,
    args: Any,
) -> tuple[list[core.Candidate], dict[str, Any]]:
    if decision_ts <= PRIOR_CUTOFF_TS:
        raise RuntimeError("fill-conditioned prior is not strictly pre-decision")

    routed: list[core.Candidate] = []
    reasons: Counter[str] = Counter()
    stats: dict[str, Any] = {
        "inside_considered": 0,
        "inside_kept": 0,
        "reverted_to_touch": 0,
        "dropped_inside": 0,
        "router_candidates": 0,
        "router_post_at_touch": 0,
        "router_improve_one_tick": 0,
        "router_skipped": 0,
        "prior_cutoff_ts": PRIOR_CUTOFF_TS,
        "prior_source": PRIOR_SOURCE,
        "at_touch_markout_prior_per_share": AT_TOUCH_MARKOUT_PRIOR,
        "inside_markout_prior_per_share": INSIDE_MARKOUT_PRIOR,
        "future_current_window_markout_used_for_admission": False,
    }
    config = RouterConfig(
        min_post_cost_edge=max(0.00005, float(args.min_edge)),
        min_recent_trades=1,
        min_compatible_sell_prints=1,
        min_recent_clearance_ratio=0.005,
        toxicity_min_trades=4,
        max_sell_share=MAX_RECURRENT_SELL_SHARE,
        min_fill_probability=max(0.005, float(args.min_fill_probability)),
        min_inside_confidence=INSIDE_MIN_CONFIDENCE,
    )

    for source in candidates:
        book = books.get(source.token_id)
        if book is None:
            reasons["missing_book"] += 1
            continue
        bid, ask = book.best_bid(), book.best_ask()
        if not (math.isfinite(bid) and math.isfinite(ask) and ask > bid > 0):
            reasons["invalid_touch"] += 1
            continue
        market_trades = flows.get(source.market.condition_id, [])
        touch, touch_flow, touch_edge, touch_p, touch_queue = _candidate_for_price(
            source, book, market_trades, decision_ts, args, bid, 0
        )
        if touch is None:
            reasons["touch_not_economic_or_sizeable"] += 1
            continue

        inside = None
        inside_flow = touch_flow
        inside_edge = touch_edge - source.tick_size
        inside_p = 0.0
        inside_price = bid + source.tick_size
        if inside_price < ask - 1e-12:
            stats["inside_considered"] += 1
            inside, inside_flow, inside_edge, inside_p, _ = _candidate_for_price(
                source, book, market_trades, decision_ts, args, inside_price, 1
            )

        evidence = CandidateEvidence(
            post_cost_edge=touch_edge,
            tick_size=source.tick_size,
            queue_ahead=touch_queue,
            own_shares=touch.shares,
            recent_trade_count=touch_flow.trade_count,
            compatible_sell_prints=touch_flow.compatible_sell_prints,
            compatible_sell_volume=touch_flow.compatible_sell_volume,
            recent_buy_volume=touch_flow.buy_volume,
            recent_sell_volume=touch_flow.sell_volume,
            at_touch_fill_probability=touch_p,
            at_touch_markout_per_share=AT_TOUCH_MARKOUT_PRIOR,
            inside_fill_probability=inside_p,
            inside_markout_per_share=INSIDE_MARKOUT_PRIOR,
            inside_post_cost_edge=inside_edge,
            confidence=source.confidence,
            capital_latency_cost_per_share=0.0,
        )
        decision = evaluate(evidence, config)
        stats["router_candidates"] += 1
        reasons[decision.reason] += 1

        if decision.action == "SKIP":
            stats["router_skipped"] += 1
            continue
        if decision.action == "IMPROVE_ONE_TICK" and inside is not None:
            if not core.activity_eligible(inside_flow, inside.queue_ahead, inside.shares, args):
                reasons["inside_failed_activity_eligibility"] += 1
                stats["dropped_inside"] += 1
                continue
            inside.score = decision.inside_ev_per_share
            routed.append(inside)
            stats["inside_kept"] += 1
            stats["router_improve_one_tick"] += 1
            continue

        if not core.activity_eligible(touch_flow, touch.queue_ahead, touch.shares, args):
            reasons["touch_failed_activity_eligibility"] += 1
            stats["router_skipped"] += 1
            continue
        touch.score = decision.at_touch_ev_per_share
        routed.append(touch)
        stats["router_post_at_touch"] += 1
        if source.improvement_ticks > 0 or inside is not None:
            stats["reverted_to_touch"] += 1

    routed.sort(key=lambda item: item.score, reverse=True)
    stats["router_reason_counts"] = dict(sorted(reasons.items()))
    return routed, stats


def rolling_revalidated_consume(order: core.ShadowOrder, trades: list[core.Trade], received_ts: int) -> None:
    """Replay fills first, then revalidate the still-resting residual.

    A pending cancel remains fillable until its explicit effective timestamp. This keeps
    cancel-latency risk in the paper counterfactual instead of erasing it optimistically.
    """
    args = _ACTIVE_ARGS
    if args is None:
        _ORIGINAL_CONSUME(order, trades, received_ts)
        return

    cancel_effective = getattr(order, "hf_cancel_effective_ts", None)
    replay_rows = trades
    if cancel_effective is not None:
        replay_rows = [trade for trade in trades if trade.ts <= float(cancel_effective) + 1e-12]
    _ORIGINAL_CONSUME(order, replay_rows, received_ts)
    if order.remaining <= 1e-12:
        _REVALIDATION_STATS["filled_before_cancel"] += 1
        return

    if cancel_effective is not None:
        if received_ts >= float(cancel_effective):
            order.remaining = 0.0
            _REVALIDATION_STATS["cancel_effective"] += 1
        else:
            _REVALIDATION_STATS["cancel_pending_wait"] += 1
        return

    age = max(0.0, float(received_ts - order.created_ts))
    flow = core.flow_stats(
        trades,
        order.candidate.token_id,
        received_ts,
        args.recent_lookback_seconds,
        order.candidate.limit_price,
    )
    p_remaining = core.fill_probability_proxy(flow, order.queue_ahead, order.remaining)
    prior = INSIDE_MARKOUT_PRIOR if order.candidate.improvement_ticks > 0 else AT_TOUCH_MARKOUT_PRIOR
    evidence = RestingEvidence(
        age_seconds=age,
        current_post_cost_edge=order.candidate.adjusted_edge,
        remaining_fill_probability=p_remaining,
        recent_trade_count=flow.trade_count,
        recent_buy_volume=flow.buy_volume,
        recent_sell_volume=flow.sell_volume,
        conservative_markout_prior_per_share=prior,
        capital_latency_cost_per_share=0.0,
    )
    decision = evaluate_resting(
        evidence,
        RestingConfig(
            grace_seconds=RESTING_GRACE_SECONDS,
            min_post_cost_edge=max(0.00005, float(args.min_edge)),
            min_remaining_fill_probability=max(0.005, float(args.min_fill_probability)),
            toxicity_min_trades=4,
            max_sell_share=MAX_RECURRENT_SELL_SHARE,
            cancel_latency_seconds=CANCEL_LATENCY_SECONDS,
        ),
    )
    _REVALIDATION_STATS[decision.reason] += 1
    if decision.action == "CANCEL_PENDING":
        setattr(order, "hf_cancel_effective_ts", float(received_ts) + decision.cancel_latency_seconds)
        _REVALIDATION_STATS["cancel_requested"] += 1
    else:
        _REVALIDATION_STATS["kept"] += 1


def run_fill_conditioned(args: Any) -> dict[str, Any]:
    global _ACTIVE_ARGS, _REVALIDATION_STATS
    _ACTIVE_ARGS = args
    _REVALIDATION_STATS = Counter()
    result = _ORIGINAL_RUN_BATCHED(args)
    arms = result.get("arms")
    if isinstance(arms, dict) and "active_flow" in arms:
        router_arm = arms.pop("active_flow")
        for detail in router_arm.get("orders_detail", []) if isinstance(router_arm, dict) else []:
            if isinstance(detail, dict):
                detail["arm"] = "fill_conditioned_router"
        for outcome in router_arm.get("outcomes", []) if isinstance(router_arm, dict) else []:
            if isinstance(outcome, dict):
                outcome["arm"] = "fill_conditioned_router"
        arms["fill_conditioned_router"] = router_arm
    result["schema"] = "hf_fill_conditioned_maker_seeded_probe_v2"
    method = result.setdefault("method", {})
    if isinstance(method, dict):
        method["fill_conditioned_router_live_wired"] = True
        method["fill_conditioned_prior_cutoff_ts"] = PRIOR_CUTOFF_TS
        method["fill_conditioned_prior_source"] = PRIOR_SOURCE
        method["future_current_window_markout_used_for_admission"] = False
        method["rolling_resting_revalidation"] = True
        method["resting_grace_seconds"] = RESTING_GRACE_SECONDS
        method["cancel_latency_seconds"] = CANCEL_LATENCY_SECONDS
        method["resting_revalidation_stats"] = dict(sorted(_REVALIDATION_STATS.items()))
        method["resting_edge_revalidation_scope"] = "flow hazard and toxicity are rolling; quote-edge/book state remains frozen at admission in this research version"
    _ACTIVE_ARGS = None
    return result


def main() -> int:
    seeded.apply_zero_fill_research_profile = apply_fill_conditioned_profile
    batched.gate_inside_improvements = fill_conditioned_gate
    core.consume = rolling_revalidated_consume
    batched.run_batched = run_fill_conditioned
    return seeded.main()


if __name__ == "__main__":
    raise SystemExit(main())
