import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"
if str(MONITORING) not in sys.path:
    sys.path.insert(0, str(MONITORING))

import v7_multi_crypto_exporter as exporter  # noqa: E402


class MultiCryptoExporterTests(unittest.TestCase):
    def test_health_accepts_safe_paper_runtime(self) -> None:
        snapshot = {
            "runtime": {
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "real_capital_at_risk": False,
                "pid": os.getpid(),
            },
            "performance": {"ledger_valid": True},
            "shadow": {"present": True, "valid": True, "safe": True, "age_seconds": 0.5},
        }
        self.assertEqual(exporter.health_reasons(snapshot), [])

    def test_health_rejects_unsafe_or_stale_state(self) -> None:
        snapshot = {
            "runtime": {
                "paper_only": False,
                "authenticated_execution": True,
                "real_order_submission": True,
                "real_capital_at_risk": True,
                "pid": -1,
            },
            "performance": {"ledger_valid": False},
            "shadow": {"present": True, "valid": False, "safe": False, "age_seconds": 99.0},
        }
        reasons = exporter.health_reasons(snapshot, max_shadow_age=30.0)
        for expected in (
            "paper_only_not_true",
            "authenticated_execution_not_false",
            "real_order_submission_not_false",
            "real_capital_at_risk_not_false",
            "paper_runtime_pid_not_alive",
            "canonical_ledger_invalid",
            "shadow_status_invalid",
            "shadow_not_safe",
            "shadow_status_stale",
        ):
            self.assertIn(expected, reasons)

    def test_collector_reads_only_multi_crypto_surface(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            run = base / "run"
            repo = base / "repo"
            (run / "control").mkdir(parents=True)
            (run / "ledger").mkdir(parents=True)
            (repo / "config").mkdir(parents=True)
            (run / "control/runtime_status.json").write_text('{"model_sha":"abc"}')
            (run / "control/portfolio_state.json").write_text("{}")
            (run / "canonical_economics.json").write_text("{}")
            (run / "control/global_portfolio_coordinator.json").write_text("{}")
            (repo / "config/v7_crypto_settlement_markets.json").write_text("{}")
            (repo / "config/v7_crypto_settlement_model_registry.json").write_text("{}")
            with mock.patch.object(exporter, "summarize_ledger", return_value={"valid": True}), mock.patch.object(
                exporter, "summarize_multi_crypto", return_value={"ledger_valid": True}
            ) as perf, mock.patch.object(exporter, "summarize_shadow_runtime", return_value={"present": False}):
                snapshot = exporter.collect_snapshot(run, repo, None, now_ns=1)
            self.assertTrue(snapshot["performance"]["ledger_valid"])
            self.assertTrue(perf.call_args.kwargs["ledger_valid"])
            self.assertNotIn("profit_experiments", snapshot)
            self.assertNotIn("maker_fillability", snapshot)


if __name__ == "__main__":
    unittest.main()
