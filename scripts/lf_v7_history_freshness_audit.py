#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class FreshnessAssessment:
    latest_history_ts: int | None
    history_age_seconds: int | None
    bucket_seconds: int
    maximum_age_seconds: int
    regular: bool
    fresh: bool
    reason: str


def is_regular(times: Sequence[int], bucket_seconds: int) -> bool:
    ordered = [int(t) for t in times]
    if len(ordered) < 2 or bucket_seconds <= 0:
        return False
    return all(b - a == bucket_seconds for a, b in zip(ordered, ordered[1:]))


def assess_history_freshness(
    times: Sequence[int],
    now: int,
    bucket_seconds: int,
    maximum_age_buckets: float = 2.0,
) -> FreshnessAssessment:
    ordered = sorted(set(int(t) for t in times))
    maximum_age_seconds = int(max(0.0, float(maximum_age_buckets)) * max(1, int(bucket_seconds)))
    regular = is_regular(ordered, int(bucket_seconds))
    if not ordered:
        return FreshnessAssessment(None, None, int(bucket_seconds), maximum_age_seconds, False, False, "missing_history")
    latest = ordered[-1]
    age = int(now) - latest
    if age < 0:
        return FreshnessAssessment(latest, age, int(bucket_seconds), maximum_age_seconds, regular, False, "future_history_timestamp")
    if not regular:
        return FreshnessAssessment(latest, age, int(bucket_seconds), maximum_age_seconds, False, False, "irregular_history")
    if age > maximum_age_seconds:
        return FreshnessAssessment(latest, age, int(bucket_seconds), maximum_age_seconds, True, False, "stale_history_state")
    return FreshnessAssessment(latest, age, int(bucket_seconds), maximum_age_seconds, True, True, "fresh_regular_history")


def stale_state_forecast_overstatement(phi: float, stale_bars: int, hold_bars: int, initial_residual: float = 2.0) -> dict[str, float]:
    if not 0.0 < phi < 1.0:
        raise ValueError("phi must lie in (0,1)")
    if stale_bars < 0 or hold_bars <= 0:
        raise ValueError("invalid horizons")
    stale_residual = float(initial_residual)
    expected_current_residual = (phi ** stale_bars) * stale_residual
    stale_origin_change = (phi ** hold_bars - 1.0) * stale_residual
    current_origin_change = (phi ** hold_bars - 1.0) * expected_current_residual
    ratio = abs(stale_origin_change) / max(1e-15, abs(current_origin_change))
    return {
        "phi": phi,
        "stale_bars": stale_bars,
        "hold_bars": hold_bars,
        "stale_origin_residual_sd": stale_residual,
        "expected_current_residual_sd": expected_current_residual,
        "stale_origin_forecast_change_sd": stale_origin_change,
        "current_origin_forecast_change_sd": current_origin_change,
        "absolute_forecast_overstatement_ratio": ratio,
    }


def build_counterexample(now: int, bucket_seconds: int = 3600) -> dict[str, object]:
    points = 60
    stale_bars = 12
    last = now - stale_bars * bucket_seconds
    first = last - (points - 1) * bucket_seconds
    times = tuple(first + i * bucket_seconds for i in range(points))
    assessment = assess_history_freshness(times, now, bucket_seconds, maximum_age_buckets=2.0)
    forecast = stale_state_forecast_overstatement(phi=0.80, stale_bars=stale_bars, hold_bars=6)
    return {
        "regular_points": points,
        "last_history_age_hours": stale_bars,
        "assessment": asdict(assessment),
        "forecast_counterexample": forecast,
        "interpretation": (
            "A regular panel can satisfy a warmup while ending many bars before entry. "
            "Using its last residual as the state for a current executable book starts the AR forecast from an obsolete state."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V7 Local Factor history-state freshness before current-book execution")
    parser.add_argument("--now", type=int, default=1_800_000_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {
        "schema": "lf_v7_history_freshness_audit_v1",
        "research_only": True,
        "paper_only": True,
        "authenticated_execution": False,
        "counterexample": build_counterexample(args.now),
        "required_successor_contract": [
            "Bind every fitted pair to a latest common history timestamp and expose its age at signal origin.",
            "Fail closed when the latest pair panel is older than a predeclared bounded number of fidelity buckets.",
            "Do not combine a stale residual state with a current executable book or current p*(1-p) hedge sensitivity.",
            "Use only completed regular fidelity buckets, treating an incomplete current bucket separately.",
            "Keep pair-specific controls, conditional null-preserving unit-root bootstrap, IUT and dependence-robust multiplicity unchanged.",
            "Only after freshness is satisfied may residual-z, n-step/TTR economics and threshold-aggression comparisons proceed.",
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
