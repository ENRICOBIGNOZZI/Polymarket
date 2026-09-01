from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_crypto_economic_validation import assess  # noqa: E402


REGISTRY = ROOT / "config/v7_crypto_settlement_markets.json"


def mature() -> dict:
    return {
        "settlement_labeled_contracts": 400, "days": 40,
        "terminal_economic_units": 400, "day_block_lcb_pnl": 1.0,
        "net_pnl_2x_costs": 2.0, "costs_complete": True,
        "observed_drawdown": 0.08, "executable_capacity_usd": 1000.0,
        "capital_hours": 250.0, "calibration_stable": True,
        "regime_stratified": True, "source_health_stratified": True,
        "brier_score": 0.20, "log_loss": 0.60, "maker_reach": 0.40,
        "fill_given_reach": 0.30, "fill_conditioned_markout": -0.001,
        "taker_fill_fraction": 0.70, "latency_decay": 0.002, "net_pnl": 10.0,
    }


def test_all_contexts_are_gated_independently_and_missing_evidence_fails_closed() -> None:
    report = assess(REGISTRY, {"contexts": {"BTC_M5": mature()}})
    assert report["context_count"] == 8
    assert report["contexts"]["BTC_M5"]["economically_ready"] is True
    assert report["contexts"]["BTC_M5"]["new_risk_authorized"] is False
    assert report["contexts"]["ETH_M5"]["economically_ready"] is False
    assert "EVIDENCE_MISSING" in report["contexts"]["ETH_M5"]["blocking_reasons"]


def test_frozen_thresholds_and_manual_promotion_cannot_be_bypassed() -> None:
    weak = mature()
    weak.update({"terminal_economic_units": 299, "days": 29, "net_pnl_2x_costs": -1})
    report = assess(REGISTRY, {"contexts": {"SOL_M15": weak}})["contexts"]["SOL_M15"]
    assert report["economically_ready"] is False
    assert report["automatic_promotion"] is False
    assert report["new_risk_authorized"] is False
    assert {"TERMINAL_UNITS_LT_300", "INDEPENDENT_DAY_BLOCKS_LT_30", "TWO_X_COST_PNL_NOT_POSITIVE"} <= set(report["blocking_reasons"])


if __name__ == "__main__":
    test_all_contexts_are_gated_independently_and_missing_evidence_fails_closed()
    test_frozen_thresholds_and_manual_promotion_cannot_be_bypassed()
