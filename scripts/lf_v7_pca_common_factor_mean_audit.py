#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "scripts" / "v7_pca_stat_arb_core.py"
INFERENCE = ROOT / "scripts" / "v7_pca_stat_arb_inference.py"
CONFIG = ROOT / "config" / "research_v7_pca_stat_arb.json"


def direction(move: float) -> str:
    if move > 0.0:
        return "YES"
    if move < 0.0:
        return "NO"
    return "FLAT"


def deterministic_counterexamples() -> dict[str, Any]:
    cases = []
    for residual_move, common_move in ((-0.05, 0.20), (0.05, -0.20), (-0.05, 0.0)):
        total_move = residual_move + common_move
        cases.append(
            {
                "residual_expected_logit_move": residual_move,
                "common_expected_logit_move": common_move,
                "total_expected_logit_move": total_move,
                "incumbent_single_leg_side": direction(residual_move),
                "total_mean_single_leg_side": direction(total_move),
                "sign_reversal": direction(residual_move) != direction(total_move),
            }
        )
    return {
        "common_up_reverses_residual_no": cases[0],
        "common_down_reverses_residual_yes": cases[1],
        "zero_common_mean_control": cases[2],
    }


def source_contract() -> dict[str, Any]:
    core = CORE.read_text(encoding="utf-8")
    inference = INFERENCE.read_text(encoding="utf-8")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    return {
        "residual_move_is_entire_predicted_mean": "predicted_logit_move = residual_move * model.target_scale" in core,
        "single_leg_side_uses_predicted_move_sign": 'side = "YES" if score.predicted_logit_move > 0.0 else "NO"' in core,
        "common_component_enters_total_risk": "common_error = common[end] - common[start]" in inference,
        "common_component_does_not_update_predicted_mean_in_risk_wrapper": "return replace(score, sigma_logit=max(score.sigma_logit, total_sigma))" in inference,
        "uncertainty_penalty": float(cfg["execution_shadow"]["uncertainty_z"]),
        "single_leg_only": not bool(cfg.get("hedge_legs_allowed", True)),
    }


def audit() -> dict[str, Any]:
    contract = source_contract()
    cases = deterministic_counterexamples()
    defect = bool(
        contract["residual_move_is_entire_predicted_mean"]
        and contract["single_leg_side_uses_predicted_move_sign"]
        and contract["common_component_enters_total_risk"]
        and contract["common_component_does_not_update_predicted_mean_in_risk_wrapper"]
        and cases["common_up_reverses_residual_no"]["sign_reversal"]
        and cases["common_down_reverses_residual_yes"]["sign_reversal"]
        and not cases["zero_common_mean_control"]["sign_reversal"]
    )
    return {
        "status": "STRUCTURAL_BLOCKER" if defect else "NOT_REPRODUCED",
        "finding": (
            "V7 PCA is single-leg and unhedged, but its conditional mean forecast contains only residual "
            "mean reversion. The common PCA component is included in forecast-risk sigma but not in the "
            "expected target move that determines side and executable EV. A non-zero conditional common-mode "
            "mean can therefore reverse the correct single-leg direction."
        ),
        "source_contract": contract,
        "counterexamples": cases,
        "identity": (
            "For target logit z_t = c_t + r_t, the single-leg horizon mean is "
            "E[Delta z|I] = E[Delta c|I] + E[Delta r|I]. The incumbent uses only E[Delta r|I]."
        ),
        "required_successor": [
            "Apply the independent current-book residual-z repair before economic admission.",
            "Keep target-excluded PCA and conditional residual-unit-root inference unchanged.",
            "For each 30m/1h/2h/6h horizon, estimate or explicitly validate the conditional common-component mean using only pre-decision information.",
            "Use common-mean plus residual-mean as the single-leg expected logit move; retain common-component uncertainty around that mean.",
            "If the common-mode mean is not identified, abstain rather than treating zero as an untested economic assumption.",
            "Compare residual-only versus total-mean forecasts on identical chronological data and require forward executable fill-conditioned post-cost PnL before promotion.",
        ],
        "alpha_claim": False,
        "decision": "MORE_EVIDENCE_REQUIRED",
    }


def main() -> int:
    report = audit()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "STRUCTURAL_BLOCKER" else 1


if __name__ == "__main__":
    raise SystemExit(main())
