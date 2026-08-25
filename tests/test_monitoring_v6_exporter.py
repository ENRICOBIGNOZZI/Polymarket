#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from exporter_latest import LatestCollector  # noqa: E402


def load_runtime_module():
    path = ROOT / "scripts" / "v6_runtime_status.py"
    spec = importlib.util.spec_from_file_location("v6_runtime_status_contract_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

            strategies = {
                "micro_maker": {
                    "capital_fraction": 0.12,
                    "starting_capital": 1200,
                    "cash": 1150,
                    "equity": 1210,
                    "pnl": 10,
                    "realized_pnl": 2,
                    "gross_exposure": 75,
                    "drawdown": 0.01,
                    "live_units": 2,
                    "killed": False,
                    "signals": 4,
                    "best_edge": 0.0012,
                    "fills": 5,
                    "buy_fills": 3,
                    "sell_fills": 2,
                    "settle_fills": 0,
                },
                "relative_value": {
                    "capital_fraction": 0.50,
                    "starting_capital": 5000,
                    "cash": 4900,
                    "equity": 5025,
                    "pnl": 25,
                    "realized_pnl": 7,
                    "gross_exposure": 180,
                    "drawdown": 0.02,
                    "live_units": 3,
                    "killed": False,
                    "signals": 6,
                    "best_edge": 0.004,
                    "fills": 7,
                    "buy_fills": 4,
                    "sell_fills": 2,
                    "settle_fills": 1,
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
                        "local_factor": {"bundles": 1, "clusters": 3},
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
            self.assertIn('polymarket_model_signals_total{expert="micro_maker",model="micro_maker"} 4', text)
            self.assertIn('polymarket_allocator_global_max_gross_fraction 0.45', text)

    def test_relative_value_fill_counter_uses_only_execution_events(self) -> None:
        runtime = load_runtime_module()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "multileg_events.csv"
            fields = ["timestamp", "event", "bundle_id", "market_id", "side", "shares", "price", "queue_ahead", "detail"]
            rows = [
                {"timestamp": 1, "event": "POST", "shares": 0},
                {"timestamp": 2, "event": "QUEUE_TRADE_DEPLETION", "shares": 0},
                {"timestamp": 3, "event": "PARTIAL_FILL", "shares": 10},
                {"timestamp": 4, "event": "PARTIAL_FILL", "shares": 5},
                {"timestamp": 5, "event": "EXIT_TAKER", "shares": 15},
                {"timestamp": 6, "event": "SETTLE", "shares": 2},
                {"timestamp": 7, "event": "CANCEL_EFFECTIVE", "shares": 0},
            ]
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
            counts = runtime.multileg_fill_counts(path)
            self.assertEqual(counts, {"fills": 4, "buy_fills": 2, "sell_fills": 1, "settle_fills": 1})


if __name__ == "__main__":
    unittest.main()
