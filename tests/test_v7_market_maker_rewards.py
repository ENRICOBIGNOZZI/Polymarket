from __future__ import annotations

import importlib.util
import csv
import json
import math
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_market_maker_rewards", ROOT / "scripts" / "v7_market_maker_rewards.py"
)
assert SPEC and SPEC.loader
rewards = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rewards
SPEC.loader.exec_module(rewards)
SHA = "a" * 40


def _universe(path: Path, *, timestamp_ms: int, model_sha: str = SHA, markets=None) -> Path:
    default_markets = [{
        "market_id": "m1",
        "condition_id": "c1",
        "event_ids": ["e1"],
        "question": "Question?",
        "slug": "question",
        "clob_token_ids": ["yes", "no"],
        "outcome_prices": [0.45, 0.55],
        "best_bid": 0.44,
        "best_ask": 0.46,
        "midpoint": 0.45,
        "spread": 0.02,
        "liquidity": 1000.0,
        "volume_24h": 500.0,
        "score": 12.0,
        "active": True,
        "closed": False,
        "accepting_orders": True,
    }]
    path.write_text(json.dumps({
        "schema": "polymarket_v7_adaptive_universe_snapshot_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "model_sha": model_sha,
        "timestamp_ms": timestamp_ms,
        "discovery_exhaustive": True,
        "pagination_loop_guard_hit": False,
        "membership_sha256": "membership",
        "markets": default_markets if markets is None else markets,
    }), encoding="utf-8")
    return path


class MakerRewardSelectorTests(unittest.TestCase):
    def test_fill_projection_decays_old_burst_in_event_time(self) -> None:
        fresh_rate, fresh_weight = rewards._decayed_opposite_flow_rate(
            shares_10m=120.0, prints_10m=2, prints_30s=2,
            age_ms=100, half_life_seconds=30.0,
        )
        old_rate, old_weight = rewards._decayed_opposite_flow_rate(
            shares_10m=120.0, prints_10m=2, prints_30s=0,
            age_ms=60_000, half_life_seconds=30.0,
        )
        self.assertGreater(fresh_rate, 3.9)
        self.assertGreater(fresh_weight, 0.99)
        self.assertAlmostEqual(old_rate, 0.05, places=6)
        self.assertAlmostEqual(old_weight, 0.25, places=6)

    def test_flow_reach_uses_print_arrivals_not_whale_size(self) -> None:
        print_rate = rewards._decayed_opposite_print_rate(
            prints_10m=1, prints_30s=1, age_ms=0,
            half_life_seconds=30.0,
        )
        reach = 1.0 - math.exp(-print_rate * 5.0)
        self.assertAlmostEqual(print_rate, 1.0 / 30.0)
        self.assertLess(reach, 0.16)
        # Print size is deliberately absent: one 10,000-share print and one
        # 5-share print carry the same evidence that another print will arrive.

    def test_canonical_live_flow_is_exact_sha_and_event_time_grounded(self) -> None:
        from tempfile import TemporaryDirectory
        now_ms = 1_000_000
        with TemporaryDirectory() as directory:
            path = Path(directory) / "live_trade_flow.json"
            path.write_text(json.dumps({
                "schema": rewards.LIVE_FLOW_SCHEMA,
                "timestamp_ms": now_ms - 100,
                "producer": "FAST_STRUCTURAL_CPP_WEBSOCKET",
                "model_sha": SHA,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "rows": [{
                    "condition_id": "c1", "token_id": "yes",
                    "last_receive_ts_ms": now_ms - 200,
                    "tick_size": 0.01, "best_bid": 0.49, "best_ask": 0.50,
                    "best_bid_depth": 25.0, "best_ask_depth": 30.0,
                    "buy_prints_5s": 2, "buy_prints_30s": 3,
                    "buy_prints_120s": 4, "buy_prints_600s": 5,
                    "buy_shares_120s": 8.0,
                    "buy_shares_600s": 12.0, "buy_notional_600s": 6.0,
                    "last_buy_receive_ts_ms_600s": now_ms - 200,
                    "sell_prints_5s": 1, "sell_prints_30s": 1,
                    "sell_prints_120s": 2, "sell_prints_600s": 2,
                    "sell_shares_120s": 2.0,
                    "sell_shares_600s": 3.0, "sell_notional_600s": 1.2,
                    "last_sell_receive_ts_ms_600s": now_ms - 300,
                }],
            }), encoding="utf-8")
            aggregates, latest = rewards._canonical_live_flow_aggregates(
                path, model_sha=SHA, now_ms=now_ms, maximum_age_ms=30_000,
            )
            self.assertEqual(latest, now_ms - 200)
            self.assertEqual(aggregates["c1"]["prints"], 7)
            self.assertEqual(aggregates["c1"]["buy_prints_2m"], 4)
            self.assertEqual(
                aggregates["c1"]["token_flow"]["yes"]["best_bid_depth"], 25.0
            )
            self.assertEqual(
                aggregates["c1"]["token_flow"]["yes"]["sell_shares_2m"], 2.0
            )
            with self.assertRaisesRegex(ValueError, "contract_invalid"):
                rewards._canonical_live_flow_aggregates(
                    path, model_sha="b" * 40, now_ms=now_ms,
                    maximum_age_ms=30_000,
                )

    def test_live_flow_ranking_prefers_depletable_l1_queue(self) -> None:
        from tempfile import TemporaryDirectory
        now_ms = 1_000_000
        base = {
            "question": "Q", "active": True, "closed": False,
            "accepting_orders": True, "spread": 0.01, "liquidity": 10_000.0,
            "volume_24h": 10_000.0, "midpoint": 0.495,
            "timed_sports": False, "end_date": "2099-01-01T00:00:00Z",
        }
        markets = [
            {**base, "event_ids": [name], "market_id": name,
             "condition_id": f"c-{name}", "slug": name,
             "clob_token_ids": [f"y-{name}", f"n-{name}"]}
            for name in ("large-queue", "small-queue")
        ]
        rows = []
        for name, depth in (("large-queue", 100_000.0), ("small-queue", 5.0)):
            rows.append({
                "condition_id": f"c-{name}", "token_id": f"y-{name}",
                "last_receive_ts_ms": now_ms - 100,
                "tick_size": 0.01, "best_bid": 0.49, "best_ask": 0.50,
                "best_bid_depth": depth, "best_ask_depth": 10_000.0,
                "buy_prints_5s": 0, "buy_prints_30s": 0,
                "buy_prints_120s": 0, "buy_prints_600s": 0,
                "buy_shares_120s": 0.0, "buy_shares_600s": 0.0,
                "buy_notional_600s": 0.0, "last_buy_receive_ts_ms_600s": 0,
                "sell_prints_5s": 2, "sell_prints_30s": 2,
                "sell_prints_120s": 2, "sell_prints_600s": 2,
                "sell_shares_120s": 120.0, "sell_shares_600s": 120.0,
                "sell_notional_600s": 59.0,
                "last_sell_receive_ts_ms_600s": now_ms - 100,
            })
        with TemporaryDirectory() as directory:
            root = Path(directory)
            universe = _universe(root / "current.json", timestamp_ms=now_ms, markets=markets)
            live_flow = root / "live_trade_flow.json"
            live_flow.write_text(json.dumps({
                "schema": rewards.LIVE_FLOW_SCHEMA,
                "timestamp_ms": now_ms - 50,
                "producer": "FAST_STRUCTURAL_CPP_WEBSOCKET",
                "model_sha": SHA,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "rows": rows,
            }), encoding="utf-8")
            _, selection_cfg, capacity_cfg, capacity = rewards._validated_config(
                ROOT / "config" / "v7_professional_market_maker.json"
            )
            snapshot = rewards._recent_flow_snapshot(
                universe, root / "unused.csv", selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, now_ms=now_ms, live_flow_path=live_flow,
            )
        self.assertEqual(
            [row["market_id"] for row in snapshot["markets"]],
            ["small-queue", "large-queue"],
        )
        small, large = snapshot["markets"]
        self.assertGreater(
            small["best_projected_fill_probability"],
            large["best_projected_fill_probability"],
        )
        self.assertEqual(small["quote_opportunities"][0]["queue_ahead_shares"], 5.0)

    def test_config_requires_fail_closed_timed_sports_exclusion(self) -> None:
        from tempfile import TemporaryDirectory
        config = json.loads((ROOT / "config" / "v7_professional_market_maker.json").read_text())
        config["market_selection"]["exclude_timed_sports_without_verified_mapping"] = False
        with TemporaryDirectory() as directory:
            path = Path(directory) / "maker.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "timed_sports_exclusion_not_fail_closed"):
                rewards._validated_config(path)

    def test_recent_flow_is_preferred_over_quiet_reward_catalog(self) -> None:
        flow_snapshot = {
            "schema": "polymarket_v7_maker_reward_selection_v1",
            "timestamp_ms": 1_000_000,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": SHA,
            "source": "adaptive_universe_recent_flow",
            "selection_mode": "RECENT_EXECUTABLE_SELL_FLOW",
            "degraded": False,
            "selected_count": 1,
            "resource_capacity_markets": 40,
            "markets": [{
                "condition_id": "c1", "market_id": "m1",
                "yes_token": "yes", "no_token": "no",
            }],
        }
        with mock.patch.object(rewards, "_recent_flow_snapshot", return_value=flow_snapshot), \
             mock.patch.object(rewards, "_primary_snapshot") as primary:
            snapshot = rewards.build_snapshot(
                ROOT / "config" / "v7_professional_market_maker.json",
                fallback_universe_path=Path("universe.json"),
                trade_tape_path=Path("trades.csv"),
                model_sha=SHA,
                now_ms=1_000_000,
            )
        primary.assert_not_called()
        self.assertEqual(snapshot["source"], "adaptive_universe_recent_flow")
        self.assertEqual(rewards.selector_status(snapshot)["state"], "OPERATIONAL_BILATERAL_FLOW")

    def test_recent_flow_filters_expiry_extremes_and_duplicate_events(self) -> None:
        from tempfile import TemporaryDirectory
        base = {
            "question": "Q", "slug": "q", "active": True, "closed": False,
            "accepting_orders": True, "spread": 0.02, "liquidity": 1_000.0,
            "volume_24h": 10_000.0, "end_date": "2099-01-01T00:00:00Z",
        }
        markets = [
            {**base, "event_ids": ["e1"], "market_id": "active", "condition_id": "ca",
             "clob_token_ids": ["ya", "na"], "midpoint": 0.50},
            {**base, "event_ids": ["e1"], "market_id": "same-event", "condition_id": "cb",
             "clob_token_ids": ["yb", "nb"], "midpoint": 0.45},
            {**base, "event_ids": ["e2"], "market_id": "second", "condition_id": "cc",
             "clob_token_ids": ["yc", "nc"], "midpoint": 0.40},
            {**base, "event_ids": ["e3"], "market_id": "extreme", "condition_id": "cd",
             "clob_token_ids": ["yd", "nd"], "midpoint": 0.001},
            {**base, "event_ids": ["e4"], "market_id": "expired", "condition_id": "ce",
             "clob_token_ids": ["ye", "ne"], "midpoint": 0.50,
             "end_date": "1970-01-01T00:00:00Z"},
        ]
        now_ms = 1_000_000
        with TemporaryDirectory() as directory:
            root = Path(directory)
            universe = _universe(root / "current.json", timestamp_ms=now_ms, markets=markets)
            tape = root / "trade_tape.csv"
            with tape.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "timestamp", "received_ms", "condition_id", "asset_id", "side",
                    "price", "size", "transaction_hash",
                ])
                writer.writeheader()
                for condition, count in (("ca", 5), ("cb", 4), ("cc", 3), ("cd", 8), ("ce", 8)):
                    for index in range(count):
                        writer.writerow({
                            "timestamp": "1000", "received_ms": str(now_ms - index),
                            "condition_id": condition, "asset_id": "token", "side": "SELL",
                            "price": "0.5", "size": "10",
                            "transaction_hash": f"{condition}-{index}",
                        })
            _, selection_cfg, capacity_cfg, capacity = rewards._validated_config(
                ROOT / "config" / "v7_professional_market_maker.json"
            )
            selection_cfg = json.loads(json.dumps(selection_cfg))
            selection_cfg["recent_flow"]["minimum_markets"] = 2
            selection_cfg["recent_flow"]["minimum_operational_markets"] = 2
            snapshot = rewards._recent_flow_snapshot(
                universe, tape, selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, now_ms=now_ms,
            )
        self.assertEqual([row["market_id"] for row in snapshot["markets"]], ["active", "second"])
        self.assertEqual(snapshot["markets"][0]["recent_prints"], 5)
        self.assertEqual(snapshot["selection_mode"], "BILATERAL_AGGRESSOR_FLOW")
        self.assertEqual(snapshot["markets"][0]["recent_sell_prints_10m"], 5)
        self.assertFalse(snapshot["reward_data_available"])

    def test_generic_maker_excludes_timed_sports_without_verified_mapping(self) -> None:
        from tempfile import TemporaryDirectory
        now_ms = 1_000_000
        base = {
            "question": "Q", "slug": "q", "active": True, "closed": False,
            "accepting_orders": True, "spread": 0.02, "liquidity": 1_000.0,
            "volume_24h": 10_000.0, "end_date": "2099-01-01T00:00:00Z",
            "clob_token_ids": ["yes", "no"], "midpoint": 0.50,
        }
        markets = [
            {**base, "event_ids": ["sports"], "market_id": "sports", "condition_id": "cs",
             "timed_sports": True},
            {**base, "event_ids": ["generic"], "market_id": "generic", "condition_id": "cg",
             "timed_sports": False},
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            universe_path = _universe(root / "current.json", timestamp_ms=now_ms, markets=markets)
            tape = root / "trade_tape.csv"
            with tape.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "timestamp", "received_ms", "condition_id", "asset_id", "side",
                    "price", "size", "transaction_hash",
                ])
                writer.writeheader()
                for condition in ("cs", "cg"):
                    for index in range(3):
                        writer.writerow({
                            "timestamp": "1000", "received_ms": str(now_ms - index),
                            "condition_id": condition, "asset_id": "token", "side": "SELL",
                            "price": "0.5", "size": "10",
                            "transaction_hash": f"{condition}-{index}",
                        })
            _, selection_cfg, capacity_cfg, capacity = rewards._validated_config(
                ROOT / "config" / "v7_professional_market_maker.json"
            )
            selection_cfg = json.loads(json.dumps(selection_cfg))
            self.assertEqual(selection_cfg["recent_flow"]["minimum_markets"], 1)
            self.assertEqual(selection_cfg["recent_flow"]["lookback_seconds"], 1800)
            snapshot = rewards._recent_flow_snapshot(
                universe_path, tape, selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, now_ms=now_ms,
            )
            fallback = rewards._fallback_snapshot(
                universe_path, selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, primary_error="test", now_ms=now_ms,
            )
        self.assertEqual([row["market_id"] for row in snapshot["markets"]], ["generic"])
        self.assertEqual([row["market_id"] for row in fallback["markets"]], ["generic"])

    def test_buy_only_prints_select_inventory_backed_ask_opportunity(self) -> None:
        from tempfile import TemporaryDirectory
        now_ms = 1_000_000
        market = {
            "event_ids": ["e1"], "market_id": "m1", "condition_id": "c1",
            "question": "Q", "slug": "q", "active": True, "closed": False,
            "accepting_orders": True, "spread": 0.02, "liquidity": 1_000.0,
            "volume_24h": 10_000.0, "clob_token_ids": ["yes", "no"],
            "midpoint": 0.50, "timed_sports": False,
            "end_date": "2099-01-01T00:00:00Z",
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            universe = _universe(root / "current.json", timestamp_ms=now_ms, markets=[market])
            tape = root / "trade_tape.csv"
            with tape.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "timestamp", "received_ms", "condition_id", "asset_id", "side",
                    "price", "size", "transaction_hash",
                ])
                writer.writeheader()
                for index in range(3):
                    writer.writerow({
                        "timestamp": "1000", "received_ms": str(now_ms - index),
                        "condition_id": "c1", "asset_id": "yes", "side": "BUY",
                        "price": "0.5", "size": "10", "transaction_hash": f"tx-{index}",
                    })
            _, selection_cfg, capacity_cfg, capacity = rewards._validated_config(
                ROOT / "config" / "v7_professional_market_maker.json"
            )
            snapshot = rewards._recent_flow_snapshot(
                universe, tape, selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, now_ms=now_ms,
            )
        row = snapshot["markets"][0]
        self.assertEqual(row["side_mode"], "INVENTORY_BACKED_ASK")
        self.assertGreater(row["ask_opportunity_score"], row["bid_opportunity_score"])
        self.assertEqual(row["recent_buy_prints_2m"], 3)
        self.assertEqual(row["recent_sell_prints_2m"], 0)

    def test_recent_flow_reserve_is_explicitly_bounded(self) -> None:
        from tempfile import TemporaryDirectory
        now_ms = 1_000_000
        base = {
            "question": "Q", "slug": "q", "active": True, "closed": False,
            "accepting_orders": True, "spread": 0.02, "liquidity": 1_000.0,
            "volume_24h": 10_000.0, "midpoint": 0.50,
            "timed_sports": False, "end_date": "2099-01-01T00:00:00Z",
        }
        markets = [
            {**base, "event_ids": [f"e{i}"], "market_id": f"m{i}",
             "condition_id": f"c{i}", "clob_token_ids": [f"y{i}", f"n{i}"]}
            for i in range(3)
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            universe = _universe(root / "current.json", timestamp_ms=now_ms, markets=markets)
            tape = root / "trade_tape.csv"
            with tape.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "timestamp", "received_ms", "condition_id", "asset_id", "side",
                    "price", "size", "transaction_hash",
                ])
                writer.writeheader()
                for index in range(3):
                    writer.writerow({
                        "timestamp": "1000", "received_ms": str(now_ms - index),
                        "condition_id": "c0", "asset_id": "y0", "side": "BUY",
                        "price": "0.5", "size": "10", "transaction_hash": f"tx-{index}",
                    })
            _, selection_cfg, capacity_cfg, capacity = rewards._validated_config(
                ROOT / "config" / "v7_professional_market_maker.json"
            )
            selection_cfg = json.loads(json.dumps(selection_cfg))
            selection_cfg["recent_flow"]["minimum_operational_markets"] = 3
            selection_cfg["recent_flow"]["maximum_zero_flow_reserve_markets"] = 2
            snapshot = rewards._recent_flow_snapshot(
                universe, tape, selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, now_ms=now_ms,
            )
        self.assertEqual(snapshot["selected_count"], 3)
        self.assertEqual(snapshot["stable_reserve_added"], 2)
        self.assertEqual(snapshot["markets"][0]["market_id"], "m0")
        self.assertEqual(
            {row["side_mode"] for row in snapshot["markets"][1:]},
            {"STABLE_SPREAD_EXPLORATION"},
        )

    def test_resource_capacity_does_not_force_zero_flow_markets(self) -> None:
        from tempfile import TemporaryDirectory
        now_ms = 1_000_000
        base = {
            "question": "Q", "slug": "q", "active": True, "closed": False,
            "accepting_orders": True, "spread": 0.02, "liquidity": 1_000.0,
            "volume_24h": 10_000.0, "midpoint": 0.50,
            "timed_sports": False, "end_date": "2099-01-01T00:00:00Z",
        }
        markets = [
            {**base, "event_ids": [f"e{i}"], "market_id": f"m{i}",
             "condition_id": f"c{i}", "clob_token_ids": [f"y{i}", f"n{i}"]}
            for i in range(3)
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            universe = _universe(root / "current.json", timestamp_ms=now_ms, markets=markets)
            tape = root / "trade_tape.csv"
            with tape.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "timestamp", "received_ms", "condition_id", "asset_id", "side",
                    "price", "size", "transaction_hash",
                ])
                writer.writeheader()
                for index in range(3):
                    writer.writerow({
                        "timestamp": "1000", "received_ms": str(now_ms - index),
                        "condition_id": "c0", "asset_id": "y0", "side": "BUY",
                        "price": "0.5", "size": "10", "transaction_hash": f"tx-{index}",
                    })
            _, selection_cfg, capacity_cfg, capacity = rewards._validated_config(
                ROOT / "config" / "v7_professional_market_maker.json"
            )
            snapshot = rewards._recent_flow_snapshot(
                universe, tape, selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, now_ms=now_ms,
            )
        self.assertEqual(snapshot["selected_count"], 1)
        self.assertEqual(snapshot["stable_reserve_added"], 0)
        self.assertEqual(snapshot["unused_resource_capacity_markets"], capacity - 1)
        self.assertEqual(snapshot["resource_capacity_markets"], capacity)

    def test_recent_flow_requires_sustained_sell_flow_and_a_fresh_last_print(self) -> None:
        from tempfile import TemporaryDirectory
        now_ms = 1_000_000
        base = {
            "question": "Q", "slug": "q", "active": True, "closed": False,
            "accepting_orders": True, "spread": 0.02, "liquidity": 1_000.0,
            "volume_24h": 10_000.0, "end_date": "2099-01-01T00:00:00Z",
            "clob_token_ids": ["yes", "no"], "midpoint": 0.50,
            "timed_sports": False,
        }
        markets = [
            {**base, "event_ids": ["stale"], "market_id": "stale", "condition_id": "cs"},
            {**base, "event_ids": ["fresh"], "market_id": "fresh", "condition_id": "cf"},
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            universe = _universe(root / "current.json", timestamp_ms=now_ms, markets=markets)
            tape = root / "trade_tape.csv"
            with tape.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "timestamp", "received_ms", "condition_id", "asset_id", "side",
                    "price", "size", "transaction_hash",
                ])
                writer.writeheader()
                for condition, ages in (("cs", (70_000, 80_000)), ("cf", (1_000, 50_000))):
                    for index, age in enumerate(ages):
                        writer.writerow({
                            "timestamp": "1000", "received_ms": str(now_ms - age),
                            "condition_id": condition, "asset_id": "yes", "side": "SELL",
                            "price": "0.5", "size": "10",
                            "transaction_hash": f"{condition}-{index}",
                        })
            _, selection_cfg, capacity_cfg, capacity = rewards._validated_config(
                ROOT / "config" / "v7_professional_market_maker.json"
            )
            snapshot = rewards._recent_flow_snapshot(
                universe, tape, selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, now_ms=now_ms,
            )
        self.assertEqual([row["market_id"] for row in snapshot["markets"]], ["fresh", "stale"])
        self.assertEqual(snapshot["minimum_side_prints_2m"], 2)
        self.assertEqual(snapshot["maximum_last_side_age_ms"], 60_000)
        self.assertEqual(snapshot["markets"][0]["recent_sell_prints_2m"], 2)
        self.assertEqual(snapshot["markets"][0]["recent_last_sell_age_ms"], 1_000)
        self.assertEqual(snapshot["markets"][0]["side_mode"], "COLLATERAL_BACKED_BID")
        self.assertEqual(snapshot["markets"][1]["side_mode"], "STABLE_SPREAD_EXPLORATION")

    def test_selector_status_suppresses_degraded_fallback_rotation(self) -> None:
        runtime = {
            "model_sha": SHA, "timestamp_ms": 1_000,
            "source": "adaptive_universe_recent_flow", "degraded": False,
            "selected_count": 1, "markets": [{
                "condition_id": "c1", "yes_token": "yes", "no_token": "no",
            }],
        }
        candidate = {
            "model_sha": SHA, "timestamp_ms": 2_000,
            "source": "adaptive_universe_fallback", "degraded": True,
            "selected_count": 1, "markets": [{
                "condition_id": "c2", "yes_token": "yes2", "no_token": "no2",
            }],
        }
        status = rewards.selector_status(
            runtime, candidate_snapshot=candidate, runtime_selection_pinned=True
        )
        self.assertFalse(status["candidate_rotation_pending"])
        self.assertTrue(status["candidate_rotation_suppressed_no_fresh_flow"])
        self.assertFalse(status["candidate_fresh_flow_eligible"])
        self.assertEqual(status["candidate_max_last_sell_age_seconds"], -1.0)

    def test_recent_flow_failure_uses_filtered_universe_not_untyped_reward_catalog(self) -> None:
        from tempfile import TemporaryDirectory
        now_ms = 1_000_000
        generic = {
            "event_ids": ["generic"], "market_id": "generic", "condition_id": "cg",
            "question": "Q", "slug": "q", "active": True, "closed": False,
            "accepting_orders": True, "spread": 0.02, "liquidity": 1_000.0,
            "volume_24h": 10_000.0, "clob_token_ids": ["yes", "no"],
            "midpoint": 0.50, "timed_sports": False,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((ROOT / "config" / "v7_professional_market_maker.json").read_text())
            config["market_selection"]["recent_flow"]["initial_wait_seconds"] = 0
            config_path = root / "maker.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            universe_path = _universe(root / "current.json", timestamp_ms=now_ms, markets=[generic])
            tape = root / "trade_tape.csv"
            tape.write_text("timestamp,received_ms,condition_id,asset_id,side,price,size,transaction_hash\n", encoding="utf-8")
            with mock.patch.object(rewards, "_primary_snapshot") as primary:
                snapshot = rewards.build_snapshot(
                    config_path,
                    fallback_universe_path=universe_path,
                    trade_tape_path=tape,
                    model_sha=SHA,
                    now_ms=now_ms,
                    deadline_seconds=0.01,
                )
        primary.assert_not_called()
        self.assertEqual(snapshot["source"], "adaptive_universe_fallback")
        self.assertEqual([row["market_id"] for row in snapshot["markets"]], ["generic"])

    def test_reward_failure_publishes_safe_fresh_universe_fallback(self) -> None:
        with self.subTest("fresh exact-SHA universe"):
            from tempfile import TemporaryDirectory
            with TemporaryDirectory() as directory:
                now_ms = 1_000_000
                universe = _universe(Path(directory) / "current.json", timestamp_ms=now_ms - 1000)
                with mock.patch.object(rewards, "_primary_snapshot", side_effect=TimeoutError("endpoint")):
                    snapshot = rewards.build_snapshot(
                        ROOT / "config" / "v7_professional_market_maker.json",
                        fallback_universe_path=universe,
                        model_sha=SHA,
                        now_ms=now_ms,
                    )
        self.assertEqual(snapshot["source"], "adaptive_universe_fallback")
        self.assertEqual(snapshot["selection_mode"], "FLOW_FILLABILITY_FALLBACK")
        self.assertTrue(snapshot["degraded"])
        self.assertFalse(snapshot["reward_data_available"])
        self.assertTrue(snapshot["paper_only"])
        self.assertFalse(snapshot["authenticated_execution"])
        self.assertFalse(snapshot["real_order_submission"])
        self.assertEqual(snapshot["model_sha"], SHA)
        self.assertEqual(snapshot["selected_count"], 1)
        self.assertEqual(snapshot["markets"][0]["reward_intensity"], 0.0)
        self.assertEqual(snapshot["markets"][0]["midpoint"], 0.45)
        self.assertEqual(snapshot["markets"][0]["flow_to_depth_24h"], 0.5)
        self.assertEqual(snapshot["cold_start_maximum_markets"], 1)
        self.assertEqual(snapshot["markets"][0]["side_mode"], "STABLE_SPREAD_EXPLORATION")
        self.assertEqual(snapshot["markets"][0]["quote_opportunities"], [])
        status = rewards.selector_status(snapshot)
        self.assertTrue(status["ready"])
        self.assertEqual(status["state"], "OPERATIONAL_FALLBACK")

    def test_cold_start_fallback_does_not_fill_shard_capacity(self) -> None:
        from tempfile import TemporaryDirectory
        now_ms = 1_000_000
        base = {
            "question": "Q", "active": True, "closed": False,
            "accepting_orders": True, "spread": 0.02, "liquidity": 1_000.0,
            "volume_24h": 10_000.0, "midpoint": 0.50,
            "timed_sports": False, "end_date": "2099-01-01T00:00:00Z",
        }
        markets = [
            {**base, "event_ids": [f"e{i}"], "market_id": f"m{i}",
             "condition_id": f"c{i}", "slug": f"m{i}",
             "clob_token_ids": [f"y{i}", f"n{i}"]}
            for i in range(8)
        ]
        with TemporaryDirectory() as directory:
            universe = _universe(
                Path(directory) / "current.json", timestamp_ms=now_ms, markets=markets
            )
            _, selection_cfg, capacity_cfg, capacity = rewards._validated_config(
                ROOT / "config" / "v7_professional_market_maker.json"
            )
            snapshot = rewards._fallback_snapshot(
                universe, selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, primary_error="cold-start", now_ms=now_ms,
            )
        self.assertEqual(snapshot["selected_count"], 1)
        self.assertEqual(snapshot["unused_resource_capacity_markets"], capacity - 1)
        self.assertEqual(snapshot["markets"][0]["recent_prints"], 0)

    def test_recent_flow_admits_liquid_two_cent_tail(self) -> None:
        from tempfile import TemporaryDirectory
        now_ms = 1_000_000
        market = {
            "event_ids": ["e1"], "market_id": "tail", "condition_id": "ct",
            "question": "Tail", "slug": "tail", "active": True, "closed": False,
            "accepting_orders": True, "spread": 0.002, "liquidity": 500_000.0,
            "volume_24h": 300_000.0, "clob_token_ids": ["yes", "no"],
            "midpoint": 0.0265, "timed_sports": False,
            "end_date": "2099-01-01T00:00:00Z",
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            universe = _universe(root / "current.json", timestamp_ms=now_ms, markets=[market])
            tape = root / "trade_tape.csv"
            with tape.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "timestamp", "received_ms", "condition_id", "asset_id", "side",
                    "price", "size", "transaction_hash",
                ])
                writer.writeheader()
                for index in range(3):
                    writer.writerow({
                        "timestamp": "1000", "received_ms": str(now_ms - index),
                        "condition_id": "ct", "asset_id": "no", "side": "SELL",
                        "price": "0.973", "size": "10", "transaction_hash": f"tx-{index}",
                    })
            _, selection_cfg, capacity_cfg, capacity = rewards._validated_config(
                ROOT / "config" / "v7_professional_market_maker.json"
            )
            snapshot = rewards._recent_flow_snapshot(
                universe, tape, selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, now_ms=now_ms,
            )
        self.assertEqual(snapshot["selected_count"], 1)
        self.assertEqual(snapshot["markets"][0]["market_id"], "tail")
        self.assertEqual(snapshot["markets"][0]["side_mode"], "COLLATERAL_BACKED_BID")
        self.assertEqual(snapshot["markets"][0]["recent_sell_prints_2m"], 3)

    def test_fallback_rejects_stale_or_wrong_sha_universe(self) -> None:
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            universe = _universe(Path(directory) / "current.json", timestamp_ms=1, model_sha="b" * 40)
            with mock.patch.object(rewards, "_primary_snapshot", side_effect=TimeoutError("endpoint")):
                with self.assertRaisesRegex(ValueError, "fallback_universe_contract_invalid"):
                    rewards.build_snapshot(
                        ROOT / "config" / "v7_professional_market_maker.json",
                        fallback_universe_path=universe,
                        model_sha=SHA,
                        now_ms=1_000_000,
                    )

    def test_fallback_excludes_extreme_prices_and_prefers_flow_to_depth(self) -> None:
        from tempfile import TemporaryDirectory
        base = {
            "event_ids": ["e"], "question": "Q", "slug": "q",
            "active": True, "closed": False, "accepting_orders": True,
            "spread": 0.02,
        }
        rows = [
            {**base, "event_ids": ["eh"], "market_id": "huge", "condition_id": "ch", "clob_token_ids": ["yh", "nh"],
             "midpoint": 0.50, "liquidity": 1_000_000, "volume_24h": 10_000},
            {**base, "event_ids": ["ef"], "market_id": "flow", "condition_id": "cf", "clob_token_ids": ["yf", "nf"],
             "midpoint": 0.45, "liquidity": 1_000, "volume_24h": 10_000},
            {**base, "event_ids": ["ee"], "market_id": "extreme", "condition_id": "ce", "clob_token_ids": ["ye", "ne"],
             "midpoint": 0.001, "liquidity": 100, "volume_24h": 1_000_000},
        ]
        with TemporaryDirectory() as directory:
            universe = _universe(Path(directory) / "current.json", timestamp_ms=999_000, markets=rows)
            config = json.loads(
                (ROOT / "config" / "v7_professional_market_maker.json").read_text()
            )
            config["market_selection"]["cold_start_maximum_markets"] = 2
            config_path = Path(directory) / "maker.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.object(rewards, "_primary_snapshot", side_effect=TimeoutError("endpoint")):
                snapshot = rewards.build_snapshot(
                    config_path,
                    fallback_universe_path=universe, model_sha=SHA, now_ms=1_000_000,
                )
        self.assertEqual([row["market_id"] for row in snapshot["markets"]], ["flow", "huge"])
        self.assertGreater(snapshot["markets"][0]["flow_to_depth_24h"], snapshot["markets"][1]["flow_to_depth_24h"])

    def test_fallback_excludes_wide_books_that_recent_flow_would_reject(self) -> None:
        from tempfile import TemporaryDirectory
        base = {
            "question": "Q", "slug": "q", "active": True, "closed": False,
            "accepting_orders": True, "liquidity": 1_000.0,
            "volume_24h": 10_000.0, "midpoint": 0.50,
        }
        rows = [
            {**base, "event_ids": ["wide"], "market_id": "wide", "condition_id": "cw",
             "clob_token_ids": ["yw", "nw"], "spread": 0.44},
            {**base, "event_ids": ["tight"], "market_id": "tight", "condition_id": "ct",
             "clob_token_ids": ["yt", "nt"], "spread": 0.02},
        ]
        with TemporaryDirectory() as directory:
            universe = _universe(Path(directory) / "current.json", timestamp_ms=1_000_000, markets=rows)
            _, selection_cfg, capacity_cfg, capacity = rewards._validated_config(
                ROOT / "config" / "v7_professional_market_maker.json"
            )
            snapshot = rewards._fallback_snapshot(
                universe, selection_cfg, capacity_cfg, capacity,
                model_sha=SHA, primary_error="test", now_ms=1_000_000,
            )
        self.assertEqual([row["market_id"] for row in snapshot["markets"]], ["tight"])

    def test_request_budget_caps_each_network_call(self) -> None:
        seen: list[float] = []

        def fetcher(_url: str, *, timeout: float) -> dict[str, object]:
            seen.append(timeout)
            return {"data": [], "next_cursor": "LTE="}

        pools = rewards.fetch_reward_pools(
            "https://clob.polymarket.com",
            deadline=rewards.time.monotonic() + 0.5,
            request_timeout=5.0,
            max_pages=2,
            fetcher=fetcher,
        )
        self.assertEqual(pools, {})
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0.0)
        self.assertLessEqual(seen[0], 0.5)

    def test_catalog_uses_documented_page_size_and_single_pass_pool_market_join(self) -> None:
        seen: list[str] = []

        def fetcher(url: str, *, timeout: float) -> dict[str, object]:
            seen.append(url)
            self.assertGreater(timeout, 0.0)
            return {
                "data": [{
                    "condition_id": "c1",
                    "market_id": "m1",
                    "event_id": "e1",
                    "market_slug": "market",
                    "question": "Question?",
                    "market_competitiveness": 2.0,
                    "volume_24hr": 1234.0,
                    "rewards_max_spread": 3.5,
                    "rewards_min_size": 20.0,
                    "rewards_config": [{"rate_per_day": 5.0}],
                    "tokens": [
                        {"outcome": "Yes", "token_id": "yes"},
                        {"outcome": "No", "token_id": "no"},
                    ],
                }],
                "next_cursor": "LTE=",
            }

        pools, markets = rewards.fetch_reward_catalog(
            "https://clob.polymarket.com",
            min_volume_24h=100.0,
            deadline=rewards.time.monotonic() + 1.0,
            request_timeout=0.5,
            fetcher=fetcher,
        )
        self.assertEqual(len(seen), 1)
        self.assertIn("page_size=500", seen[0])
        self.assertIn("min_volume_24hr=100", seen[0])
        self.assertNotIn("limit=500", seen[0])
        self.assertEqual(pools["c1"].total_daily_rate, 5.0)
        self.assertEqual(markets["c1"].yes_token, "yes")

    def test_primary_snapshot_uses_one_catalog_fetch(self) -> None:
        pool = rewards.RewardPool("c1", 3.0, 20.0, 4.0, 0.0, 4.0)
        market = rewards.RewardMarket("c1", "m1", "e1", "m", "Q", "yes", "no", 1000.0, 1.0)
        with mock.patch.object(
            rewards, "fetch_reward_catalog", return_value=({"c1": pool}, {"c1": market})
        ) as fetch:
            snapshot = rewards.build_snapshot(
                ROOT / "config" / "v7_professional_market_maker.json",
                model_sha=SHA,
            )
        fetch.assert_called_once()
        self.assertEqual(snapshot["source"], "public_clob_rewards")
        self.assertEqual(snapshot["reward_pool_count"], 1)
        self.assertEqual(snapshot["reward_market_count"], 1)
        self.assertEqual(snapshot["selected_count"], 1)

    def test_live_allocation_is_the_validated_reward_budget_source(self) -> None:
        from tempfile import TemporaryDirectory
        allocation = {
            "paper_only": True,
            "starting_capital": 2000.0,
            "v7": {"authenticated_execution": False, "real_order_submission": False},
            "capital_scope": {
                "sleeve": "micro_maker",
                "sleeve_starting_capital": 2000.0,
                "strategy_budgets": {"professional_maker": 2000.0},
                "strategy_budget_sum": 2000.0,
                "double_counting_forbidden": True,
            },
        }
        pool = rewards.RewardPool("c1", 3.0, 20.0, 4.0, 0.0, 4.0)
        market = rewards.RewardMarket("c1", "m1", "e1", "m", "Q", "yes", "no", 1000.0, 1.0)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "micro_maker.json"
            path.write_text(json.dumps(allocation), encoding="utf-8")
            with mock.patch.object(
                rewards, "fetch_reward_catalog", return_value=({"c1": pool}, {"c1": market})
            ):
                snapshot = rewards.build_snapshot(
                    ROOT / "config" / "v7_professional_market_maker.json",
                    allocation_path=path,
                    model_sha=SHA,
                )
            allocation["capital_scope"]["strategy_budget_sum"] = 1999.0
            path.write_text(json.dumps(allocation), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "allocation_capital_mismatch"):
                rewards.build_snapshot(
                    ROOT / "config" / "v7_professional_market_maker.json",
                    allocation_path=path,
                    model_sha=SHA,
                )
        self.assertEqual(snapshot["reward_qualification_max_order_notional_usd"], 20.0)

    def test_rank_rejects_reward_size_outside_sleeve_risk_budget(self) -> None:
        market = rewards.RewardMarket(
            "c1", "m1", "e1", "m", "Q", "yes", "no", 1000.0, 1.0,
            yes_price=0.6, no_price=0.4, spread=0.02,
        )
        affordable = rewards.RewardPool("c1", 3.0, 20.0, 4.0, 0.0, 4.0)
        too_large = rewards.RewardPool("c1", 3.0, 50.0, 4.0, 0.0, 4.0)
        rows = rewards.rank_markets(
            {"c1": affordable}, {"c1": market}, max_active=1,
            min_volume_24h=100.0, max_order_notional_usd=20.0,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reward_qualification_notional_usd, 12.0)
        self.assertTrue(rows[0].reward_touch_qualifies_at_selection)
        self.assertEqual(
            rewards.rank_markets(
                {"c1": too_large}, {"c1": market}, max_active=1,
                min_volume_24h=100.0, max_order_notional_usd=20.0,
            ),
            [],
        )

    def test_rank_rejects_touch_outside_reward_spread(self) -> None:
        market = rewards.RewardMarket(
            "c1", "m1", "e1", "m", "Q", "yes", "no", 1000.0, 1.0,
            yes_price=0.5, no_price=0.5, spread=0.10,
        )
        pool = rewards.RewardPool("c1", 4.0, 20.0, 4.0, 0.0, 4.0)
        self.assertEqual(
            rewards.rank_markets(
                {"c1": pool}, {"c1": market}, max_active=1,
                min_volume_24h=100.0, max_order_notional_usd=20.0,
            ),
            [],
        )

    def test_live_publication_pins_runtime_membership_and_tracks_candidate(self) -> None:
        from tempfile import TemporaryDirectory
        base = {
            "schema": "polymarket_v7_maker_reward_selection_v1",
            "timestamp_ms": 1000,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": SHA,
            "source": "public_clob_rewards",
            "selection_mode": "REWARDED",
            "degraded": False,
            "reward_pool_count": 1,
            "reward_market_count": 1,
            "selected_count": 1,
            "resource_capacity_markets": 40,
            "markets": [{
                "condition_id": "c1", "market_id": "m1",
                "yes_token": "y1", "no_token": "n1",
            }],
        }
        rotated = json.loads(json.dumps(base))
        rotated["timestamp_ms"] = 2000
        rotated["markets"][0].update({
            "condition_id": "c2", "market_id": "m2",
            "yes_token": "y2", "no_token": "n2",
        })
        with TemporaryDirectory() as directory:
            output = Path(directory) / "runtime.json"
            candidate_output = Path(directory) / "candidate.json"
            first, first_pinned = rewards.publish_runtime_selection(
                base, output, pin_runtime_selection=True,
                candidate_output_path=candidate_output,
            )
            second, second_pinned = rewards.publish_runtime_selection(
                rotated, output, pin_runtime_selection=True,
                candidate_output_path=candidate_output,
            )
            status = rewards.selector_status(
                second, candidate_snapshot=rotated,
                runtime_selection_pinned=second_pinned,
            )
            published = json.loads(output.read_text(encoding="utf-8"))
            candidate = json.loads(candidate_output.read_text(encoding="utf-8"))
        self.assertFalse(first_pinned)
        self.assertTrue(second_pinned)
        self.assertEqual(first["markets"][0]["market_id"], "m1")
        self.assertEqual(second["markets"][0]["market_id"], "m1")
        self.assertEqual(published["markets"][0]["market_id"], "m1")
        self.assertEqual(candidate["markets"][0]["market_id"], "m2")
        self.assertTrue(status["runtime_selection_pinned"])
        self.assertTrue(status["candidate_rotation_pending"])
        self.assertEqual(status["timestamp_ms"], 2000)


if __name__ == "__main__":
    unittest.main()
