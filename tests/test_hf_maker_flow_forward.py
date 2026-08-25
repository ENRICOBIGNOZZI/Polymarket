from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.hf_maker_flow_admission import evaluate
from scripts.hf_maker_forward_audit import audit, realized_roundtrip_pnl
from scripts.v6_market_common import TapeFlow
from scripts.v6_micro_maker_v2 import enforce_total_gross_cap


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

    def test_research_workflow_runs_flow_aware_engine_not_posthoc_only(self) -> None:
        workflow = Path(".github/workflows/v6-research-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/v6_micro_maker_v2.py", workflow)
        self.assertIn("root=v6_evidence/maker_ab", workflow)
        self.assertIn('baseline="$root/baseline"', workflow)
        self.assertIn('flow="$root/flow"', workflow)
        self.assertIn('--run-dir "$flow"', workflow)
        self.assertIn("--min-fill-probability 0.005", workflow)
        self.assertIn("V6_MAKER_DEAD_FLOW_CANCEL_SECONDS", workflow)

    def test_research_workflow_accepts_authorized_paper_ceiling(self) -> None:
        workflow = Path(".github/workflows/v6-research-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("float(cfg['max_market_fraction']) <= .05", workflow)
        self.assertIn("float(cfg['max_event_fraction']) <= .15", workflow)
        self.assertIn("float(cfg['max_gross_fraction']) <= .70", workflow)
        self.assertIn("float(cfg['max_drawdown']) <= .15", workflow)
        self.assertNotIn("float(cfg['max_market_fraction']) <= .025", workflow)
        self.assertNotIn("float(cfg['max_event_fraction']) <= .08", workflow)
        self.assertNotIn("float(cfg['max_gross_fraction']) <= .45", workflow)


if __name__ == "__main__":
    unittest.main()
