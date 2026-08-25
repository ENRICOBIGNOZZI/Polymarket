#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class HorizonCase:
    phi: float
    fidelity_minutes: int
    time_to_resolution_hours: float
    exit_buffer_hours: float = 0.25
    min_hold_hours: float = 0.5


def half_life_bars(phi: float) -> float:
    if not 0.0 < phi < 1.0:
        raise ValueError("phi must be in (0,1)")
    return -math.log(2.0) / math.log(phi)


def incumbent_hold_hours(phi: float) -> float:
    """Replicate V6's current implicit one-hour-bar hold conversion."""
    bars = max(1.0, min(24.0, 2.0 * half_life_bars(phi)))
    return bars


def natural_hold_hours(phi: float, fidelity_minutes: int) -> float:
    if fidelity_minutes <= 0:
        raise ValueError("fidelity_minutes must be positive")
    bars = max(1.0, min(24.0, 2.0 * half_life_bars(phi)))
    return bars * fidelity_minutes / 60.0


def guarded_hold_hours(case: HorizonCase) -> float | None:
    latest_exit = case.time_to_resolution_hours - case.exit_buffer_hours
    if latest_exit < case.min_hold_hours:
        return None
    return min(natural_hold_hours(case.phi, case.fidelity_minutes), latest_exit)


def horizon_matched_residual_change(phi: float, deviation: float, hold_hours: float, fidelity_minutes: int) -> float:
    if hold_hours < 0.0 or fidelity_minutes <= 0:
        raise ValueError("invalid horizon")
    bars = hold_hours * 60.0 / fidelity_minutes
    return (phi ** bars - 1.0) * deviation


def source_contract(source: str) -> dict[str, object]:
    market_block_match = re.search(r"@dataclass\s+class Market:\s*(.*?)(?:\n\n|\Z)", source, flags=re.S)
    market_block = market_block_match.group(1) if market_block_match else ""
    expiry_tokens = ("end_date", "endDate", "expiry", "resolution", "time_to_resolution")
    has_expiry = any(token in market_block for token in expiry_tokens)
    has_half_life_hold = "2.0 * max(half_lives" in source and "hold = now + int(bars * 3600)" in source
    if not has_half_life_hold:
        has_half_life_hold = "2.0 * max(half_lives" in source and "hold=now + int(bars * 3600)" in source
    build_sig = re.search(r"def build_pair_intent\((.*?)\) ->", source, flags=re.S)
    signature = build_sig.group(1) if build_sig else ""
    passes_fidelity = "fidelity" in signature
    passes_expiry = any(token in signature for token in expiry_tokens)
    uses_one_step_change = "(phi - 1.0) * (resid[-1] - rmu)" in source
    return {
        "market_has_expiry_or_resolution_metadata": has_expiry,
        "hold_is_two_half_lives_capped_24_then_times_3600": has_half_life_hold,
        "build_pair_intent_receives_fidelity": passes_fidelity,
        "build_pair_intent_receives_expiry_or_ttr": passes_expiry,
        "candidate_uses_one_step_ar_change": uses_one_step_change,
    }


def evaluate(case: HorizonCase) -> dict[str, object]:
    incumbent_hours = incumbent_hold_hours(case.phi)
    natural_hours = natural_hold_hours(case.phi, case.fidelity_minutes)
    guarded_hours = guarded_hold_hours(case)
    row: dict[str, object] = {
        **asdict(case),
        "half_life_bars": half_life_bars(case.phi),
        "incumbent_hold_hours": incumbent_hours,
        "fidelity_aware_natural_hold_hours": natural_hours,
        "incumbent_exit_after_resolution_hours": max(0.0, incumbent_hours - case.time_to_resolution_hours),
        "guarded_hold_hours": guarded_hours,
        "one_step_residual_change_per_unit_deviation": case.phi - 1.0,
    }
    if guarded_hours is None:
        row["decision"] = "ABSTAIN_TTR_TOO_SHORT"
        row["horizon_matched_change_per_unit_deviation"] = None
    else:
        row["decision"] = "MARKOUT_HORIZON_VALID"
        row["horizon_matched_change_per_unit_deviation"] = horizon_matched_residual_change(
            case.phi, 1.0, guarded_hours, case.fidelity_minutes
        )
    return row


def build_report(source: str) -> dict[str, object]:
    contract = source_contract(source)
    cases = [
        HorizonCase(0.90, 60, 2.0),
        HorizonCase(0.90, 60, 6.0),
        HorizonCase(0.90, 60, 18.0),
        HorizonCase(0.95, 60, 4.0),
        HorizonCase(0.90, 30, 6.0),
    ]
    results = [evaluate(case) for case in cases]
    return {
        "decision": "MORE_EVIDENCE_REQUIRED",
        "finding": "V6 local-factor exit horizon is not bound to market resolution and assumes one-hour bars inside build_pair_intent.",
        "source_contract": contract,
        "deterministic_cases": results,
        "successor_contract": {
            "market_metadata": "carry point-in-time expiry/resolution timestamp into every local-factor candidate",
            "hold_horizon": "min(two half-lives, 24h, time-to-resolution minus exit buffer), converted using fidelity_minutes",
            "short_ttr": "abstain from markout RV when no pre-resolution exit window remains; terminal probability requires a separate calibrated model",
            "forecast_horizon": "use the AR n-step residual change for the actual guarded hold horizon, not the one-step change",
            "chronology": "evaluate only point-in-time metadata and histories available before each decision",
            "economics": "retain executable bid/ask, depth, fees, slippage, partial-fill and unwind accounting",
        },
        "paper_aggressive_common_sample": {
            "incumbent": {"markets": 400, "min_liquidity": 10, "max_clusters": 15, "min_common_points": 48, "min_abs_z": 1.0},
            "challenger_after_horizon_repair": {"markets": 700, "min_liquidity": 5, "max_clusters": 30, "min_common_points": 36, "min_abs_z": 0.75},
            "unchanged_economic_floor": {"min_edge": 0.0002, "max_trade_usd": 60, "slippage_bps": 5},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("scripts/v6_local_factor_intents.py"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(args.source.read_text(encoding="utf-8"))
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
