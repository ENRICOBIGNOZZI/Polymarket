#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from exporter_latest import LatestCollector  # noqa: E402


class MonitoringV7ExporterTests(unittest.TestCase):
    def test_latest_collector_selects_v7_adapter_and_execution_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            config_dir = root / "config"
            run = runs / "paper_v7_live"
            execution = run / "execution"
            shadow = run / "shadow"
            execution.mkdir(parents=True)
            shadow.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            config_path = config_dir / "paper_v7.json"
            config_path.write_text((ROOT / "config" / "paper_v7.json").read_text(encoding="utf-8"), encoding="utf-8")

            (execution / "runtime_status.json").write_text(
                json.dumps({
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
                    "strategies": {},
                }),
                encoding="utf-8",
            )
            (run / "v7_supervisor.json").write_text(
                json.dumps({"execution_alive": True, "shadow_alive": True}), encoding="utf-8"
            )
            (shadow / "local_factor_30m.json").write_text(
                json.dumps({"by_selected_pairs": 2, "post_multiplicity_pair_signals": 1, "promotion_ready": False}),
                encoding="utf-8",
            )
            (shadow / "local_factor_60m.json").write_text(
                json.dumps({"by_selected_pairs": 1, "post_multiplicity_pair_signals": 0, "promotion_ready": False}),
                encoding="utf-8",
            )
            (shadow / "cross_sectional_rank.json").write_text(
                json.dumps({"forward": [{
                    "horizon_minutes": 120,
                    "completed_sections": 10,
                    "mean_rank_ic": 0.03,
                    "mean_top_bottom_logit_spread": 0.02,
                    "forward_statistical_gate": False,
                }]}),
                encoding="utf-8",
            )
            (shadow / "hf_frequency_probe.json").write_text(
                json.dumps({"cadences": [{
                    "cadence_seconds": 5,
                    "nonempty_bucket_fraction": 0.4,
                    "maker_clearable_fraction": 0.1,
                    "max_best_queue_clearance_ratio": 0.8,
                }]}),
                encoding="utf-8",
            )
            (shadow / "scheduler_status.json").write_text(json.dumps({"timestamp": 1}), encoding="utf-8")

            collector = LatestCollector(runs, config_dir, "paper_v7_live", str(config_path), 20)
            text = collector.collect()
            self.assertIn('adapter="v7"', text)
            self.assertIn('version="v7"', text)
            self.assertIn("polymarket_v7_runtime_info 1", text)
            self.assertIn("polymarket_v7_execution_alive 1", text)
            self.assertIn('polymarket_v7_local_factor_by_selected_pairs{fidelity="30m"} 2', text)
            self.assertIn('polymarket_v7_rank_mean_ic{horizon_minutes="120"} 0.03', text)
            self.assertIn('polymarket_v7_hf_maker_clearable_fraction{cadence_seconds="5"} 0.1', text)
            self.assertIn("polymarket_runtime_equity_usd 10012", text)

    def test_latest_v7_health_wrapper_targets_execution_subdir(self) -> None:
        source = (ROOT / "monitoring" / "exporter_latest_v7.py").read_text(encoding="utf-8")
        self.assertIn('Path(root) / "execution"', source)
        compose = (ROOT / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
        self.assertIn("/app/exporter_latest_v7.py", compose)


if __name__ == "__main__":
    unittest.main()
