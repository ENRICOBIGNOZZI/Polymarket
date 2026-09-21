"""Zero-authority maker-vs-taker economic challenger.

This module does not choose or rank a strategy. It puts the already-existing
Maker research endpoints and Direct Action taker endpoints in one typed receipt,
while preserving their different evidence semantics and promotion blockers.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

SCHEMA = "polymarket_v7_maker_vs_taker_challenger_v1"
SAFETY = {
    "paper_only": True,
    "authenticated_execution": False,
    "real_order_submission": False,
    "real_capital_at_risk": False,
}


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def load(path: Path):
    value=json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value,dict):
        raise ValueError("JSON object required")
    return value


def direct_action_view(report):
    summary=report.get("summary") or {}
    folds=report.get("folds") or []
    if report.get("paper_only") is not True:
        raise ValueError("Direct Action report not PAPER")
    observed=int(summary.get("observed_selected_trades") or 0)
    selected=int(summary.get("selected_trades") or 0)
    pnl=summary.get("total_observed_net_pnl")
    return {
        "strategy_family":"DIRECT_ACTION_TAKER",
        "state":report.get("state"),
        "evidence_semantics":"OUTER_OOS_EXECUTABLE_CASH_PNL_WITH_CAUSAL_CENSORING",
        "selected_trades":selected,
        "observed_selected_trades":observed,
        "censored_selected_trades":int(summary.get("censored_selected_trades") or 0),
        "observed_fraction":observed/selected if selected else None,
        "total_observed_net_pnl":float(pnl) if finite(pnl) else None,
        "mean_observed_net_pnl":summary.get("mean_observed_net_pnl"),
        "max_active_positions":summary.get("max_active_positions"),
        "max_gross_notional":summary.get("max_gross_notional"),
        "support_probability":summary.get("selected_evidence_support_probability"),
        "selection_optimism_penalty_total":summary.get(
            "total_predicted_selection_optimism_penalty"),
        "support_robustness_penalty_total":summary.get(
            "total_support_robustness_penalty"),
        "fold_count":len(folds),
        "promotion_grade":False,
        "promotion_blockers":[
            "NO_AUTOMATIC_PROMOTION",
            *(
                ["CENSORED_SELECTED_TRADES"]
                if int(summary.get("censored_selected_trades") or 0)>0 else []
            ),
        ],
    }


def maker_forward_view(report):
    if report.get("paper_only") is not True:
        raise ValueError("Maker report not PAPER")
    schema=str(report.get("schema") or "")
    if not schema.startswith("polymarket_v7_maker_forward_window_report"):
        raise ValueError("canonical Maker forward report required")
    endpoints=report.get("endpoints") or report.get("primary_endpoints") or {}
    return {
        "strategy_family":"SELECTIVE_MAKER",
        "state":report.get("state"),
        "evidence_semantics":"PREREGISTERED_FORWARD_WINDOW_CANONICAL_MAKER",
        "experiment_id":report.get("experiment_id"),
        "code_sha":report.get("code_sha"),
        "authority_source_audit":report.get("authority_source_audit"),
        "endpoints":endpoints,
        "promotion_grade":False,
        "promotion_blockers":[
            "NO_AUTOMATIC_PROMOTION",
            *(
                []
                if report.get("state") in (
                    "COMPLETE", "PASS", "COMPLETED"
                )
                else ["MAKER_FORWARD_WINDOW_NOT_COMPLETE_OR_NOT_PASSING"]
            ),
        ],
    }


def maker_historical_view(report):
    if report.get("paper_only") is not True:
        raise ValueError("Maker historical report not PAPER")
    schema=str(report.get("schema") or "")
    allowed=(
        "polymarket_v7_maker_execution_horse_race_v1",
        "polymarket_v7_maker_decision_time_toxicity_v1",
        "polymarket_v7_maker_bilateral_fillability_report_v1",
        "polymarket_v7_selective_pnl_challenger_report_v1",
    )
    if schema not in allowed:
        raise ValueError("unsupported Maker historical report")
    return {
        "strategy_family":"SELECTIVE_MAKER",
        "state":report.get("state") or "DIAGNOSTIC",
        "schema":schema,
        "evidence_semantics":"HISTORICAL_OR_SHADOW_DIAGNOSTIC_NOT_FORWARD_PROOF",
        "maker_diagnostics":report,
        "promotion_grade":False,
        "promotion_blockers":[
            "HISTORICAL_OR_SHADOW_ONLY",
            "NO_AUTOMATIC_PROMOTION",
        ],
    }


def compare(taker_report, maker_report):
    taker=direct_action_view(taker_report)
    maker_schema=str(maker_report.get("schema") or "")
    if maker_schema.startswith("polymarket_v7_maker_forward_window_report"):
        maker=maker_forward_view(maker_report)
        comparability="FORWARD_MAKER_VS_OUTER_OOS_TAKER_DIFFERENT_WINDOWS"
    else:
        maker=maker_historical_view(maker_report)
        comparability="DIAGNOSTIC_ONLY_DIFFERENT_EVIDENCE_DESIGNS"

    return {
        "schema":SCHEMA,
        **SAFETY,
        "state":"READY",
        "automatic_promotion":False,
        "ranking":"NONE",
        "winner":None,
        "comparability":comparability,
        "decision_rule":(
            "NO_STRATEGY_SELECTION_FROM_THIS_REPORT;"
            "MAKER_REQUIRES_ITS_OWN_PREREGISTERED_FORWARD_SUCCESS;"
            "TAKER_REQUIRES_ITS_OWN_HELDOUT_SUPPORT_AND_RISK_GATES"
        ),
        "taker":taker,
        "maker":maker,
    }


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--taker-report",type=Path,required=True)
    parser.add_argument("--maker-report",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    result=compare(load(args.taker_report),load(args.maker_report))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
