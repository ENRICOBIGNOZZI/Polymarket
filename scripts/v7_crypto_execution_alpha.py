#!/usr/bin/env python3
"""Unified conservative MAKE/TAKE/CANCEL/NOTHING valuation for CRYPTO_SETTLEMENT_ENGINE.

This module owns no OMS, signer, inventory, capital, ledger or venue access. It
turns one causal settlement-fair/market/execution snapshot into directly
comparable action values and an explicit PnL attribution. Runtime consumers may
publish the resulting proposals, but this file cannot authorize execution.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable


SCHEMA = "polymarket_v7_crypto_execution_alpha_v1"
ACTIONS = ("MAKE", "TAKE", "CANCEL", "NOTHING")
OUTCOMES = ("YES", "NO")
ATTRIBUTION_FIELDS = (
    "settlement_alpha", "spread_capture", "rebate", "fees", "slippage",
    "adverse_selection", "latency", "inventory", "unwind", "cancel", "capital",
)


class ExecutionAlphaError(ValueError):
    pass


def finite(value: Any, default: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        if default is None:
            raise ExecutionAlphaError("nonfinite")
        return float(default)
    if not math.isfinite(number):
        if default is None:
            raise ExecutionAlphaError("nonfinite")
        return float(default)
    return number


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, finite(value)))


def fee_per_share(price: float, schedule: dict[str, Any], *, taker: bool) -> float:
    """Exact checked-in Polymarket fee functional form used by the PAPER router."""
    price = finite(price)
    if not 0.0 < price < 1.0:
        return math.inf
    try:
        rate = finite(schedule["rate"])
        exponent = finite(schedule["exponent"])
    except (KeyError, ExecutionAlphaError):
        return math.inf
    if rate < 0.0 or exponent < 0.0:
        return math.inf
    if not taker and bool(schedule.get("takerOnly", True)):
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


@dataclass(frozen=True)
class OutcomeBook:
    outcome: str
    token_id: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    def validate(self) -> None:
        if self.outcome not in OUTCOMES or not self.token_id:
            raise ExecutionAlphaError("book_identity")
        if not 0.0 <= self.bid < self.ask <= 1.0:
            raise ExecutionAlphaError("book_crossed_or_invalid")
        if self.bid_size < 0.0 or self.ask_size < 0.0:
            raise ExecutionAlphaError("book_depth")

    @property
    def midpoint(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class MakerEvidence:
    reach_probability_lower: float
    fill_given_reach_probability_lower: float
    fill_probability_point: float
    adverse_markout_upper_per_share: float
    toxic_fill_probability_upper: float
    rebate_per_share: float
    rebate_authoritative: bool
    cancel_latency_risk_per_share: float
    inventory_cost_per_share: float
    cancel_cost_per_quote: float
    capital_cost_per_quote: float
    mature: bool

    def validate(self) -> None:
        for value in (
            self.reach_probability_lower, self.fill_given_reach_probability_lower,
            self.fill_probability_point, self.toxic_fill_probability_upper,
        ):
            if not 0.0 <= value <= 1.0:
                raise ExecutionAlphaError("maker_probability")
        for value in (
            self.adverse_markout_upper_per_share, self.rebate_per_share,
            self.cancel_latency_risk_per_share, self.inventory_cost_per_share,
            self.cancel_cost_per_quote, self.capital_cost_per_quote,
        ):
            if value < 0.0 or not math.isfinite(value):
                raise ExecutionAlphaError("maker_cost")
        if not self.rebate_authoritative and abs(self.rebate_per_share) > 1e-12:
            raise ExecutionAlphaError("unauthoritative_rebate")

    @property
    def fill_probability_lower(self) -> float:
        return clamp(
            self.reach_probability_lower * self.fill_given_reach_probability_lower,
            0.0, 1.0,
        )


@dataclass(frozen=True)
class TakerEvidence:
    fill_probability_lower: float
    slippage_per_share: float
    latency_risk_per_share: float
    unwind_loss_per_share: float
    capital_cost_per_trade: float
    mature: bool

    def validate(self) -> None:
        if not 0.0 <= self.fill_probability_lower <= 1.0:
            raise ExecutionAlphaError("taker_probability")
        for value in (
            self.slippage_per_share, self.latency_risk_per_share,
            self.unwind_loss_per_share, self.capital_cost_per_trade,
        ):
            if value < 0.0 or not math.isfinite(value):
                raise ExecutionAlphaError("taker_cost")


@dataclass(frozen=True)
class CancelEvidence:
    signal_active: bool
    mandatory_risk_cancel: bool
    quote_size: float
    avoidable_fill_probability_lower: float
    avoided_adverse_loss_lower_per_share: float
    cancel_cost: float
    mature: bool

    def validate(self) -> None:
        if self.quote_size < 0.0 or self.cancel_cost < 0.0:
            raise ExecutionAlphaError("cancel_cost_or_size")
        if not 0.0 <= self.avoidable_fill_probability_lower <= 1.0:
            raise ExecutionAlphaError("cancel_probability")
        if self.avoided_adverse_loss_lower_per_share < 0.0:
            raise ExecutionAlphaError("cancel_avoided_loss")


@dataclass(frozen=True)
class MarketState:
    market_id: str
    event_id: str
    asset: str
    horizon: str
    fair_lower_yes: float
    fair_point_yes: float
    fair_upper_yes: float
    yes: OutcomeBook
    no: OutcomeBook
    fee_schedule: dict[str, Any]
    maker: MakerEvidence
    taker: TakerEvidence
    cancel: CancelEvidence
    target_size: float
    tte_seconds: float
    settlement_verified: bool
    fair_mature: bool
    source_snapshot_identity: str

    def validate(self) -> None:
        if not self.market_id or not self.event_id or not self.source_snapshot_identity:
            raise ExecutionAlphaError("market_identity")
        if self.asset not in {"BTC", "ETH", "SOL", "XRP"}:
            raise ExecutionAlphaError("asset")
        if self.horizon not in {"M1", "M5", "M15", "H1", "H4"}:
            raise ExecutionAlphaError("horizon")
        if not 0.0 <= self.fair_lower_yes <= self.fair_point_yes <= self.fair_upper_yes <= 1.0:
            raise ExecutionAlphaError("fair_bounds")
        self.yes.validate(); self.no.validate(); self.maker.validate(); self.taker.validate(); self.cancel.validate()
        if self.target_size <= 0.0 or self.tte_seconds < 0.0:
            raise ExecutionAlphaError("target_or_tte")


@dataclass(frozen=True)
class ActionCandidate:
    action: str
    outcome: str
    token_id: str
    price: float
    size: float
    expected_fill_probability: float
    conservative_expected_wealth_change: float
    point_expected_wealth_change: float
    capital_at_risk: float
    expected_return_on_capital: float
    information_score: float
    eligible: bool
    evidence_mature: bool
    attribution: dict[str, float]
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        return value


def outcome_fair(state: MarketState, outcome: str) -> tuple[float, float, float]:
    if outcome == "YES":
        return state.fair_lower_yes, state.fair_point_yes, state.fair_upper_yes
    if outcome == "NO":
        return 1.0 - state.fair_upper_yes, 1.0 - state.fair_point_yes, 1.0 - state.fair_lower_yes
    raise ExecutionAlphaError("outcome")


def _zero_attribution() -> dict[str, float]:
    return {name: 0.0 for name in ATTRIBUTION_FIELDS}


def _finalize_candidate(
    *, action: str, outcome: str, token_id: str, price: float, size: float,
    fill_probability: float, attribution: dict[str, float], point_delta: float,
    eligible: bool, mature: bool, information_score: float, reasons: Iterable[str],
) -> ActionCandidate:
    if set(attribution) != set(ATTRIBUTION_FIELDS):
        raise ExecutionAlphaError("attribution_partition")
    conservative = sum(attribution.values())
    if not math.isfinite(conservative):
        raise ExecutionAlphaError("candidate_nonfinite")
    capital = max(0.0, price * size)
    roc = conservative / capital if capital > 1e-12 else 0.0
    return ActionCandidate(
        action=action, outcome=outcome, token_id=token_id, price=price, size=size,
        expected_fill_probability=clamp(fill_probability, 0.0, 1.0),
        conservative_expected_wealth_change=conservative,
        point_expected_wealth_change=conservative + point_delta,
        capital_at_risk=capital, expected_return_on_capital=roc,
        information_score=max(0.0, information_score), eligible=bool(eligible),
        evidence_mature=bool(mature), attribution=attribution,
        reason_codes=tuple(sorted(set(str(reason) for reason in reasons if reason))),
    )


def take_candidate(state: MarketState, book: OutcomeBook) -> ActionCandidate:
    lower, point, _upper = outcome_fair(state, book.outcome)
    size = min(state.target_size, max(0.0, book.ask_size))
    fee = fee_per_share(book.ask, state.fee_schedule, taker=True)
    p_fill = state.taker.fill_probability_lower if size > 0.0 else 0.0
    attribution = _zero_attribution()
    if math.isfinite(fee) and size > 0.0:
        attribution["settlement_alpha"] = p_fill * size * (lower - book.midpoint)
        attribution["spread_capture"] = p_fill * size * (book.midpoint - book.ask)
        attribution["fees"] = -p_fill * size * fee
        attribution["slippage"] = -p_fill * size * state.taker.slippage_per_share
        attribution["latency"] = -p_fill * size * state.taker.latency_risk_per_share
        attribution["unwind"] = -p_fill * size * state.taker.unwind_loss_per_share
        attribution["capital"] = -state.taker.capital_cost_per_trade
    eligible = (
        state.settlement_verified and size > 0.0 and math.isfinite(fee)
        and state.tte_seconds > 0.0
    )
    point_delta = p_fill * size * (point - lower)
    reasons = ["SETTLEMENT_VERIFIED" if state.settlement_verified else "SETTLEMENT_UNVERIFIED"]
    if not state.taker.mature:
        reasons.append("TAKER_EXECUTION_EVIDENCE_IMMATURE")
    if size <= 0.0:
        reasons.append("NO_TAKER_CAPACITY")
    return _finalize_candidate(
        action="TAKE", outcome=book.outcome, token_id=book.token_id, price=book.ask,
        size=size, fill_probability=p_fill, attribution=attribution,
        point_delta=point_delta, eligible=eligible, mature=state.fair_mature and state.taker.mature,
        information_score=abs(point - book.midpoint) * max(size, state.target_size),
        reasons=reasons,
    )


def make_candidate(state: MarketState, book: OutcomeBook) -> ActionCandidate:
    lower, point, _upper = outcome_fair(state, book.outcome)
    size = min(state.target_size, max(0.0, book.bid_size))
    p_fill_lower = state.maker.fill_probability_lower if state.maker.mature else 0.0
    p_fill_point = state.maker.fill_probability_point if size > 0.0 else 0.0
    fee = fee_per_share(book.bid, state.fee_schedule, taker=False)
    attribution = _zero_attribution()
    if math.isfinite(fee) and size > 0.0:
        attribution["settlement_alpha"] = p_fill_lower * size * (lower - book.midpoint)
        attribution["spread_capture"] = p_fill_lower * size * (book.midpoint - book.bid)
        attribution["rebate"] = p_fill_lower * size * state.maker.rebate_per_share
        attribution["fees"] = -p_fill_lower * size * fee
        attribution["adverse_selection"] = -p_fill_lower * size * state.maker.adverse_markout_upper_per_share
        attribution["latency"] = -size * state.maker.cancel_latency_risk_per_share
        attribution["inventory"] = -p_fill_lower * size * state.maker.inventory_cost_per_share
        attribution["cancel"] = -state.maker.cancel_cost_per_quote
        attribution["capital"] = -state.maker.capital_cost_per_quote
    # Point EV is diagnostic only while maker evidence is immature. It is never
    # substituted into the conservative selection criterion.
    point_conditional = (
        (point - book.bid)
        + state.maker.rebate_per_share - (fee if math.isfinite(fee) else 0.0)
        - state.maker.adverse_markout_upper_per_share - state.maker.inventory_cost_per_share
    )
    point_ev = p_fill_point * size * point_conditional \
        - size * state.maker.cancel_latency_risk_per_share \
        - state.maker.cancel_cost_per_quote - state.maker.capital_cost_per_quote
    conservative = sum(attribution.values())
    eligible = state.settlement_verified and size > 0.0 and math.isfinite(fee) and state.tte_seconds > 0.0
    reasons = ["SETTLEMENT_VERIFIED" if state.settlement_verified else "SETTLEMENT_UNVERIFIED"]
    if not state.maker.mature:
        reasons.append("MAKER_EXECUTION_EVIDENCE_IMMATURE_CONSERVATIVE_FILL_ZERO")
    if size <= 0.0:
        reasons.append("NO_MAKER_CAPACITY")
    information = max(0.0, p_fill_point - p_fill_lower) * max(0.0, book.spread) * max(size, state.target_size)
    return _finalize_candidate(
        action="MAKE", outcome=book.outcome, token_id=book.token_id, price=book.bid,
        size=size, fill_probability=p_fill_lower, attribution=attribution,
        point_delta=point_ev - conservative, eligible=eligible,
        mature=state.fair_mature and state.maker.mature,
        information_score=information, reasons=reasons,
    )


def cancel_candidate(state: MarketState) -> ActionCandidate:
    attribution = _zero_attribution()
    if state.cancel.signal_active and state.cancel.quote_size > 0.0:
        attribution["adverse_selection"] = (
            state.cancel.quote_size * state.cancel.avoidable_fill_probability_lower
            * state.cancel.avoided_adverse_loss_lower_per_share
        )
        attribution["cancel"] = -state.cancel.cancel_cost
    reasons = []
    if state.cancel.mandatory_risk_cancel:
        reasons.append("MANDATORY_RISK_CANCEL")
    if state.cancel.signal_active:
        reasons.append("EXTERNAL_STALE_QUOTE_SIGNAL")
    if not state.cancel.mature:
        reasons.append("EXTERNAL_CANCEL_EVIDENCE_IMMATURE")
    return _finalize_candidate(
        action="CANCEL", outcome="NONE", token_id="", price=0.0,
        size=max(0.0, state.cancel.quote_size), fill_probability=0.0,
        attribution=attribution, point_delta=0.0,
        eligible=state.cancel.signal_active or state.cancel.mandatory_risk_cancel,
        mature=state.cancel.mature, information_score=0.0, reasons=reasons,
    )


def nothing_candidate() -> ActionCandidate:
    return _finalize_candidate(
        action="NOTHING", outcome="NONE", token_id="", price=0.0, size=0.0,
        fill_probability=0.0, attribution=_zero_attribution(), point_delta=0.0,
        eligible=True, mature=True, information_score=0.0, reasons=["BASELINE_NO_ACTION"],
    )


def evaluate_market(state: MarketState) -> dict[str, Any]:
    state.validate()
    candidates = [
        take_candidate(state, state.yes), take_candidate(state, state.no),
        make_candidate(state, state.yes), make_candidate(state, state.no),
        cancel_candidate(state), nothing_candidate(),
    ]
    mandatory = [row for row in candidates if row.action == "CANCEL" and row.eligible and state.cancel.mandatory_risk_cancel]
    if mandatory:
        selected = mandatory[0]
        selection_reason = "RISK_CANCEL_PREEMPTS_ALPHA"
    else:
        eligible = [row for row in candidates if row.eligible and row.action != "CANCEL"]
        selected = max(
            eligible,
            key=lambda row: (
                row.conservative_expected_wealth_change,
                row.expected_return_on_capital,
                1 if row.action == "TAKE" else 0,
                row.outcome,
            ),
        ) if eligible else nothing_candidate()
        if selected.conservative_expected_wealth_change <= 0.0:
            selected = nothing_candidate()
            selection_reason = "NO_POSITIVE_CONSERVATIVE_ACTION_VALUE"
        else:
            selection_reason = "MAX_CONSERVATIVE_EXPECTED_CHANGE_IN_ACCOUNT_WEALTH"
    best_point = max(candidates, key=lambda row: (row.point_expected_wealth_change, row.information_score, row.action, row.outcome))
    report = {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
        "market_id": state.market_id,
        "event_id": state.event_id,
        "asset": state.asset,
        "horizon": state.horizon,
        "source_snapshot_identity": state.source_snapshot_identity,
        "selected_action": selected.to_dict(),
        "selection_reason": selection_reason,
        "best_point_action": best_point.to_dict(),
        "maker_information_probe_recommended": (
            best_point.action == "MAKE" and best_point.information_score > 0.0
            and not state.maker.mature
        ),
        "candidates": [row.to_dict() for row in candidates],
        "settlement_alpha_and_execution_alpha_separated": True,
        "attribution_identity": list(ATTRIBUTION_FIELDS),
    }
    return report


def market_selection_value(report: dict[str, Any]) -> float:
    selected = report.get("selected_action") if isinstance(report.get("selected_action"), dict) else {}
    expected = finite(selected.get("conservative_expected_wealth_change"), 0.0)
    capital = finite(selected.get("capital_at_risk"), 0.0)
    if expected <= 0.0:
        return 0.0
    return expected / max(capital, 1e-9)


def select_top_markets(
    reports: Iterable[dict[str, Any]], *, top_fraction: float = 0.20,
    minimum_expected_wealth_change: float = 0.0,
) -> list[dict[str, Any]]:
    rows = list(reports)
    if not rows:
        return []
    fraction = clamp(top_fraction, 1e-9, 1.0)
    eligible = []
    for report in rows:
        selected = report.get("selected_action") if isinstance(report.get("selected_action"), dict) else {}
        if finite(selected.get("conservative_expected_wealth_change"), 0.0) > minimum_expected_wealth_change:
            eligible.append(report)
    if not eligible:
        return []
    keep = max(1, math.ceil(len(eligible) * fraction))
    return sorted(
        eligible,
        key=lambda report: (
            market_selection_value(report),
            finite((report.get("selected_action") or {}).get("conservative_expected_wealth_change"), 0.0),
            str(report.get("market_id") or ""),
        ),
        reverse=True,
    )[:keep]


def aggregate_attribution(reports: Iterable[dict[str, Any]]) -> dict[str, float]:
    totals = _zero_attribution()
    for report in reports:
        action = report.get("selected_action") if isinstance(report.get("selected_action"), dict) else {}
        attribution = action.get("attribution") if isinstance(action.get("attribution"), dict) else {}
        for name in ATTRIBUTION_FIELDS:
            totals[name] += finite(attribution.get(name), 0.0)
    totals["total_expected_wealth_change"] = sum(totals[name] for name in ATTRIBUTION_FIELDS)
    totals["execution_alpha"] = totals["total_expected_wealth_change"] - totals["settlement_alpha"]
    return totals
