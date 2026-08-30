from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"
if str(MONITORING) not in sys.path:
    sys.path.insert(0, str(MONITORING))

import v7_ledger_metrics
from v7_execution_ledger import EVENT_TYPES as CANONICAL_EVENT_TYPES

SPEC = importlib.util.spec_from_file_location("exporter_v7_ledger_test", MONITORING / "exporter_v7.py")
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


class V7LedgerMonitoringTest(unittest.TestCase):
    @staticmethod
    def _event(event_type: str, *, complete=None, unwind_loss=None, final_pnl=None, capital_duration_ms=None, markouts=None):
        return {
            "schema_version": 1,
            "event_type": event_type,
            "strategy": "graph_rv",
            "model_sha": "a" * 40,
            "paper_only": True,
            "authenticated_execution": False,
            "complete": complete,
            "unwind_loss": unwind_loss,
            "final_pnl": final_pnl,
            "capital_duration_ms": capital_duration_ms,
            "markouts": markouts or {},
        }

    def test_read_only_ledger_summary_counts_partial_complete_unwind_and_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger" / "execution.jsonl"
            path.parent.mkdir(parents=True)
            events = [
                self._event("OPPORTUNITY"),
                self._event("ORDER_SUBMITTED"),
                self._event("FILL", complete=False, markouts={"10s": -0.01}),
                self._event("FILL", complete=True, markouts={"10s": 0.03}),
                self._event("EXIT", unwind_loss=1.5),
                self._event("FINAL", final_pnl=2.25, capital_duration_ms=7_200_000),
            ]
            path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            summary = v7_ledger_metrics.summarize_ledger(path)
            self.assertTrue(summary["present"])
            self.assertTrue(summary["valid"])
            self.assertEqual(summary["model_shas"], ["a" * 40])
            total = summary["total"]
            self.assertEqual(total["opportunities"], 1)
            self.assertEqual(total["orders_submitted"], 1)
            self.assertEqual(total["fills"], 2)
            self.assertEqual(total["partial_fills"], 1)
            self.assertEqual(total["complete_fills"], 1)
            self.assertEqual(total["unwinds"], 1)
            self.assertAlmostEqual(total["final_pnl"], 2.25)
            self.assertAlmostEqual(total["capital_duration_ms"] / 3_600_000, 2.0)
            self.assertEqual(total["markout_count"]["10s"], 2)
            self.assertAlmostEqual(total["markout_sum"]["10s"] / 2, 0.01)

    def test_mixed_sha_or_non_paper_row_invalidates_ledger_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.jsonl"
            first = self._event("OPPORTUNITY")
            second = self._event("FINAL", final_pnl=1.0)
            second["model_sha"] = "b" * 40
            third = self._event("CANDIDATE")
            third["authenticated_execution"] = True
            path.write_text("\n".join(json.dumps(row) for row in (first, second, third)) + "\n", encoding="utf-8")
            summary = v7_ledger_metrics.summarize_ledger(path)
            self.assertFalse(summary["valid"])
            self.assertEqual(summary["invalid_rows"], 1)
            self.assertEqual(set(summary["model_shas"]), {"a" * 40, "b" * 40})

    def test_monitoring_accepts_every_canonical_ledger_event_type(self) -> None:
        self.assertEqual(set(v7_ledger_metrics.EVENT_TYPES), set(CANONICAL_EVENT_TYPES))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.jsonl"
            path.write_text(
                "".join(json.dumps(self._event(event_type)) + "\n"
                        for event_type in sorted(CANONICAL_EVENT_TYPES)),
                encoding="utf-8",
            )
            summary = v7_ledger_metrics.summarize_ledger(path)
            self.assertTrue(summary["valid"])
            self.assertEqual(summary["invalid_rows"], 0)

    def test_exporter_surface_contains_canonical_ledger_economics(self) -> None:
        snapshot = {
            "sha": "a" * 40,
            "run_root": "paper_v7_live",
            "runtime": {
                "version": 7,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
            },
            "runtime_alive": True,
            "portfolio": {"paper_only": True, "authenticated_execution": False},
            "authority": {"valid": True, "max_drawdown": 0.15},
            "canonical_economics": {
                "paper_only": True,
                "authenticated_execution": False,
                "expected_model_sha": "a" * 40,
                "promotion_ready": False,
                "submitted_units": 1,
                "complete_units": 0,
            },
            "trade_tape": {"rows": 1, "assets": 1},
            "ledger": {
                "present": True,
                "valid": True,
                "rows": 6,
                "invalid_rows": 0,
                "model_shas": ["a" * 40],
                "total": {
                    "opportunities": 2,
                    "orders_submitted": 1,
                    "fills": 1,
                    "complete_fills": 0,
                    "partial_fills": 1,
                    "unwinds": 1,
                    "final_pnl": -0.5,
                    "capital_duration_ms": 3_600_000,
                    "markout_sum": {"10s": -0.02},
                    "markout_count": {"10s": 1},
                },
            },
            "economics": {
                "equity": 10000,
                "pnl": 0,
                "realized_pnl": 0,
                "unrealized_executable_pnl": 0,
                "drawdown": 0,
                "gross_exposure": 0,
                "capital_utilization": 0,
                "live_units": 0,
                "killed": False,
            },
            "ages": {},
            "strategies": {},
        }
        metrics = exporter.render_prometheus(snapshot)
        self.assertIn("polymarket_execution_opportunities 2", metrics)
        self.assertIn("polymarket_execution_partial_fills 1", metrics)
        self.assertIn("polymarket_execution_complete_fills 0", metrics)
        self.assertIn("polymarket_execution_unwinds 1", metrics)
        self.assertIn("polymarket_execution_final_pnl_usd -0.5", metrics)
        self.assertIn("polymarket_execution_capital_hours 1", metrics)
        self.assertIn('polymarket_execution_mean_markout{horizon="10s"} -0.02', metrics)
        self.assertNotIn("execution_supervisor", metrics)
        self.assertNotIn("market_proxy", metrics)


if __name__ == "__main__":
    unittest.main()
