#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from exporter_latest import LatestCollector  # noqa: E402


class MonitoringV5ExporterTests(unittest.TestCase):
    def test_latest_collector_selects_v5_and_exports_each_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            config_dir = root / "config"
            run = runs / "paper_v5_live"
            strategy = run / "strategies" / "graph"
            strategy.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            config_path = config_dir / "paper_v5.json"
            config_path.write_text((ROOT / "config" / "paper_v5.json").read_text(encoding="utf-8"), encoding="utf-8")
            now = int(time.time())

            (run / "runtime_status.json").write_text(
                json.dumps(
                    {
                        "equity": 10012.5,
                        "pnl": 12.5,
                        "drawdown": 0.01,
                        "killed": False,
                        "live_units": 2,
                        "reserved_cash": 1000.0,
                        "gross_exposure": 120.0,
                        "realized_pnl": 4.0,
                        "execution_imbalance": 0.0,
                        "execution_staleness": 3.0,
                        "oos": {},
                    }
                ),
                encoding="utf-8",
            )
            (run / "allocator_status.json").write_text(
                json.dumps(
                    {
                        "models_alive": 5,
                        "models_expected": 5,
                        "reserve_fraction": 0.10,
                        "global_gross_fraction": 0.012,
                        "global_max_gross_fraction": 0.35,
                        "global_max_drawdown": 0.15,
                    }
                ),
                encoding="utf-8",
            )
            fields = [
                "timestamp", "name", "expert", "capital_fraction", "starting_capital", "cash", "equity", "pnl",
                "realized_pnl", "peak_equity", "drawdown", "gross_exposure", "open_positions", "killed", "alive",
                "status_age_seconds", "restarts", "fills", "buy_fills", "sell_fills", "settle_fills", "last_error",
            ]

            def write_strategy_status(status_age: float) -> None:
                with (run / "strategy_status.csv").open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerow(
                        {
                            "timestamp": now,
                            "name": "graph",
                            "expert": "graph",
                            "capital_fraction": 0.30,
                            "starting_capital": 3000,
                            "cash": 2940,
                            "equity": 3012.5,
                            "pnl": 12.5,
                            "realized_pnl": 4.0,
                            "peak_equity": 3020,
                            "drawdown": 0.0025,
                            "gross_exposure": 72.5,
                            "open_positions": 2,
                            "killed": 0,
                            "alive": 1,
                            "status_age_seconds": status_age,
                            "restarts": 0,
                            "fills": 3,
                            "buy_fills": 2,
                            "sell_fills": 1,
                            "settle_fills": 0,
                            "last_error": "",
                        }
                    )

            def write_start_event(timestamp: float) -> None:
                with (run / "allocator_events.csv").open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=["timestamp", "event", "strategy"])
                    writer.writeheader()
                    writer.writerow({"timestamp": timestamp, "event": "start", "strategy": "graph"})

            write_strategy_status(2)
            with (strategy / "signals.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["cost_adjusted_edge", "net_edge"])
                writer.writeheader()
                writer.writerow({"cost_adjusted_edge": 0.012, "net_edge": 0.010})
                writer.writerow({"cost_adjusted_edge": -0.001, "net_edge": -0.002})

            collector = LatestCollector(runs, config_dir, "paper_v5_live", str(config_path), 20)
            text = collector.collect()
            self.assertIn('adapter="v5"', text)
            self.assertIn('version="v5"', text)
            self.assertIn('polymarket_runtime_pnl_usd 12.5', text)
            self.assertIn('polymarket_model_pnl_usd{expert="graph",model="graph"} 12.5', text)
            self.assertIn('polymarket_model_alive{expert="graph",model="graph"} 1', text)
            self.assertIn('polymarket_model_fills_total{action="all",expert="graph",model="graph"} 3', text)
            self.assertIn('polymarket_model_cost_positive_signals_total{expert="graph",model="graph"} 1', text)
            self.assertIn('polymarket_model_best_net_edge_ratio{expert="graph",model="graph"} 0.01', text)

            write_strategy_status(1e12)
            write_start_event(time.time())
            startup_text = collector.collect()
            self.assertIn('polymarket_model_status_age_seconds{expert="graph",model="graph"} 1e+12', startup_text)
            self.assertIn('polymarket_model_alert_staleness_seconds{expert="graph",model="graph"} 0', startup_text)
            self.assertIn('polymarket_model_startup_grace_active{expert="graph",model="graph"} 1', startup_text)

            write_start_event(time.time() - 601)
            expired_text = collector.collect()
            self.assertIn('polymarket_model_alert_staleness_seconds{expert="graph",model="graph"} 1e+12', expired_text)
            self.assertIn('polymarket_model_startup_grace_active{expert="graph",model="graph"} 0', expired_text)


if __name__ == "__main__":
    unittest.main()
