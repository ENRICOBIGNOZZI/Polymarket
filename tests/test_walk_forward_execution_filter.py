from __future__ import annotations

import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "walk_forward_v4.py"

spec = importlib.util.spec_from_file_location("walk_forward_v4", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


class WalkForwardExecutionFilterTest(unittest.TestCase):
    def test_only_executed_capital_enters_oos_sample(self):
        fields = [
            "bundle_id", "strategy", "event_id", "created_ts", "closed_ts", "status",
            "expected_edge", "max_notional", "entry_cash", "gross_pnl", "fees", "slippage",
            "net_pnl", "return_on_capital", "fill_fraction", "adverse_mark_pnl", "abort_reason",
        ]
        rows = [
            {
                "bundle_id": "zero-fill", "strategy": "B1", "event_id": "e1", "created_ts": "1",
                "closed_ts": "2", "status": "UNWOUND", "expected_edge": "0.01", "max_notional": "100",
                "entry_cash": "0", "gross_pnl": "0", "fees": "0", "slippage": "0", "net_pnl": "0",
                "return_on_capital": "0", "fill_fraction": "0", "adverse_mark_pnl": "0", "abort_reason": "timeout",
            },
            {
                "bundle_id": "partial-fill", "strategy": "B1", "event_id": "e2", "created_ts": "3",
                "closed_ts": "4", "status": "UNWOUND", "expected_edge": "0.01", "max_notional": "100",
                "entry_cash": "12.5", "gross_pnl": "0.4", "fees": "0.1", "slippage": "0.1", "net_pnl": "0.2",
                "return_on_capital": "0.016", "fill_fraction": "0", "adverse_mark_pnl": "0", "abort_reason": "leg_risk",
            },
            {
                "bundle_id": "closed-fill", "strategy": "B2", "event_id": "e3", "created_ts": "5",
                "closed_ts": "6", "status": "CLOSED", "expected_edge": "0.02", "max_notional": "100",
                "entry_cash": "20", "gross_pnl": "1", "fees": "0.2", "slippage": "0.1", "net_pnl": "0.7",
                "return_on_capital": "0.035", "fill_fraction": "1", "adverse_mark_pnl": "0", "abort_reason": "",
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bundle_ledger.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            trades = module.load_ledger(path)

        self.assertEqual([t.bundle_id for t in trades], ["partial-fill", "closed-fill"])
        self.assertEqual([t.capital for t in trades], [12.5, 20.0])
        self.assertEqual(trades[0].status, "UNWOUND")
        self.assertAlmostEqual(trades[0].ret, 0.016)


if __name__ == "__main__":
    unittest.main()
