#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from exporter_v7 import V7Collector  # noqa: E402


class MonitoringV7ExporterTests(unittest.TestCase):
    def test_v7_collector_reads_execution_and_shadow_without_legacy_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "runs" / "paper_v7_live"
            execution = run / "execution"
            shadow = run / "shadow"
            execution.mkdir(parents=True)
            shadow.mkdir(parents=True)
            config = root / "config" / "paper_v7.json"
            config.parent.mkdir(parents=True)
            config.write_text((ROOT / "config" / "paper_v7.json").read_text(encoding="utf-8"), encoding="utf-8")

            (execution / "runtime_status.json").write_text(json.dumps({
                "schema": "polymarket_v7_runtime_status_v1",
                "version": 7,
                "paper_only": True,
                "authenticated_execution": False,
                "equity": 10012.0,
                "pnl": 12.0,
                "drawdown": 0.001,
                "killed": False,
                "live_units": 2,
                "reserved_cash": 100.0,
                "gross_exposure": 200.0,
                "realized_pnl": 5.0,
                "execution_imbalance": 0.0,
                "execution_staleness": 0.0,
            }), encoding="utf-8")
            (execution / "allocator_status.json").write_text(json.dumps({
                "models_expected": 5,
                "models_alive": 5,
                "global_gross_fraction": 0.02,
            }), encoding="utf-8")
            (execution / "strategy_status.csv").write_text(
                "name,pnl,equity,open_positions,fills,alive,status_age_seconds,drawdown,gross_exposure\n"
                "micro_maker,3,2203,1,2,1,0,0,40\n",
                encoding="utf-8",
            )
            (run / "v7_supervisor.json").write_text(json.dumps({
                "timestamp": __import__("time").time(),
                "execution_alive": True,
                "shadow_alive": True,
            }), encoding="utf-8")
            (execution / "v7_execution_supervisor.json").write_text(json.dumps({
                "timestamp": __import__("time").time(),
            }), encoding="utf-8")
            (execution / "market_proxy_status.json").write_text(json.dumps({
                "schema": "polymarket_v7_market_proxy_status_v1",
                "timestamp": __import__("time").time(),
            }), encoding="utf-8")
            (shadow / "local_factor_30m.json").write_text(json.dumps({"by_selected_pairs": 2, "post_multiplicity_pair_signals": 1}), encoding="utf-8")
            (shadow / "local_factor_60m.json").write_text(json.dumps({"by_selected_pairs": 1, "post_multiplicity_pair_signals": 0}), encoding="utf-8")
            (shadow / "cross_sectional_rank.json").write_text(json.dumps({"forward": [{
                "horizon_minutes": 120,
                "completed_sections": 10,
                "mean_rank_ic": 0.03,
                "mean_top_bottom_logit_spread": 0.02,
                "forward_statistical_gate": False,
            }]}), encoding="utf-8")
            (shadow / "hf_frequency_probe.json").write_text(json.dumps({"cadences": [{
                "cadence_seconds": 5,
                "nonempty_bucket_fraction": 0.4,
                "maker_clearable_fraction": 0.1,
                "max_best_queue_clearance_ratio": 0.8,
            }]}), encoding="utf-8")

            collector = V7Collector(run, config, 20)
            healthy, detail = collector.health()
            self.assertTrue(healthy, detail)
            text = collector.collect()
            self.assertIn('adapter="v7"', text)
            self.assertIn('version="v7"', text)
            self.assertIn("polymarket_v7_runtime_info 1", text)
            self.assertIn("polymarket_v7_execution_alive 1", text)
            self.assertIn('polymarket_v7_local_factor_by_selected_pairs{fidelity="30m"} 2', text)
            self.assertIn('polymarket_v7_rank_mean_ic{horizon_minutes="120"} 0.03', text)
            self.assertIn('polymarket_v7_hf_maker_clearable_fraction{cadence_seconds="5"} 0.1', text)
            self.assertIn("polymarket_runtime_equity_usd 10012", text)
            self.assertIn('polymarket_model_pnl_usd{model="micro_maker"} 3', text)

    def test_v7_monitoring_has_no_version_adapter_chain(self) -> None:
        exporter = (ROOT / "monitoring" / "exporter_v7.py").read_text(encoding="utf-8")
        entrypoint = (ROOT / "monitoring" / "exporter_latest_v7.py").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
        self.assertNotIn("exporter_v6", exporter)
        self.assertNotIn("exporter_latest", exporter)
        self.assertNotIn("v6_runtime_data_health", entrypoint)
        self.assertNotIn("importlib", entrypoint)
        self.assertIn("legacy/non-V7 run name rejected", entrypoint)
        self.assertIn("/app/exporter_latest_v7.py", compose)
        self.assertFalse((ROOT / "monitoring" / "exporter_latest.py").exists())


if __name__ == "__main__":
    unittest.main()
