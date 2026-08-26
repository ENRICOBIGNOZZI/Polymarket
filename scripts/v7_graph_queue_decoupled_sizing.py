#!/usr/bin/env python3
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CapacityResult:
    risk_units: float
    queue_ahead: float
    unwind_depth_shares: float
    weight: float
    incumbent_units: float
    challenger_units: float


def _nonnegative(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("capacity inputs must be finite")
    return max(0.0, value)


def incumbent_queue_coupled_units(*, risk_units: float, queue_ahead: float, weight: float) -> float:
    """Reproduce the current V7 broker's queue-coupled sizing formula.

    This is diagnostic only. A larger queue mechanically grants more size under
    the incumbent formula; that property is the behavior this challenger tests.
    """
    risk_units = _nonnegative(risk_units)
    queue_ahead = _nonnegative(queue_ahead)
    weight = _nonnegative(weight)
    if weight <= 0.0:
        raise ValueError("weight must be positive")
    return min(risk_units, 0.25 * max(1.0, queue_ahead) / weight)


def queue_decoupled_units(
    *,
    risk_units: float,
    unwind_depth_shares: float,
    weight: float,
    max_unwind_depth_fraction: float = 0.25,
) -> float:
    """Bound target units by risk budget and independent executable unwind depth.

    Queue ahead is intentionally absent. Queue belongs in fill/completion
    probability; cumulative executable bid depth belongs in a capacity bound for
    closing a partially filled long leg. The fraction is a research parameter,
    not a live-risk authorization.
    """
    risk_units = _nonnegative(risk_units)
    unwind_depth_shares = _nonnegative(unwind_depth_shares)
    weight = _nonnegative(weight)
    max_unwind_depth_fraction = _nonnegative(max_unwind_depth_fraction)
    if weight <= 0.0:
        raise ValueError("weight must be positive")
    if max_unwind_depth_fraction > 1.0:
        raise ValueError("max_unwind_depth_fraction must be <= 1")
    return min(risk_units, max_unwind_depth_fraction * unwind_depth_shares / weight)


def compare_capacity(
    *,
    risk_units: float,
    queue_ahead: float,
    unwind_depth_shares: float,
    weight: float = 1.0,
    max_unwind_depth_fraction: float = 0.25,
) -> CapacityResult:
    return CapacityResult(
        risk_units=_nonnegative(risk_units),
        queue_ahead=_nonnegative(queue_ahead),
        unwind_depth_shares=_nonnegative(unwind_depth_shares),
        weight=_nonnegative(weight),
        incumbent_units=incumbent_queue_coupled_units(
            risk_units=risk_units,
            queue_ahead=queue_ahead,
            weight=weight,
        ),
        challenger_units=queue_decoupled_units(
            risk_units=risk_units,
            unwind_depth_shares=unwind_depth_shares,
            weight=weight,
            max_unwind_depth_fraction=max_unwind_depth_fraction,
        ),
    )
