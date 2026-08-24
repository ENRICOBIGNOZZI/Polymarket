#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


_NUMBER = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_kv_log(line: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for token in line.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if not key:
            continue
        if _NUMBER.match(value):
            number = float(value)
            out[key] = int(number) if number.is_integer() else number
        else:
            out[key] = value
    return out


def classify_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = _number(candidate.get("raw_expected_edge"))
    maker = _number(candidate.get("maker_entry_net_edge"))
    taker = _number(candidate.get("taker_net_edge"))
    maker_wedge = raw - maker
    taker_wedge = raw - taker

    if raw <= 0.0:
        failure = "no_raw_edge"
    elif maker <= 0.0 and taker <= 0.0:
        failure = "execution_cost_bound"
    else:
        failure = "post_cost_positive"

    return {
        "market": str(candidate.get("market", "")),
        "side": str(candidate.get("side", "")),
        "raw_edge": raw,
        "maker_edge": maker,
        "taker_edge": taker,
        "maker_cost_wedge": maker_wedge,
        "taker_cost_wedge": taker_wedge,
        "maker_break_even_raw_edge": max(0.0, maker_wedge),
        "taker_break_even_raw_edge": max(0.0, taker_wedge),
        "maker_required_raw_multiple": (maker_wedge / raw) if raw > 0.0 else None,
        "taker_required_raw_multiple": (taker_wedge / raw) if raw > 0.0 else None,
        "failure": failure,
    }


def analyze(snapshot: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    source = snapshot.get("candidates", {})
    for family in ("b1", "b2"):
        for row in source.get(family, []) or []:
            item = classify_candidate(row)
            item["family"] = family
            candidates.append(item)

    raw_positive = [row for row in candidates if row["raw_edge"] > 0.0]
    post_cost_positive = [
        row for row in raw_positive if row["maker_edge"] > 0.0 or row["taker_edge"] > 0.0
    ]
    execution_bound = [row for row in raw_positive if row["failure"] == "execution_cost_bound"]

    logs = snapshot.get("logs", {})
    funnel = {
        family: parse_kv_log((logs.get(family) or [""])[0])
        for family in ("b1", "b2")
    }

    strategies = {
        item.get("name"): item
        for item in config.get("multi_strategy", {}).get("strategies", [])
        if isinstance(item, dict) and item.get("name")
    }

    threshold_relaxation_rejected = bool(raw_positive) and not post_cost_positive
    decision = (
        "REJECT_THRESHOLD_RELAXATION_CURRENT_SAMPLE"
        if threshold_relaxation_rejected
        else "MORE_EVIDENCE_REQUIRED"
    )

    return {
        "schema": "polymarket_v5_activity_frontier_v1",
        "source_sha": snapshot.get("git_sha"),
        "generated_ts": snapshot.get("generated_ts"),
        "decision": decision,
        "paper_only": True,
        "candidate_counts": {
            "total": len(candidates),
            "raw_positive": len(raw_positive),
            "post_cost_positive": len(post_cost_positive),
            "execution_cost_bound": len(execution_bound),
        },
        "candidates": candidates,
        "funnel": funnel,
        "incumbent": {
            "market_limit": config.get("market_limit"),
            "min_liquidity": config.get("min_liquidity"),
            "strategies": {
                name: {
                    "capital_fraction": item.get("capital_fraction"),
                    "market_limit": item.get("overrides", {}).get("market_limit"),
                    "interval_seconds": item.get("overrides", {}).get("interval_seconds"),
                    "min_net_edge": item.get("overrides", {}).get("min_net_edge"),
                }
                for name, item in strategies.items()
            },
        },
        "hypothesis_test": {
            "lower_thresholds_can_rescue_current_raw_positive_rows": not threshold_relaxation_rejected,
            "reason": (
                "All observed raw-positive B1/B2 rows are non-positive after executable maker and taker costs; "
                "lowering an admission threshold cannot change their post-cost sign."
                if threshold_relaxation_rejected
                else "Current evidence does not establish that threshold relaxation is dominated."
            ),
        },
        "recommended_ablation_order": [
            "run_all_five_v5_sleeves_and_publish_per_cycle_funnel",
            "expand_universe_and_lower_discovery_liquidity_floor_with_post_cost_gates_unchanged",
            "test_model_specific_factor_relation_reversion_and_execution_changes_on_common_chronological_rows",
            "consider_threshold_relaxation_only_for_post_cost_positive_threshold_blocked_rows",
        ],
        "promotion_requirements": [
            "same chronological rows for incumbent and challenger",
            "positive incremental realized net paper PnL",
            "positive incremental utility at 1x, 1.5x and 2x execution costs",
            "queue/fill/latency and adverse-selection evidence where maker execution is required",
            "existing OOS, drawdown, concentration and kill-switch gates remain unchanged",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze the V5 opportunity funnel before relaxing economic gates")
    parser.add_argument("--live-smoke", required=True)
    parser.add_argument("--config", default="config/paper_v5.json")
    parser.add_argument("--output")
    args = parser.parse_args()

    snapshot = json.loads(Path(args.live_smoke).read_text(encoding="utf-8"))
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    report = analyze(snapshot, config)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
