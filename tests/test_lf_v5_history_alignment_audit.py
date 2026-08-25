#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import math
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_v5_history_alignment_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v5_history_alignment_audit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class V5HistoryAlignmentAuditTest(unittest.TestCase):
    def test_incumbent_source_drops_history_timestamps_before_pca(self) -> None:
        contract = MODULE.source_contract()
        self.assertTrue(contract["history_container_drops_timestamps"])
        self.assertTrue(contract["history_csv_persists_timestamp"])
        self.assertTrue(contract["load_state_discards_timestamp"])
        self.assertTrue(contract["pca_uses_positional_index_returns"])

    def test_positional_alignment_can_reverse_correlation_sign(self) -> None:
        result = MODULE.deterministic_counterexample()
        self.assertEqual(result["exact_common_interval_endpoints"], [3, 4])
        self.assertTrue(result["correlation_sign_flip"])
        self.assertTrue(math.isclose(result["positional_correlation"], 1.0, abs_tol=1e-12))
        self.assertTrue(math.isclose(result["exact_timestamp_correlation"], -1.0, abs_tol=1e-12))

    def test_fixture_is_logit_exact_not_probability_linearization(self) -> None:
        result = MODULE.deterministic_counterexample()
        self.assertEqual([round(x, 12) for x in result["positional_returns_a"]], [1.0, -1.0, 1.0, -1.0])
        self.assertEqual([round(x, 12) for x in result["positional_returns_b"]], [1.0, -1.0, 1.0, -1.0])
        self.assertEqual([round(x, 12) for x in result["exact_returns_a"]], [1.0, -1.0])
        self.assertEqual([round(x, 12) for x in result["exact_returns_b"]], [-1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
