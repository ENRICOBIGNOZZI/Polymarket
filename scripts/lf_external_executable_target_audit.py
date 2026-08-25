#!/usr/bin/env python3
"""Research-only audit of external-intelligence execution admission semantics.

The production external worker predicts a future *midpoint* delta. Its current
trade admission threshold charges the current half-spread and extra cost, while
realized paper PnL exits at the future bid/ask. This diagnostic shows that the
horizon exit spread is therefore omitted from the admission hurdle.

No production signal, champion, risk or execution path is changed here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class Fixture:
    name: str
    mid: float
    bid: float
    ask: float
    future_mid: float
    future_bid: float
    future_ask: float
    predicted_mid_delta: float
    extra_cost: float


def incumbent_threshold(fixture: Fixture) -> float:
    """Current external-intelligence hurdle for either direction."""
    return 0.5 * max(0.0, fixture.ask - fixture.bid) + fixture.extra_cost


def causal_roundtrip_threshold(fixture: Fixture) -> float:
    """Simple causal baseline using current half-spread as expected exit half-spread.

    This is intentionally only an ablation baseline. A stronger model should predict
    the horizon executable bid/ask or an exit-spread distribution from prior data.
    """
    entry_half_spread = 0.5 * max(0.0, fixture.ask - fixture.bid)
    expected_exit_half_spread = entry_half_spread
    return entry_half_spread + expected_exit_half_spread + fixture.extra_cost


def incumbent_decision(fixture: Fixture) -> int:
    threshold = incumbent_threshold(fixture)
    if fixture.predicted_mid_delta > threshold:
        return 1
    if fixture.predicted_mid_delta < -threshold:
        return -1
    return 0


def causal_roundtrip_decision(fixture: Fixture) -> int:
    threshold = causal_roundtrip_threshold(fixture)
    if fixture.predicted_mid_delta > threshold:
        return 1
    if fixture.predicted_mid_delta < -threshold:
        return -1
    return 0


def realized_executable_pnl(fixture: Fixture, side: int) -> float:
    if side > 0:
        return fixture.future_bid - fixture.ask - fixture.extra_cost
    if side < 0:
        return fixture.bid - fixture.future_ask - fixture.extra_cost
    return 0.0


def fixtures() -> list[Fixture]:
    return [
        Fixture(
            name="long_midpoint_signal_does_not_cover_exit_spread",
            mid=0.50,
            bid=0.49,
            ask=0.51,
            future_mid=0.52,
            future_bid=0.50,
            future_ask=0.54,
            predicted_mid_delta=0.015,
            extra_cost=0.002,
        ),
        Fixture(
            name="short_midpoint_signal_does_not_cover_exit_spread",
            mid=0.50,
            bid=0.49,
            ask=0.51,
            future_mid=0.48,
            future_bid=0.46,
            future_ask=0.50,
            predicted_mid_delta=-0.015,
            extra_cost=0.002,
        ),
        Fixture(
            name="large_signal_survives_roundtrip_hurdle",
            mid=0.50,
            bid=0.49,
            ask=0.51,
            future_mid=0.54,
            future_bid=0.53,
            future_ask=0.55,
            predicted_mid_delta=0.035,
            extra_cost=0.002,
        ),
    ]


def evaluate() -> dict:
    rows = []
    for fixture in fixtures():
        incumbent_side = incumbent_decision(fixture)
        causal_side = causal_roundtrip_decision(fixture)
        rows.append({
            **asdict(fixture),
            "incumbent_threshold": incumbent_threshold(fixture),
            "causal_roundtrip_threshold": causal_roundtrip_threshold(fixture),
            "incumbent_side": incumbent_side,
            "causal_roundtrip_side": causal_side,
            "incumbent_realized_executable_pnl": realized_executable_pnl(fixture, incumbent_side),
            "causal_roundtrip_realized_executable_pnl": realized_executable_pnl(fixture, causal_side),
        })
    return {
        "schema": "lf_external_executable_target_audit_v1",
        "interpretation": (
            "A future-midpoint forecast must cover entry crossing plus expected horizon exit crossing. "
            "The current hurdle covers only the current half-spread plus extra cost; this can admit "
            "trades whose own executable exit accounting is negative. The causal roundtrip hurdle is "
            "an ablation baseline, not a production estimator."
        ),
        "fixtures": rows,
    }


def main() -> int:
    payload = evaluate()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
