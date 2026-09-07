from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_external_cancel_activation import evaluate_activation  # noqa: E402


def report(state: str = "PASS") -> dict:
    return {
        "schema": "polymarket_v7_btc_m5_external_cancel_forward_report_v3",
        "experiment_id": "btc-m5-external-cancel-overlay-forward-v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_money_authority": False,
        "automatic_promotion": False,
        "state": state,
        "market_count": 35,
        "minimum_markets": 30,
        "avoidable_fill_events": 60,
        "minimum_avoidable_fill_events": 50,
        "equal_weight_500ms_improvement_per_share": 0.002,
        "leave_best_market_out_500ms_improvement_per_share": 0.001,
        "positive_market_fraction": 0.80,
        "stress_3x_queue_200ms_cancel_improvement_per_share": 0.0005,
        "reason_codes": [],
        "freeze_merge_sha": "a" * 40,
        "rule_sha256": "b" * 64,
    }


def test_valid_pass_only_grants_paper_overlay_eligibility() -> None:
    result = evaluate_activation(report())
    assert result["paper_execution_alpha_overlay_eligible"] is True
    assert result["real_money_authority"] is False
    assert result["automatic_promotion"] is False
    assert result["manual_exact_sha_promotion_required"] is True
    assert result["frozen_rule_retuning_allowed"] is False


def test_insufficient_forward_evidence_stays_disabled() -> None:
    value = report("FORWARD_EVIDENCE_INSUFFICIENT")
    value["market_count"] = 10
    value["avoidable_fill_events"] = 12
    value["reason_codes"] = [
        "INSUFFICIENT_INDEPENDENT_MARKETS", "INSUFFICIENT_AVOIDABLE_FILL_EVENTS",
    ]
    result = evaluate_activation(value)
    assert result["paper_execution_alpha_overlay_eligible"] is False
    assert "state_pass" in result["failed_checks"]
    assert "minimum_independent_markets" in result["failed_checks"]


def test_spoofed_pass_with_failed_stress_is_rejected() -> None:
    value = report()
    value["stress_3x_queue_200ms_cancel_improvement_per_share"] = -0.0001
    result = evaluate_activation(value)
    assert result["paper_execution_alpha_overlay_eligible"] is False
    assert result["checks"]["queue_3x_cancel_200ms_positive"] is False


def test_pass_with_nonempty_reason_codes_is_rejected() -> None:
    value = report()
    value["reason_codes"] = ["SUSPICIOUS"]
    result = evaluate_activation(value)
    assert result["paper_execution_alpha_overlay_eligible"] is False
    assert result["checks"]["reason_codes_empty"] is False


def test_unsafe_authority_claim_is_rejected() -> None:
    value = report()
    value["real_money_authority"] = True
    try:
        evaluate_activation(value)
    except ValueError as exc:
        assert str(exc) == "unsafe_report_authority"
    else:
        raise AssertionError("unsafe report authority accepted")


if __name__ == "__main__":
    test_valid_pass_only_grants_paper_overlay_eligibility()
    test_insufficient_forward_evidence_stays_disabled()
    test_spoofed_pass_with_failed_stress_is_rejected()
    test_pass_with_nonempty_reason_codes_is_rejected()
    test_unsafe_authority_claim_is_rejected()
