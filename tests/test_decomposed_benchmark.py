from research.walk_forward_v3.decomposed_benchmark import performance_snapshot


DAY_NS = 86_400_000_000_000


def test_performance_snapshot_reports_frequency_turnover_pnl_and_risk():
    outcomes = [
        {
            "market_id": "m1",
            "asset": "BTC",
            "contract_horizon": "M5",
            "decision_ns": 1_000_000_000,
            "action": "TRADE",
            "side": "YES",
            "notional": 10.0,
            "exit_horizon_ms": 500,
            "target_state": "OBSERVED_FULL_FILL",
            "realized_pnl": 1.0,
            "replay_max_active_positions": 1,
            "replay_max_gross_notional": 10.0,
        },
        {
            "market_id": "m2",
            "asset": "ETH",
            "contract_horizon": "M5",
            "decision_ns": 1_000_000_000 + DAY_NS // 2,
            "action": "TRADE",
            "side": "NO",
            "notional": 5.0,
            "exit_horizon_ms": 500,
            "target_state": "OBSERVED_FULL_FILL",
            "realized_pnl": -0.5,
            "replay_max_active_positions": 1,
            "replay_max_gross_notional": 10.0,
        },
        {
            "market_id": "m3",
            "asset": "BTC",
            "contract_horizon": "M5",
            "decision_ns": 1_000_000_000 + DAY_NS,
            "action": "NO_TRADE",
            "reason": "LOWER_VALUE_NONPOSITIVE",
            "realized_pnl": 0.0,
            "observed": True,
            "replay_max_active_positions": 1,
            "replay_max_gross_notional": 10.0,
        },
    ]
    result = performance_snapshot(outcomes)
    assert result["paper_only"] is True
    assert result["authenticated_execution"] is False
    assert result["real_order_submission"] is False
    assert result["real_capital_at_risk"] is False
    assert result["opportunities"] == 3
    assert result["selected_trades"] == 2
    assert result["selected_trades_per_day"] == 2.0
    assert result["fill_count"] == 2
    assert result["fill_rate_given_selected"] == 1.0
    assert result["total_observed_net_pnl"] == 0.5
    assert result["net_pnl_per_day"] == 0.5
    assert result["turnover_notional"] == 15.0
    assert abs(result["net_pnl_per_dollar_turnover"] - 1.0 / 30.0) < 1e-12
    assert result["max_drawdown"] is not None
    assert result["risk"]["selected_trades"] == 2


def test_horse_race_source_is_research_only_and_has_no_promotion_path():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "research/walk_forward_v3/decomposed_benchmark.py"
    ).read_text()
    assert "NONE_RESEARCH_ONLY_NO_RUNTIME_PROMOTION" in source
    assert '"automatic_promotion": False' in source
    assert "A0_BASELINE" in source
    assert "A1_DIRECT_CHALLENGER" in source
    assert "A2_DECOMPOSED" in source


def test_frequency_frontier_is_minimum_size_and_research_only():
    from research.walk_forward_v3.decomposed_benchmark import (
        FREQUENCY_CONFIDENCE_SCALES,
        MINIMUM_SIZE_DIAGNOSTIC_POLICY,
    )
    assert FREQUENCY_CONFIDENCE_SCALES == (1.0, 0.50, 0.0)
    assert MINIMUM_SIZE_DIAGNOSTIC_POLICY.desired_notional(
        0.50, 10_000.0) == 0.0
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "research/walk_forward_v3/decomposed_benchmark.py"
    ).read_text()
    assert "VENUE_MINIMUM_ONLY_TO_ISOLATE_ENTRY_FREQUENCY" in source
    assert "NONE_FAST_DIAGNOSTIC_ONLY" in source
