#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "research_v5_sleeve_oos.py"
SPEC = importlib.util.spec_from_file_location("research_v5_sleeve_oos", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


SIGNAL_FIELDS = [
    "timestamp", "market_id", "slug", "side", "mid", "exec_price", "fair_side", "fair_yes",
    "uncertainty", "fee_per_share", "slippage_per_share", "gross_edge", "cost_adjusted_edge",
    "net_edge", "score", "desired_notional", "experts",
]
FILL_FIELDS = ["timestamp", "market_id", "slug", "action", "side", "shares", "price", "notional", "fee"]


class V5SleeveOOSAttributionTest(unittest.TestCase):
    def test_buy_sell_round_trip_is_attributed_to_prior_signal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "strategies" / "micro"
            write_csv(run / "signals.csv", SIGNAL_FIELDS, [{
                "timestamp": 100, "market_id": "m1", "slug": "m1", "side": "YES", "exec_price": 0.39,
                "net_edge": 0.03, "desired_notional": 10,
            }])
            write_csv(run / "fills.csv", FILL_FIELDS, [
                {"timestamp": 101, "market_id": "m1", "slug": "m1", "action": "BUY", "side": "YES", "shares": 10, "price": 0.40, "notional": 4.0, "fee": 0.10},
                {"timestamp": 200, "market_id": "m1", "slug": "m1", "action": "SELL", "side": "YES", "shares": 10, "price": 0.50, "notional": 5.0, "fee": 0.10},
            ])
            trades, audit = MODULE.reconstruct_strategy("micro", run, signal_max_lag_seconds=15)
            self.assertEqual(len(trades), 1)
            trade = trades[0]
            self.assertTrue(trade.lineage_ok)
            self.assertEqual(trade.signal_ts, 100)
            self.assertEqual(trade.signal_age_seconds, 1)
            self.assertAlmostEqual(trade.expected_edge, 0.03)
            self.assertAlmostEqual(trade.gross_pnl, 1.0)
            self.assertAlmostEqual(trade.net_pnl, 0.8)
            self.assertAlmostEqual(trade.capital, 4.1)
            self.assertAlmostEqual(trade.entry_price_drift_cost, 0.1)
            self.assertEqual(audit["entry_signal_missing"], 0)

    def test_future_signal_cannot_backfill_entry_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "strategies" / "micro"
            write_csv(run / "signals.csv", SIGNAL_FIELDS, [{
                "timestamp": 102, "market_id": "m1", "slug": "m1", "side": "YES", "exec_price": 0.40,
                "net_edge": 0.02,
            }])
            write_csv(run / "fills.csv", FILL_FIELDS, [
                {"timestamp": 101, "market_id": "m1", "slug": "m1", "action": "BUY", "side": "YES", "shares": 5, "price": 0.40, "notional": 2.0, "fee": 0.02},
                {"timestamp": 110, "market_id": "m1", "slug": "m1", "action": "SELL", "side": "YES", "shares": 5, "price": 0.45, "notional": 2.25, "fee": 0.02},
            ])
            trades, audit = MODULE.reconstruct_strategy("micro", run)
            self.assertEqual(len(trades), 1)
            self.assertFalse(trades[0].lineage_ok)
            self.assertIsNone(trades[0].expected_edge)
            self.assertEqual(audit["entry_signal_missing"], 1)

    def test_open_buy_is_not_misreported_as_realized_trade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "strategies" / "graph"
            write_csv(run / "signals.csv", SIGNAL_FIELDS, [{
                "timestamp": 10, "market_id": "m2", "slug": "m2", "side": "NO", "exec_price": 0.30,
                "net_edge": 0.02,
            }])
            write_csv(run / "fills.csv", FILL_FIELDS, [{
                "timestamp": 11, "market_id": "m2", "slug": "m2", "action": "BUY", "side": "NO",
                "shares": 20, "price": 0.30, "notional": 6.0, "fee": 0.03,
            }])
            trades, audit = MODULE.reconstruct_strategy("graph", run)
            self.assertEqual(trades, [])
            self.assertEqual(audit["open_lots"], 1)
            self.assertAlmostEqual(audit["open_shares"], 20.0)

    def test_settlement_closes_position_and_stress_is_conservative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "strategies" / "semantic"
            write_csv(run / "signals.csv", SIGNAL_FIELDS, [{
                "timestamp": 20, "market_id": "m3", "slug": "m3", "side": "YES", "exec_price": 0.60,
                "net_edge": 0.05,
            }])
            write_csv(run / "fills.csv", FILL_FIELDS, [
                {"timestamp": 20, "market_id": "m3", "slug": "m3", "action": "BUY", "side": "YES", "shares": 10, "price": 0.60, "notional": 6.0, "fee": 0.06},
                {"timestamp": 500, "market_id": "m3", "slug": "m3", "action": "SETTLE", "side": "YES", "shares": 10, "price": 1.0, "notional": 10.0, "fee": 0.0},
            ])
            trades, _ = MODULE.reconstruct_strategy("semantic", run)
            self.assertEqual(len(trades), 1)
            base = MODULE.stressed_pnl(trades[0], 1.0, 5.0)
            one_half = MODULE.stressed_pnl(trades[0], 1.5, 5.0)
            twice = MODULE.stressed_pnl(trades[0], 2.0, 5.0)
            self.assertAlmostEqual(base, 3.94)
            self.assertGreater(base, one_half)
            self.assertGreater(one_half, twice)

    def test_partial_exit_is_fifo_and_does_not_lose_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "strategies" / "external"
            write_csv(run / "signals.csv", SIGNAL_FIELDS, [{
                "timestamp": 1, "market_id": "m4", "slug": "m4", "side": "YES", "exec_price": 0.20,
                "net_edge": 0.04,
            }])
            write_csv(run / "fills.csv", FILL_FIELDS, [
                {"timestamp": 1, "market_id": "m4", "slug": "m4", "action": "BUY", "side": "YES", "shares": 10, "price": 0.20, "notional": 2.0, "fee": 0.02},
                {"timestamp": 2, "market_id": "m4", "slug": "m4", "action": "SELL", "side": "YES", "shares": 4, "price": 0.30, "notional": 1.2, "fee": 0.01},
                {"timestamp": 3, "market_id": "m4", "slug": "m4", "action": "SELL", "side": "YES", "shares": 6, "price": 0.35, "notional": 2.1, "fee": 0.015},
            ])
            trades, audit = MODULE.reconstruct_strategy("external", run)
            self.assertEqual(len(trades), 2)
            self.assertAlmostEqual(sum(row.shares for row in trades), 10.0)
            self.assertEqual(audit["open_lots"], 0)
            self.assertEqual(audit["unmatched_exit_rows"], 0)

    def test_run_root_ablation_excludes_unlinked_trade_from_economic_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / "strategies" / "micro"
            write_csv(run / "signals.csv", SIGNAL_FIELDS, [])
            write_csv(run / "fills.csv", FILL_FIELDS, [
                {"timestamp": 1, "market_id": "m5", "slug": "m5", "action": "BUY", "side": "YES", "shares": 10, "price": 0.4, "notional": 4.0, "fee": 0.01},
                {"timestamp": 2, "market_id": "m5", "slug": "m5", "action": "SELL", "side": "YES", "shares": 10, "price": 0.5, "notional": 5.0, "fee": 0.01},
            ])
            report, trades = MODULE.evaluate_run_root(root, strategies=["micro"], min_closed_trades=1)
            self.assertEqual(len(trades), 1)
            self.assertEqual(report["lineage"]["linked_closed_trade_fragments"], 0)
            self.assertEqual(report["aggregate"]["1x"]["trades"], 0)
            self.assertFalse(report["evidence_ready"])
            self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
