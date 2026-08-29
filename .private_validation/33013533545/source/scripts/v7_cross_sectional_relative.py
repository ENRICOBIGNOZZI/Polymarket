#!/usr/bin/env python3
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

import v7_cross_sectional_rank_core as core


@dataclass(frozen=True)
class RelativePairCandidate:
    top_market_id: str
    bottom_market_id: str
    top_event_id: str
    bottom_event_id: str
    horizon_seconds: int
    top_side: str
    bottom_side: str
    top_shares_per_pair_dollar: float
    bottom_shares_per_pair_dollar: float
    common_logit_delta_per_pair_dollar: float
    predicted_top_relative_logit: float
    predicted_bottom_relative_logit: float
    predicted_relative_logit_spread: float
    predicted_gross_markout: float
    round_trip_spread_cost: float
    fees: float
    slippage: float
    capital_cost: float
    adverse_penalty: float
    completed_pair_net_edge: float
    uncertainty_pnl_upper_bound: float
    economic_score: float
    max_pair_notional: float = 0.0


@dataclass(frozen=True)
class JointFillStates:
    both: float
    top_only: float
    bottom_only: float
    none: float

    def validate(self, tol: float = 1e-9) -> None:
        values = (self.both, self.top_only, self.bottom_only, self.none)
        if any((not math.isfinite(x)) or x < -tol or x > 1.0 + tol for x in values):
            raise ValueError("joint fill-state probabilities must lie in [0,1]")
        if abs(sum(values) - 1.0) > tol:
            raise ValueError("joint fill-state probabilities must sum to one")


@dataclass(frozen=True)
class JointExecutionEV:
    ev: float
    full_completion_component: float
    top_only_component: float
    bottom_only_component: float
    capital_latency_cost: float


def probability_logit_delta(p: float) -> float:
    p = core.clamp(float(p), 1e-6, 1.0 - 1e-6)
    return p * (1.0 - p)


def neutral_share_weights(
    top_probability: float,
    bottom_probability: float,
    top_yes_ask: float,
    bottom_no_ask: float,
) -> tuple[float, float, float]:
    """Return share weights that neutralize one common YES-logit mode.

    BUY YES on the top contract has local YES-logit delta +p(1-p), while BUY NO
    on the bottom contract has local YES-logit delta -p(1-p).  Choosing raw
    shares (s_bottom, s_top) makes the two common-mode deltas equal and opposite.
    We then normalize by taker entry cost so one unit of pair notional costs $1.
    """
    s_top = probability_logit_delta(top_probability)
    s_bottom = probability_logit_delta(bottom_probability)
    raw_top = s_bottom
    raw_bottom = s_top
    entry_cost = raw_top * float(top_yes_ask) + raw_bottom * float(bottom_no_ask)
    if not math.isfinite(entry_cost) or entry_cost <= 1e-12:
        raise ValueError("pair entry cost must be positive")
    w_top = raw_top / entry_cost
    w_bottom = raw_bottom / entry_cost
    common_delta = w_top * s_top
    if abs(common_delta - w_bottom * s_bottom) > 1e-10:
        raise AssertionError("relative pair weights failed common-logit neutrality")
    return w_top, w_bottom, common_delta


def first_order_pair_markout(
    top_relative_logit: float,
    bottom_relative_logit: float,
    common_delta: float,
    common_mode_logit: float = 0.0,
) -> float:
    """First-order pair PnL; common mode cancels by construction."""
    top_move = common_mode_logit + float(top_relative_logit)
    bottom_move = common_mode_logit + float(bottom_relative_logit)
    return float(common_delta) * top_move - float(common_delta) * bottom_move


def _book_ok(
    book: core.BookEconomics,
    *,
    now: int,
    min_liquidity: float,
    max_spread: float,
    max_book_age_seconds: int,
    side: str,
) -> bool:
    if not book.authoritative_fee or book.liquidity < min_liquidity:
        return False
    if book.received_ts <= 0 or book.received_ts > now + 5 or now - book.received_ts > max_book_age_seconds:
        return False
    if side == "YES":
        bid, ask = book.yes_bid, book.yes_ask
    else:
        bid, ask = book.no_bid, book.no_ask
    return 0.0 < bid < ask < 1.0 and ask - bid <= max_spread


def completed_pair_candidate(
    top: core.ScoreRow,
    bottom: core.ScoreRow,
    top_book: core.BookEconomics,
    bottom_book: core.BookEconomics,
    *,
    horizon_seconds: int,
    now: int,
    min_liquidity: float,
    max_spread: float,
    slippage_bps_round_trip_leg: float,
    capital_cost_bps_per_hour: float,
    adverse_penalty_bps: float,
    max_book_age_seconds: int,
) -> RelativePairCandidate | None:
    if top.market_id == bottom.market_id:
        return None
    if top.predicted_logit_move <= bottom.predicted_logit_move:
        return None
    if not _book_ok(
        top_book,
        now=now,
        min_liquidity=min_liquidity,
        max_spread=max_spread,
        max_book_age_seconds=max_book_age_seconds,
        side="YES",
    ):
        return None
    if not _book_ok(
        bottom_book,
        now=now,
        min_liquidity=min_liquidity,
        max_spread=max_spread,
        max_book_age_seconds=max_book_age_seconds,
        side="NO",
    ):
        return None

    w_top, w_bottom, common_delta = neutral_share_weights(
        top.probability,
        bottom.probability,
        top_book.yes_ask,
        bottom_book.no_ask,
    )
    relative_spread = top.predicted_logit_move - bottom.predicted_logit_move
    gross = first_order_pair_markout(
        top.predicted_logit_move,
        bottom.predicted_logit_move,
        common_delta,
    )

    # With gross markout expressed from the current side mid, one current spread per
    # leg is a conservative round-trip crossing approximation (half-spread in and
    # half-spread out).  Ledger replay remains authoritative for promotion.
    spread_cost = (
        w_top * (top_book.yes_ask - top_book.yes_bid)
        + w_bottom * (bottom_book.no_ask - bottom_book.no_bid)
    )
    fees = (
        w_top
        * (
            core.fee_per_share(top_book.yes_ask, top_book.fee_rate, top_book.fee_exponent)
            + core.fee_per_share(top_book.yes_bid, top_book.fee_rate, top_book.fee_exponent)
        )
        + w_bottom
        * (
            core.fee_per_share(bottom_book.no_ask, bottom_book.fee_rate, bottom_book.fee_exponent)
            + core.fee_per_share(bottom_book.no_bid, bottom_book.fee_rate, bottom_book.fee_exponent)
        )
    )
    slippage = max(0.0, float(slippage_bps_round_trip_leg)) / 10000.0
    capital = max(0.0, float(capital_cost_bps_per_hour)) / 10000.0 * (horizon_seconds / 3600.0)
    adverse = max(0.0, float(adverse_penalty_bps)) / 10000.0
    net = gross - spread_cost - fees - slippage - capital - adverse
    uncertainty = max(
        1e-8,
        common_delta * (abs(top.sigma_logit) + abs(bottom.sigma_logit)),
    )
    economic_score = net / uncertainty / math.sqrt(max(1.0 / 12.0, horizon_seconds / 3600.0))
    return RelativePairCandidate(
        top_market_id=top.market_id,
        bottom_market_id=bottom.market_id,
        top_event_id=top.event_id,
        bottom_event_id=bottom.event_id,
        horizon_seconds=int(horizon_seconds),
        top_side="YES",
        bottom_side="NO",
        top_shares_per_pair_dollar=w_top,
        bottom_shares_per_pair_dollar=w_bottom,
        common_logit_delta_per_pair_dollar=common_delta,
        predicted_top_relative_logit=top.predicted_logit_move,
        predicted_bottom_relative_logit=bottom.predicted_logit_move,
        predicted_relative_logit_spread=relative_spread,
        predicted_gross_markout=gross,
        round_trip_spread_cost=spread_cost,
        fees=fees,
        slippage=slippage,
        capital_cost=capital,
        adverse_penalty=adverse,
        completed_pair_net_edge=net,
        uncertainty_pnl_upper_bound=uncertainty,
        economic_score=economic_score,
    )


def select_relative_pairs(
    scored: Sequence[core.ScoreRow],
    books: Mapping[str, core.BookEconomics],
    *,
    tail_fraction: float,
    horizon_seconds: int,
    now: int,
    minimum_completed_pair_net_edge: float,
    max_pairs: int,
    maximum_pair_notional_usd: float,
    shadow_sleeve_budget_usd: float,
    one_contract_per_event: bool,
    min_liquidity: float,
    max_spread: float,
    slippage_bps_round_trip_leg: float,
    capital_cost_bps_per_hour: float,
    adverse_penalty_bps: float,
    max_book_age_seconds: int,
) -> list[RelativePairCandidate]:
    if len(scored) < 2:
        return []
    fraction = min(0.49, max(0.01, float(tail_fraction)))
    ordered = sorted(scored, key=lambda row: (row.predicted_logit_move, row.market_id))
    n_tail = max(1, int(len(ordered) * fraction))
    bottoms = ordered[:n_tail]
    tops = list(reversed(ordered[-n_tail:]))

    candidates: list[RelativePairCandidate] = []
    used_markets: set[str] = set()
    used_events: set[str] = set()
    for top in tops:
        if top.market_id in used_markets:
            continue
        for bottom in bottoms:
            if bottom.market_id in used_markets or bottom.market_id == top.market_id:
                continue
            if one_contract_per_event and (
                top.event_id == bottom.event_id
                or top.event_id in used_events
                or bottom.event_id in used_events
            ):
                continue
            top_book = books.get(top.market_id)
            bottom_book = books.get(bottom.market_id)
            if top_book is None or bottom_book is None:
                continue
            candidate = completed_pair_candidate(
                top,
                bottom,
                top_book,
                bottom_book,
                horizon_seconds=horizon_seconds,
                now=now,
                min_liquidity=min_liquidity,
                max_spread=max_spread,
                slippage_bps_round_trip_leg=slippage_bps_round_trip_leg,
                capital_cost_bps_per_hour=capital_cost_bps_per_hour,
                adverse_penalty_bps=adverse_penalty_bps,
                max_book_age_seconds=max_book_age_seconds,
            )
            if candidate is None or candidate.completed_pair_net_edge < minimum_completed_pair_net_edge or candidate.economic_score <= 0.0:
                continue
            candidates.append(candidate)
            used_markets.update((top.market_id, bottom.market_id))
            if one_contract_per_event:
                used_events.update((top.event_id, bottom.event_id))
            break
        if len(candidates) >= max(1, int(max_pairs)):
            break

    if not candidates:
        return []
    ranked = sorted(candidates, key=lambda item: item.economic_score, reverse=True)
    strengths = [max(1e-12, item.economic_score) for item in ranked]
    total_strength = sum(strengths)
    allocated: list[RelativePairCandidate] = []
    for candidate, strength in zip(ranked, strengths):
        notional = min(
            max(0.0, float(maximum_pair_notional_usd)),
            max(0.0, float(shadow_sleeve_budget_usd)) * strength / total_strength,
        )
        allocated.append(replace(candidate, max_pair_notional=notional))
    return allocated


def joint_execution_ev(
    candidate: RelativePairCandidate,
    states: JointFillStates,
    *,
    top_only_unwind_loss_per_pair_dollar: float,
    bottom_only_unwind_loss_per_pair_dollar: float,
    capital_latency_cost_per_pair_dollar: float = 0.0,
) -> JointExecutionEV:
    """Explicit four-state execution EV; never infer joint completion from marginals."""
    states.validate()
    notional = max(0.0, candidate.max_pair_notional)
    full = states.both * candidate.completed_pair_net_edge * notional
    top_only = -states.top_only * max(0.0, top_only_unwind_loss_per_pair_dollar) * notional
    bottom_only = -states.bottom_only * max(0.0, bottom_only_unwind_loss_per_pair_dollar) * notional
    capital = max(0.0, capital_latency_cost_per_pair_dollar) * notional
    return JointExecutionEV(
        ev=full + top_only + bottom_only - capital,
        full_completion_component=full,
        top_only_component=top_only,
        bottom_only_component=bottom_only,
        capital_latency_cost=capital,
    )
