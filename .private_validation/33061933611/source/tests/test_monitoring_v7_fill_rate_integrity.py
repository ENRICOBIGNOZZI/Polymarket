from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = ROOT / "monitoring" / "exporter_v7.py"
SPEC = importlib.util.spec_from_file_location("exporter_v7_fill_rate_integrity", EXPORTER_PATH)
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = exporter
SPEC.loader.exec_module(exporter)


class V7FillRateIntegrityTest(unittest.TestCase):
    @staticmethod
    def _snapshot(models: dict[str, dict]) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            evidence_path = run_root / "execution" / "v7_execution_evidence.json"
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(
                json.dumps(
                    {
                        "timestamp": 1_000,
                        "paper_only": True,
                        "summary": {},
                        "models": models,
                    }
                ),
                encoding="utf-8",
            )
            return exporter.collect_snapshot(run_root, ROOT, now=1_000)

    def test_relative_value_leg_fill_ratio_is_never_exported_as_fill_probability(self) -> None:
        for raw_fill_rate in (1.0, 5.0):
            with self.subTest(raw_fill_rate=raw_fill_rate):
                snapshot = self._snapshot(
                    {
                        "relative_value": {
                            "target": "hedged_convergence",
                            "orders_submitted": 20,
                            "fills": 20,
                            "fill_rate": raw_fill_rate,
                        }
                    }
                )
                row = snapshot["strategies"]["relative_value"]
                self.assertFalse(row["fill_rate_valid"])
                self.assertIsNone(row["fill_rate"])
                metrics = exporter.render_prometheus(snapshot)
                self.assertNotIn('polymarket_strategy_fill_rate{strategy="relative_value"}', metrics)
                self.assertIn('polymarket_strategy_fill_rate_valid{strategy="relative_value"} 0', metrics)

    def test_single_order_fill_rate_remains_visible_only_inside_probability_bounds(self) -> None:
        good = self._snapshot(
            {
                "micro_maker": {
                    "target": "short_horizon_markout",
                    "orders_submitted": 20,
                    "fills": 5,
                    "fill_rate": 0.25,
                }
            }
        )
        metrics = exporter.render_prometheus(good)
        self.assertIn('polymarket_strategy_fill_rate{strategy="micro_maker"} 0.25', metrics)
        self.assertIn('polymarket_strategy_fill_rate_valid{strategy="micro_maker"} 1', metrics)

        invalid = self._snapshot(
            {
                "micro_maker": {
                    "target": "short_horizon_markout",
                    "orders_submitted": 4,
                    "fills": 5,
                    "fill_rate": 1.25,
                }
            }
        )
        metrics = exporter.render_prometheus(invalid)
        self.assertNotIn('polymarket_strategy_fill_rate{strategy="micro_maker"}', metrics)
        self.assertIn('polymarket_strategy_fill_rate_valid{strategy="micro_maker"} 0', metrics)


if __name__ == "__main__":
    unittest.main()
