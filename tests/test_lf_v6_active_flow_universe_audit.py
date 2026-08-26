#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_v6_active_flow_universe_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v6_active_flow_universe_audit", MODULE_PATH)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class ActiveFlowUniverseAuditTest(unittest.TestCase):
    def test_active_global_tape_with_zero_overlap_is_mismatch(self) -> None:
        result = AUDIT.classify_flow_coverage(
            ["A", "B", "C"],
            ["X", "Y"],
        )
        self.assertEqual(result.classification, "ACTIVE_UNIVERSE_MISMATCH")
        self.assertFalse(result.execution_evidence_eligible)
        self.assertEqual(result.overlap_conditions, 0)

    def test_overlap_is_measured_against_discovered_universe(self) -> None:
        result = AUDIT.classify_flow_coverage(
            ["A", "B", "C", "D"],
            ["B", "X"],
            min_overlap_fraction=0.20,
        )
        self.assertEqual(result.classification, "ACTIVE_FLOW_COVERAGE_PRESENT")
        self.assertTrue(result.execution_evidence_eligible)
        self.assertAlmostEqual(result.overlap_fraction, 0.25)

    def test_tiny_overlap_can_fail_closed(self) -> None:
        discovered = [f"M{i}" for i in range(220)]
        result = AUDIT.classify_flow_coverage(
            discovered,
            ["M0", "X"],
            min_overlap_fraction=0.01,
        )
        self.assertEqual(result.classification, "INSUFFICIENT_ACTIVE_FLOW_COVERAGE")
        self.assertFalse(result.execution_evidence_eligible)

    def test_current_diagnostic_shape_is_not_zero_fill_evidence(self) -> None:
        payload = {
            "classification": "global_tape_active_but_sampled_universe_has_no_recent_matches",
            "discovered_conditions": 220,
            "probes": {
                "global_recent": {
                    "http_status": 200,
                    "response_rows": 1000,
                    "local_window_rows": 16,
                    "local_window_discovered_matches": 0,
                }
            },
        }
        result = AUDIT.audit_diagnostic(payload)
        self.assertEqual(result["classification"], "ACTIVE_UNIVERSE_MISMATCH")
        self.assertFalse(result["execution_evidence_eligible"])
        self.assertEqual(result["recent_global_rows"], 16)
        self.assertEqual(result["recent_discovered_matches"], 0)

    def test_no_global_recent_flow_is_separate_state(self) -> None:
        payload = {
            "discovered_conditions": 220,
            "probes": {"global_recent": {"local_window_rows": 0}},
        }
        result = AUDIT.audit_diagnostic(payload)
        self.assertEqual(result["classification"], "NO_RECENT_GLOBAL_FLOW")
        self.assertFalse(result["execution_evidence_eligible"])


if __name__ == "__main__":
    unittest.main()
