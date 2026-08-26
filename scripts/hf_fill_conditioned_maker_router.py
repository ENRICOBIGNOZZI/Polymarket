#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CandidateEvidence:
    post_cost_edge: float
    tick_size: float
    queue_ahead: float
    own_shares: float
    recent_trade_count: int
    compatible_sell_prints: int
    compatible_sell_volume: float
    recent_buy_volume: float
    recent_sell_volume: float
    at_touch_fill_probability: float
    at_touch_markout_per_share: float
    inside_fill_probability: float
    inside_markout_per_share: float
    inside_post_cost_edge: float | None = None
    confidence: float = 1.0
    capital_latency_cost_per_share: float = 0.0


@dataclass(frozen=True)
class RouterConfig:
    min_post_cost_edge: float = 0.00005
    min_recent_trades: int = 2
    min_compatible_sell_prints: int = 1
    min_recent_clearance_ratio: float = 0.01
    toxicity_min_trades: int = 4
    max_sell_share: float = 0.90
    min_fill_probability: float = 0.005
    min_inside_confidence: float = 0.80


@dataclass(frozen=True)
class RouterDecision:
    action: str
    reason: str
    recent_clearance_ratio: float
    sell_share: float
    at_touch_ev_per_share: float
    inside_ev_per_share: float
    inside_post_cost_edge: float


@dataclass(frozen=True)
class RestingEvidence:
    age_seconds: float
    current_post_cost_edge: float
    remaining_fill_probability: float
    recent_trade_count: int
    recent_buy_volume: float
    recent_sell_volume: float
    conservative_markout_prior_per_share: float = 0.0
    capital_latency_cost_per_share: float = 0.0


@dataclass(frozen=True)
class RestingConfig:
    grace_seconds: float = 20.0
    min_post_cost_edge: float = 0.00005
    min_remaining_fill_probability: float = 0.005
    toxicity_min_trades: int = 4
    max_sell_share: float = 0.775
    cancel_latency_seconds: float = 1.0


@dataclass(frozen=True)
class RestingDecision:
    action: str
    reason: str
    sell_share: float
    remaining_ev_per_share: float
    cancel_latency_seconds: float


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, float(x)))


def evaluate(candidate: CandidateEvidence, config: RouterConfig = RouterConfig()) -> RouterDecision:
    total_flow = max(0.0, candidate.recent_buy_volume) + max(0.0, candidate.recent_sell_volume)
    sell_share = max(0.0, candidate.recent_sell_volume) / total_flow if total_flow > 0.0 else 0.0
    clearance_denominator = max(1e-12, candidate.queue_ahead + candidate.own_shares)
    clearance = max(0.0, candidate.compatible_sell_volume) / clearance_denominator

    p_touch = _clamp01(candidate.at_touch_fill_probability)
    p_inside = _clamp01(candidate.inside_fill_probability)
    touch_payoff = candidate.post_cost_edge + candidate.at_touch_markout_per_share
    inside_edge = (
        float(candidate.inside_post_cost_edge)
        if candidate.inside_post_cost_edge is not None
        else candidate.post_cost_edge - max(0.0, candidate.tick_size)
    )
    inside_payoff = inside_edge + candidate.inside_markout_per_share
    touch_ev = p_touch * touch_payoff - max(0.0, candidate.capital_latency_cost_per_share)
    inside_ev = p_inside * inside_payoff - max(0.0, candidate.capital_latency_cost_per_share)

    def decision(action: str, reason: str) -> RouterDecision:
        return RouterDecision(action, reason, clearance, sell_share, touch_ev, inside_ev, inside_edge)

    if candidate.post_cost_edge < config.min_post_cost_edge:
        return decision("SKIP", "post_cost_edge_below_floor")
    if candidate.recent_trade_count < config.min_recent_trades:
        return decision("SKIP", "insufficient_recent_activity")
    if candidate.compatible_sell_prints < config.min_compatible_sell_prints:
        return decision("SKIP", "no_compatible_contra_flow")
    if clearance < config.min_recent_clearance_ratio:
        return decision("SKIP", "recent_flow_cannot_clear_queue")
    if candidate.recent_trade_count >= config.toxicity_min_trades and sell_share > config.max_sell_share:
        return decision("SKIP", "directional_sell_flow_too_toxic")
    if p_touch < config.min_fill_probability:
        return decision("SKIP", "calibrated_touch_fill_probability_too_low")
    if touch_ev <= 0.0:
        return decision("SKIP", "nonpositive_touch_fill_conditioned_ev")

    inside_has_edge = inside_edge >= config.min_post_cost_edge
    inside_has_fill_support = p_inside >= config.min_fill_probability
    inside_is_better = inside_ev > touch_ev and inside_ev > 0.0
    recurrent_support = candidate.recent_trade_count >= config.toxicity_min_trades
    confidence_support = candidate.confidence >= config.min_inside_confidence
    if inside_has_edge and inside_has_fill_support and inside_is_better and recurrent_support and confidence_support:
        return decision("IMPROVE_ONE_TICK", "incremental_fill_conditioned_ev_pays_tick")
    return decision("POST_AT_TOUCH", "positive_touch_fill_conditioned_ev")


def evaluate_resting(evidence: RestingEvidence, config: RestingConfig = RestingConfig()) -> RestingDecision:
    """Revalidate an already-resting paper quote from information available now.

    The function is deliberately fail-closed. A quote keeps its queue position during
    the grace interval. Afterwards the remaining order is cancelled when current
    post-cost edge is gone, recurrent contra-flow is directionally toxic, the rolling
    fill hazard has collapsed, or fill-conditioned remaining EV is non-positive.
    Cancellation is not instantaneous: callers must continue replaying public fills
    through ``cancel_latency_seconds`` before removing residual size.
    """
    total_flow = max(0.0, evidence.recent_buy_volume) + max(0.0, evidence.recent_sell_volume)
    sell_share = max(0.0, evidence.recent_sell_volume) / total_flow if total_flow > 0.0 else 0.0
    p_fill = _clamp01(evidence.remaining_fill_probability)
    payoff = evidence.current_post_cost_edge + evidence.conservative_markout_prior_per_share
    remaining_ev = p_fill * payoff - max(0.0, evidence.capital_latency_cost_per_share)

    def result(action: str, reason: str) -> RestingDecision:
        return RestingDecision(action, reason, sell_share, remaining_ev, max(0.0, config.cancel_latency_seconds))

    if evidence.age_seconds < config.grace_seconds:
        return result("KEEP", "grace_interval")
    if evidence.current_post_cost_edge < config.min_post_cost_edge:
        return result("CANCEL_PENDING", "resting_edge_below_floor")
    if evidence.recent_trade_count >= config.toxicity_min_trades and sell_share > config.max_sell_share:
        return result("CANCEL_PENDING", "resting_flow_too_toxic")
    if p_fill < config.min_remaining_fill_probability:
        return result("CANCEL_PENDING", "remaining_fill_probability_too_low")
    if remaining_ev <= 0.0:
        return result("CANCEL_PENDING", "nonpositive_remaining_fill_conditioned_ev")
    return result("KEEP", "positive_remaining_fill_conditioned_ev")


def _load_candidate(path: Path) -> CandidateEvidence:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("candidate JSON must be an object")
    return CandidateEvidence(**data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Research-only maker admission router using fill-conditioned EV")
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    candidate = _load_candidate(args.candidate)
    result = evaluate(candidate)
    payload = {"paper_only": True, "authenticated_execution": False, "candidate": asdict(candidate), "decision": asdict(result)}
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
