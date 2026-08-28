#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_external_fair_report import build_report, to_markdown  # noqa: E402


def main() -> None:
    forecasting = {
        "models": {
            "PM Mid": {"scores": {"log_loss": 0.69, "brier": 0.25, "ece": 0.08,
                                    "calibration_slope": 0.7, "contracts": 20}},
            "Full External": {"scores": {"log_loss": 0.55, "brier": 0.18, "ece": 0.03,
                                          "calibration_slope": 0.95, "contracts": 20},
                              "coverage": 0.90, "net_replay_pnl": 1.5},
        }
    }
    opportunities = [
        {"contract_id": "c1", "robust_edge_per_share": 0.003,
         "requested_quantity": 2.0, "filled_quantity": 1.0, "realized_net_pnl": 0.02},
        {"contract_id": "c2", "robust_edge_per_share": 0.012,
         "requested_quantity": 1.0, "filled_quantity": 1.0, "realized_net_pnl": 0.03},
    ]
    cancels = [
        {"contract_id": "c1", "reason": "FAIR_SHOCK", "would_fill": True,
         "would_markout": -0.02, "estimated_loss_avoided": 0.02},
        {"contract_id": "c2", "reason": "ORACLE_INVALID", "would_fill": False,
         "estimated_loss_avoided": 0.01},
    ]
    actions = [
        {"contract_id": "c1", "action": "CANCEL", "purpose": "RISK",
         "expected_ev": 0.0, "counterfactual_value": 0.01},
        {"contract_id": "c2", "action": "TAKE", "purpose": "ALPHA",
         "expected_ev": 0.04, "realized_pnl": 0.03},
    ]
    pnl = {
        "gross_trading_pnl": 0.05,
        "taker_fees": 0.005,
        "net_trading_pnl": 0.045,
        "total_economic_pnl": 0.045,
    }

    cold = build_report(
        forecasting=forecasting,
        opportunities=opportunities,
        cancels=cancels,
        actions=actions,
        pnl=pnl,
        maker_execution_evidence="COLD_START",
        forward_shadow_contracts=20,
        synthetic_test_only=True,
    )
    assert cold["economic_validation_state"] == "NOT_VALIDATED"
    assert cold["C_maker_repricing"]["economically_promotable"] is False
    fair_shock = next(row for row in cold["B_cancel_overlay"]["table"]
                      if row["cancel_reason"] == "FAIR_SHOCK")
    assert fair_shock["estimated_loss_avoed"] if False else True  # spelling guard below
    assert fair_shock["estimated_loss_avoided"] is None
    assert fair_shock["counterfactual_cold_start"] is True
    assert cold["D_informed_taker"]["robust_edge_table"]
    markdown = to_markdown(cold)
    assert "Synthetic/unit-test evidence only" in markdown
    assert "COLD_START" in markdown

    mature = build_report(
        forecasting=forecasting,
        opportunities=opportunities,
        cancels=cancels,
        actions=actions,
        pnl=pnl,
        maker_execution_evidence="MATURE",
        forward_shadow_contracts=20,
        synthetic_test_only=False,
    )
    assert mature["economic_validation_state"] == "EVIDENCE_AVAILABLE"
    fair_shock_mature = next(row for row in mature["B_cancel_overlay"]["table"]
                             if row["cancel_reason"] == "FAIR_SHOCK")
    assert abs(fair_shock_mature["estimated_loss_avoided"] - 0.02) < 1e-12
    assert fair_shock_mature["counterfactual_cold_start"] is False
    assert mature["C_maker_repricing"]["economically_promotable"] is True


if __name__ == "__main__":
    main()
