#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from exporter_latest import LatestCollector  # noqa: E402
from exporter_v6 import _multileg_fill_counts  # noqa: E402


class MonitoringV6ExporterTests(unittest.TestCase):
    def test_latest_collector_exports_stable_grafana_schema_for_v6(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            config_dir = root / "config"
            run = runs / "paper_v6_live"
            run.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            config_path = config_dir / "paper_v6.json"
            config_path.write_text((ROOT / "config" / "paper_v6.json").read_text(encoding="utf-8"), encoding="utf-8")

            # runtime_status deliberately contains stale/zero relative-value fill
            # data. Grafana must still report the durable multi-leg ledger below.
            strategies = {
                "micro_maker": {
                    "equity": 1210,
                    "pnl": 10,
                    "live_units": 2,
                    "killed": False,
                    "signals": 4,
                    "best_edge": 0.0012,
                    "fills": 0,
                    "buy_fills": 0,
                    "sell_fills": 0,
                    "settle_fills": 0,
                },
                "relative_value": {
                    "equity": 5025,
                    "pnl": 25,
                    "live_units": 3,
                    "killed": False,
                },
            }
            (run / "runtime_status.json").write_text(
                json.dumps(
                    {
                        "equity": 10035,
                        "pnl": 35,
                        "drawdown": 0.02,
                        "killed": False,
                        "live_units": 5,
                        "reserved_cash": 300,
                        "gross_exposure": 255,
                        "realized_pnl": 9,
                        "execution_imbalance": 0,
                        "execution_staleness": 0,
                        "strategies": strategies,
                        "relations": {"bundles": 2, "best_edge": 0.004},
                        "local_factor": {"bundles": 1, "clusters": 3, "best_edge": 0.003},
                        "external_bridge": {"materialized_signals": 0},
                    }
                ),
                encoding="utf-8",
            )
            (run / "allocator_status.json").write_text(
                json.dumps(
                    {
                        "models_alive": 5,
                        "models_expected": 5,
                        "reserve_fraction": 0.05,
                        "global_gross_fraction": 0.0255,
                        "global_max_gross_fraction": 0.45,
                        "global_max_drawdown": 0.15,
                    }
                ),
                encoding="utf-8",
            )
            with (run / "multileg_equity.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["timestamp", "cash", "equity", "reserved_cash", "gross_entry_cash", "peak_equity", "drawdown", "killed", "live_bundles"],
                )
                writer.writeheader()
                writer.writerow({"timestamp": 1, "cash": 4900, "equity": 5025, "reserved_cash": 30, "gross_entry_cash": 150, "peak_equity": 5030, "drawdown": 0.02, "killed": 0, "live_bundles": 3})
            self._write_multileg_events(run / "multileg_events.csv")
            with (run / "bundle_ledger.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["net_pnl"])
                writer.writeheader()
                writer.writerow({"net_pnl": 7})

            collector = LatestCollector(runs, config_dir, "paper_v6_live", str(config_path), 20)
            text = collector.collect()
            self.assertIn('adapter="v6"', text)
            self.assertIn('version="v6"', text)
            self.assertIn('polymarket_model_gross_exposure_usd{expert="relative_value",model="relative_value"} 180', text)
            self.assertIn('polymarket_model_drawdown_ratio{expert="relative_value",model="relative_value"} 0.02', text)
            self.assertIn('polymarket_model_alive{expert="relative_value",model="relative_value"} 1', text)
            self.assertIn('polymarket_model_fills_total{action="all",expert="relative_value",model="relative_value"} 7', text)
            self.assertIn('polymarket_model_fills_total{action="buy",expert="relative_value",model="relative_value"} 4', text)
            self.assertIn('polymarket_model_fills_total{action="sell",expert="relative_value",model="relative_value"} 2', text)
            self.assertIn('polymarket_model_fills_total{action="settle",expert="relative_value",model="relative_value"} 1', text)
            self.assertIn('polymarket_model_realized_pnl_usd{expert="relative_value",model="relative_value"} 7', text)
            self.assertIn('polymarket_model_signals_total{expert="micro_maker",model="micro_maker"} 4', text)
            self.assertIn('polymarket_allocator_global_max_gross_fraction 0.45', text)

    @staticmethod
    def _write_multileg_events(path: Path) -> None:
        fields = ["timestamp", "event", "bundle_id", "market_id", "side", "shares", "price", "queue_ahead", "detail"]
        rows = [
            {"timestamp": 1, "event": "POST", "shares": 0},
            {"timestamp": 2, "event": "QUEUE_TRADE_DEPLETION", "shares": 0},
            {"timestamp": 3, "event": "PARTIAL_FILL", "shares": 10},
            {"timestamp": 4, "event": "PARTIAL_FILL", "shares": 5},
            {"timestamp": 5, "event": "PARTIAL_FILL", "shares": 4},
            {"timestamp": 6, "event": "PARTIAL_FILL", "shares": 3},
            {"timestamp": 7, "event": "EXIT_TAKER", "shares": 15},
            {"timestamp": 8, "event": "EXIT_TAKER", "shares": 7},
            {"timestamp": 9, "event": "SETTLE", "shares": 2},
            {"timestamp": 10, "event": "CANCEL_EFFECTIVE", "shares": 0},
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def test_relative_value_fill_counter_uses_only_execution_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "multileg_events.csv"
            self._write_multileg_events(path)
            counts = _multileg_fill_counts(path)
            self.assertEqual(counts, {"fills": 7, "buy_fills": 4, "sell_fills": 2, "settle_fills": 1})


if __name__ == "__main__":
    unittest.main()
