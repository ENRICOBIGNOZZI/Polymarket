from research.walk_forward_v3 import multi_alpha_2h as m


def _row(decision_id, ns, market="m1", asset="BTC", horizon="M5", features=None):
    return {
        "decision_id": decision_id,
        "decision_ns": ns,
        "market_id": market,
        "asset": asset,
        "horizon": horizon,
        "features": features or {},
    }


def test_required_baseline_grid_is_exact():
    assert tuple(m.LATENCIES) == (5, 10, 25, 50, 100, 250)
    assert tuple(m.EXITS) == (
        100, 250, 500, 750, 1000, 1500, 2000, 3000, 4000, 5000, 7500, 10000
    )
    assert m.SIZE == 5.0


def test_family_classifier_covers_tier1_and_slow_state():
    cases = {
        "external.binance_return_100ms_bp": "cross_venue",
        "tape.external.aggregate_trade_imbalance": "flow",
        "tape.external.aggregate_ofi": "ofi",
        "tape.derivatives.perp_basis_bp": "perp",
        "tape.derivatives.open_interest": "oi_funding",
        "tape.derivatives.funding_rate": "oi_funding",
        "tape.derivatives.liquidation_imbalance": "liquidations",
        "external.native_vol_fast": "volatility",
        "tape.leader_features.common_factor_move": "cross_asset",
        "tape.derivatives.deribit_atm_iv": "options",
        "tape.distance_to_reference_bp": "settlement",
        "tape.pm_yes_imbalance": "pm_response",
        "signal_return_bp": "baseline",
        "tape.execution.queue_ahead": "maker_queue",
        "tape.execution.fill_probability": "maker_queue",
        "tape.execution.inventory_fraction": "maker_inventory",
        "tape.execution.toxic_fill_probability": "maker_toxicity",
        "tape.execution.predicted_markout": "maker_toxicity",
    }
    assert {name: m.classify_source_feature(name) for name in cases} == cases


def test_backward_asof_join_never_uses_future():
    index = {
        "m1": {
            "stamps": [100, 200, 300],
            "rows": [
                {"available_at_ns": 100, "features": {"tape.external.return_100ms_bp": 1.0}},
                {"available_at_ns": 200, "features": {"tape.external.return_100ms_bp": 2.0}},
                {"available_at_ns": 300, "features": {"tape.external.return_100ms_bp": 3.0}},
            ],
        }
    }
    rows = [_row("a", 250)]
    joined, diag = m.attach_rich_state(rows, index, delay_ms=0)
    assert joined[0]["features"]["tape.external.return_100ms_bp"] == 2.0
    assert joined[0]["rich_feature_available_at_ns"] == 200
    assert diag["joined_rows"] == 1

    delayed, _ = m.attach_rich_state(rows, index, delay_ms=1)
    # 1ms is much larger than the synthetic 250ns decision clock, so no row
    # can satisfy the delayed cutoff. Fail closed rather than using future data.
    assert "tape.external.return_100ms_bp" not in delayed[0]["features"]


def test_window_selection_uses_quality_not_pnl():
    hour = 60 * 60 * 1_000_000_000
    rows = []
    for i in range(181):
        ns = i * 60 * 1_000_000_000
        asset = "BTC" if i % 2 else "ETH"
        row = _row(
            str(i), ns, market="m" + str(i % 4), asset=asset,
            features={"signal_return_bp": float(i % 3)},
        )
        row["cash_pnl"] = 10_000_000.0 if 60 <= i < 120 else -10_000_000.0
        rows.append(row)
    cache = {str(i): (object(), None) for i in range(181)}
    first = m.select_two_hour_window(rows, cache)

    for row in rows:
        row["cash_pnl"] *= -1.0
    second = m.select_two_hour_window(rows, cache)
    assert first["start_ns"] == second["start_ns"]
    assert first["end_ns"] - first["start_ns"] == 2 * hour
    assert first["profitability_used_for_selection"] is False


def test_information_gate_keeps_pm_core_and_adds_declared_families():
    assert m.core_model_feature_allowed("state.ask", frozenset())
    assert not m.core_model_feature_allowed(
        "state.cross_venue_consensus_100ms_bp", frozenset()
    )
    assert m.core_model_feature_allowed(
        "state.cross_venue_consensus_100ms_bp", frozenset({"cross_venue"})
    )
    assert not m.core_model_feature_allowed(
        "x.tape.derivatives.funding_rate", frozenset({"perp"})
    )
    assert m.core_model_feature_allowed(
        "x.tape.derivatives.funding_rate", frozenset({"oi_funding"})
    )


def test_nested_information_sets_are_monotone():
    previous = set()
    for key in ("F0", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "F13", "F14"):
        current = set(m.NESTED[key])
        assert previous <= current
        previous = current


def test_realized_future_fields_are_rejected_from_information_sets():
    for name in (
        "tape.realized_markout_1s",
        "future_return_250ms",
        "label_fill",
        "outcome_pnl",
        "post_fill_markout",
    ):
        assert m.classify_source_feature(name) is None


def test_monotonicity_report_detects_shape():
    surface = {"cells": {}}
    for latency in m.LATENCIES:
        for horizon in m.EXITS:
            surface["cells"][f"{latency}::{horizon}"] = {
                "pnl_per_fill": -float(latency),
                "pnl_per_observed_action": -float(latency),
                "hit_rate": 1.0 / (1.0 + latency),
                "observed_actions": 100,
                "fills": max(1, 100 - int(latency / 3)),
            }
    report = m.monotonicity_report(surface)
    assert report["latency"]["pnl_per_fill"][str(m.EXITS[0])]["shape"] == "NONINCREASING"
    assert report["latency"]["hit_rate"][str(m.EXITS[0])]["shape"] == "NONINCREASING"
