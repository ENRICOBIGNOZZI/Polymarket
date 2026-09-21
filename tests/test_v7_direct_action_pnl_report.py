import json
import math
from pathlib import Path

from scripts.v7_direct_action_pnl_report import build, render_svg, write_csv


def sample_result():
    return {
        "schema": "polymarket_direct_action_value_v3_walk_forward_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "data_sha256": "abc",
        "summary": {"selected_trades": 3},
        "diagnostic_selected_outcomes": [
            {
                "action": "TRADE",
                "decision_ns": 1_000_000_000,
                "market_id": "m1",
                "asset": "BTC",
                "side": "YES",
                "size": 5.0,
                "notional": 2.0,
                "exit_horizon_ms": 500,
                "latency_ms": 50,
                "effective_action_age_ms": 60.0,
                "policy_utility": .2,
                "predicted_total_net_cash_pnl": .3,
                "realized_pnl": .1,
                "censored_worst_case_pnl": -.5,
                "cash_pnl_policy_regret": {
                    "same_horizon_cash_regret": .02,
                    "all_horizon_cash_regret": .03,
                },
            },
            {
                "action": "TRADE",
                "decision_ns": 2_000_000_000,
                "market_id": "m2",
                "asset": "ETH",
                "side": "NO",
                "size": 4.0,
                "notional": 1.5,
                "exit_horizon_ms": 1000,
                "latency_ms": 50,
                "effective_action_age_ms": 90.0,
                "policy_utility": .1,
                "predicted_total_net_cash_pnl": .2,
                "realized_pnl": None,
                "censored_worst_case_pnl": -.4,
                "cash_pnl_policy_regret": {
                    "same_horizon_cash_regret": None,
                    "all_horizon_cash_regret": None,
                },
            },
            {
                "action": "TRADE",
                "decision_ns": 3_000_000_000,
                "market_id": "m3",
                "asset": "BTC",
                "side": "YES",
                "size": 3.0,
                "notional": 1.0,
                "exit_horizon_ms": 500,
                "latency_ms": 50,
                "effective_action_age_ms": 110.0,
                "policy_utility": .15,
                "predicted_total_net_cash_pnl": .25,
                "realized_pnl": -.2,
                "censored_worst_case_pnl": -.3,
                "cash_pnl_policy_regret": {
                    "same_horizon_cash_regret": .1,
                    "all_horizon_cash_regret": .2,
                },
            },
        ],
    }


def test_pnl_over_time_tracks_observed_and_worst_case_separately():
    report = build(sample_result(), rolling_trades=2, bucket_seconds=1)
    assert report["diagnostics_complete"] is True
    assert report["summary"]["observed_selected_trades"] == 2
    assert report["summary"]["censored_selected_trades"] == 1
    assert math.isclose(report["summary"]["total_observed_pnl"], -.1)
    assert math.isclose(
        report["summary"]["total_worst_case_pnl_lower_bound"], -.5)
    assert math.isclose(report["summary"]["observed_hit_rate"], .5)
    assert math.isclose(report["summary"]["same_horizon_regret_total"], .12)
    assert math.isclose(report["summary"]["all_horizon_regret_total"], .23)
    path = report["trade_path"]
    assert math.isclose(path[0]["cumulative_observed_pnl"], .1)
    assert math.isclose(path[1]["cumulative_observed_pnl"], .1)
    assert math.isclose(path[1]["cumulative_worst_case_pnl"], -.3)
    assert math.isclose(path[2]["cumulative_observed_pnl"], -.1)
    assert math.isclose(path[2]["cumulative_worst_case_pnl"], -.5)


def test_pnl_report_marks_bounded_diagnostics_as_partial():
    value = sample_result()
    value["summary"]["selected_trades"] = 4
    report = build(value)
    assert report["diagnostics_complete"] is False
    assert report["coverage_warning"] is not None


def test_pnl_report_writes_csv_and_svg(tmp_path: Path):
    report = build(sample_result())
    csv_path = tmp_path / "path.csv"
    svg_path = tmp_path / "curve.svg"
    write_csv(report, csv_path)
    render_svg(report, svg_path)
    assert csv_path.read_text().startswith("asset,")
    svg = svg_path.read_text()
    assert "Direct Action OOS cumulative PnL" in svg
    assert "worst-case lower bound" in svg
