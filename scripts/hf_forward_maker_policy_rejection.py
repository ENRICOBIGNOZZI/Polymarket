#!/usr/bin/env python3
"""Classify forward-maker quote policies using realized forward evidence only.

This research-only audit is intentionally fail-closed. It does not promote a
policy and never mutates the live champion. A policy is economically rejected
on the current sample only when it has a minimum number of observed fills and
the moving-block bootstrap upper confidence bound of ex-reward PnL per probe is
strictly negative. Sparse-fill policies remain MORE_EVIDENCE_REQUIRED rather
than being over-interpreted.
"""
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


def classify_policy(report: dict[str, Any], min_fills_for_rejection: int = 3) -> dict[str, Any]:
    policy = str(report.get("policy") or "unknown")
    any_fills = max(0, int(finite(report.get("any_fills"))))
    pair_fills = max(0, int(finite(report.get("pair_fills"))))
    one_sided = max(0, int(finite(report.get("one_sided_only"))))
    total_pnl = finite(report.get("total_pnl_ex_rewards_usd"))
    pnl_lcb = finite(report.get("block_bootstrap_lcb_mean_pnl_ex_rewards_per_probe_usd"))
    pnl_ucb = finite(report.get("block_bootstrap_ucb_mean_pnl_ex_rewards_per_probe_usd"))
    markout_60 = report.get("filled_share_weighted_markout_60_bid_per_share")
    markout_300 = report.get("filled_share_weighted_markout_300_bid_per_share")

    hard_negative = (
        any_fills >= max(1, min_fills_for_rejection)
        and total_pnl < 0.0
        and pnl_ucb < 0.0
    )
    state = "REJECT_CURRENT_SAMPLE" if hard_negative else "MORE_EVIDENCE_REQUIRED"
    reasons: list[str] = []
    if hard_negative:
        reasons.append("negative_ex_reward_pnl_with_negative_block_bootstrap_ucb")
    if any_fills > 0 and pair_fills == 0 and one_sided == any_fills:
        reasons.append("all_observed_fills_one_sided")
    if markout_60 is not None and finite(markout_60) < 0.0:
        reasons.append("negative_fill_weighted_markout_60")
    if markout_300 is not None and finite(markout_300) < 0.0:
        reasons.append("negative_fill_weighted_markout_300")
    if any_fills < max(1, min_fills_for_rejection):
        reasons.append("too_few_fills_for_hard_economic_rejection")

    return {
        "policy": policy,
        "research_state": state,
        "economically_rejected_on_current_sample": hard_negative,
        "reasons": reasons,
        "any_fills": any_fills,
        "pair_fills": pair_fills,
        "one_sided_only": one_sided,
        "total_pnl_ex_rewards_usd": total_pnl,
        "block_bootstrap_lcb_mean_pnl_ex_rewards_per_probe_usd": pnl_lcb,
        "block_bootstrap_ucb_mean_pnl_ex_rewards_per_probe_usd": pnl_ucb,
        "filled_share_weighted_markout_60_bid_per_share": markout_60,
        "filled_share_weighted_markout_300_bid_per_share": markout_300,
    }


def audit(calibration: dict[str, Any], min_fills_for_rejection: int = 3) -> dict[str, Any]:
    by_policy = calibration.get("by_policy")
    if not isinstance(by_policy, dict):
        raise ValueError("calibration.by_policy must be an object")
    policies = {
        str(name): classify_policy(report, min_fills_for_rejection)
        for name, report in sorted(by_policy.items())
        if isinstance(report, dict)
    }
    rejected = sorted(
        name for name, report in policies.items()
        if report["economically_rejected_on_current_sample"]
    )
    return {
        "schema": "polymarket_hf_forward_maker_policy_rejection_v1",
        "read_only": True,
        "real_money_eligible": False,
        "production_action": "no_change",
        "decision_rule": (
            "reject current sample only when observed fills meet the minimum and the "
            "moving-block bootstrap upper confidence bound of ex-reward PnL per probe is negative"
        ),
        "min_fills_for_rejection": max(1, int(min_fills_for_rejection)),
        "rejected_policies": rejected,
        "policies": policies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit forward-maker policy economics")
    parser.add_argument("--calibration", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-fills-for-rejection", type=int, default=3)
    args = parser.parse_args()

    calibration = json.loads(Path(args.calibration).read_text(encoding="utf-8"))
    payload = audit(calibration, args.min_fills_for_rejection)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
