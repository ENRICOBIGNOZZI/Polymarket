#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_v6_local_factor_fdr_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v6_local_factor_fdr_audit", MODULE_PATH)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


class V6LocalFactorFdrAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_cwd = Path.cwd()
        import os
        os.chdir(ROOT)

    def tearDown(self) -> None:
        import os
        os.chdir(self.old_cwd)

    def test_source_contract_reintroduces_unit_root_t_as_normal_pvalue(self) -> None:
        checks = MOD.source_contract(ROOT / "scripts" / "v6_local_factor_intents.py")
        self.assertTrue(all(checks.values()), checks)

    def test_all_null_iid_clusters_do_not_have_nominal_ten_percent_fdr(self) -> None:
        expected = {
            48: (122, 39, 216),
            96: (123, 48, 239),
            336: (138, 53, 237),
        }
        for points, (any_reject, pairable, eligible_total) in expected.items():
            cell = MOD.run_cell(points, 0.0)
            self.assertEqual(cell["clusters_with_any_bh_rejection"], any_reject)
            self.assertEqual(cell["clusters_with_pairable_false_signals"], pairable)
            self.assertEqual(cell["eligible_signal_count"], eligible_total)
            self.assertGreater(cell["all_null_fdr"], 0.50)
            self.assertGreater(cell["pairable_rate"], 0.15)

    def test_serially_dependent_null_also_exceeds_nominal_fdr(self) -> None:
        expected = {
            48: (43, 3, 56),
            96: (30, 4, 39),
            336: (35, 1, 40),
        }
        for points, (any_reject, pairable, eligible_total) in expected.items():
            cell = MOD.run_cell(points, 0.5)
            self.assertEqual(cell["clusters_with_any_bh_rejection"], any_reject)
            self.assertEqual(cell["clusters_with_pairable_false_signals"], pairable)
            self.assertEqual(cell["eligible_signal_count"], eligible_total)
            self.assertGreater(cell["all_null_fdr"], 0.10)

    def test_report_is_explicitly_non_promotional(self) -> None:
        report = MOD.build_report()
        self.assertEqual(report["nominal_bh_q"], 0.10)
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertIn("unit-root", report["interpretation"])
        self.assertEqual(len(report["cells"]), 6)


if __name__ == "__main__":
    unittest.main()
