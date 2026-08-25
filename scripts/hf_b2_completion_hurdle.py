#!/usr/bin/env python3
"""Quantify execution hurdles for maker-positive B2 multi-leg candidates.

This is a read-only research diagnostic. It consumes public live-smoke evidence
and can audit the repository's paper-broker source contract. It never submits
orders or alters production configuration.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def leg_count(spec: Any) -> int:
    return sum(1 for raw in str(spec or "").split("|") if len(raw.split(":")) == 3)


def break_even_completion_probability(maker_edge: float, taker_edge: float) -> float | None:
    """Break-even probability under a hypothetical binary taker-fallback model."""
    if maker_edge <= 0.0 or taker_edge >= 0.0:
        return None
    denom = maker_edge - taker_edge
    if denom <= 0.0:
        return None
    return min(1.0, max(0.0, -taker_edge / denom))


def iid_per_leg_fill_floor(bundle_probability: float | None, legs: int) -> float | None:
    """Diagnostic per-leg fill floor under an explicitly hypothetical IID model."""
    if bundle_probability is None or legs <= 0:
        return None
    if not 0.0 <= bundle_probability <= 1.0:
        return None
    return bundle_probability ** (1.0 / legs)


def expected_edge_at_completion(
    completion_probability: float,
    maker_edge: float,
    taker_edge: float,
) -> float:
    """Binary maker-vs-taker scenario value; not the live broker's partial-fill PnL."""
    p = min(1.0, max(0.0, completion_probability))
    return p * maker_edge + (1.0 - p) * taker_edge


def _flag(source: str, needle: str) -> bool:
    return needle in source


def _numeric_arg(source: str, flag: str) -> float | None:
    match = re.search(rf"{re.escape(flag)}\s+([0-9]+(?:\.[0-9]+)?)", source)
    return float(match.group(1)) if match else None


def broker_semantics(broker_source: str, runtime_loop_source: str) -> dict[str, Any]:
    """Extract the live V5 multi-leg completion/unwind contract from source text."""
    threshold = _numeric_arg(runtime_loop_source, "--completion-threshold")
    submit_latency = _numeric_arg(runtime_loop_source, "--submit-latency-ms")
    cancel_latency = _numeric_arg(runtime_loop_source, "--cancel-latency-ms")
    max_leg_risk = _numeric_arg(runtime_loop_source, "--max-leg-risk-usd")

    minimum_leg_fraction = (
        _flag(broker_source, "return pm::minimum_completion(ft);")
        and _flag(broker_source, "if(c>=completion_threshold_)")
    )
    timeout_aborts = _flag(broker_source, 'abort_bundle(id,killed_?"drawdown_kill":"execution_timeout")')
    abort_unwinds = _flag(broker_source, 'exit_bundle(id,books,markets,"UNWOUND")')
    filled_only_exit = (
        _flag(broker_source, "if (l->filled_shares<=1e-12||l->exited) continue;")
        and _flag(broker_source, "sell_all(bk->second,l->filled_shares")
    )
    unmatched_risk_gate = (
        _flag(broker_source, "bundle_requires_unmatched_risk_check")
        and _flag(broker_source, "bundle_leg_risk(id)>max_leg_risk_usd_")
    )

    return {
        "runtime_completion_threshold_min_leg_fraction": threshold,
        "runtime_submit_latency_ms": submit_latency,
        "runtime_cancel_latency_ms": cancel_latency,
        "runtime_max_unmatched_leg_risk_usd": max_leg_risk,
        "completion_is_minimum_leg_fill_fraction": minimum_leg_fraction,
        "timeout_transitions_to_abort": timeout_aborts,
        "abort_unwinds_only_filled_inventory": abort_unwinds and filled_only_exit,
        "buys_missing_legs_as_taker_on_timeout": False if abort_unwinds and filled_only_exit else None,
        "unmatched_leg_risk_gate_remains_active_for_complete_state": unmatched_risk_gate,
        "binary_taker_fallback_matches_live_runtime": False if abort_unwinds and filled_only_exit else None,
    }


def candidate_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    coherence = payload.get("b2_coherence")
    if isinstance(coherence, dict) and isinstance(coherence.get("top_raw"), list):
        source = coherence["top_raw"]
    else:
        candidates = payload.get("candidates")
        source = candidates.get("b2", []) if isinstance(candidates, dict) else []
    return [row for row in source if isinstance(row, dict)]


def analyze(
    payload: dict[str, Any],
    *,
    broker_source: str | None = None,
    runtime_loop_source: str | None = None,
) -> dict[str, Any]:
    semantics = None
    runtime_threshold = None
    if broker_source is not None and runtime_loop_source is not None:
        semantics = broker_semantics(broker_source, runtime_loop_source)
        runtime_threshold = semantics.get("runtime_completion_threshold_min_leg_fraction")

    rows: list[dict[str, Any]] = []
    for candidate in candidate_rows(payload):
        maker = finite(candidate.get("maker_entry_net_edge"), math.nan)
        taker = finite(candidate.get("taker_net_edge"), math.nan)
        if not (math.isfinite(maker) and math.isfinite(taker) and maker > 0.0 and taker < 0.0):
            continue
        legs = leg_count(candidate.get("legs"))
        break_even = break_even_completion_probability(maker, taker)
        per_leg = iid_per_leg_fill_floor(break_even, legs)
        row = {
            "market": str(candidate.get("market") or ""),
            "slug": str(candidate.get("slug") or ""),
            "legs": legs,
            "raw_expected_edge": finite(candidate.get("raw_expected_edge"), math.nan),
            "maker_entry_net_edge": maker,
            "taker_net_edge": taker,
            "hypothetical_binary_taker_fallback_break_even_probability": break_even,
            "iid_per_leg_fill_floor_for_hypothetical_break_even": per_leg,
            "binary_scenario_expected_edge_if_completion_75pct": expected_edge_at_completion(0.75, maker, taker),
            "binary_scenario_expected_edge_if_completion_80pct": expected_edge_at_completion(0.80, maker, taker),
            "binary_scenario_expected_edge_if_completion_90pct": expected_edge_at_completion(0.90, maker, taker),
            "binary_scenario_expected_edge_if_completion_95pct": expected_edge_at_completion(0.95, maker, taker),
            "binary_scenario_expected_edge_if_completion_99pct": expected_edge_at_completion(0.99, maker, taker),
            "interpretation": (
                "The binary taker-fallback hurdle is a counterfactual stress diagnostic. "
                "It is not the live broker's partial-fill economics and the IID per-leg number is not an estimator."
            ),
        }
        if runtime_threshold is not None:
            row["runtime_completion_threshold_min_leg_fraction"] = runtime_threshold
            row["hypothetical_break_even_exceeds_runtime_completion_threshold"] = bool(
                break_even is not None and break_even > float(runtime_threshold)
            )
        rows.append(row)

    rows.sort(key=lambda row: float(row["maker_entry_net_edge"]), reverse=True)
    result: dict[str, Any] = {
        "schema": "polymarket_hf_b2_completion_hurdle_v2",
        "source_git_sha": payload.get("git_sha"),
        "source_generated_ts": payload.get("generated_ts"),
        "read_only": True,
        "submitted_orders": 0,
        "maker_positive_candidates": len(rows),
        "rows": rows,
        "economic_acceptance_target": (
            "Estimate the joint event-time distribution of common minimum completion c, "
            "leg-specific excess fills above c, unmatched-entry risk, fill-conditioned markout, "
            "and timeout unwind PnL. Static maker/taker candidate edges alone cannot identify "
            "the live broker's expected execution PnL."
        ),
    }
    if semantics is not None:
        result["runtime_broker_semantics"] = semantics
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-smoke", type=Path, required=True)
    parser.add_argument("--broker-source", type=Path)
    parser.add_argument("--runtime-loop-source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.live_smoke.read_text(encoding="utf-8"))
    broker_source = args.broker_source.read_text(encoding="utf-8") if args.broker_source else None
    runtime_loop_source = args.runtime_loop_source.read_text(encoding="utf-8") if args.runtime_loop_source else None
    if (broker_source is None) != (runtime_loop_source is None):
        raise SystemExit("--broker-source and --runtime-loop-source must be supplied together")
    result = analyze(
        payload,
        broker_source=broker_source,
        runtime_loop_source=runtime_loop_source,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
