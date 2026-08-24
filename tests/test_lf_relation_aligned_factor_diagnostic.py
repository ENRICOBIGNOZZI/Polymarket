from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_relation_aligned_factor_diagnostic.py"
SPEC = importlib.util.spec_from_file_location("lf_relation_aligned_factor_diagnostic", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RelationAlignedFactorDiagnosticTest(unittest.TestCase):
    def test_incumbent_builds_global_basis_before_relation_filter(self) -> None:
        source = (ROOT / "src" / "pca_stat_arb.cpp").read_text(encoding="utf-8")
        state = MODULE.inspect_incumbent_source(source)
        self.assertTrue(state["global_factor_basis_detected"])
        self.assertTrue(state["relation_restricted_hedges_detected"])
        self.assertTrue(state["global_basis_precedes_relation_filter"])
        self.assertTrue(state["fixed_default_factor_budget_detected"])

    def test_global_k3_omits_a_locally_strong_fourth_cluster(self) -> None:
        source = (ROOT / "src" / "pca_stat_arb.cpp").read_text(encoding="utf-8")
        report = MODULE.build_report(source, factors=3)
        fixture = report["fixture"]
        self.assertEqual(fixture["retained_clusters"], ["cluster_a", "cluster_b", "cluster_c"])
        self.assertEqual(fixture["omitted_cluster_count"], 1)
        self.assertEqual(fixture["omitted_locally_strong_clusters"], ["cluster_d"])
        row = next(item for item in fixture["clusters"] if item["name"] == "cluster_d")
        self.assertAlmostEqual(row["local_explained_share"], 0.895, places=12)
        self.assertEqual(row["global_common_factor_coverage"], 0.0)

    def test_factor_budget_four_restores_common_factor_coverage(self) -> None:
        source = (ROOT / "src" / "pca_stat_arb.cpp").read_text(encoding="utf-8")
        report = MODULE.build_report(source, factors=4)
        fixture = report["fixture"]
        self.assertEqual(fixture["omitted_cluster_count"], 0)
        self.assertEqual(fixture["omitted_locally_strong_clusters"], [])


if __name__ == "__main__":
    unittest.main()
