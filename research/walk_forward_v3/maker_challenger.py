"""Aggregate zero-authority Maker challenger evidence.

The challenger is deliberately separate from Direct Action taker learning.
It combines:
- descriptive fillability / queue evidence;
- chronological fill-conditioned toxicity and markout test metrics;
- cancel-overlay horse race when fills exist;
- optional frozen-forward and complete-set evidence.

A product such as fill-rate * fill-conditioned markout is exposed only as a
screening statistic. It is never a promotion claim because the two estimates
can come from different cohorts and because subsidy / inventory economics may
be incomplete.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

SCHEMA = "polymarket_v7_maker_challenger_v1"
SAFETY = {
    "paper_only": True,
    "authenticated_execution": False,
    "real_order_submission": False,
    "real_capital_at_risk": False,
}


def read(path: Path | None):
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def number(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def fillability_actions(report):
    output = {}
    for row in (report or {}).get("actions") or []:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "UNKNOWN").upper()
        orders = int(number(row.get("orders")) or 0)
        filled = number(row.get("filled_orders"))
        if filled is None:
            filled = (
                (number(row.get("full_fills")) or 0)
                + (number(row.get("partial_fills")) or 0)
            )
        fill_rate = float(filled) / orders if orders > 0 else None
        output[action] = {
            "orders": orders,
            "filled_orders": int(filled),
            "empirical_fill_rate": fill_rate,
            "trade_reachable": int(number(row.get("trade_reachable")) or 0),
            "fill_opportunities": int(number(row.get("fill_opportunities")) or 0),
            "mean_rest_ms": number(row.get("mean_rest_ms")),
            "mean_near_miss_ratio": number(row.get("mean_near_miss_ratio")),
        }
    return output


def toxicity_by_action(reports):
    output = {}
    for report in reports:
        if not isinstance(report, dict):
            continue
        actions = report.get("placement_actions") or []
        if len(actions) != 1:
            continue
        action = str(actions[0]).upper()
        test = report.get("test_safe_to_quote") or {}
        metrics = (report.get("metrics") or {}).get("test") or {}
        output[action] = {
            "state": report.get("state"),
            "eligible_fill_rows": int(number(report.get("eligible_fill_rows")) or 0),
            "independent_fill_clusters": int(
                number(report.get("independent_fill_clusters")) or 0),
            "threshold_role": (
                ((report.get("validation_safe_threshold_selection") or {})
                 .get("threshold"))
            ),
            "test_rows": int(number(test.get("rows")) or 0),
            "test_coverage": number(test.get("coverage")),
            "test_filled_shares": number(test.get("filled_shares")),
            "test_adverse_rate": number(test.get("adverse_rate")),
            "test_share_weighted_markout_per_share": number(
                test.get("share_weighted_markout_per_share")),
            "test_auc": number(metrics.get("auc")),
            "test_brier": number(metrics.get("brier")),
        }
    return output


def build(fillability, toxicity_reports, horse_race=None,
          complete_set=None, forward_report=None):
    fills = fillability_actions(fillability)
    tox = toxicity_by_action(toxicity_reports)
    actions = {}
    for action in sorted(set(fills) | set(tox)):
        fill = fills.get(action, {})
        toxic = tox.get(action, {})
        p_fill = fill.get("empirical_fill_rate")
        markout = toxic.get("test_share_weighted_markout_per_share")
        screening = (
            p_fill * markout
            if p_fill is not None and markout is not None
            else None
        )
        actions[action] = {
            "fillability": fill,
            "toxicity_oos": toxic,
            "trading_only_screening_ev_per_submitted_share": screening,
            "screening_semantics": (
                "EMPIRICAL_FILL_RATE_TIMES_FILL_CONDITIONED_TEST_MARKOUT;"
                "DESCRIPTIVE_ONLY_NOT_A_JOINT_OOS_ESTIMATOR"
            ),
            "rebate_reward_credit": "NOT_INCLUDED_UNLESS_SEPARATELY_OBSERVED",
            "inventory_legging_cost": "NOT_NETTED_IN_SCREENING_SCORE",
        }

    ranked = sorted(
        (
            (action, value["trading_only_screening_ev_per_submitted_share"])
            for action, value in actions.items()
            if value["trading_only_screening_ev_per_submitted_share"] is not None
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    forward_state = (
        str((forward_report or {}).get("state") or "UNAVAILABLE")
    )
    complete_state = (
        str((complete_set or {}).get("state") or "UNAVAILABLE")
    )
    return {
        "schema": SCHEMA,
        **SAFETY,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "state": (
            "FORWARD_EVIDENCE_AVAILABLE"
            if forward_report is not None
            else "SCREENING_ONLY_NO_FROZEN_FORWARD_REPORT"
        ),
        "objective": (
            "MAKER_FILLABILITY_AND_FILL_CONDITIONED_MARKOUT;"
            "QUEUE_TOXICITY_LEGGING_AND_SUBSIDY_KEPT_SEPARATE"
        ),
        "actions": actions,
        "screening_order": [
            {"action": action, "screening_ev_per_submitted_share": value}
            for action, value in ranked
        ],
        "screening_order_warning": (
            "NOT_POLICY_SELECTION_NOT_JOINT_OOS_NOT_PROMOTION_GRADE"
        ),
        "fillability_root_cause": (fillability or {}).get("root_cause"),
        "fillability_next_experiment": (fillability or {}).get("next_experiment"),
        "fillability_forward_exact_ws": (
            (fillability or {}).get("forward_exact_ws") or {}
        ),
        "cancel_overlay_horse_race": horse_race,
        "complete_set_shadow": complete_set,
        "complete_set_state": complete_state,
        "frozen_forward_report": forward_report,
        "frozen_forward_state": forward_state,
        "profitability_proven": False,
        "promotion_gate": (
            "REQUIRES_FROZEN_FORWARD_OOS_WITH_CANONICAL_FILLS_MARKOUTS_AND_COSTS"
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fillability", type=Path, required=True)
    parser.add_argument("--toxicity", type=Path, action="append", default=[])
    parser.add_argument("--horse-race", type=Path)
    parser.add_argument("--complete-set", type=Path)
    parser.add_argument("--forward-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build(
        read(args.fillability) or {},
        [read(path) for path in args.toxicity if read(path) is not None],
        read(args.horse_race),
        read(args.complete_set),
        read(args.forward_report),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
