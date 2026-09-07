#!/usr/bin/env python3
"""Fail-closed activation gate for the frozen BTC M5 external-cancel overlay.

The forward evaluator remains research zero-authority.  This gate converts its
immutable report into a PAPER execution-alpha eligibility fact only after all
pre-registered success conditions are independently rechecked.  It never grants
real-money authority and never promotes a policy automatically.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any


REPORT_SCHEMA = "polymarket_v7_btc_m5_external_cancel_forward_report_v3"
OUTPUT_SCHEMA = "polymarket_v7_external_cancel_activation_v1"
EXPERIMENT_ID = "btc-m5-external-cancel-overlay-forward-v1"


class ActivationError(ValueError):
    pass


def _finite(value: Any, name: str) -> float:
    if value is None or isinstance(value, bool):
        raise ActivationError(name)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ActivationError(name) from exc
    if not math.isfinite(number):
        raise ActivationError(name)
    return number


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActivationError(name)
    return value


def evaluate_activation(report: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(report, dict) or report.get("schema") != REPORT_SCHEMA:
        raise ActivationError("report_schema")
    if report.get("experiment_id") != EXPERIMENT_ID:
        raise ActivationError("experiment_identity")
    if (
        report.get("paper_only") is not True
        or report.get("authenticated_execution") is not False
        or report.get("real_order_submission") is not False
        or report.get("real_money_authority") is not False
        or report.get("automatic_promotion") is not False
    ):
        raise ActivationError("unsafe_report_authority")

    state = str(report.get("state") or "")
    reasons = report.get("reason_codes")
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise ActivationError("reason_codes")

    market_count = _integer(report.get("market_count"), "market_count")
    avoidable_events = _integer(report.get("avoidable_fill_events"), "avoidable_fill_events")
    minimum_markets = _integer(report.get("minimum_markets"), "minimum_markets")
    minimum_avoidable = _integer(
        report.get("minimum_avoidable_fill_events"), "minimum_avoidable_fill_events"
    )
    primary = report.get("equal_weight_500ms_improvement_per_share")
    leave_best = report.get("leave_best_market_out_500ms_improvement_per_share")
    positive_fraction = report.get("positive_market_fraction")
    stress = report.get("stress_3x_queue_200ms_cancel_improvement_per_share")

    checks: dict[str, bool] = {
        "state_pass": state == "PASS",
        "minimum_independent_markets": market_count >= minimum_markets > 0,
        "minimum_avoidable_fill_events": avoidable_events >= minimum_avoidable > 0,
        "reason_codes_empty": reasons == [],
    }
    if state == "PASS":
        checks.update({
            "primary_500ms_positive": _finite(primary, "primary_500ms") > 0.0,
            "leave_best_market_out_positive": _finite(leave_best, "leave_best_market_out") > 0.0,
            "positive_market_fraction_ge_70pct": _finite(positive_fraction, "positive_market_fraction") >= 0.70,
            "queue_3x_cancel_200ms_positive": _finite(stress, "stress_3x_queue_200ms") > 0.0,
        })
    else:
        checks.update({
            "primary_500ms_positive": False,
            "leave_best_market_out_positive": False,
            "positive_market_fraction_ge_70pct": False,
            "queue_3x_cancel_200ms_positive": False,
        })

    eligible = all(checks.values())
    failed_checks = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema": OUTPUT_SCHEMA,
        "experiment_id": EXPERIMENT_ID,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_money_authority": False,
        "automatic_promotion": False,
        "paper_execution_alpha_overlay_eligible": eligible,
        "manual_exact_sha_promotion_required": True,
        "frozen_rule_retuning_allowed": False,
        "forward_state": state,
        "checks": checks,
        "failed_checks": failed_checks,
        "forward_reason_codes": sorted(set(reasons)),
        "evidence": {
            "market_count": market_count,
            "minimum_markets": minimum_markets,
            "avoidable_fill_events": avoidable_events,
            "minimum_avoidable_fill_events": minimum_avoidable,
            "equal_weight_500ms_improvement_per_share": primary,
            "leave_best_market_out_500ms_improvement_per_share": leave_best,
            "positive_market_fraction": positive_fraction,
            "stress_3x_queue_200ms_cancel_improvement_per_share": stress,
            "freeze_merge_sha": report.get("freeze_merge_sha"),
            "rule_sha256": report.get("rule_sha256"),
        },
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        decision = evaluate_activation(report)
    except (OSError, json.JSONDecodeError, ActivationError) as exc:
        decision = {
            "schema": OUTPUT_SCHEMA,
            "experiment_id": EXPERIMENT_ID,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_money_authority": False,
            "automatic_promotion": False,
            "paper_execution_alpha_overlay_eligible": False,
            "manual_exact_sha_promotion_required": True,
            "frozen_rule_retuning_allowed": False,
            "forward_state": "INVALID_OR_MISSING",
            "checks": {},
            "failed_checks": [f"FAIL_CLOSED:{type(exc).__name__}:{exc}"],
            "forward_reason_codes": [],
            "evidence": {},
        }
        atomic_json(args.output, decision)
        print(json.dumps(decision, sort_keys=True))
        return 2
    atomic_json(args.output, decision)
    print(json.dumps(decision, sort_keys=True))
    return 0 if decision["paper_execution_alpha_overlay_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
