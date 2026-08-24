#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def filled_leg_fragility(result: dict[str, Any], side: str) -> dict[str, Any] | None:
    leg = result.get(side) or {}
    filled = max(0.0, finite(leg.get("filled_shares")))
    if filled <= 0.0:
        return None
    queue = max(0.0, finite(leg.get("initial_queue_ahead")))
    compatible = max(0.0, finite(leg.get("compatible_sell_volume")))
    erase_multiplier = compatible / queue if queue > 0.0 else None
    headroom = erase_multiplier - 1.0 if erase_multiplier is not None else None
    unmatched = max(0.0, finite(result.get("unmatched_yes_shares"))) + max(
        0.0, finite(result.get("unmatched_no_shares"))
    )
    downside = max(0.0, -finite(result.get("conservative_pnl_ex_rewards_usd")))
    downside_per_share = downside / unmatched if unmatched > 0.0 else 0.0
    paired_gain = max(0.0, finite(result.get("locked_edge_per_matched_share")))
    denominator = downside_per_share + paired_gain
    break_even_pair_probability = downside_per_share / denominator if denominator > 0.0 else None
    return {
        "market_id": str(result.get("market_id") or ""),
        "policy": str(result.get("policy") or ""),
        "side": side.upper(),
        "filled_shares": filled,
        "initial_queue_ahead": queue,
        "compatible_sell_volume": compatible,
        "queue_erase_multiplier": erase_multiplier,
        "queue_relative_headroom": headroom,
        "markout_60_bid_per_share": leg.get("markout_60_bid_per_share"),
        "markout_300_bid_per_share": leg.get("markout_300_bid_per_share"),
        "one_sided_downside_per_share": downside_per_share,
        "paired_locked_edge_per_share": paired_gain,
        "break_even_pair_completion_probability": break_even_pair_probability,
    }


def analyze(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("probe payload must contain a results list")

    policies = sorted({str(row.get("policy") or "") for row in results if isinstance(row, dict)})
    summaries: dict[str, Any] = {}
    all_filled_legs: list[dict[str, Any]] = []

    for policy in policies:
        rows = [row for row in results if isinstance(row, dict) and str(row.get("policy") or "") == policy]
        any_fill = [row for row in rows if bool(row.get("any_fill"))]
        pair_fill = [row for row in rows if bool(row.get("pair_fill"))]
        one_sided = [row for row in rows if bool(row.get("one_sided_only"))]
        downside = sum(max(0.0, -finite(row.get("conservative_pnl_ex_rewards_usd"))) for row in one_sided)
        unmatched = sum(
            max(0.0, finite(row.get("unmatched_yes_shares")))
            + max(0.0, finite(row.get("unmatched_no_shares")))
            for row in one_sided
        )
        exit_fees = sum(max(0.0, finite(row.get("exit_fees_usd"))) for row in one_sided)
        conditional_rewards = sum(max(0.0, finite(row.get("conditional_prorated_reward_usd"))) for row in rows)
        filled_legs = [
            item
            for row in rows
            for side in ("yes", "no")
            if (item := filled_leg_fragility(row, side)) is not None
        ]
        all_filled_legs.extend(filled_legs)

        markout_weight = 0.0
        markout_sum = 0.0
        for item in filled_legs:
            mark = item.get("markout_60_bid_per_share")
            if mark is None:
                continue
            weight = max(0.0, finite(item.get("filled_shares")))
            markout_weight += weight
            markout_sum += weight * finite(mark)

        break_evens = [
            finite(item.get("break_even_pair_completion_probability"))
            for item in filled_legs
            if item.get("break_even_pair_completion_probability") is not None
        ]
        queue_headrooms = [
            finite(item.get("queue_relative_headroom"))
            for item in filled_legs
            if item.get("queue_relative_headroom") is not None
        ]
        total_pnl = sum(finite(row.get("conservative_pnl_ex_rewards_usd")) for row in rows)
        total_with_rewards = sum(finite(row.get("conditional_pnl_including_reward_usd")) for row in rows)

        summaries[policy] = {
            "probes": len(rows),
            "any_fill_count": len(any_fill),
            "pair_fill_count": len(pair_fill),
            "one_sided_only_count": len(one_sided),
            "pair_completion_given_any_fill": len(pair_fill) / len(any_fill) if any_fill else None,
            "pnl_ex_rewards_usd": total_pnl,
            "conditional_pnl_with_rewards_usd": total_with_rewards,
            "one_sided_downside_usd": downside,
            "one_sided_downside_per_share": downside / unmatched if unmatched > 0.0 else None,
            "exit_fee_fraction_of_one_sided_downside": exit_fees / downside if downside > 0.0 else None,
            "conditional_reward_offset_fraction": conditional_rewards / downside if downside > 0.0 else None,
            "filled_share_weighted_markout_60": markout_sum / markout_weight if markout_weight > 0.0 else None,
            "minimum_queue_relative_headroom": min(queue_headrooms) if queue_headrooms else None,
            "maximum_break_even_pair_completion_probability": max(break_evens) if break_evens else None,
            "filled_legs": filled_legs,
        }

    severe = [
        item
        for item in all_filled_legs
        if item.get("break_even_pair_completion_probability") is not None
        and finite(item.get("break_even_pair_completion_probability")) >= 0.90
    ]
    fragile = [
        item
        for item in all_filled_legs
        if item.get("queue_relative_headroom") is not None
        and finite(item.get("queue_relative_headroom")) <= 0.25
    ]
    negative_markout = [
        item for item in all_filled_legs if item.get("markout_60_bid_per_share") is not None and finite(item.get("markout_60_bid_per_share")) < 0.0
    ]

    return {
        "schema": "polymarket_forward_maker_fragility_v1",
        "source_schema": payload.get("schema"),
        "source_generated_ts": payload.get("generated_ts"),
        "read_only": True,
        "production_action": "no_change",
        "research_state": "MORE_EVIDENCE_REQUIRED",
        "policy_summaries": summaries,
        "diagnostics": {
            "filled_leg_count": len(all_filled_legs),
            "break_even_pair_probability_ge_90pct_count": len(severe),
            "queue_headroom_le_25pct_count": len(fragile),
            "negative_60s_markout_count": len(negative_markout),
        },
        "interpretation": (
            "A passive complete-set edge is not sufficient evidence of executable alpha. "
            "Promotion requires repeated paired fills, robust queue headroom, and non-adverse post-fill markout."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    report = analyze(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "forward_maker_fragility"
        f" filled_legs={report['diagnostics']['filled_leg_count']}"
        f" severe_pair_gate={report['diagnostics']['break_even_pair_probability_ge_90pct_count']}"
        f" fragile_queue={report['diagnostics']['queue_headroom_le_25pct_count']}"
        f" negative_markout60={report['diagnostics']['negative_60s_markout_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
