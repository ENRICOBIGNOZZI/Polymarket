import json
from pathlib import Path

from research.walk_forward_v3.maker_vs_taker import compare, direct_action_view


def taker():
    return {
        "schema":"polymarket_direct_action_value_v3_walk_forward_v1",
        "state":"READY",
        "paper_only":True,
        "authenticated_execution":False,
        "real_order_submission":False,
        "real_capital_at_risk":False,
        "summary":{
            "selected_trades":10,
            "observed_selected_trades":8,
            "censored_selected_trades":2,
            "total_observed_net_pnl":1.25,
            "mean_observed_net_pnl":0.15625,
            "max_active_positions":2,
            "max_gross_notional":4.0,
        },
        "folds":[{"fold":1}],
    }


def test_taker_view_keeps_censoring_as_promotion_blocker():
    view=direct_action_view(taker())
    assert view["total_observed_net_pnl"]==1.25
    assert view["observed_fraction"]==.8
    assert "CENSORED_SELECTED_TRADES" in view["promotion_blockers"]
    assert view["promotion_grade"] is False


def test_historical_maker_never_becomes_comparable_winner():
    maker={
        "schema":"polymarket_v7_maker_bilateral_fillability_report_v1",
        "paper_only":True,
        "authenticated_execution":False,
        "real_order_submission":False,
        "real_capital_at_risk":False,
        "economics":{
            "unique_order_ids":57,
            "unique_fill_ids":0,
            "realized_pnl":0,
            "promotion_gate":"MORE_EVIDENCE_REQUIRED",
        },
    }
    result=compare(taker(),maker)
    assert result["ranking"]=="NONE"
    assert result["winner"] is None
    assert result["comparability"]=="DIAGNOSTIC_ONLY_DIFFERENT_EVIDENCE_DESIGNS"
    assert "HISTORICAL_OR_SHADOW_ONLY" in result["maker"]["promotion_blockers"]


def test_forward_maker_receipt_still_cannot_auto_select_strategy():
    maker={
        "schema":"polymarket_v7_maker_forward_window_report_v2",
        "paper_only":True,
        "authenticated_execution":False,
        "real_order_submission":False,
        "automatic_promotion":False,
        "state":"COMPLETE",
        "experiment_id":"maker-test",
        "code_sha":"a"*40,
        "authority_source_audit":{"orders_unproven":0},
        "endpoints":{"share_weighted_250ms_markout":0.01},
    }
    result=compare(taker(),maker)
    assert result["ranking"]=="NONE"
    assert result["winner"] is None
    assert result["maker"]["promotion_grade"] is False
    assert result["comparability"]==(
        "FORWARD_MAKER_VS_OUTER_OOS_TAKER_DIFFERENT_WINDOWS")
