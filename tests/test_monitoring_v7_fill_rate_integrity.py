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
    def _event(event_type: str, *, strategy: str, complete=None) -> dict:
        return {
            "schema_version": 1,
            "event_type": event_type,
            "strategy": strategy,
            "model_sha": "a" * 40,
            "paper_only": True,
            "authenticated_execution": False,
            "complete": complete,
        }

    def _snapshot(self, *, strategy: str, orders: int, fills: int) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            ledger = run_root / "ledger" / "execution.jsonl"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            events = [self._event("ORDER_SUBMITTED", strategy=strategy) for _ in range(orders)]
            events += [self._event("FILL", strategy=strategy, complete=True) for _ in range(fills)]
            ledger.write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")

            return exporter.collect_snapshot(run_root, ROOT, now=1_000)

    def test_multileg_counts_never_become_joint_completion_probability(self) -> None:
        snapshot = self._snapshot(strategy="relative_value", orders=20, fills=20)
        metrics = exporter.render_prometheus(snapshot)
        self.assertNotIn('polymarket_strategy_fill_rate{strategy="relative_value"}', metrics)
        self.assertNotIn("polymarket_strategy_fill_rate_valid", metrics)
        self.assertIn('polymarket_strategy_ledger_orders_submitted{strategy="relative_value"} 20', metrics)
        self.assertIn('polymarket_strategy_ledger_fills{strategy="relative_value"} 20', metrics)

    def test_single_order_monitoring_uses_canonical_counts(self) -> None:
        snapshot = self._snapshot(
            strategy="micro_maker",
            orders=20,
            fills=5,
        )
        metrics = exporter.render_prometheus(snapshot)
        self.assertNotIn('polymarket_strategy_fill_rate{strategy="micro_maker"}', metrics)
        self.assertNotIn("polymarket_strategy_fill_rate_valid", metrics)
        self.assertIn('polymarket_strategy_ledger_orders_submitted{strategy="micro_maker"} 20', metrics)
        self.assertIn('polymarket_strategy_ledger_fills{strategy="micro_maker"} 5', metrics)


if __name__ == "__main__":
    unittest.main()
