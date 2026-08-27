#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from v7_execution_core import JointStateDistribution, joint_bundle_ev
from v7_execution_ledger import LedgerEvent


class GraphExecutionError(ValueError):
    pass


class Placement(str, Enum):
    JOIN = "JOIN"
    IMPROVE = "IMPROVE"
    CROSS = "CROSS"


class Timing(str, Enum):
    NEAR_SIMULTANEOUS = "NEAR_SIMULTANEOUS"
    SEQUENTIAL = "SEQUENTIAL"


@dataclass(frozen=True)
class PriceLevel:
    price: float
    size: float

    def validate(self) -> None:
        if not math.isfinite(self.price) or not 0.0 < self.price < 1.0:
            raise GraphExecutionError("depth_price_invalid")
        if not math.isfinite(self.size) or self.size <= 0.0:
            raise GraphExecutionError("depth_size_invalid")


@dataclass(frozen=True)
class GraphLeg:
    leg_id: str
    market_id: str
    event_id: str
    token_id: str
    side: str
    weight: float
    bid: float
    ask: float
    tick: float
    queue_ahead: float
    bid_depth: tuple[PriceLevel, ...]
    ask_depth: tuple[PriceLevel, ...]
    fair_exit_price: float
    maker_fee_per_share: float
    taker_fee_per_share: float
    exit_fee_per_share: float
    fee_source: str
    adverse_markout_per_share: float
    exchange_ts_ms: int
    receive_ts_ms: int
    book_snapshot_id: str
    min_order: float = 1.0

    def validate(self) -> None:
        if not self.leg_id or not self.market_id or not self.event_id or not self.token_id:
            raise GraphExecutionError("leg_identity_missing")
        if self.side.upper() not in {"BUY", "SELL"}:
            raise GraphExecutionError("leg_side_invalid")
        if not math.isfinite(self.weight) or self.weight <= 0.0:
            raise GraphExecutionError("leg_weight_invalid")
        if not (0.0 < self.bid <= self.ask < 1.0):
            raise GraphExecutionError("book_invalid")
        if not math.isfinite(self.tick) or self.tick <= 0.0:
            raise GraphExecutionError("tick_invalid")
        if not math.isfinite(self.queue_ahead) or self.queue_ahead < 0.0:
            raise GraphExecutionError("queue_invalid")
        if not (0.0 < self.fair_exit_price < 1.0):
            raise GraphExecutionError("fair_exit_invalid")
        if not self.fee_source.strip():
            raise GraphExecutionError("authoritative_fee_source_missing")
        for value in (
            self.maker_fee_per_share,
            self.taker_fee_per_share,
            self.exit_fee_per_share,
            self.adverse_markout_per_share,
        ):
            if not math.isfinite(value) or value < 0.0:
                raise GraphExecutionError("cost_invalid")
        if self.exchange_ts_ms <= 0 or self.receive_ts_ms <= 0:
            raise GraphExecutionError("clock_missing")
        if not self.book_snapshot_id:
            raise GraphExecutionError("snapshot_missing")
        if not math.isfinite(self.min_order) or self.min_order <= 0.0:
            raise GraphExecutionError("min_order_invalid")
        for level in self.bid_depth + self.ask_depth:
            level.validate()


@dataclass(frozen=True)
class ActionPlan:
    name: str
    placements: tuple[Placement, ...]
    timing: Timing = Timing.NEAR_SIMULTANEOUS
    improve_ticks: int = 1

    def validate(self, leg_count: int) -> None:
        if not self.name:
            raise GraphExecutionError("plan_name_missing")
        if len(self.placements) != leg_count:
            raise GraphExecutionError("plan_leg_count_mismatch")
        if self.improve_ticks < 0:
            raise GraphExecutionError("improve_ticks_invalid")


@dataclass(frozen=True)
class EntryQuote:
    price: float
    fee_per_share: float
    depth_capacity: float
    depth_slippage_per_share: float
    action: Placement


@dataclass(frozen=True)
class StateEconomics:
    pnl: float
    explicit_cost: float
    partial_unwind: bool


@dataclass(frozen=True)
class PlanEvaluation:
    plan: ActionPlan
    units: float
    capital_required: float
    completion_probability: float
    expected_ev: float
    stress_ev: Mapping[float, float]
    state_pnl_1x: Mapping[int, float]
    state_cost_1x: Mapping[int, float]
    queue_used_for_capacity: bool = False


def _vwap(levels: Sequence[PriceLevel], shares: float) -> tuple[float, float]:
    if shares <= 0.0:
        raise GraphExecutionError("shares_must_be_positive")
    remaining = float(shares)
    cash = 0.0
    filled = 0.0
    for level in levels:
        level.validate()
        take = min(remaining, level.size)
        cash += take * level.price
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            break
    if filled + 1e-12 < shares:
        raise GraphExecutionError("insufficient_executable_depth")
    return cash / filled, filled


def _depth_total(levels: Sequence[PriceLevel]) -> float:
    return sum(level.size for level in levels)


def _entry_quote(leg: GraphLeg, placement: Placement, shares: float) -> EntryQuote:
    leg.validate()
    if shares <= 0.0:
        raise GraphExecutionError("shares_must_be_positive")
    side = leg.side.upper()
    if placement == Placement.CROSS:
        levels = leg.ask_depth if side == "BUY" else leg.bid_depth
        vwap, _ = _vwap(levels, shares)
        touch = leg.ask if side == "BUY" else leg.bid
        slip = abs(vwap - touch)
        return EntryQuote(vwap, leg.taker_fee_per_share, _depth_total(levels), slip, placement)

    if side == "BUY":
        price = leg.bid
        if placement == Placement.IMPROVE:
            price = min(leg.ask - 1e-12, leg.bid + leg.tick)
    else:
        price = leg.ask
        if placement == Placement.IMPROVE:
            price = max(leg.bid + 1e-12, leg.ask - leg.tick)
    if not 0.0 < price < 1.0:
        raise GraphExecutionError("maker_quote_invalid")
    return EntryQuote(price, leg.maker_fee_per_share, math.inf, 0.0, placement)


def _unwind_quote(leg: GraphLeg, shares: float) -> tuple[float, float]:
    side = leg.side.upper()
    levels = leg.bid_depth if side == "BUY" else leg.ask_depth
    price, _ = _vwap(levels, shares)
    touch = leg.bid if side == "BUY" else leg.ask
    return price, abs(price - touch)


def validate_snapshot_coherence(
    legs: Sequence[GraphLeg],
    *,
    decision_ts_ms: int,
    max_receive_age_ms: int,
    max_exchange_skew_ms: int,
    max_receive_skew_ms: int,
) -> None:
    if not legs:
        raise GraphExecutionError("no_legs")
    if decision_ts_ms <= 0:
        raise GraphExecutionError("decision_clock_missing")
    for leg in legs:
        leg.validate()
        if decision_ts_ms < leg.receive_ts_ms:
            raise GraphExecutionError("decision_before_receive")
        if decision_ts_ms - leg.receive_ts_ms > max_receive_age_ms:
            raise GraphExecutionError("stale_book")
    exchange = [leg.exchange_ts_ms for leg in legs]
    received = [leg.receive_ts_ms for leg in legs]
    if max(exchange) - min(exchange) > max_exchange_skew_ms:
        raise GraphExecutionError("cross_leg_exchange_skew")
    if max(received) - min(received) > max_receive_skew_ms:
        raise GraphExecutionError("cross_leg_receive_skew")


def queue_decoupled_capacity(
    legs: Sequence[GraphLeg],
    plan: ActionPlan,
    distribution: JointStateDistribution,
    *,
    risk_capital: float,
    unwind_depth_fraction: float = 0.25,
    min_joint_completion_for_full_size: float = 0.50,
) -> float:
    """Capacity uses risk, executable entry/unwind depth and joint completion.

    Queue ahead is deliberately absent. It belongs in the empirically learned
    joint state distribution, never in the capital-capacity equation.
    """
    plan.validate(len(legs))
    distribution.validate()
    if distribution.leg_count != len(legs):
        raise GraphExecutionError("distribution_leg_count_mismatch")
    if not math.isfinite(risk_capital) or risk_capital <= 0.0:
        return 0.0
    if not 0.0 < unwind_depth_fraction <= 1.0:
        raise GraphExecutionError("unwind_depth_fraction_invalid")
    if not 0.0 < min_joint_completion_for_full_size <= 1.0:
        raise GraphExecutionError("min_joint_completion_for_full_size_invalid")

    per_unit_capital = 0.0
    max_units = math.inf
    for leg, placement in zip(legs, plan.placements):
        leg.validate()
        one_share = max(leg.min_order, leg.weight)
        quote = _entry_quote(leg, placement, one_share)
        per_unit_capital += leg.weight * max(1e-12, quote.price + quote.fee_per_share)
        if placement == Placement.CROSS:
            max_units = min(max_units, quote.depth_capacity / leg.weight)
        unwind_levels = leg.bid_depth if leg.side.upper() == "BUY" else leg.ask_depth
        max_units = min(
            max_units,
            unwind_depth_fraction * _depth_total(unwind_levels) / leg.weight,
        )

    if per_unit_capital <= 0.0:
        return 0.0
    max_units = min(max_units, risk_capital / per_unit_capital)
    p_complete = float(distribution.probabilities[distribution.full_mask])
    completion_scale = min(1.0, p_complete / min_joint_completion_for_full_size)
    max_units *= completion_scale
    if not math.isfinite(max_units) or max_units <= 0.0:
        return 0.0
    for leg in legs:
        if max_units * leg.weight + 1e-12 < leg.min_order:
            return 0.0
    return max_units


def _state_economics(
    legs: Sequence[GraphLeg],
    plan: ActionPlan,
    *,
    units: float,
    state: int,
    stress_multiplier: float,
    explicit_partial_unwind_penalty: float,
) -> StateEconomics:
    if stress_multiplier < 1.0:
        raise GraphExecutionError("stress_multiplier_below_one")
    full_mask = (1 << len(legs)) - 1
    is_partial = state not in {0, full_mask}
    pnl = 0.0
    explicit_cost = 0.0
    for index, (leg, placement) in enumerate(zip(legs, plan.placements)):
        if not (state & (1 << index)):
            continue
        shares = units * leg.weight
        entry = _entry_quote(leg, placement, shares)
        if leg.side.upper() == "BUY":
            entry_cash = -shares * entry.price
        else:
            entry_cash = shares * entry.price
        pnl += entry_cash
        entry_fee = shares * entry.fee_per_share
        entry_depth_cost = shares * entry.depth_slippage_per_share
        adverse = shares * leg.adverse_markout_per_share
        explicit_cost += entry_fee + entry_depth_cost + adverse

        if is_partial:
            exit_price, exit_depth_slip = _unwind_quote(leg, shares)
            if leg.side.upper() == "BUY":
                pnl += shares * exit_price
            else:
                pnl -= shares * exit_price
            exit_fee = shares * leg.exit_fee_per_share
            exit_depth_cost = shares * exit_depth_slip
            explicit_cost += exit_fee + exit_depth_cost
        else:
            if leg.side.upper() == "BUY":
                pnl += shares * leg.fair_exit_price
            else:
                pnl -= shares * leg.fair_exit_price

    if is_partial:
        explicit_cost += max(0.0, explicit_partial_unwind_penalty)
    pnl -= explicit_cost * stress_multiplier
    return StateEconomics(pnl, explicit_cost, is_partial)


def evaluate_plan(
    legs: Sequence[GraphLeg],
    plan: ActionPlan,
    distribution: JointStateDistribution,
    *,
    decision_ts_ms: int,
    risk_capital: float,
    max_receive_age_ms: int = 1000,
    max_exchange_skew_ms: int = 1000,
    max_receive_skew_ms: int = 1000,
    unwind_depth_fraction: float = 0.25,
    min_joint_completion_for_full_size: float = 0.50,
    min_joint_observations: int = 20,
    capital_time_rate_per_second: float = 0.0,
    expected_capital_seconds: float = 0.0,
    expected_latency_seconds: float = 0.0,
    explicit_partial_unwind_penalty: float = 0.0,
    cost_stress: Sequence[float] = (1.0, 1.5, 2.0),
) -> PlanEvaluation:
    plan.validate(len(legs))
    distribution.validate()
    if distribution.leg_count != len(legs):
        raise GraphExecutionError("distribution_leg_count_mismatch")
    if distribution.observations < min_joint_observations:
        raise GraphExecutionError("insufficient_joint_state_observations")
    validate_snapshot_coherence(
        legs,
        decision_ts_ms=decision_ts_ms,
        max_receive_age_ms=max_receive_age_ms,
        max_exchange_skew_ms=max_exchange_skew_ms,
        max_receive_skew_ms=max_receive_skew_ms,
    )
    units = queue_decoupled_capacity(
        legs,
        plan,
        distribution,
        risk_capital=risk_capital,
        unwind_depth_fraction=unwind_depth_fraction,
        min_joint_completion_for_full_size=min_joint_completion_for_full_size,
    )
    if units <= 0.0:
        raise GraphExecutionError("zero_executable_capacity")

    capital_required = 0.0
    for leg, placement in zip(legs, plan.placements):
        shares = units * leg.weight
        entry = _entry_quote(leg, placement, shares)
        capital_required += shares * (entry.price + entry.fee_per_share)

    stress_ev: dict[float, float] = {}
    state_pnl_1x: dict[int, float] = {}
    state_cost_1x: dict[int, float] = {}
    for multiplier in cost_stress:
        multiplier = float(multiplier)
        state_pnl: dict[int, float] = {}
        for state in distribution.probabilities:
            econ = _state_economics(
                legs,
                plan,
                units=units,
                state=int(state),
                stress_multiplier=multiplier,
                explicit_partial_unwind_penalty=explicit_partial_unwind_penalty,
            )
            state_pnl[int(state)] = econ.pnl
            if abs(multiplier - 1.0) <= 1e-12:
                state_pnl_1x[int(state)] = econ.pnl
                state_cost_1x[int(state)] = econ.explicit_cost
        bundle = joint_bundle_ev(
            distribution,
            state_pnl,
            capital_usd=capital_required,
            capital_time_rate_per_second=max(0.0, capital_time_rate_per_second),
            expected_latency_seconds=max(0.0, expected_capital_seconds + expected_latency_seconds),
        )
        extra_capital = (multiplier - 1.0) * bundle.capital_latency_cost
        stress_ev[multiplier] = bundle.expected_value - extra_capital

    if 1.0 not in stress_ev:
        raise GraphExecutionError("cost_stress_requires_one_x")
    return PlanEvaluation(
        plan=plan,
        units=units,
        capital_required=capital_required,
        completion_probability=float(distribution.probabilities[distribution.full_mask]),
        expected_ev=stress_ev[1.0],
        stress_ev=stress_ev,
        state_pnl_1x=state_pnl_1x,
        state_cost_1x=state_cost_1x,
        queue_used_for_capacity=False,
    )


def select_best_plan(
    legs: Sequence[GraphLeg],
    plans: Sequence[ActionPlan],
    distributions: Mapping[str, JointStateDistribution],
    *,
    decision_ts_ms: int,
    risk_capital: float,
    require_positive_all_stresses: bool = True,
    **kwargs: object,
) -> PlanEvaluation | None:
    """Choose by total executable EV, never by quoted edge or marginal fills."""
    evaluations: list[PlanEvaluation] = []
    for plan in plans:
        distribution = distributions.get(plan.name)
        if distribution is None:
            continue
        try:
            evaluation = evaluate_plan(
                legs,
                plan,
                distribution,
                decision_ts_ms=decision_ts_ms,
                risk_capital=risk_capital,
                **kwargs,
            )
        except (GraphExecutionError, ValueError):
            continue
        if evaluation.expected_ev <= 0.0:
            continue
        if require_positive_all_stresses and any(value <= 0.0 for value in evaluation.stress_ev.values()):
            continue
        evaluations.append(evaluation)
    return max(evaluations, key=lambda value: value.expected_ev, default=None)


def default_two_leg_plans() -> tuple[ActionPlan, ...]:
    return (
        ActionPlan("maker_maker_join", (Placement.JOIN, Placement.JOIN)),
        ActionPlan("maker_maker_improve", (Placement.IMPROVE, Placement.IMPROVE)),
        ActionPlan("maker_taker", (Placement.JOIN, Placement.CROSS)),
        ActionPlan("taker_maker", (Placement.CROSS, Placement.JOIN)),
        ActionPlan("taker_taker", (Placement.CROSS, Placement.CROSS)),
        ActionPlan("sequential_maker_taker", (Placement.JOIN, Placement.CROSS), Timing.SEQUENTIAL),
        ActionPlan("sequential_taker_maker", (Placement.CROSS, Placement.JOIN), Timing.SEQUENTIAL),
    )


def _bundle_snapshot_id(legs: Sequence[GraphLeg]) -> str:
    raw = "|".join(sorted(f"{leg.leg_id}:{leg.book_snapshot_id}" for leg in legs))
    return "graph:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_candidate_ledger_events(
    legs: Sequence[GraphLeg],
    evaluation: PlanEvaluation,
    distribution: JointStateDistribution,
    *,
    model_sha: str,
    decision_ts_ms: int,
    opportunity_id: str,
    candidate_id: str,
    bundle_id: str,
) -> list[LedgerEvent]:
    """Build records; the canonical runtime remains the only ledger writer."""
    if not opportunity_id or not candidate_id or not bundle_id:
        raise GraphExecutionError("ledger_identity_missing")
    distribution.validate()
    if distribution.leg_count != len(legs):
        raise GraphExecutionError("distribution_leg_count_mismatch")
    snapshot_id = _bundle_snapshot_id(legs)
    exchange_ts_ms = min(leg.exchange_ts_ms for leg in legs)
    receive_ts_ms = max(leg.receive_ts_ms for leg in legs)
    metadata = {
        "plan": evaluation.plan.name,
        "timing": evaluation.plan.timing.value,
        "required_legs": [
            {"leg_id": leg.leg_id, "token_id": leg.token_id, "target_size": evaluation.units * leg.weight}
            for leg in legs
        ],
        "joint_state_probabilities": {str(k): float(v) for k, v in distribution.probabilities.items()},
        "joint_state_observations": int(distribution.observations),
        "state_pnl_1x": {str(k): float(v) for k, v in evaluation.state_pnl_1x.items()},
        "state_cost_1x": {str(k): float(v) for k, v in evaluation.state_cost_1x.items()},
        "stress_ev": {str(k): float(v) for k, v in evaluation.stress_ev.items()},
        "queue_used_for_capacity": False,
        "capacity_contract": "risk+entry_depth+unwind_depth+empirical_joint_completion",
    }
    bundle_event = LedgerEvent(
        event_type="CANDIDATE",
        strategy="GRAPH_RV",
        model_sha=model_sha,
        opportunity_id=opportunity_id,
        candidate_id=candidate_id,
        bundle_id=bundle_id,
        event_id=legs[0].event_id,
        decision_ts_ms=decision_ts_ms,
        exchange_ts_ms=exchange_ts_ms,
        receive_ts_ms=receive_ts_ms,
        book_snapshot_id=snapshot_id,
        predicted_fill_probability=evaluation.completion_probability,
        expected_ev=evaluation.expected_ev,
        intended_action=evaluation.plan.name,
        intended_size=evaluation.units,
        metadata=metadata,
    )
    bundle_event.validate()
    result = [bundle_event]
    for leg, placement in zip(legs, evaluation.plan.placements):
        shares = evaluation.units * leg.weight
        entry = _entry_quote(leg, placement, shares)
        leg_event = LedgerEvent(
            event_type="CANDIDATE",
            strategy="GRAPH_RV",
            model_sha=model_sha,
            opportunity_id=opportunity_id,
            candidate_id=candidate_id,
            bundle_id=bundle_id,
            leg_id=leg.leg_id,
            market_id=leg.market_id,
            event_id=leg.event_id,
            token_id=leg.token_id,
            decision_ts_ms=decision_ts_ms,
            exchange_ts_ms=leg.exchange_ts_ms,
            receive_ts_ms=leg.receive_ts_ms,
            book_snapshot_id=leg.book_snapshot_id,
            side=leg.side.upper(),
            bid=leg.bid,
            ask=leg.ask,
            bid_depth=_depth_total(leg.bid_depth),
            ask_depth=_depth_total(leg.ask_depth),
            queue_ahead=leg.queue_ahead,
            limit_price=entry.price,
            predicted_fill_probability=evaluation.completion_probability,
            expected_ev=evaluation.expected_ev,
            intended_action=placement.value,
            intended_size=shares,
            fee=shares * entry.fee_per_share,
            fee_source=leg.fee_source,
            slippage=shares * entry.depth_slippage_per_share,
            metadata={
                "bundle_plan": evaluation.plan.name,
                "queue_used_for_capacity": False,
                "joint_completion_probability": evaluation.completion_probability,
            },
        )
        leg_event.validate()
        result.append(leg_event)
    return result
