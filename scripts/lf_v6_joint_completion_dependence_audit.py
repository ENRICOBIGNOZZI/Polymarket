#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class LegRequirement:
    token_id: str
    limit_price: float
    queue_ahead: float
    own_shares: float

    @property
    def required_compatible_volume(self) -> float:
        return max(0.0, self.queue_ahead) + max(0.0, self.own_shares)


@dataclass(frozen=True)
class TapeTrade:
    timestamp: int
    token_id: str
    side: str
    price: float
    size: float


def independent_joint_probability(marginals: Sequence[float]) -> float:
    out = 1.0
    for value in marginals:
        out *= max(0.0, min(1.0, float(value)))
    return out


def frechet_joint_bounds(marginals: Sequence[float]) -> tuple[float, float]:
    ps = [max(0.0, min(1.0, float(value))) for value in marginals]
    if not ps:
        return 0.0, 0.0
    return max(0.0, sum(ps) - (len(ps) - 1)), min(ps)


def empirical_marginals(states: Sequence[Sequence[bool]]) -> list[float]:
    if not states:
        return []
    width = len(states[0])
    if width == 0 or any(len(row) != width for row in states):
        raise ValueError("states must be a non-empty rectangular boolean matrix")
    n = len(states)
    return [sum(bool(row[j]) for row in states) / n for j in range(width)]


def empirical_joint_probability(states: Sequence[Sequence[bool]]) -> float:
    if not states:
        return 0.0
    return sum(all(bool(value) for value in row) for row in states) / len(states)


def state_distribution(states: Sequence[Sequence[bool]]) -> dict[str, float]:
    if not states:
        return {}
    counts: Counter[str] = Counter()
    for row in states:
        key = "".join("1" if bool(value) else "0" for value in row)
        counts[key] += 1
    total = len(states)
    return {key: counts[key] / total for key in sorted(counts)}


def state_based_bundle_ev(
    states: Sequence[Sequence[bool]],
    *,
    complete_profit_usd: float,
    unwind_loss_per_filled_leg_usd: float,
) -> float:
    """Empirical per-window EV using observed same-window completion states.

    Complete states earn complete_profit_usd. In incomplete states every filled leg
    is assumed to be unwound at the supplied stressed loss. This is deliberately
    simple: the purpose is to show that subset-state frequencies, not marginal
    fill probabilities alone, identify completion/abort economics.
    """
    if not states:
        return 0.0
    total = 0.0
    for row in states:
        if all(bool(value) for value in row):
            total += complete_profit_usd
        else:
            total -= sum(bool(value) for value in row) * max(0.0, unwind_loss_per_filled_leg_usd)
    return total / len(states)


def read_tape(path: Path) -> list[TapeTrade]:
    rows: list[TapeTrade] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            try:
                ts = int(float(raw.get("timestamp") or 0))
                token = str(raw.get("asset_id") or raw.get("token_id") or "")
                side = str(raw.get("side") or "").strip().upper()
                price = float(raw.get("price") or 0.0)
                size = float(raw.get("size") or 0.0)
            except (TypeError, ValueError):
                continue
            if ts > 0 and token and side in {"BUY", "SELL"} and 0.0 < price < 1.0 and size > 0.0:
                rows.append(TapeTrade(ts, token, side, price, size))
    rows.sort(key=lambda row: row.timestamp)
    return rows


def completion_state_for_window(
    trades: Iterable[TapeTrade],
    legs: Sequence[LegRequirement],
    *,
    start_ts: int,
    end_ts: int,
) -> tuple[bool, ...]:
    compatible = [0.0 for _ in legs]
    for trade in trades:
        if trade.timestamp < start_ts or trade.timestamp >= end_ts or trade.side != "SELL":
            continue
        for i, leg in enumerate(legs):
            if trade.token_id == leg.token_id and trade.price <= leg.limit_price + 1e-12:
                compatible[i] += trade.size
    return tuple(
        compatible[i] + 1e-12 >= leg.required_compatible_volume
        for i, leg in enumerate(legs)
    )


def rolling_completion_states(
    trades: Sequence[TapeTrade],
    legs: Sequence[LegRequirement],
    *,
    start_ts: int,
    end_ts: int,
    window_seconds: int,
) -> list[tuple[bool, ...]]:
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    states: list[tuple[bool, ...]] = []
    cursor = start_ts
    while cursor + window_seconds <= end_ts:
        states.append(
            completion_state_for_window(
                trades,
                legs,
                start_ts=cursor,
                end_ts=cursor + window_seconds,
            )
        )
        cursor += window_seconds
    return states


def same_marginal_counterexample(n_legs: int = 5, windows: int = 100, filled_windows_per_leg: int = 2) -> dict[str, object]:
    if n_legs < 2 or windows < n_legs * filled_windows_per_leg:
        raise ValueError("counterexample requires enough windows for disjoint fills")

    common = [tuple(False for _ in range(n_legs)) for _ in range(windows)]
    for i in range(filled_windows_per_leg):
        common[i] = tuple(True for _ in range(n_legs))

    exclusive = [tuple(False for _ in range(n_legs)) for _ in range(windows)]
    cursor = 0
    for leg in range(n_legs):
        for _ in range(filled_windows_per_leg):
            row = [False for _ in range(n_legs)]
            row[leg] = True
            exclusive[cursor] = tuple(row)
            cursor += 1

    marginals = empirical_marginals(common)
    if marginals != empirical_marginals(exclusive):
        raise AssertionError("fixtures must have identical marginals")
    lower, upper = frechet_joint_bounds(marginals)
    independent = independent_joint_probability(marginals)
    common_joint = empirical_joint_probability(common)
    exclusive_joint = empirical_joint_probability(exclusive)

    return {
        "n_legs": n_legs,
        "windows": windows,
        "marginals": marginals,
        "independent_joint": independent,
        "frechet_lower": lower,
        "frechet_upper": upper,
        "common_shock_empirical_joint": common_joint,
        "exclusive_empirical_joint": exclusive_joint,
        "upper_to_independence_ratio": (upper / independent) if independent > 0 else math.inf,
        "common_shock_ev_usd": state_based_bundle_ev(
            common,
            complete_profit_usd=10.0,
            unwind_loss_per_filled_leg_usd=1.0,
        ),
        "exclusive_ev_usd": state_based_bundle_ev(
            exclusive,
            complete_profit_usd=10.0,
            unwind_loss_per_filled_leg_usd=1.0,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit independence assumptions in V6 multi-leg completion economics")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    example = same_marginal_counterexample()
    report = {
        "status": "MORE_EVIDENCE_REQUIRED",
        "finding": "marginal leg-fill probabilities do not identify joint bundle completion or partial-fill state probabilities",
        "counterexample": example,
        "successor_contract": [
            "measure compatible flow for every leg in the same chronological fill window",
            "compute queue_ahead_plus_actual_target_shares before declaring a leg filled",
            "estimate empirical full-completion and subset-state frequencies without multiplying marginals",
            "use block/bootstrap uncertainty over chronological windows and fail closed when history is insufficient",
            "price each incomplete subset with candidate-specific abort/unwind fees, depth and slippage at 1x/1.5x/2x costs",
            "score graph/local-factor bundles by state-weighted fill-conditioned PnL rather than quote edge or independent joint fill",
        ],
    }
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
