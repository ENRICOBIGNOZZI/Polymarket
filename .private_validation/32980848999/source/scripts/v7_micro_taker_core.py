#!/usr/bin/env python3
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FeeSpec:
    enabled: bool
    rate: float
    exponent: float = 1.0
    taker_only: bool = True
    authoritative: bool = True


@dataclass(frozen=True)
class BookSnapshot:
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    liquidity: float
    received_ts: int

    @property
    def yes_mid(self) -> float:
        return 0.5 * (self.yes_bid + self.yes_ask)

    @property
    def no_mid(self) -> float:
        return 0.5 * (self.no_bid + self.no_ask)


@dataclass(frozen=True)
class RoundTripEconomics:
    side: str
    horizon_seconds: int
    entry_price: float
    expected_exit_price: float
    entry_fee_per_share: float
    exit_fee_per_share: float
    gross_markout_per_share: float
    uncertainty_penalty_per_share: float
    adverse_markout_penalty_per_share: float
    capital_time_cost_per_share: float
    net_pnl_per_share: float
    capital_per_share: float
    net_edge: float
    economic_score: float


def clamp_probability(value: float) -> float:
    return min(0.999999, max(0.000001, float(value)))


def fee_per_share(price: float, fee: FeeSpec, *, taker: bool = True) -> float:
    if (
        not fee.authoritative
        or not fee.enabled
        or fee.rate <= 0.0
        or not 0.0 < price < 1.0
        or (fee.taker_only and not taker)
    ):
        return 0.0
    return max(0.0, fee.rate) * (price * (1.0 - price)) ** max(0.0, fee.exponent)


def valid_book(book: BookSnapshot) -> bool:
    return (
        0.0 < book.yes_bid < book.yes_ask < 1.0
        and 0.0 < book.no_bid < book.no_ask < 1.0
        and book.liquidity >= 0.0
    )


def round_trip_economics(
    *,
    side: str,
    book: BookSnapshot,
    predicted_yes_mid: float,
    prediction_sigma_probability: float,
    fee: FeeSpec,
    horizon_seconds: int,
    now: int,
    slippage_bps_per_leg: float,
    uncertainty_z: float,
    adverse_markout_penalty_bps: float,
    capital_cost_bps_per_hour: float,
    max_book_age_seconds: int,
) -> RoundTripEconomics | None:
    """Price a fixed-horizon taker trade as a complete entry/exit round trip.

    The forecast is a future YES mid, not a terminal event probability. Entry crosses
    the current ask. Exit is conservatively proxied by the forecast future side-mid
    less half the *current* side spread, then exit slippage. Both taker fees, forecast
    uncertainty, adverse-selection stress and capital-time cost are deducted before
    admission. Unknown fee schedules fail closed.
    """
    side = side.upper()
    if side not in {"YES", "NO"}:
        raise ValueError("side must be YES or NO")
    if horizon_seconds <= 0 or max_book_age_seconds < 0:
        return None
    if not fee.authoritative or not valid_book(book):
        return None
    if now - int(book.received_ts) < 0 or now - int(book.received_ts) > max_book_age_seconds:
        return None

    slip = max(0.0, float(slippage_bps_per_leg)) / 10000.0
    predicted_yes = clamp_probability(predicted_yes_mid)
    sigma = max(0.0, float(prediction_sigma_probability))

    if side == "YES":
        ask = book.yes_ask
        spread = book.yes_ask - book.yes_bid
        future_side_mid = predicted_yes
    else:
        ask = book.no_ask
        spread = book.no_ask - book.no_bid
        future_side_mid = 1.0 - predicted_yes

    entry_price = clamp_probability(ask * (1.0 + slip))
    # A fixed-horizon mid forecast is not directly executable. Approximate the
    # future liquidation bid by paying half the current spread plus exit slippage.
    future_bid_proxy = clamp_probability(future_side_mid - 0.5 * spread)
    expected_exit_price = clamp_probability(future_bid_proxy * (1.0 - slip))

    entry_fee = fee_per_share(entry_price, fee, taker=True)
    exit_fee = fee_per_share(expected_exit_price, fee, taker=True)
    gross_markout = expected_exit_price - entry_price
    uncertainty_penalty = max(0.0, float(uncertainty_z)) * sigma
    capital_per_share = entry_price + entry_fee
    adverse_penalty = max(0.0, float(adverse_markout_penalty_bps)) / 10000.0 * capital_per_share
    capital_time_cost = (
        max(0.0, float(capital_cost_bps_per_hour))
        / 10000.0
        * (float(horizon_seconds) / 3600.0)
        * capital_per_share
    )
    net_pnl = (
        gross_markout
        - entry_fee
        - exit_fee
        - uncertainty_penalty
        - adverse_penalty
        - capital_time_cost
    )
    net_edge = net_pnl / max(capital_per_share, 1e-12)
    economic_score = net_edge / max(sigma, 1e-4)
    return RoundTripEconomics(
        side=side,
        horizon_seconds=int(horizon_seconds),
        entry_price=entry_price,
        expected_exit_price=expected_exit_price,
        entry_fee_per_share=entry_fee,
        exit_fee_per_share=exit_fee,
        gross_markout_per_share=gross_markout,
        uncertainty_penalty_per_share=uncertainty_penalty,
        adverse_markout_penalty_per_share=adverse_penalty,
        capital_time_cost_per_share=capital_time_cost,
        net_pnl_per_share=net_pnl,
        capital_per_share=capital_per_share,
        net_edge=net_edge,
        economic_score=economic_score,
    )


def choose_side(
    *,
    book: BookSnapshot,
    predicted_yes_mid: float,
    prediction_sigma_probability: float,
    fee: FeeSpec,
    horizon_seconds: int,
    now: int,
    slippage_bps_per_leg: float,
    uncertainty_z: float,
    adverse_markout_penalty_bps: float,
    capital_cost_bps_per_hour: float,
    max_book_age_seconds: int,
    minimum_net_edge: float,
) -> RoundTripEconomics | None:
    candidates = [
        round_trip_economics(
            side=side,
            book=book,
            predicted_yes_mid=predicted_yes_mid,
            prediction_sigma_probability=prediction_sigma_probability,
            fee=fee,
            horizon_seconds=horizon_seconds,
            now=now,
            slippage_bps_per_leg=slippage_bps_per_leg,
            uncertainty_z=uncertainty_z,
            adverse_markout_penalty_bps=adverse_markout_penalty_bps,
            capital_cost_bps_per_hour=capital_cost_bps_per_hour,
            max_book_age_seconds=max_book_age_seconds,
        )
        for side in ("YES", "NO")
    ]
    eligible = [
        candidate
        for candidate in candidates
        if candidate is not None
        and candidate.net_edge >= float(minimum_net_edge)
        and candidate.net_pnl_per_share > 0.0
    ]
    return max(eligible, key=lambda item: item.economic_score, default=None)
