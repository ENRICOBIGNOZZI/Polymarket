import json
import math
from pathlib import Path

from research.walk_forward_v3.maker_challenger import build
import ops.v7_maker_challenger_ssm as runner


ROOT = Path(__file__).resolve().parents[1]


def test_maker_challenger_screening_is_not_promotion_claim():
    fillability = {
        "root_cause": "QUEUE_NOT_EXHAUSTED",
        "actions": [
            {"action": "JOIN", "orders": 100, "filled_orders": 10},
            {"action": "IMPROVE1", "orders": 50, "filled_orders": 10},
        ],
    }
    toxicity = [
        {
            "state": "FIT_COMPLETE_ZERO_AUTHORITY",
            "placement_actions": ["JOIN"],
            "eligible_fill_rows": 20,
            "independent_fill_clusters": 12,
            "test_safe_to_quote": {
                "rows": 5,
                "coverage": .5,
                "filled_shares": 25,
                "adverse_rate": .2,
                "share_weighted_markout_per_share": .01,
            },
            "metrics": {"test": {"auc": .7, "brier": .1}},
        },
        {
            "state": "FIT_COMPLETE_ZERO_AUTHORITY",
            "placement_actions": ["IMPROVE1"],
            "eligible_fill_rows": 20,
            "independent_fill_clusters": 12,
            "test_safe_to_quote": {
                "rows": 5,
                "coverage": .5,
                "filled_shares": 25,
                "adverse_rate": .2,
                "share_weighted_markout_per_share": -.02,
            },
            "metrics": {"test": {"auc": .7, "brier": .1}},
        },
    ]
    result = build(fillability, toxicity)
    assert result["paper_only"] is True
    assert result["automatic_promotion"] is False
    assert result["profitability_proven"] is False
    assert result["state"] == "SCREENING_ONLY_NO_FROZEN_FORWARD_REPORT"
    assert math.isclose(
        result["actions"]["JOIN"][
            "trading_only_screening_ev_per_submitted_share"],
        .001, abs_tol=1e-12,
    )
    assert math.isclose(
        result["actions"]["IMPROVE1"][
            "trading_only_screening_ev_per_submitted_share"],
        -.004, abs_tol=1e-12,
    )
    assert result["screening_order"][0]["action"] == "JOIN"
    assert "NOT_POLICY_SELECTION" in result["screening_order_warning"]


def test_maker_challenger_forward_evidence_has_priority_without_auto_promotion():
    result = build(
        {"actions": []},
        [],
        forward_report={
            "schema": "polymarket_v7_maker_forward_window_report_v2",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "state": "READY",
        },
    )
    assert result["state"] == "FORWARD_EVIDENCE_AVAILABLE"
    assert result["frozen_forward_state"] == "READY"
    assert result["automatic_promotion"] is False


def test_maker_challenger_source_archive_is_regular_files_only():
    assert runner.SOURCE_PATHS
    for relative in runner.SOURCE_PATHS:
        assert (ROOT / relative).is_file(), relative
    payload = runner.source_archive(ROOT)
    assert isinstance(payload, bytes)
    assert len(payload) > 0



def test_causal_value_evidence_has_priority_over_descriptive_screening():
    causal = {
        "schema": "polymarket_v7_maker_value_challenger_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "state": "READY",
        "positive_lcb_actions_diagnostic_only": ["JOIN"],
    }
    result = build({"actions": []}, [], causal_value=causal)
    assert result["state"] == "CAUSAL_VALUE_EVIDENCE_AVAILABLE"
    assert result["causal_value_state"] == "READY"
    assert result["causal_positive_lcb_actions_diagnostic_only"] == ["JOIN"]
    assert result["profitability_proven"] is False
    assert result["automatic_promotion"] is False
