#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

ONE_SIDED_95_Z = 1.6448536269514722


def _finite(value: Any) -> float:
    x = float(value)
    if not math.isfinite(x):
        raise ValueError("non-finite numeric input")
    return x


def wilson_bounds(successes: int, trials: int, z: float = ONE_SIDED_95_Z) -> tuple[float, float]:
    if trials <= 0:
        raise ValueError("trials must be positive")
    if successes < 0 or successes > trials:
        raise ValueError("successes must be in [0, trials]")
    phat = successes / trials
    z2 = z * z
    denom = 1.0 + z2 / trials
    center = (phat + z2 / (2.0 * trials)) / denom
    radius = z * math.sqrt((phat * (1.0 - phat) + z2 / (4.0 * trials)) / trials) / denom
    return max(0.0, center - radius), min(1.0, center + radius)


def maker_edge(limit_prices: list[float]) -> float:
    if len(limit_prices) < 2:
        raise ValueError("a graph basket needs at least two legs")
    prices = [_finite(p) for p in limit_prices]
    if any(p <= 0.0 or p >= 1.0 for p in prices):
        raise ValueError("limit prices must lie strictly between zero and one")
    return 1.0 - sum(prices)


def completion_break_even(complete_pnl: float, abort_pnl: float) -> float | None:
    complete_pnl = _finite(complete_pnl)
    abort_pnl = _finite(abort_pnl)
    if complete_pnl <= 0.0:
        return None
    if abort_pnl >= 0.0:
        return 0.0
    return -abort_pnl / (complete_pnl - abort_pnl)


def expected_session_pnl(q: float, complete_pnl: float, abort_pnl: float) -> float:
    q = _finite(q)
    if q < 0.0 or q > 1.0:
        raise ValueError("completion probability must lie in [0, 1]")
    return q * _finite(complete_pnl) + (1.0 - q) * _finite(abort_pnl)


def analyze(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("candidate") or {}
    execution = payload.get("candidate_specific_execution") or {}
    policy = payload.get("policy") or {}

    prices = [_finite(x) for x in candidate.get("limit_prices", [])]
    quoted_edge = maker_edge(prices)
    reported_edge = _finite(candidate.get("reported_expected_edge", quoted_edge))
    if abs(quoted_edge - reported_edge) > 1e-9:
        raise ValueError("reported expected edge does not match complete-basket limit prices")

    guard_stress_bps = max(0.0, _finite(policy.get("guard_stress_bps", 10.0)))
    min_edge = max(0.0, _finite(policy.get("min_edge", 0.0002)))
    guard_stressed_edge = quoted_edge - guard_stress_bps / 10000.0
    static_guard_pass = guard_stressed_edge > min_edge

    result: dict[str, Any] = {
        "candidate_id": str(candidate.get("bundle_id") or ""),
        "event_id": str(candidate.get("event_id") or ""),
        "strategy": str(candidate.get("strategy") or "GRAPH_RV"),
        "legs": len(prices),
        "limit_price_sum": sum(prices),
        "quoted_maker_edge_per_share": quoted_edge,
        "guard_stress_bps": guard_stress_bps,
        "guard_stressed_edge_per_share": guard_stressed_edge,
        "configured_min_edge": min_edge,
        "static_guard_pass": static_guard_pass,
        "static_guard_is_execution_evidence": False,
        "hard_arb_claim": False,
        "evidence_state": "MORE_EVIDENCE_REQUIRED",
        "reasons": [],
    }

    if str(candidate.get("mode") or "MAKER").upper() != "MAKER":
        result["reasons"].append("candidate_is_not_passive_maker")
        result["evidence_state"] = "REJECTED"
        return result

    if not static_guard_pass:
        result["reasons"].append("fails_existing_static_cost_stress_guard")
        result["evidence_state"] = "REJECTED"
        return result

    trials = int(execution.get("sessions", 0) or 0)
    completions = int(execution.get("completed_sessions", 0) or 0)
    min_sessions = int(policy.get("min_candidate_sessions", 12) or 12)
    min_completions = int(policy.get("min_candidate_completions", 10) or 10)
    result["candidate_specific_sessions"] = trials
    result["candidate_specific_completions"] = completions
    result["min_candidate_sessions"] = min_sessions
    result["min_candidate_completions"] = min_completions

    complete_pnl_raw = execution.get("mean_complete_net_pnl_per_share")
    abort_pnl_raw = execution.get("mean_abort_net_pnl_per_share")
    if trials <= 0 or complete_pnl_raw is None or abort_pnl_raw is None:
        result["reasons"].append("missing_candidate_specific_completion_abort_economics")
        result["required_evidence"] = [
            "prospective recurrence of the exact event/leg-set",
            "event-time queue state through order arrival for every leg",
            "common-minimum completion and excess-fill distribution under the live multileg contract",
            "cancel-latency fills plus timeout unwind PnL on actually filled inventory",
            "fill-conditioned 60s/300s markout",
            "common chronological 1x/1.5x/2x queue-latency-slippage stress",
        ]
        return result

    if completions < 0 or completions > trials:
        raise ValueError("completed_sessions must be in [0, sessions]")

    complete_pnl = _finite(complete_pnl_raw)
    abort_pnl = _finite(abort_pnl_raw)
    q_lo, q_hi = wilson_bounds(completions, trials)
    q_star = completion_break_even(complete_pnl, abort_pnl)
    conservative_expected = expected_session_pnl(q_lo, complete_pnl, abort_pnl)
    optimistic_expected = expected_session_pnl(q_hi, complete_pnl, abort_pnl)
    result.update(
        {
            "completion_rate": completions / trials,
            "completion_rate_wilson_lower_one_sided_95": q_lo,
            "completion_rate_wilson_upper_one_sided_95": q_hi,
            "mean_complete_net_pnl_per_share": complete_pnl,
            "mean_abort_net_pnl_per_share": abort_pnl,
            "break_even_completion_probability": q_star,
            "conservative_expected_net_pnl_per_share": conservative_expected,
            "optimistic_expected_net_pnl_per_share": optimistic_expected,
        }
    )

    if trials < min_sessions or completions < min_completions:
        result["reasons"].append("insufficient_candidate_specific_execution_sample")
        return result
    if q_star is None:
        result["reasons"].append("nonpositive_complete_state_economics")
        result["evidence_state"] = "REJECTED"
        return result
    if conservative_expected <= 0.0:
        result["reasons"].append("completion_lower_bound_does_not_cover_abort_downside")
        result["evidence_state"] = "REJECTED"
        return result

    stressed = execution.get("cost_stress_expected_net_pnl_per_share") or {}
    required_stresses = ("1x", "1.5x", "2x")
    missing = [name for name in required_stresses if name not in stressed]
    if missing:
        result["reasons"].append("missing_cost_stress_economics")
        result["missing_cost_stresses"] = missing
        return result
    stressed_values = {name: _finite(stressed[name]) for name in required_stresses}
    result["cost_stress_expected_net_pnl_per_share"] = stressed_values
    if any(value <= 0.0 for value in stressed_values.values()):
        result["reasons"].append("nonpositive_cost_stressed_execution_economics")
        result["evidence_state"] = "REJECTED"
        return result

    recurrent_windows = int(execution.get("independent_positive_windows", 0) or 0)
    result["independent_positive_windows"] = recurrent_windows
    if recurrent_windows < 2:
        result["reasons"].append("insufficient_prospective_recurrence")
        return result

    result["evidence_state"] = "EVIDENCE_READY_FOR_GOVERNANCE_REVIEW"
    result["reasons"].append("candidate_specific_execution_and_cost_stress_pass")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Research-only V6 GRAPH_RV completion-risk frontier")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    result = analyze(payload)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
