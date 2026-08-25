#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MakerAdmission:
    admit: bool
    reason: str
    queue_clearance_ratio: float
    fill_probability_proxy: float
    expected_filled_edge: float


def evaluate(
    *,
    post_cost_edge: float,
    min_edge: float,
    queue_ahead: float,
    own_shares: float,
    compatible_sell_rate_per_second: float,
    horizon_seconds: float,
    inside_spread: bool = False,
) -> MakerAdmission:
    edge = float(post_cost_edge)
    queue = max(0.0, float(queue_ahead))
    own = max(0.0, float(own_shares))
    rate = max(0.0, float(compatible_sell_rate_per_second))
    horizon = max(0.0, float(horizon_seconds))
    expected_contra = rate * horizon

    if not math.isfinite(edge) or edge < float(min_edge):
        return MakerAdmission(False, "EDGE_BELOW_FLOOR", 0.0, 0.0, 0.0)
    if own <= 1e-12:
        return MakerAdmission(False, "INVALID_SIZE", 0.0, 0.0, 0.0)
    if rate <= 1e-12:
        return MakerAdmission(False, "ZERO_CAUSAL_CONTRA_FLOW", 0.0, 0.0, 0.0)

    # This is deliberately a conservative volume-capacity proxy, not a claimed
    # probabilistic fill model. A passive bid must first consume queue ahead and
    # then its own requested size. Inside-spread improvement removes displayed
    # queue ahead but still requires actual contra-flow; it never gets a free
    # fill merely because displayed queue is zero.
    required_volume = own + (0.0 if inside_spread else queue)
    clearance = expected_contra / max(required_volume, 1e-12)
    fill_proxy = min(1.0, max(0.0, clearance))
    expected_filled_edge = edge * fill_proxy

    # The only hard research rejection beyond the edge floor is zero observed
    # causal contra-flow. Positive-flow candidates are ranked by expected filled
    # edge so calibration can determine a later promotion threshold without
    # inventing one from the current sparse sample.
    return MakerAdmission(True, "POSITIVE_CAUSAL_FLOW_RANK", clearance, fill_proxy, expected_filled_edge)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a causal flow-aware maker admission candidate")
    parser.add_argument("--post-cost-edge", type=float, required=True)
    parser.add_argument("--min-edge", type=float, default=0.00005)
    parser.add_argument("--queue-ahead", type=float, required=True)
    parser.add_argument("--own-shares", type=float, required=True)
    parser.add_argument("--compatible-sell-rate", type=float, required=True)
    parser.add_argument("--horizon-seconds", type=float, default=60.0)
    parser.add_argument("--inside-spread", action="store_true")
    args = parser.parse_args()
    result = evaluate(
        post_cost_edge=args.post_cost_edge,
        min_edge=args.min_edge,
        queue_ahead=args.queue_ahead,
        own_shares=args.own_shares,
        compatible_sell_rate_per_second=args.compatible_sell_rate,
        horizon_seconds=args.horizon_seconds,
        inside_spread=args.inside_spread,
    )
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
