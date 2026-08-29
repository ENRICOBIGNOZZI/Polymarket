#!/usr/bin/env python3
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence


def clamp(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, float(x)))


@dataclass(frozen=True)
class TapeTrade:
    trade_id: str
    token: str
    side: str
    price: float
    size: float
    event_ts_ms: int
    received_ms: int


@dataclass(frozen=True)
class RestingOrder:
    order_id: str
    token: str
    side: str
    limit_price: float
    queue_ahead: float
    remaining: float
    arrival_received_ms: int
    arrival_event_ts_ms: int


@dataclass(frozen=True)
class MakerState:
    side: str
    limit_price: float
    fair_exit_price: float
    queue_ahead: float
    own_size: float
    compatible_flow: float
    flow_horizon_seconds: float
    ofi: float
    imbalance: float
    microprice: float
    midpoint: float
    displayed_depth: float
    entry_fee_per_share: float
    exit_fee_per_share: float
    slippage_per_share: float
    adverse_markout_per_share: float
    partial_unwind_loss_per_share: float
    expected_partial_fraction: float
    capital_usd: float
    capital_time_rate_per_second: float
    expected_rest_seconds: float
    latency_seconds: float = 0.0


@dataclass(frozen=True)
class MakerEV:
    fill_probability: float
    conditional_net_pnl_per_share: float
    expected_partial_unwind_loss: float
    capital_latency_cost: float
    expected_value: float
    toxicity_score: float


@dataclass(frozen=True)
class JointStateDistribution:
    """Empirical same-window state distribution keyed by fill bitmask.

    For n legs the full-completion state is (1 << n) - 1. Probabilities are
    measured jointly; callers must not manufacture them from marginal products.
    """
    leg_count: int
    probabilities: Mapping[int, float]
    observations: int = 0

    @property
    def full_mask(self) -> int:
        return (1 << self.leg_count) - 1

    def validate(self, tol: float = 1e-9) -> None:
        if self.leg_count < 2:
            raise ValueError("joint state distribution requires at least two legs")
        expected = set(range(1 << self.leg_count))
        actual = set(int(k) for k in self.probabilities)
        if actual != expected:
            raise ValueError("joint state distribution must explicitly enumerate every state")
        total = 0.0
        for state, probability in self.probabilities.items():
            if int(state) < 0 or int(state) > self.full_mask:
                raise ValueError("invalid state bitmask")
            value = float(probability)
            # Fréchet-bound constructions can produce tiny signed round-off at
            # exact boundaries (for example -1e-16 for a mathematically zero
            # state).  Accept only numerical noise within the same tolerance
            # already used for the unit-sum check; materially negative or >1
            # probabilities still fail closed.
            if not math.isfinite(value) or value < -tol or value > 1.0 + tol:
                raise ValueError("invalid state probability")
            total += value
        if abs(total - 1.0) > tol:
            raise ValueError(f"joint state probabilities sum to {total}, not one")


@dataclass(frozen=True)
class JointBundleEV:
    expected_value: float
    full_completion_component: float
    partial_state_component: float
    none_component: float
    capital_latency_cost: float


def _toxicity(state: MakerState) -> float:
    side = state.side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("maker side must be BUY or SELL")
    sign = 1.0 if side == "BUY" else -1.0
    depth = max(1e-9, state.displayed_depth)
    micro_shift = sign * (state.microprice - state.midpoint) / max(1e-6, abs(state.midpoint))
    raw = -(sign * state.ofi) - (sign * state.imbalance) - 4.0 * micro_shift
    raw += min(2.0, state.queue_ahead / depth)
    return clamp(0.5 + 0.20 * raw, 0.0, 1.0)


def maker_fill_hazard(state: MakerState) -> float:
    """Capacity/queue fill proxy penalized for adverse-selection state."""
    burden = max(1e-9, state.queue_ahead + state.own_size)
    flow = max(0.0, state.compatible_flow)
    base = 1.0 - math.exp(-(flow / burden))
    return clamp(base * (1.0 - 0.65 * _toxicity(state)), 0.0, 1.0)


def maker_fill_conditioned_ev(state: MakerState) -> MakerEV:
    p_fill = maker_fill_hazard(state)
    toxicity = _toxicity(state)
    side_sign = 1.0 if state.side.upper() == "BUY" else -1.0
    gross_alpha = side_sign * (state.fair_exit_price - state.limit_price)
    conditional = (
        gross_alpha
        - max(0.0, state.entry_fee_per_share)
        - max(0.0, state.exit_fee_per_share)
        - max(0.0, state.slippage_per_share)
        - max(0.0, state.adverse_markout_per_share)
    )
    partial_probability = clamp(state.expected_partial_fraction, 0.0, 1.0) * (1.0 - p_fill)
    partial_loss = partial_probability * max(0.0, state.partial_unwind_loss_per_share) * max(0.0, state.own_size)
    capital_time = max(0.0, state.capital_usd) * max(0.0, state.capital_time_rate_per_second) * max(
        0.0, state.expected_rest_seconds + state.latency_seconds
    )
    ev = p_fill * conditional * max(0.0, state.own_size) - partial_loss - capital_time
    return MakerEV(p_fill, conditional, partial_loss, capital_time, ev, toxicity)


def quote_improvement_is_economic(current: MakerState, improved: MakerState, tick_cost_usd: float = 0.0) -> bool:
    current_ev = maker_fill_conditioned_ev(current).expected_value
    improved_ev = maker_fill_conditioned_ev(improved).expected_value
    return improved_ev - current_ev > max(0.0, tick_cost_usd) and improved_ev > 0.0


def joint_bundle_ev(
    distribution: JointStateDistribution,
    state_net_pnl: Mapping[int, float],
    *,
    capital_usd: float = 0.0,
    capital_time_rate_per_second: float = 0.0,
    expected_latency_seconds: float = 0.0,
) -> JointBundleEV:
    distribution.validate()
    missing = set(distribution.probabilities) - set(int(k) for k in state_net_pnl)
    if missing:
        raise ValueError(f"missing explicit unwind/settlement economics for states {sorted(missing)}")
    full = distribution.full_mask
    full_component = float(distribution.probabilities[full]) * float(state_net_pnl[full])
    none_component = float(distribution.probabilities[0]) * float(state_net_pnl[0])
    partial_component = sum(
        float(probability) * float(state_net_pnl[state])
        for state, probability in distribution.probabilities.items()
        if state not in {0, full}
    )
    capital_cost = max(0.0, capital_usd) * max(0.0, capital_time_rate_per_second) * max(0.0, expected_latency_seconds)
    return JointBundleEV(full_component + partial_component + none_component - capital_cost, full_component, partial_component, none_component, capital_cost)


def trade_known_by(trade: TapeTrade, decision_received_ms: int) -> bool:
    return int(trade.received_ms) <= int(decision_received_ms)


def trade_in_event_window(trade: TapeTrade, start_event_ts_ms: int, end_event_ts_ms: int) -> bool:
    return int(start_event_ts_ms) <= int(trade.event_ts_ms) <= int(end_event_ts_ms)


def forward_fill_eligible(trade: TapeTrade, order: RestingOrder, deadline_received_ms: int, deadline_event_ts_ms: int) -> bool:
    """A REST-backfilled old print can never fill a newly arrived paper order."""
    if int(trade.received_ms) <= int(order.arrival_received_ms):
        return False
    if int(trade.event_ts_ms) <= int(order.arrival_event_ts_ms):
        return False
    return int(trade.received_ms) <= int(deadline_received_ms) and int(trade.event_ts_ms) <= int(deadline_event_ts_ms)


def allocate_shared_trade_capacity(trade: TapeTrade, orders: Sequence[RestingOrder]) -> dict[str, float]:
    """Consume one public trade exactly once across same-token own orders."""
    remaining_trade = max(0.0, float(trade.size))
    fills: dict[str, float] = {order.order_id: 0.0 for order in orders}
    for order in sorted(orders, key=lambda item: (item.arrival_received_ms, item.order_id)):
        if remaining_trade <= 0.0:
            break
        if order.token != trade.token:
            continue
        if order.side.upper() == "BUY" and trade.side.upper() != "SELL":
            continue
        if order.side.upper() == "SELL" and trade.side.upper() != "BUY":
            continue
        if order.side.upper() == "BUY" and trade.price > order.limit_price + 1e-12:
            continue
        if order.side.upper() == "SELL" and trade.price < order.limit_price - 1e-12:
            continue
        queue_used = min(max(0.0, order.queue_ahead), remaining_trade)
        remaining_trade -= queue_used
        if remaining_trade <= 0.0:
            continue
        own = min(max(0.0, order.remaining), remaining_trade)
        fills[order.order_id] = own
        remaining_trade -= own
    if sum(fills.values()) > max(0.0, float(trade.size)) + 1e-9:
        raise AssertionError("shared public-flow conservation violated")
    return fills


def structural_terminal_floor(
    payout_matrix: Sequence[Sequence[float]],
    filled_shares: Sequence[float],
    entry_costs: Sequence[float],
    *,
    fees_and_slippage: float = 0.0,
    residual_unwind_loss: float = 0.0,
) -> float:
    if len(filled_shares) != len(entry_costs):
        raise ValueError("share/cost dimensions differ")
    if not payout_matrix:
        raise ValueError("terminal outcomes required")
    n = len(filled_shares)
    if any(len(row) != n for row in payout_matrix):
        raise ValueError("payout matrix dimension differs from legs")
    total_entry = sum(max(0.0, float(c)) for c in entry_costs)
    outcomes = [
        sum(float(payout) * max(0.0, float(shares)) for payout, shares in zip(row, filled_shares))
        - total_entry - max(0.0, fees_and_slippage) - max(0.0, residual_unwind_loss)
        for row in payout_matrix
    ]
    return min(outcomes)


def economically_complete(
    payout_matrix: Sequence[Sequence[float]],
    filled_shares: Sequence[float],
    entry_costs: Sequence[float],
    *,
    fees_and_slippage: float = 0.0,
    residual_unwind_loss: float = 0.0,
    minimum_terminal_pnl: float = 0.0,
) -> bool:
    return structural_terminal_floor(
        payout_matrix,
        filled_shares,
        entry_costs,
        fees_and_slippage=fees_and_slippage,
        residual_unwind_loss=residual_unwind_loss,
    ) > float(minimum_terminal_pnl)
