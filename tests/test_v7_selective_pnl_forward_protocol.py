import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = json.loads((ROOT / "config" / "v7_selective_pnl_forward_protocol.json").read_text())
CHALLENGER = json.loads((ROOT / "config" / "v7_selective_pnl_challenger.json").read_text())


def test_forward_protocol_is_paper_only_and_zero_authority():
    assert PROTOCOL["schema"] == "polymarket_v7_selective_pnl_forward_protocol_v1"
    assert PROTOCOL["paper_only"] is True
    assert PROTOCOL["authenticated_execution"] is False
    assert PROTOCOL["real_order_submission"] is False
    assert PROTOCOL["real_capital_at_risk"] is False
    assert PROTOCOL["execution_authority"] == "ZERO_AUTHORITY_RESEARCH_ONLY"
    assert PROTOCOL["automatic_promotion"] is False


def test_forward_window_is_fixed_eight_hours_one_look():
    window = PROTOCOL["window"]
    assert window["duration_hours"] == 8
    assert window["post_window_grace_seconds"] == 60
    assert window["look_policy"] == "ONE_FIXED_END_OF_WINDOW_ANALYSIS"
    assert window["no_early_economic_peeking"] is True
    assert window["no_threshold_change_after_start"] is True
    assert window["no_policy_change_after_start"] is True
    assert window["no_sizing_change_after_start"] is True


def test_directional_primary_matches_challenger_not_historical_posthoc_side_filter():
    forward = PROTOCOL["directional"]
    challenger = CHALLENGER["directional_lane"]
    assert forward["allowed_sides"] == ["YES", "NO"]
    assert forward["primary_minimum_net_edge_after_2x_cost_per_share"] == challenger[
        "primary_minimum_net_edge_after_2x_cost_per_share"
    ] == 0.01
    assert forward["research_threshold_grid"] == challenger["research_threshold_grid"]
    assert PROTOCOL["prospective_hypotheses"]["no_side_underperformance"]["may_affect_selection_or_sizing"] is False


def test_latency_and_cancel_identity_match_challenger():
    assert PROTOCOL["latency"]["scan_interval_ms"] == CHALLENGER["latency"]["candidate_scan_interval_ms"] == 250
    assert PROTOCOL["latency"]["synthetic_revalidation_sleep_ms"] == 0
    assert PROTOCOL["latency"]["decision_to_arrival_slo_ms"] == CHALLENGER["latency"]["decision_to_arrival_slo_ms"]
    assert PROTOCOL["external_cancel"]["rule_id"] == CHALLENGER["maker_gate"]["external_cancel"]["rule_id"]
    assert PROTOCOL["external_cancel"]["maximum_signal_age_ms"] == 100


def test_regime_grid_is_report_only_and_identical():
    forward = PROTOCOL["regimes"]
    assert forward["report_only"] is True
    assert forward["may_change_primary_selection_in_same_window"] is False
    for key, value in CHALLENGER["regimes"].items():
        assert forward[key] == value
