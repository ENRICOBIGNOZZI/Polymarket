#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lf_v6_trade_tape_health_audit", ROOT / "scripts" / "lf_v6_trade_tape_health_audit.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
analyze = MODULE.analyze


class LFV6TradeTapeHealthAuditTest(unittest.TestCase):
    def snapshot(self, recorder_status: str, graph_rows: int, resting: int, reserved: float) -> dict:
        failures = [] if recorder_status == "healthy" else ["no_public_trades_fetched", "missing_last_trade_timestamp"]
        fetched = 10 if recorder_status == "healthy" else 0
        last_trade = 123 if recorder_status == "healthy" else 0
        return {
            "data_health": {
                "trade_recorder": {
                    "status": recorder_status,
                    "failures": failures,
                    "fields": {
                        "fetched": fetched,
                        "new_trades": fetched,
                        "last_trade_ts": last_trade,
                        "seen": fetched,
                    },
                }
            },
            "intents": {
                "bundles": 1 if graph_rows else 0,
                "strategies": {"GRAPH_RV": graph_rows} if graph_rows else {},
            },
            "logs": {
                "multileg": [
                    f"multileg_tick bundles=1 resting={resting} complete=0 aborting=0 closed=0 "
                    f"unwound=0 trades_processed=0 tape_cursor=0 reserved={reserved} cash=5000"
                ]
            },
        }

    def test_unhealthy_empty_tape_with_graph_reservation_fails_closed(self) -> None:
        report = analyze(
            self.snapshot("unhealthy", 3, 1, 60.0),
            smoke_script="polymarket_trade_recorder\npolymarket_multileg_paper\n",
            loop_script="start_recorder;start_broker\n",
        )
        self.assertEqual(report["status"], "FAIL_CLOSED_REQUIRED")
        self.assertTrue(report["unsafe_graph_admission_with_unhealthy_tape"])
        self.assertEqual(report["trade_recorder"]["fetched"], 0)
        self.assertEqual(report["graph_rv"]["reserved_usd"], 60.0)
        self.assertFalse(report["source_contract"]["smoke_invokes_trade_recorder_health_gate"])
        self.assertFalse(report["source_contract"]["persistent_loop_invokes_trade_recorder_health_gate"])

    def test_healthy_tape_allows_graph_execution_evidence(self) -> None:
        report = analyze(self.snapshot("healthy", 3, 1, 60.0))
        self.assertEqual(report["status"], "NO_DEFECT_OBSERVED")
        self.assertFalse(report["unsafe_graph_admission_with_unhealthy_tape"])

    def test_unhealthy_tape_without_new_graph_activity_is_not_false_positive(self) -> None:
        report = analyze(self.snapshot("unhealthy", 0, 0, 0.0))
        self.assertEqual(report["status"], "NO_DEFECT_OBSERVED")
        self.assertFalse(report["unsafe_graph_admission_with_unhealthy_tape"])

    def test_source_gate_detection_is_explicit(self) -> None:
        report = analyze(
            self.snapshot("healthy", 0, 0, 0.0),
            smoke_script="python3 scripts/validate_trade_recorder_health.py --log recorder.log\n",
            loop_script="python3 scripts/validate_trade_recorder_health.py --log recorder.log\n",
        )
        self.assertTrue(report["source_contract"]["smoke_invokes_trade_recorder_health_gate"])
        self.assertTrue(report["source_contract"]["persistent_loop_invokes_trade_recorder_health_gate"])


if __name__ == "__main__":
    unittest.main()
