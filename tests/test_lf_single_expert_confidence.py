#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_single_expert_confidence_diagnostic.py"
SPEC = importlib.util.spec_from_file_location("lf_single_expert_confidence_diagnostic", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class LFSingleExpertConfidenceDiagnosticTest(unittest.TestCase):
    def test_v5_has_independent_single_expert_lf_books(self) -> None:
        cfg = json.loads((ROOT / "config/paper_v5.json").read_text(encoding="utf-8"))
        self.assertTrue(all(abs(float(v)) < 1e-12 for v in cfg["expert_weights"].values()))
        rows = module.lf_strategy_spec(ROOT)
        self.assertEqual({row["expert"] for row in rows}, {"pca", "graph", "semantic", "external"})
        self.assertAlmostEqual(sum(row["capital_fraction"] for row in rows), 0.80)
        self.assertTrue(module.source_contract(ROOT)["v5_child_has_exactly_one_active_expert"])

    def test_incumbent_singleton_is_exactly_confidence_invariant(self) -> None:
        high = module.incumbent_singleton(0.62, 1.0, 0.02)
        medium = module.incumbent_singleton(0.62, 0.50, 0.02)
        low = module.incumbent_singleton(0.62, 0.02, 0.02)
        self.assertEqual(high, medium)
        self.assertEqual(high, low)
        self.assertAlmostEqual(high[0], 0.62)
        self.assertAlmostEqual(high[1], 0.01)

    def test_source_contract_confirms_confidence_cancellation_mechanism(self) -> None:
        contracts = module.source_contract(ROOT)
        self.assertTrue(all(contracts.values()), contracts)

    def test_research_shrink_is_monotone_and_not_a_production_change(self) -> None:
        mid, q = 0.60, 0.62
        high = module.research_shrink_to_market(mid, q, 1.0)
        medium = module.research_shrink_to_market(mid, q, 0.50)
        low = module.research_shrink_to_market(mid, q, 0.02)
        self.assertAlmostEqual(high - mid, 0.0200)
        self.assertAlmostEqual(medium - mid, 0.0100)
        self.assertAlmostEqual(low - mid, 0.0004)
        self.assertGreater(high, medium)
        self.assertGreater(medium, low)
        report = module.build_report(ROOT)
        self.assertFalse(report["production_changed"])
        self.assertEqual(report["evidence_state"], "MORE_EVIDENCE_REQUIRED")
        self.assertTrue(report["fixture"]["incumbent_is_confidence_invariant"])
        self.assertAlmostEqual(report["fixture"]["low_vs_high_confidence_fair_difference"], 0.0)
        self.assertAlmostEqual(report["fixture"]["low_vs_high_confidence_uncertainty_difference"], 0.0)

    def test_invalid_confidence_rejected(self) -> None:
        with self.assertRaises(ValueError):
            module.incumbent_singleton(0.6, 0.0, 0.02)
        with self.assertRaises(ValueError):
            module.research_shrink_to_market(0.5, 0.6, 1.01)


if __name__ == "__main__":
    unittest.main()
