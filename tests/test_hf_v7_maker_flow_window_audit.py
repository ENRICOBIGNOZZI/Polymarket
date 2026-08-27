#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hf_v7_maker_flow_window_audit",
    ROOT / "scripts" / "hf_v7_maker_flow_window_audit.py",
)
assert SPEC is not None and SPEC.loader is not None
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class MakerFlowWindowAuditTest(unittest.TestCase):
    def test_incremental_fill_rows_must_not_be_the_rolling_decision_window(self) -> None:
        source = '''
new_trades = read_new_tape(trade_tape, state)
advance_tape_watermark(state, new_trades)
recent_for_decision = [t for t in new_trades if t.received_ms <= now and t.event_ts_ms <= now]
parser.add_argument("--interval", type=float, default=2.0)
'''
        report = mod.audit_source(source, {"flow_lookback_seconds": 300})
        self.assertTrue(report["blocking"])
        self.assertEqual(report["status"], mod.BLOCKING)
        self.assertAlmostEqual(report["nominal_interval_to_lookback_fraction"], 2.0 / 300.0)

    def test_separate_causal_rolling_reader_is_accepted(self) -> None:
        source = '''
new_trades = read_new_tape(trade_tape, state)
advance_tape_watermark(state, new_trades)
recent_for_decision = read_recent_tape(trade_tape, decision_ms=now, lookback_seconds=300)
parser.add_argument("--interval", type=float, default=2.0)
'''
        report = mod.audit_source(source, {"flow_lookback_seconds": 300})
        self.assertFalse(report["blocking"])
        self.assertEqual(report["status"], mod.OK)

    def test_future_receive_and_event_contract_is_explicit_in_evidence(self) -> None:
        report = mod.audit_source("", {"flow_lookback_seconds": 300})
        contract = report["required_contract"]
        self.assertTrue(contract["no_future_receive_leakage"])
        self.assertTrue(contract["no_double_fill_replay"])
        self.assertIn("received_ms<=decision_ms", contract["decision_flow"])
        self.assertIn("event_ts_ms<=decision_ms", contract["decision_flow"])


if __name__ == "__main__":
    unittest.main()
