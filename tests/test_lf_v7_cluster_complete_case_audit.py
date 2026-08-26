#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_v7_cluster_complete_case_audit.py"
spec = importlib.util.spec_from_file_location("lf_v7_cluster_complete_case_audit", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class ClusterCompleteCaseAuditTest(unittest.TestCase):
    def test_unrelated_sparse_cluster_member_cannot_erase_predeclared_pair(self) -> None:
        result = mod.audit()
        self.assertTrue(result["baseline_estimable"])
        self.assertEqual(result["baseline_cluster_points"], 60)
        self.assertFalse(result["broadened_cluster_estimable"])
        self.assertEqual(result["broadened_cluster_points"], 0)
        self.assertTrue(result["predeclared_pair_estimable"])
        self.assertEqual(result["pair_local_points"], 60)
        self.assertTrue(result["anti_monotone_universe_effect"])

    def test_pair_controls_are_explicit_not_missingness_selected(self) -> None:
        histories = mod.deterministic_fixture()
        panel = mod.predeclared_pair_panel(histories, ("A", "B"), ["C", "D"], 3600, 48)
        self.assertIsNotNone(panel)
        self.assertEqual(panel.markets, ("A", "B", "C", "D"))
        missing_control = mod.predeclared_pair_panel(histories, ("A", "B"), ["C", "Z"], 3600, 48)
        self.assertIsNone(missing_control)


if __name__ == "__main__":
    unittest.main()
