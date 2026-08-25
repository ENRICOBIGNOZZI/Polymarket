from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.hf_maker_flow_admission import evaluate
from scripts.hf_maker_forward_audit import audit, realized_roundtrip_pnl
from scripts.v6_market_common import TapeFlow
from scripts.v6_micro_maker import replayable_trade_timestamp
from scripts.v6_micro_maker_v2 import (
    enforce_total_gross_cap,
    gate_inside_fill_probability,
    projected_remaining_clearance_ratio,
    should_recycle_dead_order,
)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class HFMakerFlowForwardTest(unittest.TestCase):
    def test_zero_flow_is_not_admitted_even_inside_spread(self) -> None:
        result = evaluate(post_cost_edge=0.001,min_edge=0.00005,queue_ahead=0.0,own_shares=10.0,compatible_sell_rate_per_second=0.0,horizon_seconds=60,inside_spread=True)
        self.assertFalse(result.admit)
        self.assertEqual(result.reason, "ZERO_CAUSAL_CONTRA_FLOW")

    def test_positive_flow_ranks_by_queue_clearance(self) -> None:
        result = evaluate(post_cost_edge=0.001,min_edge=0.00005,queue_ahead=20.0,own_shares=10.0,compatible_sell_rate_per_second=1.0,horizon_seconds=60)
        self.assertTrue(result.admit)
        self.assertAlmostEqual(result.queue_clearance_ratio, 2.0)
        self.assertAlmostEqual(result.fill_probability_proxy, 1.0)
        self.assertAlmostEqual(result.expected_filled_edge, 0.001)

    def test_low_confidence_inside_spread_fill_uplift_is_removed(self) -> None:
        self.assertEqual(
            gate_inside_fill_probability(
                0.42, queue_ahead=0.0, confidence=0.60, min_inside_confidence=0.80
            ),
            0.0,
        )

    def test_low_confidence_at_touch_fill_probability_is_unchanged(self) -> None:
        self.assertAlmostEqual(
            gate_inside_fill_probability(
                0.42, queue_ahead=25.0, confidence=0.60, min_inside_confidence=0.80
            ),
            0.42,
        )

    def test_high_confidence_inside_spread_fill_probability_is_allowed(self) -> None:
        self.assertAlmostEqual(
            gate_inside_fill_probability(
                0.42, queue_ahead=0.0, confidence=0.90, min_inside_confidence=0.80
            ),
            0.42,
        )

    def test_high_projected_hazard_is_not_recycled_on_fixed_grace(self) -> None:
        order = {
            "created_ts": 100,
            "queue_ahead": 20.0,
            "remaining_shares": 5.0,
            "flow_rate": 5.0,
        }
        self.assertGreater(projected_remaining_clearance_ratio(order, now=125, ttl_seconds=60), 1.0)
        self.assertFalse(
            should_recycle_dead_order(
                order,
                observed_compatible_flow=0.0,
                now=125,
                grace_seconds=20,
                ttl_seconds=60,
                min_projected_clearance=1.0,
            )
        )

    def test_low_projected_hazard_is_recycled_after_grace(self) -> None:
        order = {
            "created_ts": 100,
            "queue_ahead": 100.0,
            "remaining_shares": 20.0,
            "flow_rate": 0.1,
        }
        self.assertLess(projected_remaining_clearance_ratio(order, now=125, ttl_seconds=60), 1.0)
        self.assertTrue(
            should_recycle_dead_order(
                order,
                observed_compatible_flow=0.0,
                now=125,
                grace_seconds=20,
                ttl_seconds=60,
                min_projected_clearance=1.0,
            )
        )

    def test_observed_flow_prevents_dead_queue_recycle(self) -> None:
        order = {
            "created_ts": 100,
            "queue_ahead": 100.0,
            "remaining_shares": 20.0,
            "flow_rate": 0.0,
        }
        self.assertFalse(
            should_recycle_dead_order(
                order,
                observed_compatible_flow=1.0,
                now=125,
                grace_seconds=20,
                ttl_seconds=60,
                min_projected_clearance=1.0,
            )
        )

    def test_roundtrip_pnl_is_fill_conditioned(self) -> None:
        rows = [
            {"timestamp": "100", "market_id": "m", "action": "BUY_MAKER", "shares": "10", "price": "0.40", "fee": "0"},
            {"timestamp": "160", "market_id": "m", "action": "SELL_TAKER", "shares": "10", "price": "0.42", "fee": "0.01"},
        ]
        result = realized_roundtrip_pnl(rows)
        self.assertAlmostEqual(result["realized_closed_pnl"], 0.19, places=9)
        self.assertEqual(result["maker_buy_events"], 1)
        self.assertEqual(result["taker_exit_events"], 1)

    def test_tape_flow_requires_receive_and_event_time_recency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tape = Path(tmp) / "trade_tape.csv"
            fields = ["timestamp", "received_ms", "asset_id", "side", "price", "size"]
            write_csv(tape,fields,[
                {"timestamp": 995, "received_ms": 999000, "asset_id": "t", "side": "SELL", "price": 0.40, "size": 30},
                {"timestamp": 990, "received_ms": 1001000, "asset_id": "t", "side": "SELL", "price": 0.40, "size": 100},
                {"timestamp": 800, "received_ms": 999500, "asset_id": "t", "side": "SELL", "price": 0.40, "size": 200},
            ])
            flow = TapeFlow.from_csv(tape, lookback_seconds=120, now=1000)
            self.assertEqual(flow.compatible_sell_volume("t", 0.41, lookback_seconds=120), 30.0)
            self.assertEqual(flow.compatible_sell_count("t", 0.41, lookback_seconds=120), 1)

    def test_tape_flow_raw_rate_decays_by_market_event_age_not_delivery_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tape = Path(tmp) / "trade_tape.csv"
            fields = ["timestamp", "received_ms", "asset_id", "side", "price", "size"]
            write_csv(tape, fields, [
                {"timestamp": 890, "received_ms": 999000, "asset_id": "old", "side": "SELL", "price": 0.40, "size": 100},
                {"timestamp": 995, "received_ms": 999000, "asset_id": "fresh", "side": "SELL", "price": 0.40, "size": 100},
            ])
            flow = TapeFlow.from_csv(tape, lookback_seconds=120, now=1000)
            old_rate = flow.compatible_sell_raw_rate("old", 0.41, lookback_seconds=120)
            fresh_rate = flow.compatible_sell_raw_rate("fresh", 0.41, lookback_seconds=120)
            self.assertEqual(flow.compatible_sell_recency("old", 0.41, lookback_seconds=120), 110.0)
            self.assertEqual(flow.compatible_sell_receive_recency("old", 0.41, lookback_seconds=120), 1.0)
            self.assertLess(old_rate, 0.20 * fresh_rate)

    def test_single_flow_burst_does_not_create_stationary_fill_hazard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tape = Path(tmp) / "trade_tape.csv"
            fields = ["timestamp", "received_ms", "asset_id", "side", "price", "size"]
            write_csv(tape, fields, [
                {"timestamp": 995, "received_ms": 999000, "asset_id": "t", "side": "SELL", "price": 0.40, "size": 100},
                {"timestamp": 996, "received_ms": 999100, "asset_id": "t", "side": "SELL", "price": 0.40, "size": 50},
            ])
            flow = TapeFlow.from_csv(tape, lookback_seconds=120, now=1000)
            self.assertGreater(flow.compatible_sell_raw_rate("t", 0.41, lookback_seconds=120), 0.0)
            self.assertEqual(flow.compatible_sell_burst_count("t", 0.41, lookback_seconds=120), 1)
            self.assertEqual(flow.compatible_sell_recurrence_confidence("t", 0.41, lookback_seconds=120), 0.0)
            self.assertEqual(flow.compatible_sell_rate("t", 0.41, lookback_seconds=120), 0.0)

    def test_two_separated_flow_bursts_receive_leave_one_burst_out_shrinkage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tape = Path(tmp) / "trade_tape.csv"
            fields = ["timestamp", "received_ms", "asset_id", "side", "price", "size"]
            write_csv(tape, fields, [
                {"timestamp": 970, "received_ms": 971000, "asset_id": "t", "side": "SELL", "price": 0.40, "size": 100},
                {"timestamp": 995, "received_ms": 999000, "asset_id": "t", "side": "SELL", "price": 0.40, "size": 100},
            ])
            flow = TapeFlow.from_csv(tape, lookback_seconds=120, now=1000)
            raw = flow.compatible_sell_raw_rate("t", 0.41, lookback_seconds=120)
            self.assertEqual(flow.compatible_sell_burst_count("t", 0.41, lookback_seconds=120), 2)
            self.assertAlmostEqual(flow.compatible_sell_recurrence_confidence("t", 0.41, lookback_seconds=120), 0.5)
            self.assertAlmostEqual(flow.compatible_sell_rate("t", 0.41, lookback_seconds=120), 0.5 * raw)

    def test_delayed_tape_row_inside_live_ttl_remains_replayable(self) -> None:
        order = {"created_ts": 100}
        row = {"timestamp": "150", "received_ms": "165000"}
        self.assertEqual(replayable_trade_timestamp(row, order, now=170, ttl_seconds=60), 150)

    def test_replay_rejects_trade_after_order_expiry(self) -> None:
        order = {"created_ts": 100}
        row = {"timestamp": "161", "received_ms": "165000"}
        self.assertIsNone(replayable_trade_timestamp(row, order, now=170, ttl_seconds=60))

    def test_replay_rejects_trade_not_received_by_processing_time(self) -> None:
        order = {"created_ts": 100}
        row = {"timestamp": "150", "received_ms": "171000"}
        self.assertIsNone(replayable_trade_timestamp(row, order, now=170, ttl_seconds=60))

    def test_replay_rejects_future_event_timestamp(self) -> None:
        order = {"created_ts": 100}
        row = {"timestamp": "171", "received_ms": "169000"}
        self.assertIsNone(replayable_trade_timestamp(row, order, now=170, ttl_seconds=90))

    def test_fill_loop_replays_event_time_before_ttl_cancel(self) -> None:
        source = Path("scripts/v6_micro_maker.py").read_text(encoding="utf-8")
        loop = source.split("for market_id, order in list(orders.items()):", 1)[1].split("    slip =", 1)[0]
        self.assertLess(loop.index("for row in tape_rows:"), loop.index("if expired:"))
        self.assertIn('"entry_ts": trade_ts', loop)
        self.assertIn('"timestamp": trade_ts', loop)
        self.assertIn("received_ms > now * 1000", source)

    def test_total_gross_cap_accounts_for_open_position_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text(json.dumps({"starting_capital": 1000.0, "max_gross_fraction": 0.70}), encoding="utf-8")
            (root / "state.json").write_text(json.dumps({"equity": 1000.0, "positions": {"m": {"cost": 400.0}}}), encoding="utf-8")
            tick = enforce_total_gross_cap(config, root)
            cfg = json.loads(tick.read_text(encoding="utf-8"))
            self.assertAlmostEqual(cfg["max_gross_fraction"], 0.30)

    def test_audit_uses_receive_time_for_prior_flow_and_event_time_forward(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); order = root / "maker_order_log.csv"; tape = root / "trade_tape.csv"; fills = root / "maker_fills.csv"
            write_csv(order,["timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price", "remaining_shares", "queue_ahead", "edge", "confidence"],[{"timestamp": 1000, "action": "POST", "market_id": "m", "slug": "s", "side": "YES", "token_id": "t", "limit_price": 0.41, "remaining_shares": 10, "queue_ahead": 20, "edge": 0.001, "confidence": 0.5}])
            write_csv(tape,["timestamp", "received_ms", "lag_ms", "condition_id", "asset_id", "outcome", "side", "price", "size", "transaction_hash", "slug", "event_slug"],[
                {"timestamp": 995, "received_ms": 999000, "lag_ms": 4000, "condition_id": "c", "asset_id": "t", "outcome": "Yes", "side": "SELL", "price": 0.40, "size": 30, "transaction_hash": "a", "slug": "s", "event_slug": "e"},
                {"timestamp": 990, "received_ms": 1001000, "lag_ms": 11000, "condition_id": "c", "asset_id": "t", "outcome": "Yes", "side": "SELL", "price": 0.40, "size": 100, "transaction_hash": "late", "slug": "s", "event_slug": "e"},
                {"timestamp": 1020, "received_ms": 1021000, "lag_ms": 1000, "condition_id": "c", "asset_id": "t", "outcome": "Yes", "side": "SELL", "price": 0.40, "size": 15, "transaction_hash": "b", "slug": "s", "event_slug": "e"},
            ])
            write_csv(fills, ["timestamp", "market_id", "slug", "action", "side", "shares", "price", "fee", "reason"], [])
            result = audit(order, tape, fills, prior_lookback_seconds=120, forward_horizon_seconds=60)
            post = result["posts"][0]
            self.assertEqual(post["prior_compatible_sell_shares"], 30.0)
            self.assertEqual(post["future_compatible_sell_shares"], 15.0)
            self.assertEqual(result["decision"], "ZERO_FILL_DESPITE_CAUSAL_FLOW")

    def test_research_workflow_runs_three_flow_policy_arms(self) -> None:
        workflow = Path(".github/workflows/v6-research-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/v6_micro_maker_v2.py", workflow)
        self.assertIn("root=v6_evidence/maker_ab", workflow)
        self.assertIn('flow="$root/flow"', workflow)
        self.assertIn('touch="$root/touch"', workflow)
        self.assertIn('confidence="$root/confidence"', workflow)
        self.assertIn('--run-dir "$flow"', workflow)
        self.assertIn('--run-dir "$touch"', workflow)
        self.assertIn('--run-dir "$confidence"', workflow)
        self.assertIn("--min-fill-probability 0.005", workflow)
        self.assertIn("--max-improve-ticks 0", workflow)
        self.assertIn("--min-inside-confidence 0.80", workflow)
        self.assertIn("V6_MAKER_DEAD_FLOW_CANCEL_SECONDS", workflow)

    def test_research_workflow_accepts_authorized_paper_ceiling(self) -> None:
        workflow = Path(".github/workflows/v6-research-smoke.yml").read_text(encoding="utf-8")
        self.assertIn(
            "from hard_safety_policy import V6_AUTHORIZED_CEILINGS, V6_AUTHORIZED_FLOORS",
            workflow,
        )
        self.assertIn("V6_AUTHORIZED_CEILINGS['max_market_fraction']", workflow)
        self.assertIn("V6_AUTHORIZED_CEILINGS['max_event_fraction']", workflow)
        self.assertIn("V6_AUTHORIZED_CEILINGS['max_gross_fraction']", workflow)
        self.assertIn("V6_AUTHORIZED_CEILINGS['max_drawdown']", workflow)
        self.assertIn("V6_AUTHORIZED_CEILINGS['fractional_kelly']", workflow)
        self.assertIn("V6_AUTHORIZED_CEILINGS['max_trade_usd']", workflow)
        self.assertIn("V6_AUTHORIZED_FLOORS['min_liquidity']", workflow)
        self.assertIn("V6_AUTHORIZED_FLOORS['min_net_edge']", workflow)
        self.assertNotIn("float(cfg['max_market_fraction']) <= .025", workflow)
        self.assertNotIn("float(cfg['max_event_fraction']) <= .08", workflow)
        self.assertNotIn("float(cfg['max_gross_fraction']) <= .45", workflow)


if __name__ == "__main__":
    unittest.main()
