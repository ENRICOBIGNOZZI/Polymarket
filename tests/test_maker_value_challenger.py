import math

from research.walk_forward_v3.maker_value_challenger import (
    action_cells,
    build_episodes,
    complete_set_summary,
    verified_subsidies,
)


SHA = "a" * 40


def placement_features():
    return {
        "spread_ticks": 2.0,
        "imbalance": 0.1,
        "ofi": 0.2,
        "ew_vol_ticks": 1.0,
        "trade_intensity": 2.0,
        "cancel_intensity": 1.0,
        "short_return_ticks": 0.1,
        "inventory_fraction": 0.0,
        "local_latency_ms": 1.0,
        "aggressive_sell_prints_per_second": 3.0,
        "aggressive_buy_prints_per_second": 3.0,
        "distance_from_touch_ticks": 0.0,
    }


def order(order_id, cluster, ts, *, action="JOIN", intended=5.0):
    return {
        "event_type": "ORDER_SUBMITTED",
        "record_id": "order-" + order_id,
        "recorded_ts_ms": ts,
        "model_sha": SHA,
        "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "order_id": order_id,
        "event_id": cluster,
        "market_id": "market-" + cluster,
        "token_id": "token-" + cluster,
        "side": "BUY",
        "intended_size": intended,
        "queue_ahead": 5.0,
        "bid": 0.49,
        "ask": 0.51,
        "metadata": {
            "component": "professional_maker",
            "placement_action": action,
            "outcome": "YES",
            "placement_features": placement_features(),
        },
    }


def fill(order_id, cluster, ts, fill_id, size=5.0):
    return {
        "event_type": "FILL",
        "record_id": "fill-" + fill_id,
        "recorded_ts_ms": ts,
        "model_sha": SHA,
        "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "order_id": order_id,
        "fill_id": fill_id,
        "event_id": cluster,
        "market_id": "market-" + cluster,
        "filled_size": size,
        "metadata": {"component": "professional_maker"},
    }


def markout(order_id, cluster, ts, fill_id, value):
    return {
        "event_type": "MARKOUT",
        "record_id": "mark-" + fill_id,
        "recorded_ts_ms": ts,
        "model_sha": SHA,
        "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "order_id": order_id,
        "fill_id": fill_id,
        "event_id": cluster,
        "market_id": "market-" + cluster,
        "markouts": {"1s": value},
        "metadata": {"component": "professional_maker"},
    }


def terminal(order_id, cluster, ts, state="CANCELLED"):
    return {
        "event_type": "ORDER_STATE",
        "record_id": "terminal-" + order_id,
        "recorded_ts_ms": ts,
        "model_sha": SHA,
        "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "order_id": order_id,
        "event_id": cluster,
        "market_id": "market-" + cluster,
        "order_state": state,
        "metadata": {"component": "professional_maker"},
    }


def test_signed_markout_is_the_value_label_and_spread_is_not_added_twice():
    rows = [
        order("o1", "c1", 1000),
        fill("o1", "c1", 1100, "f1", 5.0),
        markout("o1", "c1", 2100, "f1", 0.02),
        terminal("o1", "c1", 2200, "FILLED"),
    ]
    episodes, diagnostics = build_episodes(rows, markout_horizon="1s")
    assert diagnostics["episodes"] == 1
    episode = episodes[0]
    assert episode["economic_observed"] is True
    assert episode["spread"] == 0.02
    assert math.isclose(episode["signed_markout_dollars"], 0.10, abs_tol=1e-12)
    assert math.isclose(episode["value_per_posted_share"], 0.02, abs_tol=1e-12)
    assert episode["label_semantics"].endswith("NO_SEPARATE_SPREAD_CAPTURE_ADD")


def test_terminal_no_fill_is_zero_but_open_or_unlabelled_fill_is_censored():
    rows = [
        order("o1", "c1", 1000),
        terminal("o1", "c1", 1300),
        order("o2", "c2", 2000),
        order("o3", "c3", 3000),
        fill("o3", "c3", 3100, "f3", 5.0),
        terminal("o3", "c3", 3200, "FILLED"),
    ]
    episodes, diagnostics = build_episodes(rows, markout_horizon="1s")
    by_order = {row["order_id"]: row for row in episodes}
    assert by_order["o1"]["economic_observed"] is True
    assert by_order["o1"]["value_per_posted_share"] == 0.0
    assert by_order["o2"]["economic_observed"] is False
    assert by_order["o3"]["economic_observed"] is False
    assert diagnostics["open_censored_orders"] == 1
    assert diagnostics["filled_orders_missing_markout"] == 1


def test_action_cell_lcb_is_based_on_cluster_bootstrap_and_no_make_baseline_is_zero():
    rows = []
    for index in range(12):
        rows.append({
            "action": "JOIN",
            "event_cluster": "c" + str(index),
            "economic_observed": True,
            "signed_markout_dollars": 0.10,
            "intended_shares": 5.0,
            "filled_shares": 5.0,
            "filled_fraction": 1.0,
            "queue_ahead_per_quote": 1.0,
            "assigned_lifetime_ms": 500.0,
        })
    result = action_cells(rows, bootstrap_samples=200)
    cell = result["JOIN"]
    assert cell["clusters"] == 12
    assert cell["lcb95"] > 0
    assert math.isclose(cell["mean_value_per_posted_share"], .02, abs_tol=1e-12)
    assert cell["role"] == "OBSERVED_ACTION_CELL_ONLY_NO_COUNTERFACTUAL_QUEUE_TRANSFER"


def test_verified_subsidies_never_credit_unverified_rewards():
    rows = [
        {
            "event_type": "FINAL",
            "metadata": {
                "pnl_decomposition": {
                    "own_reward_share_verified": False,
                    "maker_rebates": 100.0,
                    "liquidity_rewards": 100.0,
                }
            },
        },
        {
            "event_type": "FINAL",
            "metadata": {
                "pnl_decomposition": {
                    "own_reward_share_verified": True,
                    "maker_rebates": 0.3,
                    "liquidity_rewards": 0.2,
                }
            },
        },
    ]
    result = verified_subsidies(rows)
    assert result["state"] == "VERIFIED"
    assert math.isclose(result["maker_rebates"], .3)
    assert math.isclose(result["liquidity_rewards"], .2)
    assert math.isclose(result["total_verified_subsidy"], .5)
    assert result["unverified_rows"] == 1


def test_complete_set_uses_joint_cycle_states_not_product_of_marginals(tmp_path):
    path = tmp_path / "cycles.jsonl"
    rows = [
        {
            "schema": "polymarket_v7_two_sided_complete_set_cycle_v1",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "state": "BOTH_FULL",
            "total_shadow_pnl": .2,
            "legging_loss": 0.0,
        },
        {
            "schema": "polymarket_v7_two_sided_complete_set_cycle_v1",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "state": "YES_ONLY",
            "total_shadow_pnl": -.1,
            "legging_loss": .1,
        },
    ]
    path.write_text("\n".join(__import__("json").dumps(row) for row in rows) + "\n")
    result = complete_set_summary([path])
    assert result["cycles"] == 2
    assert result["paired_full_probability"] == .5
    assert result["one_leg_probability"] == .5
    assert math.isclose(result["mean_total_shadow_pnl"], .05)
    assert result["joint_probability_semantics"] == (
        "DIRECT_EMPIRICAL_CYCLE_STATES_NOT_PRODUCT_OF_MARGINALS")
